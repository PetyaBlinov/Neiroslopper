"""Compact sketch assistant. Workers exchange plain data, never Tk objects."""
from copy import deepcopy
from dataclasses import dataclass
import queue
import threading
import tkinter as tk
from tkinter import ttk

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
        reply = engine.answer(prompt, state, use_llm=use_codex, allow_creative=True,
                              cancel=cancel)
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
        self.history = []
        self.context = {}
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
        for text in ("Напиши печатными «Привет!» высотой 5 мм", "По центру",
                     "Сдвинь рисунок вправо на 5 мм", "Ширина рисунка 80 мм",
                     "Поверни рисунок на 90 градусов", "Добавь рамку с отступом 3 мм"):
            menu.add_command(label=text, command=lambda value=text: self.fill_example(value))
        def show_examples():
            try:
                menu.tk_popup(examples.winfo_rootx(), examples.winfo_rooty() + examples.winfo_height())
            finally:
                menu.grab_release()
        examples.configure(command=show_examples)
        examples.pack(side="right")
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
        self.cancel_button = ttk.Button(row, text="Отменить запрос", command=self.cancel_request, state="disabled")
        self.cancel_button.pack(side="left")
        ttk.Label(right, textvariable=self.preview_title, style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        self.canvas = tk.Canvas(right, width=1, height=1, bg=FIELD, highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.refresh_preview())
        controls = ttk.Frame(right)
        controls.pack(fill="x", pady=(12, 0))
        self.apply_button = ttk.Button(controls, text="Применить к эскизу", command=self.apply_proposal,
                                       style="Accent.TButton", state="disabled")
        self.apply_button.pack(fill="x")
        edits = ttk.Frame(right)
        edits.pack(fill="x", pady=(8, 0))
        ttk.Button(edits, text="Отменить правку", command=self.undo).pack(side="left")
        ttk.Button(edits, text="Повторить", command=self.redo).pack(side="right")
        ttk.Label(outer, textvariable=self.status, style="Status.TLabel", wraplength=1000).pack(fill="x", pady=(12, 0))

    def append(self, text, tag=None):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text, tag or ())
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def fill_example(self, text):
        if self.busy:
            return
        self.entry.delete("1.0", "end")
        self.entry.insert("1.0", text)
        self.entry.focus_set()

    def send(self, event=None):
        if self.closed or self.busy:
            return "break"
        prompt = self.entry.get("1.0", "end").strip()
        if not prompt or len(prompt) > 1800:
            self.status.set("Введите запрос до 1800 знаков")
            return "break"
        from assistant.runtime import preview_cancel
        if preview_cancel(prompt) is not None:
            self.cancel_request()
            return "break"
        before = self.app.editor_snapshot()
        state = deepcopy(before.state)
        state["editor_paths"] = before.paths
        state["chat_history"] = tuple(self.history[-4:])
        if self.context_revision == before.revision:
            state.update(deepcopy(self.context))
            if self.pending is not None:
                state["preview_draft"] = deepcopy(self.pending.reply.plan)
        else:
            self.context = {}
        state["task_memory"] = self.memory.snapshot(before.revision)
        self.context_revision = before.revision
        token = self.token = object()
        self.cancel = threading.Event()
        self.busy = True
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
        self.send_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.codex_checkbox.configure(state="disabled" if busy else "normal")
        self.apply_button.configure(state="disabled")

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
            self.busy = False
            self.set_busy(False)
            if kind == "error":
                error_type, text = result
                self.append("Помощник: " + text + "\n\n")
                self.status.set("Предложение не применено")
                self.memory.note(self.app.editor_revision(), "rejected", self.request_prompt, error=text)
                self.restore_pending()
                record_event(self.app, "assistant.error", action="preview", caller="assistant",
                             error_type=error_type, success=False)
                continue
            current = self.app.editor_snapshot()
            if result.before.revision != current.revision or current.state.get("busy"):
                self.pending = None
                self.context = {}
                self.preview_title.set("Текущий эскиз")
                self.refresh_preview()
                self.status.set("Эскиз изменился. Повторите запрос")
                record_event(self.app, "assistant.stale", action="preview", reason="stale", caller="assistant")
                continue
            reply = result.reply
            self.append("Помощник: " + reply.text + "\n")
            self.append(reply.source + "\n\n", "source")
            self.history.append((result.request[:600], reply.text[:700]))
            self.history = self.history[-4:]
            self.context = {"last_preview_request": reply.context_request,
                            "awaiting_text_field": reply.waiting_field, **reply.waiting_edit}
            self.context_revision = current.revision
            outcome = ("rejected" if reply.validation_error else "proposed" if reply.plan is not None else
                       "needs_data" if reply.waiting_field or reply.waiting_edit else "answered")
            self.memory.note(current.revision, outcome, result.request, error=reply.validation_error)
            if reply.plan is not None:
                self.pending = result
                self.preview_title.set("Предложение · ещё не применено")
                self.apply_button.configure(state="normal")
                self.status.set("Предпросмотр готов")
            else:
                self.status.set("Ответ готов · предложение сохранено" if self.pending else "Ответ готов")
                self.restore_pending()
            self.refresh_preview()
        self.timer = self.root.after(60, self.pump_results)

    def restore_pending(self):
        if self.pending is not None:
            if (self.pending.before.revision == self.app.editor_revision() and
                    not self.app.controller.busy and not self.app.svg_loading):
                self.apply_button.configure(state="normal")
            else:
                self.pending = None
                self.preview_title.set("Текущий эскиз")
        self.refresh_preview()

    def refresh_preview(self):
        if self.closed:
            return
        from editor_tools.window import draw
        current = self.app.editor_snapshot()
        paths = self.pending.paths if self.pending is not None else current.paths
        draw(self.canvas, paths, current.state.get("sheet_width") or 210,
             current.state.get("sheet_height") or 297)

    def apply_proposal(self):
        if self.closed or self.busy or self.pending is None:
            return
        proposal = self.pending
        self.applying = True
        try:
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
        self.apply_button.configure(state="disabled")
        self.preview_title.set("Текущий эскиз")
        self.refresh_preview()

    def history_action(self, action):
        try:
            action()
        except ValueError as exc:
            self.status.set(str(exc))
        else:
            self.status.set("Эскиз обновлён")
        self.refresh_preview()

    def undo(self):
        self.history_action(self.app.undo_editor_preview)

    def redo(self):
        self.history_action(self.app.redo_editor_preview)

    def cancel_request(self):
        request = self.pending.request if self.pending is not None else self.request_prompt
        self.cancel.set()
        self.token = object()
        self.busy = False
        self.pending = None
        self.context = {}
        self.memory.note(self.app.editor_revision(), "cancelled", request)
        self.set_busy(False)
        self.preview_title.set("Текущий эскиз")
        self.status.set("Предложение отменено")
        self.refresh_preview()
        record_event(self.app, "assistant.cancelled", action="cancel", caller="assistant", cancelled=True)

    def editor_changed(self):
        if not self.closed and not self.applying and self.context_revision != self.app.editor_revision():
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
        self.cancel.set()
        self.token = object()
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.root.destroy()
        record_event(self.app, "assistant.closed", action="close", caller="assistant")
