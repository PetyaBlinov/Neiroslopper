"""Reviewed local dictation. This window can only insert text into a request."""
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from assistant.voice import VoiceService, dependency_status, model_status
from branding import apply_app_icon
from display_scaling import ui_scale, scale_widget_tree
from eventlog import record_event
from ui_theme import BACKGROUND, FIELD, INK, GOLD, EDGE


def discover_devices(service, results):
    """Never pass Tk objects or bound UI callbacks into a worker."""
    try:
        results.put((True, service.devices()))
    except Exception as exc:
        results.put((False, str(exc)))


class VoiceDialog:
    def __init__(self, owner):
        self.owner = owner
        self.closed = False
        self.events = queue.Queue()
        self.discovery = queue.Queue()
        self.service = VoiceService(self.events)
        self.token = None
        self.clip = None
        self.devices = []
        self.phase = "loading"
        self.timer = None
        self.record_started = 0.
        self.root = tk.Toplevel(owner.root)
        self.root.title("Neiroslopper · Голосовой ввод")
        self.root.configure(bg=BACKGROUND)
        self.root.transient(owner.root)
        scale = ui_scale(owner.root)
        width = min(round(680 * scale), self.root.winfo_screenwidth() - round(60 * scale))
        height = min(round(590 * scale), self.root.winfo_screenheight() - round(100 * scale))
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2 - round(25 * scale))
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(width, round(580 * scale)), min(height, round(500 * scale)))
        self.root._neiroslopper_geometry_scaled = True
        apply_app_icon(self.root)
        self.device_name = tk.StringVar(master=self.root)
        self.profile = tk.StringVar(master=self.root, value="Быстрый · tiny")
        self.status = tk.StringVar(master=self.root, value="Проверяю доступные микрофоны…")
        self.model_info = tk.StringVar(master=self.root)
        self.counter = tk.StringVar(master=self.root, value="До 30 секунд")
        self.build()
        scale_widget_tree(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", self.cancel)
        self.root.grab_set()
        self.missing = dependency_status()
        self.update_model_info()
        if self.missing:
            self.phase = "unavailable"
            self.status.set("Голосовой ввод не установлен. Закройте окно и запустите INSTALL_VOICE.cmd в папке приложения.")
        else:
            self.discover()
        self.update_controls()
        self.timer = self.root.after(60, self.pump_results)
        record_event(owner.app, "voice.open", action="voice", caller="assistant")

    def build(self):
        outer = ttk.Frame(self.root, style="App.TFrame", padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Голосовой ввод", style="Title.TLabel").pack(anchor="w", pady=(0, 12))
        body = ttk.Frame(outer, padding=14)
        body.pack(fill="both", expand=True)
        device_row = ttk.Frame(body)
        device_row.pack(fill="x")
        ttk.Label(device_row, text="Микрофон", style="Section.TLabel").pack(side="left")
        self.refresh_button = ttk.Button(device_row, text="Обновить", command=self.discover, style="Quiet.TButton")
        self.refresh_button.pack(side="right")
        self.device_box = ttk.Combobox(body, textvariable=self.device_name, state="readonly")
        self.device_box.pack(fill="x", pady=(5, 10))
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Label(row, text="Распознавание").pack(side="left", padx=(0, 12))
        self.profile_box = ttk.Combobox(row, textvariable=self.profile,
            values=("Быстрый · tiny", "Точнее · small"), state="readonly", width=23)
        self.profile_box.pack(side="right")
        self.profile_box.bind("<<ComboboxSelected>>", lambda event: self.update_model_info())
        ttk.Label(body, textvariable=self.model_info, style="Muted.TLabel", wraplength=590).pack(fill="x", pady=(6, 10))
        record_row = ttk.Frame(body)
        record_row.pack(fill="x")
        self.record_button = ttk.Button(record_row, text="Записать", command=self.start_recording)
        self.record_button.pack(side="left")
        self.stop_button = ttk.Button(record_row, text="Завершить запись", command=self.stop_recording, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(record_row, textvariable=self.counter, style="Muted.TLabel").pack(side="right")
        self.level = ttk.Progressbar(body, maximum=1., mode="determinate")
        self.level.pack(fill="x", pady=(10, 14))
        ttk.Label(body, text="Проверьте и поправьте текст", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        self.text = tk.Text(body, height=5, width=1, wrap="word", bg=FIELD, fg=INK,
            insertbackground=INK, relief="flat", highlightthickness=1, highlightbackground=EDGE,
            highlightcolor=GOLD, padx=10, pady=8, font="TkTextFont", undo=True)
        self.text.pack(fill="both", expand=True)
        self.text.bind("<KeyRelease>", lambda event: self.update_insert_state())
        self.text.bind("<<Modified>>", self.text_modified)
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(12, 0))
        self.recognize_button = ttk.Button(actions, text="Распознать", command=self.recognize)
        self.recognize_button.pack(side="left")
        self.insert_button = ttk.Button(actions, text="Вставить в запрос", command=self.insert_text,
                                       style="Accent.TButton", state="disabled")
        self.insert_button.pack(side="right")
        bottom = ttk.Frame(outer, style="App.TFrame")
        bottom.pack(fill="x", pady=(12, 0))
        ttk.Label(bottom, textvariable=self.status, style="Status.TLabel", wraplength=500).pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text="Отмена", command=self.cancel, style="Header.TButton").pack(side="right", padx=(10, 0))

    def selected_profile(self):
        return "quality" if self.profile.get() == "Точнее · small" else "fast"

    def update_model_info(self):
        status = model_status(self.selected_profile())
        self.model_info.set(f"{status.model} · локально, CPU · " +
                            ("модель загружена" if status.ready else
                             f"первая загрузка ≈ {status.download_mb} МБ, нужен интернет"))
        self.recognize_button.configure(text="Распознать" if status.ready else "Скачать и распознать")

    def discover(self):
        if self.closed or self.service.busy or self.phase == "loading" and getattr(self, "discovery_worker", None):
            return
        self.phase = "loading"
        self.status.set("Проверяю доступные микрофоны…")
        self.discovery_worker = threading.Thread(target=discover_devices,
            args=(self.service, self.discovery), daemon=True)
        self.discovery_worker.start()
        self.update_controls()

    def update_controls(self):
        active = self.phase in ("recording", "stopping", "transcribing") or self.service.busy
        ready = self.phase not in ("loading", "unavailable") and not active
        self.device_box.configure(state="readonly" if ready else "disabled")
        self.refresh_button.configure(state="normal" if ready else "disabled")
        self.profile_box.configure(state="readonly" if not active else "disabled")
        self.record_button.configure(text="Записать ещё раз" if self.clip is not None else "Записать",
                                     state="normal" if ready and self.devices else "disabled")
        self.stop_button.configure(state="normal" if self.phase == "recording" else "disabled")
        self.recognize_button.configure(state="normal" if ready and self.clip is not None else "disabled")
        self.text.configure(state="disabled" if active else "normal")
        self.update_insert_state()

    def resting_phase(self):
        return ("review" if self.text.get("1.0", "end-1c").strip() else
                "recorded" if self.clip is not None else "idle")

    def text_modified(self, event=None):
        if self.closed:
            return
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self.update_insert_state()

    def update_insert_state(self):
        if self.closed:
            return
        ready = not self.service.busy and self.phase == "review"
        text = self.text.get("1.0", "end-1c").strip()
        state = "normal" if ready and 0 < len(text) <= 1800 else "disabled"
        if str(self.insert_button.cget("state")) != state:
            self.insert_button.configure(state=state)

    def start_recording(self):
        if self.closed or self.service.busy or self.phase not in ("idle", "recorded", "review") or not self.devices:
            return
        index = self.device_box.current()
        if not 0 <= index < len(self.devices):
            self.status.set("Выберите микрофон")
            return
        try:
            token = self.service.start(device=self.devices[index].index)
        except Exception as exc:
            self.status.set(str(exc))
            self.update_controls()
            return
        self.token = token
        self.clip = None
        self.text.delete("1.0", "end")
        self.record_started = time.monotonic()
        self.phase = "recording"
        self.status.set("Запись… Нажмите «Завершить запись», когда закончите фразу.")
        self.update_controls()
        record_event(self.owner.app, "voice.record_started", action="record", caller="assistant")

    def stop_recording(self):
        if self.closed or self.phase != "recording":
            return
        self.service.stop()
        self.phase = "stopping"
        self.status.set("Завершаю запись…")
        self.update_controls()

    def recognize(self):
        if self.closed or self.service.busy or self.phase not in ("recorded", "review") or self.clip is None:
            return
        status = model_status(self.selected_profile())
        try:
            self.token = self.service.recognize(self.clip, profile=self.selected_profile(),
                                                allow_download=not status.ready)
        except Exception as exc:
            self.status.set(str(exc))
            self.update_controls()
            return
        self.phase = "transcribing"
        self.status.set("Распознаю локально…" if status.ready else
                        f"Загружаю модель {status.model} (≈ {status.download_mb} МБ), затем распознаю…")
        self.update_controls()
        record_event(self.owner.app, "voice.recognition_started", action="transcribe", caller="assistant")

    def pump_results(self):
        if self.closed:
            return
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        while True:
            try:
                ok, value = self.discovery.get_nowait()
            except queue.Empty:
                break
            if ok:
                self.devices = value
                self.device_box.configure(values=tuple(device.name for device in value))
                if value:
                    self.device_box.current(next((i for i, device in enumerate(value) if device.is_default), 0))
                self.phase = self.resting_phase()
                self.status.set("Нажмите «Записать»" if value else "Микрофон не найден. Подключите его и нажмите «Обновить».")
            else:
                self.phase = self.resting_phase()
                self.status.set(str(value))
            self.update_controls()
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if self.token is None or event.token != self.token:
                continue
            if event.kind == "state":
                self.status.set(str(event.value))
            elif event.kind == "level":
                self.level["value"] = max(0., min(1., float(event.value)))
            elif event.kind == "recorded":
                self.clip = event.value
                self.counter.set(f"Записано {self.clip.duration_s:.1f} с")
                self.status.set("Запись готова. Нажмите «Распознать»." +
                                (" " + " ".join(self.clip.warnings) if self.clip.warnings else ""))
            elif event.kind == "result":
                self.text.configure(state="normal")
                self.text.delete("1.0", "end")
                self.text.insert("1.0", event.value.text)
                self.status.set(" ".join(event.value.warnings) or "Проверьте слова и числа, затем вставьте текст в запрос.")
                record_event(self.owner.app, "voice.recognized", action="transcribe", caller="assistant",
                             success=True, duration_ms=round(event.value.elapsed_s * 1000))
            elif event.kind == "error":
                self.status.set(str(event.value))
                record_event(self.owner.app, "voice.error", action="voice", caller="assistant", success=False)
            elif event.kind == "finished":
                self.token = None
                self.phase = self.resting_phase()
                self.level["value"] = 0
                self.update_model_info()
                self.update_controls()
        if self.phase == "recording":
            elapsed = min(30, int(time.monotonic() - self.record_started))
            value = f"{elapsed} / 30 с"
            if self.counter.get() != value:
                self.counter.set(value)
        self.timer = self.root.after(60, self.pump_results)

    def insert_text(self):
        if self.closed or self.service.busy or self.phase != "review":
            return
        text = self.text.get("1.0", "end-1c").strip()
        if not text or len(text) > 1800:
            self.status.set("Оставьте текст длиной от 1 до 1800 знаков")
            return
        if self.owner.insert_voice_text(text):
            record_event(self.owner.app, "voice.inserted", action="insert", caller="assistant", success=True)
            self.close()
        else:
            self.status.set("Вместе с набранным запросом получится больше 1800 знаков. Сократите текст.")

    def cancel(self, event=None):
        self.close()
        return "break"

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.token = None
        self.clip = None
        self.service.close()
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.root.grab_release()
        self.root.destroy()
        if self.owner.voice_window is self:
            self.owner.voice_window = None
        if not self.owner.closed:
            self.owner.set_busy(self.owner.busy)
            self.owner.restore_pending()
            self.owner.entry.focus_set()
        record_event(self.owner.app, "voice.closed", action="close", caller="assistant")
