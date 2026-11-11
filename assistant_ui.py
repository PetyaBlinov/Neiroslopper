"""Compact sketch assistant. Workers exchange plain data, never Tk objects."""
from copy import deepcopy
from dataclasses import dataclass
import queue
import threading
import tkinter as tk
from tkinter import ttk, simpledialog
from pathlib import Path
from assistant_studio import SandboxDocument, scene_for, template_for_prompt, selected_request

from branding import apply_app_icon
from display_scaling import ui_scale, scale_widget_tree
from eventlog import record_event
from ui_theme import BACKGROUND, FIELD, SURFACE, INK, MUTED, GOLD, EDGE


@dataclass(frozen=True)
class Proposal:
    before: object
    reply: object
    paths: object
    request: str


def prepare_reply(engine, prompt, before, state, use_codex, cancel, token, events):
    """Pure worker target: no bound window, widget, callback or controller."""
    from assistant.planner import transform_paths
    try:
        if state.get('web_drawing') and use_codex:
            from assistant.vector_drawing import answer
            reply = answer(prompt, state, cancel)
        else:
            reply = engine.answer(selected_request(prompt, state), state, use_llm=use_codex, allow_creative=True,
                                  cancel=cancel, on_chunk=lambda chunk: events.put((token, "chunk", chunk)))
        paths = None
        if reply.plan is not None:
            if before.state.get("busy"):
                raise ValueError("Дождитесь завершения операции в пульте и повторите запрос.")
            paths = transform_paths(before.paths, reply.plan,
                                    before.state["sheet_width"], before.state["sheet_height"])
        events.put((token, "reply", Proposal(before, reply, paths, prompt)))
    except Exception as exc:
        events.put((token, "error", (type(exc).__name__, str(exc))))


class AssistantWindow:
    def __init__(self, app):
        from assistant.runtime import Assistant
        from assistant.task_memory import TaskMemory
        self.app = app
        self.engine = Assistant()
        self.memory = TaskMemory()
        self.closed = self.busy = False
        self.pending = None
        self.applying = False
        self.request_prompt = ""
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.token = object()
        self.worker = None
        self.timer = None
        self.voice_window = None
        self.reference_window = None
        self.history = []
        self.context = {}
        initial = app.editor_snapshot()
        self.sandbox = SandboxDocument(initial.state["sheet_width"], initial.state["sheet_height"])
        self.selected_objects = {}
        self.scale_base = None
        self.last_templates = {}
        self.variants = []
        self.variant_before = None
        self.streamed = ""
        from editor_tools.studio_store import Templates
        self.templates = Templates(Path(app.data_dir) / "templates") if app.data_dir else None
        self.context_revision = app.editor_revision()
        self.root = tk.Toplevel(app.root)
        self.root.title("Neiroslopper · Помощник")
        self.root.configure(bg=BACKGROUND)
        scale = ui_scale(app.root)
        width = min(round(1100 * scale), app.root.winfo_screenwidth() - round(60 * scale))
        height = min(round(740 * scale), app.root.winfo_screenheight() - round(100 * scale))
        x = max(0, (app.root.winfo_screenwidth() - width) // 2)
        y = max(0, (app.root.winfo_screenheight() - height) // 2 - round(25 * scale))
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(width, round(900 * scale)), min(height, round(620 * scale)))
        self.root._neiroslopper_geometry_scaled = True
        apply_app_icon(self.root)
        self.use_codex = tk.BooleanVar(master=self.root, value=False)
        self.mode = tk.StringVar(master=self.root, value="Эскиз плоттера")
        self.active_mode = self.mode.get()
        self.selection = tk.StringVar(master=self.root, value="Вся композиция")
        self.variant = tk.StringVar(master=self.root, value="Варианты оформления")
        self.status = tk.StringVar(master=self.root, value="Готов к запросу")
        self.preview_title = tk.StringVar(master=self.root, value="Текущий эскиз")
        self.build()
        scale_widget_tree(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", self.stop_and_cancel)
        self.refresh_preview()
        self.timer = self.root.after(60, self.pump_results)
        self.root.after_idle(self.entry.focus_set)
        record_event(app, "assistant.open", action="assistant", caller="assistant")

    def build(self):
        outer = ttk.Frame(self.root, padding=18, style="App.TFrame")
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="Помощник по эскизу", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="Закрыть", command=self.close, style="Header.TButton").pack(side="right")
        self.mode_picker = ttk.Combobox(header, textvariable=self.mode, width=18,
            values=("Эскиз плоттера", "Пробный лист"), state="readonly")
        self.mode_picker.pack(side="right", padx=10)
        self.mode_picker.bind("<<ComboboxSelected>>", self.switch_mode)
        columns = ttk.Frame(outer, style="App.TFrame")
        columns.pack(fill="both", expand=True)
        columns.columnconfigure(0, weight=1, uniform="assistant")
        columns.columnconfigure(1, weight=1, uniform="assistant")
        columns.rowconfigure(0, weight=1)
        left = ttk.Frame(columns, padding=14)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.Frame(columns, padding=14)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.transcript = tk.Text(left, height=7, width=1, bg=SURFACE, fg=INK,
                                  wrap="word", relief="flat", state="disabled", borderwidth=0,
                                  padx=3, pady=3, font=("Segoe UI", 11), takefocus=False)
        self.transcript.pack(fill="both", expand=True)
        self.transcript.tag_configure("user", foreground=GOLD)
        self.transcript.tag_configure("source", foreground=MUTED, font=("Segoe UI", 9))
        self.append("Опишите надпись или изменение рисунка. Предложение появится справа.\n\n")
        request_head = ttk.Frame(left)
        request_head.pack(fill="x", pady=(12, 6))
        ttk.Label(request_head, text="Ваш запрос", style="Section.TLabel").pack(side="left")
        examples = ttk.Button(request_head, text="Примеры ▾", style="Quiet.TButton")
        menu = tk.Menu(examples, tearoff=False, bg=FIELD, fg=INK,
                       activebackground="#244765", activeforeground=INK)
        menu.add_command(label='Привязать чат Codex…', command=self.configure_codex_chat)
        menu.add_separator()
        for text in ("Напиши печатными «Привет!» высотой 5 мм", "По центру",
                     "Сдвинь рисунок вправо на 5 мм", "Ширина рисунка 80 мм",
                     "Поверни рисунок на 90 градусов", "Добавь рамку с отступом 3 мм",
                     "Создай открытку", "Выбери объект 2", "Сделай три оформления",
                     "Оптимизируй маршрут"):
            menu.add_command(label=text, command=lambda value=text: self.fill_example(value))
        def show_examples():
            try:
                menu.tk_popup(examples.winfo_rootx(), examples.winfo_rooty() + examples.winfo_height())
            finally:
                menu.grab_release()
        examples.configure(command=show_examples)
        examples.pack(side="right")
        self.voice_button = ttk.Button(request_head, text="Голос", command=self.open_voice,
                                       style="Quiet.TButton")
        self.voice_button.pack(side="right", padx=(0, 6))
        self.reference_button = ttk.Button(request_head, text="По образцу", command=self.open_reference,
                                           style="Quiet.TButton")
        self.reference_button.pack(side="right", padx=(0, 6))
        self.entry = tk.Text(left, height=4, width=1, wrap="word", bg=FIELD, fg=INK,
                             insertbackground=INK, relief="flat", highlightthickness=1,
                             highlightbackground=EDGE, highlightcolor=GOLD, padx=10, pady=8,
                             font=("Segoe UI", 11), undo=True)
        self.entry.pack(fill="x")
        self.entry.bind("<Control-Return>", self.send)
        self.codex_checkbox = ttk.Checkbutton(left, text="Свободные запросы через Codex",
                                               variable=self.use_codex)
        self.codex_checkbox.pack(anchor="w", pady=(8, 6))
        row = ttk.Frame(left)
        row.pack(fill="x")
        self.send_button = ttk.Button(row, text="Подготовить", command=self.send, style="Accent.TButton")
        self.send_button.pack(side="right")
        self.cancel_button = ttk.Button(row, text="Отменить", command=self.cancel_request, state="disabled")
        self.cancel_button.pack(side="left")
        preview_head = ttk.Frame(right)
        preview_head.pack(fill="x", pady=(0, 8))
        ttk.Label(preview_head, textvariable=self.preview_title, style="Section.TLabel").pack(side="left")
        self.template_button = ttk.Button(preview_head, text="Шаблоны ▾", command=self.template_menu,
                                          style="Quiet.TButton")
        self.template_button.pack(side="right")
        self.object_picker = ttk.Combobox(right, textvariable=self.selection, state="readonly")
        self.object_picker.pack(fill="x", pady=(0, 8))
        self.object_picker.bind("<<ComboboxSelected>>", self.select_object)
        self.canvas = tk.Canvas(right, width=1, height=1, bg=FIELD, highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.refresh_preview())
        self.canvas.bind("<Button-1>", self.select_at)
        self.variant_picker = ttk.Combobox(right, textvariable=self.variant, state="disabled")
        self.variant_picker.pack(fill="x", pady=(8, 0))
        self.variant_picker.bind("<<ComboboxSelected>>", self.choose_variant)
        controls = ttk.Frame(right)
        controls.pack(fill="x", pady=(12, 0))
        self.apply_button = ttk.Button(controls, text="Применить к эскизу", command=self.apply_proposal,
                                       style="Accent.TButton", state="disabled")
        self.apply_button.pack(fill="x")
        self.transfer_button = ttk.Button(controls, text="В рабочий эскиз", command=self.transfer_sandbox)
        edits = ttk.Frame(right)
        edits.pack(fill="x", pady=(8, 0))
        ttk.Button(edits, text="Отменить правку", command=self.undo).pack(side="left")
        ttk.Button(edits, text="Повторить", command=self.redo).pack(side="right")
        ttk.Label(outer, textvariable=self.status, style="Status.TLabel", wraplength=1000).pack(fill="x", pady=(12, 0))

    def current_snapshot(self):
        return self.sandbox.snapshot() if self.mode.get() == "Пробный лист" else self.app.editor_snapshot()

    def configure_codex_chat(self):
        if self.busy or not self.app.data_dir:
            return
        from assistant.saved_chat import load, save
        try:
            value = simpledialog.askstring('Чат Codex',
                'ID чата или codex://threads/… . Вопросы будут добавляться в выбранный чат.\n'
                'Пустое поле отключает привязку.', initialvalue=load(self.app.data_dir) or '', parent=self.root)
            if value is not None:
                save(self.app.data_dir, value)
                self.status.set('Чат привязан' if value.strip() else 'Привязка отключена')
        except (ValueError, OSError) as exc:
            self.status.set(str(exc))

    def current_scene(self):
        return scene_for(self.current_snapshot(), self.pending)

    def selected_object(self):
        return self.selected_objects.get(self.mode.get())

    def switch_mode(self, event=None):
        if self.mode.get() not in ("Эскиз плоттера", "Пробный лист"):
            self.mode.set(self.active_mode)
            return
        if self.mode.get() == self.active_mode:
            return
        self.cancel_request()
        self.close_reference()
        self.active_mode = self.mode.get()
        self.scale_base = None
        self.history = []
        from assistant.task_memory import TaskMemory
        self.memory = TaskMemory()
        self.context_revision = self.current_snapshot().revision
        if self.mode.get() == "Пробный лист":
            self.transfer_button.pack(fill="x", pady=(8, 0))
        else:
            self.transfer_button.pack_forget()
        self.status.set(self.mode.get())
        self.refresh_preview()

    def offer_external_plan(self, before, plan, label="Рисунок по образцу"):
        """Stage a checked plan; this API never commits a sketch or starts motion."""
        if self.closed or self.busy or self.voice_active():
            return False
        current = self.current_snapshot()
        if before.revision != current.revision or current.state.get("busy"):
            self.status.set("Эскиз изменился. Подготовьте предложение заново")
            return False
        from assistant.planner import transform_paths
        from assistant.runtime import Reply
        try:
            paths = transform_paths(before.paths, plan, before.state["sheet_width"], before.state["sheet_height"])
        except (ValueError, TypeError, KeyError) as exc:
            self.status.set(str(exc))
            return False
        self.pending = Proposal(before, Reply(label, plan=plan, source="Предпросмотр"), paths, label)
        self.context_revision = before.revision
        self.preview_title.set("Предложение · ещё не применено")
        self.restore_pending()
        self.status.set("Предпросмотр готов")
        return True

    def transfer_sandbox(self):
        if self.closed or self.busy or self.voice_active() or self.mode.get() != "Пробный лист":
            return
        if self.pending is not None:
            self.status.set("Сначала примените или отмените предложение на пробном листе")
            return
        before = self.app.editor_snapshot()
        current = self.sandbox.snapshot()
        if current.state["sheet_width"] != before.state["sheet_width"] or current.state["sheet_height"] != before.state["sheet_height"]:
            self.status.set("Размеры пробного и рабочего листа должны совпадать")
            return
        if not current.paths:
            self.status.set("Пробный лист пуст")
            return
        from editor_tools.scene import scene_plan
        plan = scene_plan(scene_for(current), "Перенос с пробного листа")
        self.mode.set("Эскиз плоттера")
        self.switch_mode()
        self.offer_external_plan(before, plan, "Перенос с пробного листа")

    def open_reference(self, prompt=None):
        if self.closed or self.busy or self.voice_active():
            return
        if self.reference_window is not None and not self.reference_window.closed:
            if prompt is None:
                self.reference_window.root.lift()
                return
            self.reference_window.close()
        from assistant.reference_ui import ReferenceWindow
        self.reference_window = ReferenceWindow(self, prompt=prompt or "")

    def close_reference(self):
        if self.reference_window is not None and not self.reference_window.closed:
            self.reference_window.close()

    def select_object(self, event=None):
        if self.busy or self.voice_active():
            return
        index = self.object_picker.current()
        scene = self.current_scene()
        self.selected_objects[self.mode.get()] = scene["objects"][index - 1]["id"] if 0 < index <= len(scene["objects"]) else None
        self.scale_base = None
        self.refresh_preview()

    def select_at(self, event):
        if self.busy or self.voice_active() or not getattr(self, "canvas_transform", None):
            return
        from editor_tools.scene import hit_test
        ox, oy, scale = self.canvas_transform
        before = self.current_snapshot()
        self.selected_objects[self.mode.get()] = hit_test(self.current_scene(),
            before.state["sheet_width"], before.state["sheet_height"],
            ((event.x - ox) / scale, (event.y - oy) / scale), 6 / scale)
        self.scale_base = None
        self.refresh_preview()

    def choose_variant(self, event=None):
        if self.busy or self.voice_active() or self.variant_before is None:
            return
        index = self.variant_picker.current()
        if not 0 <= index < len(self.variants):
            return
        label, plan = self.variants[index]
        from assistant.planner import Plan
        if not isinstance(plan, Plan):
            self.status.set(str(plan))
            return
        self.offer_external_plan(self.variant_before, plan, label)

    def template_menu(self):
        if self.busy or self.voice_active():
            return
        menu = tk.Menu(self.template_button, tearoff=False, bg=FIELD, fg=INK,
                       activebackground="#244765", activeforeground=INK)
        menu.add_command(label="Сохранить композицию…", command=self.save_template,
                         state="normal" if self.templates else "disabled")
        if self.templates:
            for name in self.templates.names():
                menu.add_command(label=name, command=lambda value=name: self.load_template(value))
        try:
            menu.tk_popup(self.template_button.winfo_rootx(), self.template_button.winfo_rooty() + self.template_button.winfo_height())
        finally:
            menu.grab_release()

    def save_template(self, name=None, recipient_format="{name}"):
        if self.busy or self.voice_active() or self.templates is None:
            return False
        from geometry import Sheet
        before = self.current_snapshot()
        scene = self.current_scene()
        if not scene["objects"]:
            self.status.set("Нет композиции для шаблона")
            return False
        interactive = name is None
        if interactive:
            name = simpledialog.askstring("Шаблон", "Название", parent=self.root)
        if name is None:
            return False
        selected = self.selected_object()
        obj = next((item for item in scene["objects"] if item["id"] == selected), None)
        source = obj["source"] if obj else {}
        typed = source.get("kind") in {"print_text", "text_block"}
        if source.get("kind") == "free_preview":
            typed = sum(item["kind"] == "print_text" for item in source["parameters"]["operations"]) == 1
        recipient = selected if typed else None
        if interactive and recipient is not None:
            from assistant.studio_commands import caption
            text = caption(obj)
            suggested = "Для {name}" if text.startswith("Для ") else "{name}"
            recipient_format = simpledialog.askstring("Поле имени", "Формат имени: {name}",
                initialvalue=suggested, parent=self.root)
            if recipient_format is None:
                return False
        try:
            self.templates.save(name, Sheet(before.state["sheet_width"], before.state["sheet_height"]),
                                scene, recipient, recipient_format)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            self.status.set(str(exc))
            return False
        self.last_templates[self.mode.get()] = name.strip()
        self.status.set("Шаблон сохранён")
        return True

    def load_template(self, name):
        if self.busy or self.voice_active() or self.templates is None:
            return False
        from assistant.studio_commands import use_template
        from editor_tools.scene import scene_plan
        before = self.current_snapshot()
        try:
            scene = use_template(self.templates.load(name), before.state["sheet_width"], before.state["sheet_height"])
            applied = self.offer_external_plan(before, scene_plan(scene, "Шаблон " + name), "Шаблон " + name)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            self.status.set(str(exc))
            return False
        if applied:
            self.last_templates[self.mode.get()] = name
        return applied

    def append(self, text, tag=None):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text, tag or ())
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def fill_example(self, text):
        if self.busy or self.voice_active():
            return
        self.entry.delete("1.0", "end")
        self.entry.insert("1.0", text)
        self.entry.focus_set()

    def send(self, event=None):
        if self.closed or self.busy or self.voice_active():
            return "break"
        prompt = self.entry.get("1.0", "end").strip()
        if not prompt or len(prompt) > 1800:
            self.status.set("Введите запрос до 1800 знаков")
            return "break"
        from assistant.runtime import preview_cancel
        if preview_cancel(prompt) is not None:
            self.cancel_request()
            return "break"
        from assistant.reference_ui import reference_request
        reference = reference_request(prompt)
        if reference is not None and not self.use_codex.get():
            self.open_reference(prompt)
            return "break"
        before = self.current_snapshot()
        state = deepcopy(before.state)
        if reference is not None:
            state['web_drawing'] = prompt
        if self.app.data_dir:
            from assistant.saved_chat import load
            try:
                state['codex_chat'] = load(self.app.data_dir)
            except (ValueError, OSError) as exc:
                self.status.set(str(exc))
                return 'break'
        state["editor_paths"] = before.paths
        state["chat_history"] = tuple(self.history[-4:])
        scene = self.current_scene()
        if scene["objects"]:
            state["studio_scene"] = scene
        state["studio_selected"] = self.selected_object()
        if self.scale_base is not None and self.scale_base[0] == before.revision:
            state["studio_scale_base"] = deepcopy(self.scale_base[1])
        try:
            template = template_for_prompt(self.templates, prompt, self.last_templates.get(self.mode.get()))
        except (ValueError, OSError, TypeError, KeyError) as exc:
            self.status.set(str(exc))
            return "break"
        if template is not None:
            state["studio_template"] = template
        if self.context_revision == before.revision:
            state.update(deepcopy(self.context))
            if self.pending is not None:
                state["preview_draft"] = deepcopy(self.pending.reply.plan)
        else:
            self.context = {}
        state["task_memory"] = self.memory.snapshot(before.revision, selected=self.selected_object())
        self.context_revision = before.revision
        token = self.token = object()
        self.cancel = threading.Event()
        self.busy = True
        self.streamed = ""
        self.variants = []
        self.variant_before = None
        self.variant.set("Варианты оформления")
        self.variant_picker.configure(values=(), state="disabled")
        self.request_prompt = prompt
        self.set_busy(True)
        self.append("Вы: " + prompt + "\n", "user")
        self.status.set("Готовлю предложение…")
        self.entry.delete("1.0", "end")
        record_event(self.app, "assistant.request", action="preview", caller="assistant",
                     revision=self.app.preview_revision)
        self.worker = threading.Thread(target=prepare_reply,
            args=(self.engine, prompt, before, state, self.use_codex.get(), self.cancel, token, self.events),
            daemon=True)
        self.worker.start()
        return "break"

    def set_busy(self, busy):
        locked = busy or self.voice_active()
        self.send_button.configure(state="disabled" if locked else "normal")
        self.cancel_button.configure(state="normal" if busy or self.pending is not None or self.variants else "disabled")
        self.codex_checkbox.configure(state="disabled" if locked else "normal")
        self.voice_button.configure(state="disabled" if locked else "normal")
        self.reference_button.configure(state="disabled" if locked else "normal")
        self.template_button.configure(state="disabled" if locked else "normal")
        self.mode_picker.configure(state="disabled" if locked else "readonly")
        self.object_picker.configure(state="disabled" if locked else "readonly")
        self.variant_picker.configure(state="readonly" if self.variants and not locked else "disabled")
        self.transfer_button.configure(state="disabled" if locked else "normal")
        self.apply_button.configure(state="disabled")

    def voice_active(self):
        return self.voice_window is not None and not self.voice_window.closed

    def open_voice(self):
        if self.closed or self.busy:
            return
        if self.voice_active():
            self.voice_window.root.lift()
            return
        from voice_ui import VoiceDialog
        self.voice_window = VoiceDialog(self)
        self.set_busy(False)

    def insert_voice_text(self, text):
        """Insert a reviewed draft; never submit or change the sketch."""
        if self.closed or self.busy or not isinstance(text, str):
            return False
        text = text.strip()
        if not text or any(ord(c) < 32 and c not in "\n\t" for c in text):
            return False
        prefix = self.entry.get("1.0", "insert")
        suffix = self.entry.get("insert", "end-1c")
        insertion = (" " if prefix and not prefix[-1].isspace() else "") + text
        if suffix and not suffix[0].isspace():
            insertion += " "
        if len(prefix + insertion + suffix) > 1800:
            return False
        self.entry.insert("insert", insertion)
        self.status.set("Голосовой текст вставлен. Проверьте запрос и нажмите «Подготовить»")
        return True

    def pump_results(self):
        if self.closed:
            return
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        while True:
            try:
                token, kind, result = self.events.get_nowait()
            except queue.Empty:
                break
            if token is not self.token:
                continue
            if kind == "chunk":
                # The queue is the only bridge from worker to Tk. Late chunks
                # have the same token check as the final checked proposal.
                if isinstance(result, str) and result:
                    if not self.streamed:
                        self.append("Помощник: ")
                    self.append(result)
                    self.streamed += result
                continue
            self.busy = False
            self.set_busy(False)
            if kind == "error":
                error_type, text = result
                self.append(("\n" if self.streamed else "Помощник: ") + text + "\n\n")
                self.status.set("Предложение не применено")
                self.memory.note(self.current_snapshot().revision, "rejected", self.request_prompt, error=text)
                self.restore_pending()
                record_event(self.app, "assistant.error", action="preview", caller="assistant",
                             error_type=error_type, success=False)
                continue
            current = self.current_snapshot()
            if result.before.revision != current.revision or current.state.get("busy"):
                self.pending = None
                self.context = {}
                self.preview_title.set("Текущий эскиз")
                self.refresh_preview()
                self.status.set("Эскиз изменился. Повторите запрос")
                record_event(self.app, "assistant.stale", action="preview", reason="stale", caller="assistant")
                continue
            reply = result.reply
            if self.streamed and reply.text.startswith(self.streamed):
                self.append(reply.text[len(self.streamed):] + "\n")
            else:
                self.append(("\n" if self.streamed else "Помощник: ") + reply.text + "\n")
            self.append(reply.source + "\n\n", "source")
            self.history.append((result.request[:600], reply.text[:700]))
            self.history = self.history[-4:]
            self.context = {"last_preview_request": reply.context_request,
                            "awaiting_text_field": reply.waiting_field, **reply.waiting_edit}
            self.context_revision = current.revision
            if reply.studio_base is not None:
                self.scale_base = (current.revision, deepcopy(reply.studio_base))
            if reply.selection_id is not None or reply.select_all:
                self.selected_objects[self.mode.get()] = reply.selection_id
            if reply.template_name:
                self.last_templates[self.mode.get()] = reply.template_name
            if reply.variants:
                self.variants = list(reply.variants)
                self.variant_before = result.before
                self.variant.set("Выберите оформление")
                self.variant_picker.configure(values=[name for name, _ in self.variants], state="readonly")
            outcome = ("rejected" if reply.validation_error else "proposed" if reply.plan is not None else
                       "needs_data" if reply.waiting_field or reply.waiting_edit else "answered")
            self.memory.note(current.revision, outcome, result.request, selected=self.selected_object(),
                             awaiting=reply.waiting_field, error=reply.validation_error)
            if reply.plan is not None:
                self.pending = result
                self.preview_title.set("Предложение · ещё не применено")
                self.apply_button.configure(state="normal")
                self.status.set("Предпросмотр готов")
            else:
                self.status.set("Ответ готов · предложение сохранено" if self.pending else "Ответ готов")
                self.restore_pending()
            self.cancel_button.configure(state="normal" if self.pending is not None or self.variants else "disabled")
            self.refresh_preview()
        self.timer = self.root.after(60, self.pump_results)

    def restore_pending(self):
        if self.pending is not None:
            current = self.current_snapshot()
            if (self.pending.before.revision == current.revision and not current.state.get("busy")):
                self.apply_button.configure(state="disabled" if self.busy or self.voice_active() else "normal")
            else:
                self.pending = None
                self.preview_title.set("Текущий эскиз")
        self.cancel_button.configure(state="normal" if self.busy or self.pending is not None or self.variants else "disabled")
        self.refresh_preview()

    def refresh_preview(self):
        if self.closed:
            return
        from editor_tools.window import draw
        current = self.current_snapshot()
        paths = self.pending.paths if self.pending is not None else current.paths
        width, height = current.state.get("sheet_width") or 210, current.state.get("sheet_height") or 297
        self.canvas_transform = draw(self.canvas, paths, width, height)
        scene = self.current_scene()
        identifiers = {item["id"] for item in scene["objects"]}
        selected = self.selected_object()
        if selected not in identifiers:
            self.selected_objects[self.mode.get()] = selected = None
        self.object_picker.configure(values=["Вся композиция"] + [
            f"{i}. {item['name']}" for i, item in enumerate(scene["objects"], 1)])
        index = next((i for i, item in enumerate(scene["objects"], 1) if item["id"] == selected), 0)
        self.object_picker.current(index)
        if selected is not None:
            from editor_tools.scene import render_scene, bounds
            obj = next(item for item in scene["objects"] if item["id"] == selected)
            box = bounds(render_scene({"version": 1, "objects": [obj], "route": None}, width, height))
            ox, oy, scale = self.canvas_transform
            self.canvas.create_rectangle(ox + box[0] * scale - 3, oy + box[1] * scale - 3,
                ox + box[2] * scale + 3, oy + box[3] * scale + 3, outline=GOLD, width=2, dash=(4, 3))

    def apply_proposal(self):
        if self.closed or self.busy or self.voice_active() or self.pending is None:
            return
        proposal = self.pending
        self.applying = True
        try:
            if self.mode.get() == "Пробный лист":
                after = self.sandbox.commit(proposal.before, proposal.reply.plan)
            else:
                after = self.app.apply_assistant_preview(proposal.before, proposal.reply.plan)
        except (ValueError, TypeError, KeyError) as exc:
            self.status.set(str(exc))
            record_event(self.app, "assistant.apply_rejected", action="apply", reason="rejected", caller="assistant")
        else:
            self.memory.applied(proposal.before.revision, after.revision)
            self.context_revision = after.revision
            self.append("Применено к эскизу.\n\n", "source")
            self.status.set("Правка применена")
            record_event(self.app, "assistant.applied", action="apply", caller="assistant", success=True,
                         revision=self.app.preview_revision)
        finally:
            self.applying = False
        self.pending = None
        self.variants = []
        self.variant_before = None
        self.variant.set("Варианты оформления")
        self.variant_picker.configure(values=(), state="disabled")
        self.cancel_button.configure(state="disabled")
        self.apply_button.configure(state="disabled")
        self.preview_title.set("Текущий эскиз")
        self.refresh_preview()

    def history_action(self, action):
        if self.busy or self.voice_active():
            return
        self.cancel_request()
        try:
            action()
        except ValueError as exc:
            self.status.set(str(exc))
        else:
            self.context_revision = self.current_snapshot().revision
            self.status.set("Эскиз обновлён")
        self.refresh_preview()

    def undo(self):
        self.history_action(self.sandbox.undo if self.mode.get() == "Пробный лист" else self.app.undo_editor_preview)

    def redo(self):
        self.history_action(self.sandbox.redo if self.mode.get() == "Пробный лист" else self.app.redo_editor_preview)

    def cancel_request(self):
        if self.voice_active():
            self.voice_window.close()
        request = self.pending.request if self.pending is not None else self.request_prompt
        self.cancel.set()
        self.token = object()
        self.busy = False
        self.pending = None
        self.context = {}
        self.variants = []
        self.variant_before = None
        self.variant.set("Варианты оформления")
        self.scale_base = None
        self.memory.note(self.current_snapshot().revision, "cancelled", request)
        self.set_busy(False)
        self.preview_title.set("Текущий эскиз")
        self.status.set("Предложение отменено")
        self.refresh_preview()
        record_event(self.app, "assistant.cancelled", action="cancel", caller="assistant", cancelled=True)

    def editor_changed(self):
        if (not self.closed and not self.applying and self.mode.get() == "Эскиз плоттера" and
                self.context_revision != self.app.editor_revision()):
            self.cancel_request()
            self.context_revision = self.app.editor_revision()
            self.status.set("Эскиз обновлён")

    def stop_and_cancel(self, event=None):
        self.cancel_request()
        self.app.stop()
        return "break"

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.voice_active():
            self.voice_window.close()
        self.close_reference()
        self.cancel.set()
        self.token = object()
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.root.destroy()
        record_event(self.app, "assistant.closed", action="close", caller="assistant")
