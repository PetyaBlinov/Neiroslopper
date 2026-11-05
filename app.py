"""Windows plotter desk: official FluidNC, SVG editor and explicit machine actions."""
import math
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from controller import Controller, REQUIRED, MACHINE_MAX_FEED, MAX_JOG_DISTANCE, DLC32_MAX_ON_SECONDS
from geometry import Axes, Sheet, load_svg, test_square
from editor_history import EditHistory
from pen_lift import PenLiftSettings, load_settings, save_settings, connection_notice, firmware_profile
from machine_profiles import (UNO, DLC32, PROFILE_LABELS, profile_id, app_profile,
                              settings_ready, pen_matches, load_selected_profile,
                              save_selected_profile, pen_settings_directory)
from branding import APP_NAME, APP_TITLE, apply_app_icon
from wifi_discovery import discover_fluidnc, MDNS_AVAILABLE
from connection_preferences import load_verified_endpoints, remember_verified_endpoint


def endstop_summary(report, now=None):
    """Keep absent configuration and stale reports distinct from released NCs."""
    if not isinstance(report, dict) or report.get("pins") is None:
        return "Концевики: свежие данные ещё не получены"
    now = time.monotonic() if now is None else now
    age = max(0, now - report.get("observed_at", 0))
    if age > 3:
        return "Концевики: данные устарели; нажмите «Проверить концевики»"
    pins = report["pins"]
    configured = report.get("configured", {})
    parts = []
    for axis in ("x", "y"):
        state = ("не назначен" if not configured.get(axis) else
                 "СРАБОТАЛ / ОБРЫВ" if axis.upper() in pins else "отпущен")
        parts.append(axis.upper() + ": " + state)
    return "Концевики: " + " · ".join(parts) + f" · {age:.1f} с назад"


class App:
    def __init__(self, root, *, data_dir=None):
        self.root = root
        self.closed = False
        self.pump_timer = None
        self.data_dir = data_dir
        from editor_tools.studio_store import Autosaves
        self.autosaves = Autosaves(data_dir) if data_dir is not None else None
        # Worker-owned objects must not retain this Tk window. The legacy
        # display lease callback is no longer used by the command transport.
        self.controller = Controller()
        self.connected = False
        self.zero = False
        self.sheet = None
        # Editor paper is not a machine origin. Keep it after STOP/USB loss,
        # while self.sheet/zero continue to represent physical calibration.
        self.draft_sheet = Sheet(210, 297)
        self.relay_state_unknown = False
        self.position = None
        self.paths = []
        self.preview_revision = 0
        self.editor_window = None
        self.editor_undo = None
        self.editor_history = EditHistory()
        self.editor_task = None
        self.editor_anchor = None
        self.camera_window = None
        self.dwin_window = None
        self.network_lease_seen = False
        self.dwin_pen_state = 0  # Commanded state only; no physical pen sensor.
        self.svg_events = queue.Queue()
        self.svg_loading = False
        self.svg_token = None
        self.preview_key = None
        self.preview_paths = None
        self.preview_transform = None
        self.redraw_timer = None
        self.settings = {}
        self.endstop_report = None
        self.endstop_text = tk.StringVar(value=endstop_summary(None))
        self.wiring_text = tk.StringVar(value="")
        self.port_name = ""
        self.simulation = False
        self.file_name = "Рисунок не выбран"
        # Canonical transport stays internal; the visible controls choose a
        # connection method and a device, never an editable TCP address.
        self.port = tk.StringVar(value="DEMO")
        self.connection_mode = tk.StringVar(value="Демо")
        self.active_connection_mode = "Демо"
        self.usb_port = tk.StringVar(value="")
        self.wifi_target = tk.StringVar(value="")
        self.connection_help = tk.StringVar(value="Демо работает без платы и не отправляет команды оборудованию.")
        self.wifi_status = tk.StringVar(value="Нажмите «Найти плату», когда ноутбук подключён к нужной сети.")
        self.wifi_devices = {}
        self.wifi_events = queue.Queue()
        self.wifi_search_token = None
        self.wifi_search_cancel = None
        self.wifi_search_thread = None
        self.wifi_searching = False
        self.remembered_wifi = load_verified_endpoints(data_dir)
        self.machine_profile = tk.StringVar(value=PROFILE_LABELS[load_selected_profile(data_dir)])
        self.step = tk.StringVar(value="1")
        self.feed = tk.StringVar(value="50")
        self.jog_lift = tk.BooleanVar(value=False)
        self.jog_lift_hint = tk.StringVar(value="")
        self.margin = tk.StringVar(value="3")
        self.hold = tk.BooleanVar(value=True)
        pen = self.stored_pen_settings()
        self.pen_enabled = tk.BooleanVar(value=pen.enabled)
        self.pen_active = tk.StringVar(value="Не проверено" if pen.active_low is None else "LOW" if pen.active_low else "HIGH")
        self.pen_confirmed = tk.BooleanVar(value=pen.confirmed)
        self.pen_power_raises = tk.BooleanVar(value=pen.power_raises)
        self.pen_continuous = tk.BooleanVar(value=pen.continuous)
        self.pen_effect = tk.StringVar(value="Поднимает" if pen.power_raises else "Опускает")
        self.pen_up_ms = tk.StringVar(value=str(pen.up_ms))
        self.pen_down_ms = tk.StringVar(value=str(pen.down_ms))
        self.pen_firmware = None
        self.pen_observer = None
        self.pen_status = tk.StringVar(value="")
        self.pen_summary = tk.StringVar(value="")
        self.pen_legend = tk.StringVar(value=connection_notice(
            pen.enabled, pen.power_raises, pen.continuous,
            power_limit_seconds=DLC32_MAX_ON_SECONDS if app_profile(self) == DLC32 else None))
        # The legacy Uno maps X to the portal and Y to the carriage. Our new
        # FluidNC YAML already maps logical X to the carriage and Y to the portal.
        self.axis_profile = app_profile(self)
        self.axis_preferences = {}
        self.swap = tk.BooleanVar(value=self.axis_profile == UNO)
        # Keep the existing direction defaults separate from axis roles;
        # physical motor directions still need to be checked on the machine.
        self.reverse_right = tk.BooleanVar(value=True)
        self.reverse_down = tk.BooleanVar()
        self.status = tk.StringVar(value="Не подключено · выберите USB, Wi-Fi или Демо")
        self.coordinates = tk.StringVar(value="Положение: —")
        self.paper_text = tk.StringVar(value="Размер появится после сохранения двух углов")
        self.hint = tk.StringVar(value="Эскиз A4 готов к редактированию. Для рисования подключите плату и задайте два угла листа.")
        self.root.title(APP_TITLE)
        apply_app_icon(self.root)
        width = min(1280, max(1000, self.root.winfo_screenwidth() - 80))
        height = min(820, max(620, self.root.winfo_screenheight() - 100))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(1100, width), min(700, height))
        from ui_theme import apply_theme
        self.style = apply_theme(self.root)
        self.build()
        self.refresh_ports()
        self.update_controls()
        self.root.bind("<Escape>", lambda event: self.stop())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.pump_timer = self.root.after(60, self.pump)

    def axes(self):
        return Axes(self.swap.get(), -1 if self.reverse_right.get() else 1,
                    -1 if self.reverse_down.get() else 1)

    def drawing_sheet(self):
        return self.sheet or self.draft_sheet

    def preserve_draft(self):
        if self.sheet is not None:
            self.draft_sheet = self.sheet

    def rebase_editor_history(self):
        # Pending proposals must be invalidated by connection/calibration
        # changes; committed editor undo/redo remains usable.
        if self.editor_history.past or self.editor_history.future:
            self.editor_undo = (self.editor_revision(), None)

    def draft_notice(self):
        sheet = self.drawing_sheet()
        if sheet is None:
            return "Размер появится после сохранения двух углов"
        return (f"Эскиз: {sheet.width:.2f} × {sheet.height:.2f} мм · "
                "ноль станка не задан, рисование недоступно")

    def build(self):
        outer = ttk.Frame(self.root, padding=(20, 16), style="App.TFrame")
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 12))
        brand = ttk.Frame(header, style="App.TFrame")
        brand.pack(side="left")
        ttk.Label(brand, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(brand, text="FluidNC · Рисунок на экране → траектория на бумаге", style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))
        self.stop_button = tk.Button(header, text="СТОП  /  ESC", command=self.stop,
                                     bg="#af2936", activebackground="#94212d", fg="white", activeforeground="white",
                                     relief="flat", borderwidth=0, cursor="hand2", font=("Segoe UI", 12, "bold"),
                                     padx=25, pady=12)
        self.stop_button.pack(side="right", anchor="center")

        connection = ttk.Frame(outer, padding=(14, 12), style="Card.TFrame")
        connection.pack(fill="x")
        row = ttk.Frame(connection)
        row.pack(fill="x")
        ttk.Label(row, text="1  Подключение", style="Section.TLabel").pack(side="left", padx=(0, 15))
        self.connection_mode_buttons = []
        for mode in ("USB", "Wi-Fi", "Демо"):
            button = ttk.Radiobutton(row, text=mode, value=mode, variable=self.connection_mode,
                                     command=self.change_connection_mode)
            button.pack(side="left", padx=(0, 12))
            self.connection_mode_buttons.append(button)
        self.profile_input = ttk.Combobox(row, textvariable=self.machine_profile,
                                        values=tuple(PROFILE_LABELS.values()), state="readonly", width=27)
        self.profile_input.pack(side="left", padx=(6, 8))
        self.profile_input.bind("<<ComboboxSelected>>", self.change_machine_profile)
        self.board_setup_button = ttk.Button(row, text="Настроить плату…", command=self.open_board_setup)
        self.board_setup_button.pack(side="right", padx=(8, 0))
        target_row = ttk.Frame(connection)
        target_row.pack(fill="x", pady=(9, 0))
        self.connect_button = ttk.Button(target_row, text="Подключить", command=self.connect, style="Accent.TButton")
        self.connect_button.pack(side="right", padx=(10, 0))
        self.target_area = ttk.Frame(target_row)
        self.target_area.pack(side="left", fill="x", expand=True)
        self.usb_connection_frame = ttk.Frame(self.target_area)
        ttk.Label(self.usb_connection_frame, text="USB-порт").pack(side="left", padx=(0, 8))
        self.ports = ttk.Combobox(self.usb_connection_frame, textvariable=self.usb_port, width=24, state="readonly")
        self.ports.pack(side="left", padx=(0, 6))
        self.ports.bind("<<ComboboxSelected>>", self.select_connection_target)
        self.refresh_button = ttk.Button(self.usb_connection_frame, text="Обновить", command=self.refresh_ports)
        self.refresh_button.pack(side="left")
        self.wifi_connection_frame = ttk.Frame(self.target_area)
        ttk.Label(self.wifi_connection_frame, text="Плата").pack(side="left", padx=(0, 8))
        self.wifi_boards = ttk.Combobox(self.wifi_connection_frame, textvariable=self.wifi_target, width=38, state="readonly")
        self.wifi_boards.pack(side="left", padx=(0, 6))
        self.wifi_boards.bind("<<ComboboxSelected>>", self.select_connection_target)
        self.wifi_search_button = ttk.Button(self.wifi_connection_frame, text="Найти плату", command=self.find_wifi_boards)
        self.wifi_search_button.pack(side="left")
        self.demo_connection_frame = ttk.Frame(self.target_area)
        ttk.Label(self.demo_connection_frame, text="Проверка интерфейса без оборудования").pack(side="left")
        self.demo_connection_frame.pack(fill="x")
        self.status_label = ttk.Label(connection, textvariable=self.status, foreground="#17633f")
        self.status_label.pack(anchor="w", pady=(9, 2))
        self.connection_help_label = ttk.Label(connection, textvariable=self.connection_help,
                                                style="Muted.TLabel", wraplength=900)
        self.connection_help_label.pack(anchor="w")
        self.wifi_status_label = ttk.Label(connection, textvariable=self.wifi_status,
                                           style="Muted.TLabel", wraplength=900)
        ttk.Label(outer, text="2  Задайте лист     →     3  Подготовьте рисунок     →     4  Запустите рисование",
                  style="Steps.TLabel").pack(anchor="w", pady=(13, 10))

        columns = ttk.Frame(outer, style="App.TFrame")
        columns.pack(fill="both", expand=True)
        sidebar = ttk.Frame(columns, width=350)
        sidebar.pack(side="left", fill="y", padx=(0, 14))
        sidebar.pack_propagate(False)
        tabs = ttk.Notebook(sidebar)
        tabs.pack(fill="both", expand=True)
        controls_tab, settings_tab = ttk.Frame(tabs), ttk.Frame(tabs)
        tabs.add(controls_tab, text="Управление")
        tabs.add(settings_tab, text="Настройки")
        self.sidebar_tabs = tabs
        scroll_targets = []

        def scroll_panel(parent):
            scrollbar = ttk.Scrollbar(parent, orient="vertical")
            scrollbar.pack(side="right", fill="y")
            canvas = tk.Canvas(parent, bg="white", highlightthickness=0, yscrollcommand=scrollbar.set)
            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.configure(command=canvas.yview)
            frame = ttk.Frame(canvas, padding=(10, 12))
            window = canvas.create_window(0, 0, window=frame, anchor="nw", width=320)
            frame.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
            scroll_targets.append((parent, canvas))
            return frame, canvas

        control_left, self.sidebar_canvas = scroll_panel(controls_tab)
        settings_left, _ = scroll_panel(settings_tab)

        def scroll_sidebar(event):
            widget = event.widget
            while widget is not None:
                for parent, canvas in scroll_targets:
                    if widget is parent:
                        canvas.yview_scroll(int(-event.delta / 120), "units")
                        return "break"
                widget = getattr(widget, "master", None)
        self.root.bind("<MouseWheel>", scroll_sidebar)
        right = ttk.Frame(columns, padding=(14, 12), style="Card.TFrame")
        right.pack(side="left", fill="both", expand=True)

        jog = ttk.LabelFrame(control_left, text="Подвести ручку", padding=10)
        jog.pack(fill="x", pady=(0, 10))
        row = ttk.Frame(jog)
        row.pack(fill="x")
        ttk.Label(row, text="Шаг, мм").pack(side="left")
        self.step_input = ttk.Combobox(row, textvariable=self.step,
                                      values=("0.1", "1", "5", "10", "20", "50", "100"), width=7, state="normal")
        self.step_input.pack(side="left", padx=(6, 13))
        ttk.Label(row, text="Скорость, %").pack(side="left")
        self.feed_input = ttk.Entry(row, textvariable=self.feed, width=5)
        self.feed_input.pack(side="right")
        cross = ttk.Frame(jog)
        cross.pack(pady=(8, 2))
        self.jog_buttons = []
        for label, dx, dy, r, c in (("↑", 0, -1, 0, 1), ("←", -1, 0, 1, 0),
                                    ("→", 1, 0, 1, 2), ("↓", 0, 1, 2, 1)):
            button = ttk.Button(cross, text=label, width=4, style="Jog.TButton", command=lambda x=dx, y=dy: self.jog(x, y))
            button.grid(row=r, column=c, padx=3, pady=2)
            self.jog_buttons.append(button)
        ttk.Label(jog, textvariable=self.coordinates, wraplength=285).pack(anchor="w", pady=(6, 2))
        self.jog_lift_button = ttk.Checkbutton(jog, text="Поднимать ручку при\nперемещении стрелками",
                                               variable=self.jog_lift, command=self.update_controls)
        self.jog_lift_button.pack(anchor="w", pady=(5, 3))
        ttk.Label(jog, textvariable=self.jog_lift_hint,
                  style="Muted.TLabel", wraplength=280).pack(anchor="w")

        corners = ttk.LabelFrame(control_left, text="2  Лист по двум углам", padding=10)
        corners.pack(fill="x", pady=(0, 10))
        self.first_button = ttk.Button(corners, text="Здесь верхний левый угол", command=self.set_zero)
        self.first_button.pack(fill="x", pady=(0, 5))
        self.second_button = ttk.Button(corners, text="Здесь нижний правый угол", command=self.set_corner)
        self.second_button.pack(fill="x")
        ttk.Label(corners, textvariable=self.paper_text, wraplength=280).pack(anchor="w", pady=8)
        self.clear_button = ttk.Button(corners, text="Новый лист / сбросить углы", command=lambda: self.submit("clear"))
        self.clear_button.pack(fill="x")
        ttk.Label(control_left, text="Углы задаются на реальной бумаге. Редактор сверху можно открыть и без подключения к плате.",
                  style="Muted.TLabel", wraplength=280).pack(anchor="w", pady=(0, 5))

        setup = ttk.LabelFrame(settings_left, text="Плата и направления", padding=10)
        setup.pack(fill="x", pady=(0, 10))
        self.configure_button = ttk.Button(setup, text="Проверить конфигурацию FluidNC", command=self.configure)
        self.configure_button.pack(fill="x")
        ttk.Label(setup, textvariable=self.wiring_text, wraplength=280,
                  style="Muted.TLabel").pack(anchor="w", pady=(5, 0))
        self.check_endstops_button = ttk.Button(setup, text="Проверить концевики", command=self.check_endstops)
        self.check_endstops_button.pack(fill="x", pady=(5, 0))
        ttk.Label(setup, textvariable=self.endstop_text, wraplength=280).pack(anchor="w", pady=(5, 0))
        self.unlock_button = ttk.Button(setup, text="Снять аварию MKS…", command=self.unlock_alarm)
        self.unlock_button.pack(fill="x", pady=(5, 0))
        self.hold_button = ttk.Checkbutton(setup, text="Удерживать моторы в простое", variable=self.hold, command=self.change_hold)
        self.hold_button.pack(anchor="w", pady=(6, 3))
        self.axis_widgets = []
        for text, variable in (("Поменять оси X ↔ Y", self.swap),
                               ("Развернуть направление вправо", self.reverse_right),
                               ("Развернуть направление вниз", self.reverse_down)):
            widget = ttk.Checkbutton(setup, text=text, variable=variable, command=self.redraw)
            widget.pack(anchor="w", pady=1)
            self.axis_widgets.append(widget)
        ttk.Label(setup, text="У FluidNC удержание задаётся в YAML. Направления проверьте до сохранения первого угла.",
                  style="Muted.TLabel", wraplength=280).pack(anchor="w", pady=(5, 0))

        pen_box = ttk.LabelFrame(settings_left, text="Ручка / соленоид", padding=10)
        pen_box.pack(fill="x", pady=(0, 10))
        self.pen_widgets = []
        enabled = ttk.Checkbutton(pen_box, text="Автоматический подъём ручки", variable=self.pen_enabled, command=self.apply_pen_settings)
        enabled.pack(anchor="w")
        ttk.Label(pen_box, textvariable=self.pen_summary, wraplength=280, style="Muted.TLabel").pack(anchor="w", pady=(4, 6))
        row = ttk.Frame(pen_box)
        row.pack(fill="x")
        self.pen_up_button = ttk.Button(row, text="Проверить подъём", command=lambda: self.submit("pen_up"))
        self.pen_up_button.pack(side="left", fill="x", expand=True)
        self.pen_settings_button = ttk.Button(row, text="Настройки…", command=self.toggle_pen_settings)
        self.pen_settings_button.pack(side="left", padx=(4, 0))
        ttk.Label(pen_box, textvariable=self.pen_status, wraplength=280, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        self.pen_advanced = ttk.Frame(pen_box)
        row = ttk.Frame(self.pen_advanced)
        row.pack(fill="x", pady=(8, 4))
        ttk.Label(row, text="Активный уровень").pack(side="left")
        self.pen_polarity = ttk.Combobox(row, textvariable=self.pen_active, values=("Не проверено", "HIGH", "LOW"), state="readonly", width=13)
        self.pen_polarity.pack(side="right")
        self.pen_polarity.bind("<<ComboboxSelected>>", self.pen_polarity_changed)
        row = ttk.Frame(self.pen_advanced)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Питание ручку").pack(side="left")
        self.pen_effect_combo = ttk.Combobox(row, textvariable=self.pen_effect, values=("Поднимает", "Опускает"), state="readonly", width=13)
        self.pen_effect_combo.pack(side="right")
        self.pen_effect_combo.bind("<<ComboboxSelected>>", self.pen_effect_changed)
        verified = ttk.Checkbutton(self.pen_advanced, text="Полярность и действие проверены", variable=self.pen_confirmed, command=self.apply_pen_settings)
        verified.pack(anchor="w")
        continuous = ttk.Checkbutton(self.pen_advanced, text="Без ограничения времени катушки", variable=self.pen_continuous, command=self.apply_pen_settings)
        continuous.pack(anchor="w")
        for caption, variable in (("Подъём, мс", self.pen_up_ms), ("Опускание, мс", self.pen_down_ms)):
            row = ttk.Frame(self.pen_advanced)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=caption + " (50–500)").pack(side="left")
            field = ttk.Entry(row, textvariable=variable, width=8)
            field.pack(side="right")
            self.pen_widgets.append(field)
        save = ttk.Button(self.pen_advanced, text="Сохранить настройки", command=self.apply_pen_settings)
        save.pack(fill="x", pady=4)
        help_button = ttk.Button(self.pen_advanced, text="Определить HIGH / LOW…", command=self.open_pen_observer)
        help_button.pack(fill="x", pady=3)
        self.pen_pulse_button = ttk.Button(self.pen_advanced, text="Короткий тест ручки", command=self.pen_pulse)
        self.pen_pulse_button.pack(fill="x", pady=3)
        self.pen_widgets.extend((enabled, verified, continuous, save, help_button, self.pen_settings_button))
        ttk.Label(self.pen_advanced, text="Тест не двигает оси. Нагрев не измеряется; штатная FluidNC не добавляет аппаратный таймер соленоида.",
                  style="Muted.TLabel", wraplength=280).pack(anchor="w", pady=(5, 0))
        extra = ttk.LabelFrame(settings_left, text="Дополнительно", padding=10)
        extra.pack(fill="x")
        self.camera_button = ttk.Button(extra, text="Камера / сканер", command=self.open_camera)
        self.camera_button.pack(fill="x", pady=(0, 5))
        ttk.Button(extra, text="Дисплей DWIN…", command=self.open_dwin).pack(fill="x")
        ttk.Label(extra, text="Отдельный дисплей не нужен для USB или Wi-Fi FluidNC. Сетевой JD-мост требует специальной прошивки.",
                  style="Muted.TLabel", wraplength=280).pack(anchor="w", pady=(6, 0))

        toolbar = ttk.Frame(right)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="3  Рисунок", style="Section.TLabel").pack(side="left", padx=(0, 12))
        self.editor_button = ttk.Button(toolbar, text="Редактор", command=self.open_editor)
        self.editor_button.pack(side="left", padx=(0, 5))
        self.load_button = ttk.Button(toolbar, text="Открыть SVG", command=self.open_svg)
        self.load_button.pack(side="left")
        self.test_button = ttk.Button(toolbar, text="Тест 10 мм", command=self.load_test)
        self.test_button.pack(side="left", padx=5)
        ttk.Label(toolbar, text="Поле, мм:").pack(side="right", padx=(5, 3))
        self.margin_input = ttk.Entry(toolbar, textvariable=self.margin, width=5)
        self.margin_input.pack(side="right")
        self.file_label = ttk.Label(right, text=self.file_name, style="Muted.TLabel", wraplength=650)
        self.file_label.pack(anchor="w", pady=(0, 8))
        drawing = ttk.Frame(right)
        drawing.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(drawing, width=1, height=1, bg="#e8eee9", highlightthickness=1, highlightbackground="#dce5de")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.request_redraw())
        ttk.Label(right, textvariable=self.pen_legend, wraplength=650,
                  foreground="#86521b", font=("Segoe UI", 9)).pack(anchor="w", pady=(7, 2))
        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(8, 0))
        self.startpoint_button = ttk.Button(actions, text="К началу рисунка", command=self.go_start)
        self.startpoint_button.pack(side="left", padx=(0, 8))
        self.draw_button = ttk.Button(actions, text="4  Начать рисование", command=self.draw, style="Accent.TButton")
        self.draw_button.pack(side="left")
        self.progress = ttk.Progressbar(right, maximum=100)
        self.progress.pack(fill="x", pady=(10, 8))
        ttk.Label(right, textvariable=self.hint, wraplength=650, foreground="#17633f").pack(anchor="w")

        footer = ttk.Frame(outer, style="App.TFrame")
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text="СТОП отправляет программный сброс. При потере связи отключайте силовое питание вручную.",
                  style="Subtitle.TLabel").pack(anchor="w")
        self.log = tk.Text(footer, height=2, font=("Consolas", 9), state="disabled", bg="#172b20",
                           fg="#e1ece4", insertbackground="white", relief="flat", padx=9, pady=6)
        self.log.pack(fill="x", pady=(6, 0))

    def open_board_setup(self):
        from fluidnc_setup import show_setup
        show_setup(self.root, data_dir=self.data_dir)

    def note(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 100:
            self.log.delete("1.0", "2.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def open_editor(self):
        from editor_tools.window import EditorWindow
        if self.editor_window is not None and not self.editor_window.closed:
            self.editor_window.root.lift()
            return
        self.editor_window = EditorWindow(self)

    def studio_history(self):
        current = (self.paths,self.file_name,self.editor_task,self.editor_anchor)
        return list(self.editor_history.past)+[current]+list(reversed(self.editor_history.future)),len(self.editor_history.past)

    def open_dwin(self):
        if self.dwin_window is not None and not self.dwin_window.closed:
            self.dwin_window.window.lift()
            return
        from dwin_ui import DwinWindow
        self.dwin_window = DwinWindow(self)

    def open_camera(self):
        try:
            from camera_ui import CameraWindow
            if self.camera_window is not None and not self.camera_window.closed:
                self.camera_window.root.lift()
                return
            self.camera_window = CameraWindow(self.root, self.camera_context, self.start_camera_scan, self.stop)
        except ImportError:
            messagebox.showerror("Камера", "Установите компоненты камеры через INSTALL_CAMERA.cmd.", parent=self.root)

    def camera_context(self):
        return {"busy": self.controller.busy or self.svg_loading,
                "position": self.axes().paper(self.position) if self.position is not None else None,
                "revision": self.editor_revision(), "simulation": self.simulation,
                "connected": self.connected, "sheet": self.sheet,
                "pen_lift_enabled": self.pen_enabled.get(), "pen_lift_ready": self.pen_ready(),
                "pen_power_raises": self.pen_power_raises.get(),
                "pen_continuous": self.pen_continuous.get()}

    def start_camera_scan(self, points, take_photo):
        return self.submit("scan", points=points, capture=take_photo, feed=self.get_feed())

    def clear_editor_history(self):
        self.editor_history.clear()
        self.editor_task = self.editor_anchor = self.editor_undo = None

    def editor_snapshot(self):
        from editor_tools.preview import snapshot
        sheet = self.drawing_sheet()
        try:
            preview_feed = self.get_feed()
        except ValueError:
            preview_feed = None
        return snapshot({"sandbox": False, "connected": self.connected,
                         "machine_profile": self.effective_machine_profile(),
                         "controller_config_verified": self.connected and not self.simulation and self.machine_ready(),
                         "display_model": "DMG80480C043_01WTC",
                         "simulation": self.simulation, "file_name": self.file_name,
                         "busy": self.controller.busy or self.svg_loading,
                         "feed_mm_min": preview_feed,
                         "pen_lift_enabled": self.pen_enabled.get(), "pen_lift_ready": self.pen_ready(),
                         "pen_power_raises": self.pen_power_raises.get(),
                         "pen_continuous": self.pen_continuous.get(),
                          "sheet_width": sheet.width if sheet else None,
                          "sheet_height": sheet.height if sheet else None,
                          "machine_calibrated": bool(self.zero and self.sheet),
                         "preview_task": self.editor_task, "preview_anchor": self.editor_anchor},
                        self.paths, self.editor_revision())

    def editor_revision(self):
        return ("editor", self.preview_revision, id(self.paths), self.drawing_sheet(),
                self.zero, self.axes(), self.connected, self.port_name, self.effective_machine_profile())

    def invalidate_editor(self):
        if self.editor_window is not None and not self.editor_window.closed:
            self.editor_window.editor_changed()

    def apply_editor_preview(self, before, plan):
        from editor_tools.planner import ACTION_KINDS, transform_paths
        sheet = self.drawing_sheet()
        if (self.controller.busy or self.svg_loading or sheet is None or
                before.revision != self.editor_revision()):
            raise ValueError("Эскиз, калибровка или состояние пульта изменились. Повторите команду.")
        if plan.kind not in ACTION_KINDS:
            raise ValueError("Разрешены только изменения эскиза; команды станку недоступны.")
        # Recalculate against the current editor and validate again at commit time.
        # This method does not call Controller.submit, connect, jog or any serial method.
        result = transform_paths(self.paths, plan, sheet.width, sheet.height)
        previous = (self.paths, self.file_name, self.editor_task, self.editor_anchor)
        self.editor_history.push(previous)
        self.paths = result
        self.file_name = "Эскиз редактора · " + plan.explanation
        from editor_tools.draft_context import applied_task, plan_anchor
        self.editor_task = applied_task(plan, self.editor_task)
        self.editor_anchor = plan_anchor(plan, self.editor_anchor)
        self.loaded(preserve_history=True)
        self.editor_undo = (self.editor_revision(), previous)
        self.note("Редактор изменил только эскиз. Движение не запускалось.")
        return self.editor_snapshot()

    def undo_editor_preview(self):
        if self.controller.busy or self.svg_loading:
            raise ValueError("Сначала дождитесь завершения операции в пульте.")
        if self.editor_undo is None or self.editor_undo[0] != self.editor_revision():
            raise ValueError("Нет последней операции редактора для отмены; эскиз или калибровка могли измениться.")
        self.paths, self.file_name, self.editor_task, self.editor_anchor = self.editor_history.undo(
            (self.paths, self.file_name, self.editor_task, self.editor_anchor))
        self.loaded(preserve_history=True)
        self.editor_undo = (self.editor_revision(), None)
        return self.editor_snapshot()

    def redo_editor_preview(self):
        if self.controller.busy or self.svg_loading or self.editor_undo is None or self.editor_undo[0] != self.editor_revision():
            raise ValueError("Эскиз или состояние изменились; повтор недоступен.")
        self.paths, self.file_name, self.editor_task, self.editor_anchor = self.editor_history.redo(
            (self.paths, self.file_name, self.editor_task, self.editor_anchor))
        self.loaded(preserve_history=True)
        self.editor_undo = (self.editor_revision(), None)
        return self.editor_snapshot()

    def load_editor_project(self, project):
        sheet = self.drawing_sheet()
        if self.controller.busy or self.svg_loading:
            raise ValueError("Дождитесь завершения операции. Проект не восстанавливает ноль станка.")
        if sheet is None and not self.zero:
            self.draft_sheet = project.sheet
            sheet = project.sheet
            self.paper_text.set(self.draft_notice())
        if sheet is None:
            raise ValueError("Сначала завершите установку двух углов или сбросьте калибровку.")
        if abs(sheet.width - project.sheet.width) > 0.01 or abs(sheet.height - project.sheet.height) > 0.01:
            raise ValueError("Размер проекта отличается от области пульта. Откройте его на пробном листе.")
        from geometry import validate_paths
        validate_paths(project.paths, sheet)
        self.editor_history.push((self.paths, self.file_name, self.editor_task, self.editor_anchor))
        self.paths, self.file_name = project.paths, project.name
        self.editor_task, self.editor_anchor = project.task, project.anchor
        self.loaded(preserve_history=True)
        self.editor_undo = (self.editor_revision(), None)
        return self.editor_snapshot()

    def refresh_ports(self):
        if self.connected or self.controller.busy:
            return
        from serial.tools import list_ports
        choices = [port.device for port in list_ports.comports()
                   if re.fullmatch(r"COM[1-9]\d*", port.device, re.I)]
        self.ports["values"] = choices
        if self.usb_port.get() not in choices:
            self.usb_port.set(choices[0] if choices else "")
        self.select_connection_target()

    def wifi_worker_active(self):
        return self.wifi_search_thread is not None and self.wifi_search_thread.is_alive()

    def cancel_wifi_search(self, *, clear_results=False):
        if self.wifi_search_cancel is not None:
            self.wifi_search_cancel.set()
        self.wifi_search_token = None
        self.wifi_searching = False
        if clear_results:
            self.wifi_devices = {}
            self.wifi_target.set("")
            self.wifi_boards["values"] = ()
            if not self.connected and self.connection_mode.get() == "Wi-Fi":
                self.port.set("")

    def change_connection_mode(self):
        if self.connected or self.controller.busy or self.svg_loading:
            self.connection_mode.set(self.active_connection_mode)
            return
        mode = self.connection_mode.get()
        if mode not in ("USB", "Wi-Fi", "Демо"):
            self.connection_mode.set(self.active_connection_mode)
            return
        self.cancel_wifi_search(clear_results=True)
        self.active_connection_mode = mode
        if mode == "Wi-Fi" and app_profile(self) != DLC32:
            self.machine_profile.set(PROFILE_LABELS[DLC32])
            self.change_machine_profile()
        for frame in (self.usb_connection_frame, self.wifi_connection_frame, self.demo_connection_frame):
            frame.pack_forget()
        self.wifi_status_label.pack_forget()
        if mode == "USB":
            self.usb_connection_frame.pack(fill="x")
            self.connection_help.set("Подключите плату USB-кабелем, выберите её COM-порт и нажмите «Подключить».")
        elif mode == "Wi-Fi":
            self.wifi_connection_frame.pack(fill="x")
            self.wifi_status_label.pack(anchor="w", pady=(2, 0))
            self.connection_help.set("Сначала подключите ноутбук к сети FluidNC или общему роутеру, затем нажмите «Найти плату».")
            self.wifi_status.set("Поиск проверяет платы в вашей сети. Подключение выполняется отдельной кнопкой." if MDNS_AVAILABLE else
                                 "Доступны точка доступа FluidNC, поиск по имени и сохранённые платы; расширенный поиск в сети роутера ограничен.")
        else:
            self.demo_connection_frame.pack(fill="x")
            self.connection_help.set("Демо работает без платы и не отправляет команды оборудованию.")
        self.select_connection_target()

    def connection_target(self):
        mode = self.connection_mode.get()
        if mode == "Демо":
            return "DEMO"
        if mode == "USB":
            port = self.usb_port.get()
            if not re.fullmatch(r"COM[1-9]\d*", port, re.I):
                raise ValueError("USB-порт не найден. Подключите плату и нажмите «Обновить».")
            return port.upper()
        if mode == "Wi-Fi":
            endpoint = self.wifi_devices.get(self.wifi_target.get())
            if endpoint is None:
                raise ValueError("Нажмите «Найти плату» и выберите найденную FluidNC.")
            if app_profile(self) != DLC32:
                raise ValueError("Wi-Fi работает с профилем MKS DLC32 / FluidNC.")
            return endpoint
        raise ValueError("Выберите USB, Wi-Fi или Демо.")

    def select_connection_target(self, event=None):
        if not self.connected and not self.controller.busy:
            try:
                self.port.set(self.connection_target())
            except ValueError:
                self.port.set("")
        self.update_controls()

    def find_wifi_boards(self):
        if (self.closed or self.connected or self.controller.busy or self.svg_loading or
                self.connection_mode.get() != "Wi-Fi" or self.wifi_worker_active()):
            return False
        self.cancel_wifi_search(clear_results=True)
        token, cancel = object(), threading.Event()
        self.wifi_search_token, self.wifi_search_cancel = token, cancel
        self.wifi_searching = True
        self.wifi_status.set("Ищу и проверяю FluidNC в сети…")
        self.port.set("")
        events = self.wifi_events
        remembered = tuple(self.remembered_wifi)

        def discover():
            try:
                result = discover_fluidnc(cancel, remembered=remembered, timeout=4.0)
            except Exception as exc:
                events.put((token, None, str(exc)))
            else:
                events.put((token, result, None))

        self.wifi_search_thread = threading.Thread(target=discover, daemon=True)
        self.wifi_search_thread.start()
        self.update_controls()
        return True

    def pump_wifi_results(self):
        while True:
            try:
                token, devices, error = self.wifi_events.get_nowait()
            except queue.Empty:
                return
            if token is not self.wifi_search_token or self.connection_mode.get() != "Wi-Fi":
                continue
            self.wifi_search_token = None
            self.wifi_searching = False
            if error:
                self.wifi_status.set("Поиск не завершён: " + error + ". Проверьте сеть ноутбука и повторите поиск.")
                continue
            from network_transport import parse_tcp_endpoint
            self.wifi_devices = {}
            for device in devices or ():
                try:
                    endpoint = parse_tcp_endpoint(device.endpoint).url
                except (AttributeError, ValueError):
                    continue
                if endpoint in self.wifi_devices.values():
                    continue
                label = str(device.label).strip() or "FluidNC"
                if label in self.wifi_devices:
                    label += " · " + endpoint.removeprefix("tcp://")
                self.wifi_devices[label] = endpoint
            labels = tuple(self.wifi_devices)
            self.wifi_boards["values"] = labels
            self.wifi_target.set(labels[0] if len(labels) == 1 else "")
            if labels:
                self.wifi_status.set("Найдена FluidNC. Нажмите «Подключить»." if len(labels) == 1 else
                                     f"Найдено плат: {len(labels)}. Выберите свою и нажмите «Подключить».")
            else:
                self.wifi_status.set("FluidNC не найдена. Проверьте питание платы и сеть ноутбука, затем повторите поиск." +
                                     (" Расширенный поиск в сети роутера недоступен в этой установке приложения." if not MDNS_AVAILABLE else ""))
            self.select_connection_target()

    def submit(self, action, **params):
        if self.closed:
            return False
        params.setdefault("axes", self.axes())
        if action == "jog":
            # The same explicit manual policy applies to arrows and DWIN.
            params.setdefault("lift_pen", self.jog_lift.get())
        try:
            if action not in ("disconnect","configure","hold","clear","unlock","check_endstops"):
                params.setdefault("pen", self.read_pen_settings().as_dict())
        except ValueError as exc:
            messagebox.showerror("Настройки ручки",str(exc),parent=self.root)
            return False
        if self.controller.submit(action, **params):
            self.update_controls()
            return True
        return False

    def connect(self):
        if self.closed or self.controller.busy or self.svg_loading:
            return False
        if self.connected:
            self.submit("disconnect")
            return
        if self.wifi_searching or self.wifi_worker_active():
            self.status.set("Дождитесь завершения или остановки поиска платы.")
            return False
        try:
            target = self.connection_target()
        except ValueError as exc:
            self.status.set(str(exc))
            return False
        self.port.set(target)
        if (not self.connected and self.dwin_window is not None and
                self.dwin_window.bridge.thread is not None and self.dwin_window.bridge.thread.is_alive() and
                self.port.get().casefold() == str(self.dwin_window.bridge.port).casefold()):
            messagebox.showerror("Порт занят экраном", "Для Arduino и DWIN нужны разные COM-порты.", parent=self.root)
            return
        if self.pen_observer is not None and not self.pen_observer.closed:
            messagebox.showerror("Проверка полярности","Сначала закройте окно наблюдения A3: оно может занимать USB-порт.",parent=self.root)
            return
        selected = app_profile(self)
        if selected == UNO and self.port.get() != "DEMO" and not messagebox.askokcancel(
                "Перед подключением Uno",
                "Закройте другие программы управления. Открытие USB может сбросить Uno. "
                "Первое подключение выполните без питания двигателей и соленоида.", parent=self.root):
            return
        self.status.set("Подключение…")
        self.submit("connect", port=self.port.get(), hold=self.hold.get(), machine_profile=selected)

    def change_machine_profile(self, event=None):
        if self.connected or self.controller.busy:
            return
        selected = app_profile(self)
        if selected == UNO and self.connection_mode.get() == "Wi-Fi":
            self.connection_mode.set("USB")
            self.change_connection_mode()
        if selected != self.axis_profile:
            self.axis_preferences[self.axis_profile] = (
                self.swap.get(), self.reverse_right.get(), self.reverse_down.get())
            swap, right, down = self.axis_preferences.get(selected, (selected == UNO, True, False))
            self.swap.set(swap)
            self.reverse_right.set(right)
            self.reverse_down.set(down)
            self.axis_profile = selected
        # Relay pin and logic voltage differ even when the mechanical lift is
        # unchanged. Confirmation on the Uno does not verify a rewired DLC32.
        pen = self.stored_pen_settings()
        self.pen_enabled.set(pen.enabled)
        self.pen_active.set("Не проверено" if pen.active_low is None else "LOW" if pen.active_low else "HIGH")
        self.pen_confirmed.set(False)
        self.pen_power_raises.set(pen.power_raises)
        self.pen_continuous.set(pen.continuous)
        self.pen_up_ms.set(str(pen.up_ms))
        self.pen_down_ms.set(str(pen.down_ms))
        try:
            save_selected_profile(self.data_dir, app_profile(self))
        except OSError as exc:
            messagebox.showerror("Сохранение профиля", str(exc), parent=self.root)
        self.pen_firmware = None
        self.settings = {}
        self.hint.set("Выберите USB, Wi-Fi или Демо в блоке подключения. Для Wi-Fi сначала найдите плату в сети." if selected == DLC32 else
                      "Для Uno выберите USB и его COM-порт. Демо работает без оборудования.")
        self.update_controls()

    def stored_pen_settings(self):
        selected = app_profile(self)
        directory = pen_settings_directory(self.data_dir, selected)
        if selected == DLC32 and (directory is None or not (directory / "pen-lift-settings.json").exists()):
            return PenLiftSettings(enabled=True, power_raises=True)
        return load_settings(directory)

    def effective_machine_profile(self):
        return UNO if self.simulation and self.connected else app_profile(self)

    def machine_ready(self):
        return (settings_ready(self.settings, self.effective_machine_profile()) and
                (not self.network_controller() or self.network_lease_ready()))

    def network_controller(self):
        session = getattr(self.controller, "session", None)
        return bool(session is not None and session.machine_profile == DLC32 and
                    str(getattr(session.wire, "port", "")).lower().startswith("tcp:"))

    def network_lease_ready(self):
        """Legacy callback name: stock FluidNC needs no DWIN lease.

        This reports only the command transport, never display readiness or an
        independent firmware watchdog. Called by the worker without Tk access.
        """
        session = getattr(self.controller, "session", None)
        if session is None or session.machine_profile != DLC32:
            return False
        wire = getattr(session, "wire", None)
        return bool(str(getattr(wire, "port", "")).lower().startswith("tcp:") and
                    getattr(wire, "peer_address", None) is not None and
                    not self.controller.cancelled.is_set())

    def update_network_lease(self):
        # Stock FluidNC has no JD-DGUS lease. DWIN retains its own explicit
        # connection state in the optional display window.
        self.network_lease_seen = False

    def read_pen_settings(self):
        try:
            up,down = int(self.pen_up_ms.get()),int(self.pen_down_ms.get())
        except ValueError:
            raise ValueError("Задержки ручки задаются целыми миллисекундами: 50–500.")
        active = {"Не проверено":None,"HIGH":False,"LOW":True}.get(self.pen_active.get())
        return PenLiftSettings(self.pen_enabled.get(),active,self.pen_confirmed.get(),up,down,
                               self.pen_power_raises.get(),self.pen_continuous.get()).validate()

    def pen_ready(self):
        try:
            pen = self.read_pen_settings()
            if not pen.enabled:return True
            if self.simulation and self.connected:return True
            if not self.connected:return False
            pen.validate(require_ready=True)
            return pen_matches(self.pen_firmware, pen, self.effective_machine_profile())
        except ValueError:return False

    def pen_test_ready(self):
        try:
            pen = self.read_pen_settings()
            if not pen.enabled or not self.connected:return False
            if self.simulation:return True
            return pen.active_low is not None and pen_matches(self.pen_firmware, pen, self.effective_machine_profile())
        except ValueError:return False

    def pen_polarity_changed(self,event=None):
        self.pen_confirmed.set(False)
        self.apply_pen_settings()

    def pen_effect_changed(self,event=None):
        self.pen_power_raises.set(self.pen_effect.get() == "Поднимает")
        self.pen_confirmed.set(False)
        self.apply_pen_settings()

    def toggle_pen_settings(self):
        if self.pen_advanced.winfo_manager():
            self.pen_advanced.pack_forget()
            self.pen_settings_button.configure(text="Настройки…")
        else:
            self.pen_advanced.pack(fill="x")
            self.pen_settings_button.configure(text="Свернуть")

    def apply_pen_settings(self):
        if self.controller.busy:return
        try:
            pen = self.read_pen_settings()
            save_settings(pen_settings_directory(self.data_dir, app_profile(self)),pen)
        except (ValueError,OSError) as exc:
            messagebox.showerror("Настройки ручки",str(exc),parent=self.root)
            return
        self.pen_legend.set(connection_notice(
            pen.enabled, pen.power_raises, pen.continuous,
            power_limit_seconds=DLC32_MAX_ON_SECONDS if app_profile(self) == DLC32 else None))
        self.preview_revision += 1
        self.invalidate_editor()
        self.update_controls()

    def pen_pulse(self):
        # This explicit button is the actuation request; no repeated modal per test.
        # The controller verifies the firmware variant; the explicit test is short.
        self.submit("pen_pulse")

    def open_pen_observer(self):
        if app_profile(self) == DLC32:
            messagebox.showinfo("MKS DLC32", "Окно наблюдения A3 предназначено только для Uno. "
                                "Для FluidNC выход реле задаётся в YAML; действие M8/M9 проверяется на вашей проводке.", parent=self.root)
            return
        if self.connected or self.controller.busy:
            messagebox.showerror("Проверка полярности","Сначала отключите пульт от USB. БП моторов и соленоида должны быть выключены.",parent=self.root)
            return
        if self.pen_observer is not None and not self.pen_observer.closed:
            self.pen_observer.root.lift();return
        from relay_polarity import RelayPolarityWindow
        def choice(profile):
            self.pen_active.set('LOW' if profile.active_low else 'HIGH')
            self.pen_enabled.set(True);self.pen_confirmed.set(False)
            self.apply_pen_settings()
        self.pen_observer=RelayPolarityWindow(self.root,data_dir=self.data_dir,on_choice=choice)

    def change_hold(self):
        if self.effective_machine_profile() == DLC32:
            self.hold.set(self.settings.get(1) == 255)
            messagebox.showinfo("Удержание FluidNC", "Удержание задаётся stepping/idle_ms в config.yaml. "
                                "Пульт не переписывает конфигурацию FluidNC.", parent=self.root)
            return
        if self.connected:
            self.submit("hold", hold=self.hold.get())

    def configure(self):
        if self.effective_machine_profile() == DLC32:
            self.submit("configure", hold=self.hold.get())
            return
        if messagebox.askokcancel("Настройка без концевиков",
                "Будут сохранены: $20=0, $21=0, $22=0 (отключить лимиты и homing), "
                "$10=0 и $13=0 (рабочие координаты в мм), а также максимум скорости "
                "$110=$111=3000 мм/мин и ускорение $120=$121=150 мм/с².\n\n"
                "Удержание моторов — по галочке «Удерживать моторы в простое».\n\n"
                "При Alarm также отправится $X — разблокировка. Убедитесь, что механика свободна, "
                "причина аварии устранена и концевиков действительно нет.\n\n"
                "Шаги/мм и ток драйверов не настраиваются. Обе точки листа будут сброшены. "
                "Первую проверку 100% выполняйте без ручки.", parent=self.root):
            self.submit("configure", hold=self.hold.get())

    def check_endstops(self):
        if not self.connected or self.simulation or self.effective_machine_profile() != DLC32:
            return
        self.submit("check_endstops")

    def unlock_alarm(self):
        if (self.effective_machine_profile() != DLC32 or self.simulation or not self.connected or
                self.controller.busy or self.controller.cancelled.is_set()):
            messagebox.showinfo("Снятие аварии MKS",
                                "Сначала подключите MKS и дождитесь окончания текущей операции.", parent=self.root)
            return
        if not self.machine_ready():
            messagebox.showinfo("Снятие аварии MKS",
                                "Сначала нужна проверенная рабочая конфигурация FluidNC.",
                                parent=self.root)
            return
        if not messagebox.askokcancel("Снять аварию MKS?",
                "Сначала физически устраните причину аварии и освободите оба концевика. "
                "Не пытайтесь отъехать от нажатого концевика этой кнопкой. Если требуется ручной отвод, "
                "сначала выключите питание моторов.\n\n"
                "Подтверждаете, что причина устранена, механика свободна и концевики освобождены? "
                "Пульт прочитает свежие входы Pn и только при Alarm отправит $X. "
                "Это НЕ движение и НЕ базирование; реле не включается. "
                "Оба угла листа будут сброшены, рисунок сохранится.", parent=self.root):
            return
        self.preserve_draft()
        self.submit("unlock", confirmed=True)

    def get_feed(self):
        percent = float(self.feed.get().replace(",", "."))
        if not math.isfinite(percent) or not 1 <= percent <= 100:
            raise ValueError("Введите скорость от 1 до 100%.")
        configured_max = min(float(self.settings.get(110, MACHINE_MAX_FEED)),
                             float(self.settings.get(111, MACHINE_MAX_FEED)))
        return configured_max * percent / 100.0

    def get_step(self):
        step = float(self.step.get().replace(",", "."))
        if not math.isfinite(step) or not 0.01 <= step <= MAX_JOG_DISTANCE:
            raise ValueError(f"Введите ход за нажатие от 0,01 до {MAX_JOG_DISTANCE:g} мм.")
        return step

    def jog(self, dx, dy):
        try:
            step = self.get_step()
            return self.submit("jog", delta=(dx * step, dy * step), feed=self.get_feed())
        except ValueError as exc:
            messagebox.showerror("Проверьте ввод", str(exc), parent=self.root)

    def set_zero(self):
        self.submit("zero")

    def set_corner(self):
        self.submit("corner")

    def load_test(self):
        try:
            self.paths = test_square(self.drawing_sheet())
            self.file_name = "Тестовый квадрат: сторона ≤10 мм, отступ 1 мм"
            self.loaded()
        except ValueError as exc:
            messagebox.showerror("Тест", str(exc), parent=self.root)

    def open_svg(self):
        filename = filedialog.askopenfilename(filetypes=[("Векторный рисунок SVG", "*.svg")], parent=self.root)
        if not filename:
            return
        self.load_file(filename)

    def cancel_svg(self):
        self.svg_token = None
        self.svg_loading = False

    def load_file(self, filename):
        if self.controller.busy or self.svg_loading or self.drawing_sheet() is None:
            return
        try:
            margin = float(self.margin.get().replace(",", "."))
        except Exception as exc:
            messagebox.showerror("SVG не загружен", str(exc), parent=self.root)
            return
        sheet = self.drawing_sheet()
        token = self.svg_token = object()
        self.svg_loading = True
        self.hint.set("Загружаю рисунок…")
        self.update_controls()
        event_queue = self.svg_events

        def read_svg():
            try:
                result = load_svg(filename, sheet, margin)
            except Exception as exc:
                result = exc
            # Only the Tk thread handles widgets; obsolete imports are ignored.
            event_queue.put((token, filename, margin, result))

        threading.Thread(target=read_svg, daemon=True).start()

    def loaded(self, preserve_history=False):
        if not preserve_history:
            self.editor_history.clear()
            self.editor_undo = None
            self.editor_task = self.editor_anchor = None
        self.preview_revision += 1
        self.invalidate_editor()
        self.file_label.configure(text=self.file_name)
        self.progress["value"] = 0
        self.hint.set("Проверьте контуры и переходы. В проверенном режиме соленоида переходы выполняются с поднятой ручкой; иначе поднимайте её вручную.")
        self.redraw()
        self.update_controls()
        if self.autosaves and self.drawing_sheet() is not None and self.paths:
            enabled = self.editor_window is None or self.editor_window.closed or self.editor_window.autosave_enabled.get()
            if enabled:
                from editor_project import SketchProject
                self.autosaves.schedule('Эскиз плоттера',SketchProject(self.drawing_sheet(),self.paths,self.file_name[:500],self.editor_task,self.editor_anchor))

    def go_start(self):
        if messagebox.askokcancel("Переход к началу",
                ("Ручка будет поднята на время перемещения. После остановки питание отключится, ручка опустится.\n\n" if self.pen_enabled.get() and self.pen_power_raises.get() else
                 "Ручка будет автоматически поднята перед перемещением. Проверьте свободный ход и возвратную пружину.\n\n" if self.pen_enabled.get() else
                 "Выньте или вручную поднимите ручку, не сдвигая головку. Автоподъём отключён!\n\n")+
                "Головка переместится к первой точке рисунка.", parent=self.root):
            self.start_job("startpoint")

    def draw(self):
        if not self.zero or self.sheet is None:
            messagebox.showerror("Нужны два угла", "Эскиз сохранён. Перед рисованием заново задайте оба угла листа.", parent=self.root)
            return
        if messagebox.askokcancel("Начать рисование?",
                f"Область: {self.sheet.width:.1f} × {self.sheet.height:.1f} мм.\n\n"
                "Установите ручку, не сдвигая оси. Проверьте крепление листа и свободный ход механики.\n"
                + ("Оранжевые переходы выполняются с поднятой ручкой. После завершения питание соленоида отключится. "
                   + (f"Переходы выполняются целиком. Лимит питания катушки — {DLC32_MAX_ON_SECONDS:g} с.\n"
                      if app_profile(self) == DLC32 and not self.pen_continuous.get() else
                      "Ручка останется внизу в последней точке.\n" if self.pen_power_raises.get() else
                      "Без пауз охлаждения.\n" if self.pen_continuous.get() else "Будут паузы охлаждения.\n") if self.pen_enabled.get() else
                   "Автоподъём отключён: все оранжевые переходы тоже будут нарисованы. БП соленоида держите отключённым.\n")+
                "Возврата в угол после завершения нет.\n\n"
                + ("Сейчас выбран DEMO: реальные моторы не подключены." if self.simulation else
                   "Это запустит реальные моторы. Первый прогон сделайте без ручки и на малой скорости."), parent=self.root):
            self.start_job("draw")

    def start_job(self, action):
        try:
            if not self.connected or not self.zero or self.sheet is None:
                raise ValueError("Эскиз сохранён. Подключитесь и заново задайте оба угла перед движением.")
            from geometry import validate_paths
            validate_paths(self.paths, self.sheet)
            self.progress["value"] = 0
            self.submit(action, paths=[list(path) for path in self.paths], feed=self.get_feed())
        except ValueError as exc:
            messagebox.showerror("Проверьте ввод", str(exc), parent=self.root)

    def stop(self):
        self.cancel_wifi_search(clear_results=True)
        self.dwin_pen_state = 0
        self.preserve_draft()
        self.preview_revision += 1
        self.cancel_svg()
        self.controller.stop()
        self.zero, self.sheet = False, None
        self.position = None
        self.relay_state_unknown = True
        self.rebase_editor_history()
        self.paper_text.set(self.draft_notice())
        self.status.set("СТОП: эскиз сохранён. Для продолжения подключитесь и задайте два угла заново.")
        self.invalidate_editor()
        self.update_controls()
        self.redraw()

    def update_controls(self):
        busy = self.controller.busy or self.svg_loading
        connection_locked = busy or self.connected
        searching = self.wifi_searching or self.wifi_worker_active()
        try:
            self.connection_target()
            has_target = True
        except ValueError:
            has_target = False
        available = self.connected and not busy and not self.controller.cancelled.is_set()
        ready = available and self.machine_ready()
        motion_ready = ready and self.pen_ready()
        drawing_ready = motion_ready and self.zero and self.sheet is not None
        if not self.pen_enabled.get():
            self.jog_lift.set(False)
        self.jog_lift_button.configure(state="normal" if not busy and self.pen_enabled.get() else "disabled")
        bounded_fluidnc = app_profile(self) == DLC32 and not self.pen_continuous.get()
        if not self.jog_lift.get():
            jog_hint = ("Стрелки двигают только моторы. Перед нажатием поднимите или выньте ручку вручную. "
                        "Один клик = один ход. Начните с 1 мм.")
        elif bounded_fluidnc:
            jog_hint = ("Ручка поднимается на весь ход стрелки. Один клик = один ход. "
                        f"Лимит питания катушки — {DLC32_MAX_ON_SECONDS:g} с; после остановки питание выключается.")
        elif self.pen_continuous.get():
            jog_hint = ("Ручка поднимается на весь ход стрелки. Один клик = один ход; "
                        "после остановки питание выключается.")
        else:
            jog_hint = ("Ручка поднимается на весь ход стрелки. Один клик = один ход. "
                        "В режиме краткого питания большой шаг может быть отклонён до движения.")
        self.jog_lift_hint.set(jog_hint)
        self.endstop_text.set(endstop_summary(self.endstop_report))
        if app_profile(self) == DLC32:
            text = "Схема подключения: X + Z - портал; Y1 - каретка."
            if self.settings.get("_fluidnc_wiring") == "neiroslopper_ttl":
                text += " В YAML: X - каретка, Y - портал."
            elif self.settings.get("_fluidnc_wiring") == "legacy_xy":
                text += " Загружен YAML прежней разводки; он не подтверждает эту схему."
            self.wiring_text.set(text)
        else:
            self.wiring_text.set("")
        power_raises = self.pen_power_raises.get()
        self.pen_effect.set("Поднимает" if power_raises else "Опускает")
        self.pen_summary.set(("Питание поднимает" if power_raises else "Питание опускает") +
                             (" · выход " + self.pen_active.get() if self.pen_active.get() != "Не проверено" else " · полярность не задана") +
                             (" · непрерывный ход" if self.pen_continuous.get() else
                              f" · катушка до {DLC32_MAX_ON_SECONDS:g} с" if bounded_fluidnc else ""))
        self.pen_legend.set(connection_notice(
            self.pen_enabled.get(), power_raises, self.pen_continuous.get(),
            power_limit_seconds=DLC32_MAX_ON_SECONDS if app_profile(self) == DLC32 else None))
        if self.relay_state_unknown:
            self.pen_status.set("Состояние реле неизвестно: подтверждение отключения не получено. При необходимости выключите БП соленоида вручную.")
        elif not self.pen_enabled.get():
            self.pen_status.set("Режим без подъёма. Соленоидный БП отключите; переходы оставляют линии.")
        elif self.simulation and self.connected:
            self.pen_status.set("DEMO: виртуальный подъём, реле и USB не используются.")
        elif not self.connected:
            self.pen_status.set("Подключите плату. Настройки ручки сохраняются между запусками.")
        elif not busy and not self.pen_ready():
            self.pen_status.set("В настройках один раз подтвердите действие питания и полярность." if self.pen_test_ready() else
                                "Сначала проверьте выход реле в конфигурации платы и настройки ручки.")
        elif not busy and self.pen_ready() and self.pen_status.get().startswith(('Профиль','В настройках','Подключите','Режим без подъёма','DEMO:')):
            self.pen_status.set("Подъём включается только на время переходов и теста. После остановки ручка внизу." if power_raises else
                                "Автоподъём готов. Тест кратковременный, оси не движутся.")
        self.connect_button.configure(text="Отключить" if self.connected else "Подключить")
        for widget, enabled in (
                (self.connect_button, not busy and (self.connected or (has_target and not searching))),
                (self.refresh_button, not connection_locked and self.connection_mode.get() == "USB"),
                (self.wifi_search_button, not connection_locked and not searching and self.connection_mode.get() == "Wi-Fi"),
                (self.configure_button, available), (self.board_setup_button, not busy),
                (self.check_endstops_button, available and self.effective_machine_profile() == DLC32 and not self.simulation),
                (self.first_button, motion_ready and not self.zero),
                (self.unlock_button, ready and self.effective_machine_profile() == DLC32 and not self.simulation),
                (self.hold_button, not busy and self.effective_machine_profile() == UNO),
                (self.second_button, motion_ready and self.zero and self.sheet is None),
                (self.clear_button, available and self.zero), (self.load_button, not busy and self.drawing_sheet() is not None),
                (self.test_button, not busy and self.drawing_sheet() is not None),
                (self.startpoint_button, drawing_ready and bool(self.paths)), (self.draw_button, drawing_ready and bool(self.paths)),
                (self.feed_input, not busy), (self.margin_input, not busy)):
            widget.configure(state="normal" if enabled else "disabled")
        for widget in self.jog_buttons:
            widget.configure(state="normal" if motion_ready else "disabled")
        for widget in self.pen_widgets:
            widget.configure(state="normal" if not busy else "disabled")
        self.pen_polarity.configure(state="readonly" if not busy else "disabled")
        self.pen_effect_combo.configure(state="readonly" if not busy else "disabled")
        for widget in (self.pen_up_button,self.pen_pulse_button):
            # An isolated relay pulse does not depend on XY calibration. The
            # worker still checks the actual relay mapping and controller Idle.
            widget.configure(state="normal" if available and self.pen_test_ready() else "disabled")
        for widget in self.axis_widgets:
            widget.configure(state="normal" if not busy and not self.zero else "disabled")
        for button in self.connection_mode_buttons:
            button.configure(state="disabled" if connection_locked else "normal")
        self.ports.configure(state="readonly" if not connection_locked and self.connection_mode.get() == "USB" else "disabled")
        self.wifi_boards.configure(state="readonly" if not connection_locked and not searching and self.connection_mode.get() == "Wi-Fi" else "disabled")
        self.profile_input.configure(state="readonly" if not connection_locked else "disabled")
        self.configure_button.configure(text="Проверить конфигурацию FluidNC" if self.effective_machine_profile() == DLC32
                                        else "Настроить без концевиков")
        self.step_input.configure(state="normal" if not busy else "disabled")

    def request_redraw(self):
        if not self.closed and self.redraw_timer is None:
            self.redraw_timer = self.root.after(40, self.redraw)

    def redraw(self):
        if self.redraw_timer is not None:
            self.root.after_cancel(self.redraw_timer)
            self.redraw_timer = None
        canvas = self.canvas
        w, h = max(10, canvas.winfo_width()), max(10, canvas.winfo_height())
        sheet = self.drawing_sheet()
        key = (w, h, sheet, self.zero, self.sheet is not None, self.axes())
        if key == self.preview_key and self.preview_paths is self.paths:
            self.update_head()
            return
        self.preview_key = key
        self.preview_paths = self.paths
        self.preview_transform = None
        canvas.delete("all")
        if not sheet:
            title = "Верхний левый угол сохранён" if self.zero else "Задайте два угла листа"
            text = "Подведите головку вправо и вниз,\nзатем сохраните нижний правый угол." if self.zero else \
                "① Верхний левый — начало координат\n② Нижний правый — ширина и высота\n\nЛист положите параллельно осям станка."
            canvas.create_text(w / 2, h / 2 - 50, text=title, font=("Segoe UI", 17, "bold"), fill="#22392b")
            canvas.create_text(w / 2, h / 2 + 10, text=text, font=("Segoe UI", 12), fill="#52647b", justify="center")
            return
        scale = min((w - 90) / sheet.width, (h - 80) / sheet.height)
        if scale <= 0:
            return
        left, top = (w - scale * sheet.width) / 2, (h - scale * sheet.height) / 2
        self.preview_transform = (left, top, scale)
        def xy(point):
            return left + point[0] * scale, top + point[1] * scale
        x2, y2 = xy((sheet.width, sheet.height))
        canvas.create_rectangle(left, top, x2, y2, fill="white", outline="#a8b6c8")
        grid = 10 if max(sheet.width, sheet.height) < 400 else 100
        for u in range(grid, int(sheet.width), grid):
            canvas.create_line(*xy((u, 0)), *xy((u, sheet.height)), fill="#edf1f5")
        for v in range(grid, int(sheet.height), grid):
            canvas.create_line(*xy((0, v)), *xy((sheet.width, v)), fill="#edf1f5")
        canvas.create_text(left, top - 16, text=("① 0,0 · верхний левый" if self.zero and self.sheet else "Эскиз сохранён · перед рисованием задайте два угла"), anchor="w", fill="#344b65")
        canvas.create_text(x2, y2 + 17, text=f"② {sheet.width:.1f} × {sheet.height:.1f} мм", anchor="e", fill="#344b65")
        previous = None
        for path in self.paths:
            if not path:
                continue
            if previous is not None and math.dist(previous, path[0]) > 0.0005:
                canvas.create_line(*xy(previous), *xy(path[0]), fill="#df790d", width=2, dash=(5, 3))
            # Keep every point, but avoid one Tcl/Canvas object per segment.
            # Blocks overlap at their endpoints to preserve the exact polyline.
            for offset in range(0, len(path) - 1, 512):
                points = path[offset:offset + 513]
                coordinates = [value for point in points for value in xy(point)]
                canvas.create_line(*coordinates, fill="#17633f", width=2, tags="contour")
            previous = path[-1]
        if self.paths:
            sx, sy = xy(self.paths[0][0])
            canvas.create_oval(sx - 4, sy - 4, sx + 4, sy + 4, fill="#218657", outline="")
        canvas.create_line(0, 0, 0, 0, fill="#df790d", width=2, dash=(5, 3), tags="start_link")
        canvas.create_line(0, 0, 0, 0, fill="#ba2439", width=2, tags="head_horizontal")
        canvas.create_line(0, 0, 0, 0, fill="#ba2439", width=2, tags="head_vertical")
        self.update_head()

    def update_head(self):
        if self.preview_transform is None:
            return
        if self.position is None or self.sheet is None:
            for tag in ("head_horizontal", "head_vertical", "start_link"):
                self.canvas.itemconfigure(tag, state="hidden")
            return
        for tag in ("head_horizontal", "head_vertical"):
            self.canvas.itemconfigure(tag, state="normal")
        left, top, scale = self.preview_transform
        current = self.axes().paper(self.position) if self.position is not None else (0, 0)
        cx, cy = left + current[0] * scale, top + current[1] * scale
        self.canvas.coords("head_horizontal", cx - 7, cy, cx + 7, cy)
        self.canvas.coords("head_vertical", cx, cy - 7, cx, cy + 7)
        if self.paths and math.dist(current, self.paths[0][0]) > 0.0005:
            start = self.paths[0][0]
            self.canvas.coords("start_link", cx, cy, left + start[0] * scale, top + start[1] * scale)
            self.canvas.itemconfigure("start_link", state="normal")
        else:
            self.canvas.itemconfigure("start_link", state="hidden")

    def pump(self):
        if self.closed:
            return
        self.pump_wifi_results()
        redraw = False
        latest_position = latest_progress = None
        for _ in range(500):
            try:
                name, value = self.controller.events.get_nowait()
            except queue.Empty:
                break
            if name == "connected":
                self.connected = True
                self.port_name = value
                self.simulation = value == "DEMO"
                self.rebase_editor_history()
                self.status.set("DEMO · симуляция, USB не используется" if self.simulation else
                                f"{value} · {PROFILE_LABELS[self.effective_machine_profile()]} подключён")
                self.note("Подключено: " + value + ". Ноль нужно выставить заново.")
                if str(value).lower().startswith("tcp://"):
                    try:
                        remember_verified_endpoint(self.data_dir, value)
                    except (OSError, ValueError) as exc:
                        self.note("Подключение работает; адрес платы не сохранён: " + str(exc))
                    else:
                        self.remembered_wifi = [value] + [item for item in self.remembered_wifi if item != value][:7]
            elif name == "disconnected":
                self.dwin_pen_state = 0
                self.preserve_draft()
                self.preview_revision += 1
                self.cancel_svg()
                latest_position = latest_progress = None
                self.connected = self.zero = False
                self.port_name = ""
                self.simulation = False
                self.settings = {}
                self.endstop_report = None
                self.pen_firmware = None
                self.sheet = self.position = None
                self.relay_state_unknown = True
                self.rebase_editor_history()
                self.invalidate_editor()
                self.paper_text.set(self.draft_notice())
                self.coordinates.set("Положение: —")
                self.status.set("Не подключено · эскиз сохранён; для движения задайте оба угла заново")
                self.hint.set("Рисунок и история правок сохранены. Можно редактировать и сохранить проект; автоматического продолжения движения нет.")
                redraw = True
            elif name == "settings":
                self.settings = value
                reason = value.get("_fluidnc_reason")
                if reason:
                    self.hint.set(str(reason))
                    self.note(str(reason))
                if 1 in value:
                    self.hold.set(value[1] == 255)
                self.note("Шаги/мм: X=" + str(value.get(100, "?")) + "; Y=" + str(value.get(101, "?"))
                          + "; 100% скорости=" + str(min(value.get(110, 0), value.get(111, 0))) + " мм/мин")
            elif name == "pen_firmware":
                self.pen_firmware = value
                self.note("Прошивка ручки: "+str(value) if value else "Профиль выхода ручки не подтверждён; автоматический режим недоступен.")
            elif name == "pen":
                self.dwin_pen_state = {"up": 1, "down": 2}.get(value, 0)
                self.relay_state_unknown = value == "unknown"
                power_raises = self.pen_power_raises.get()
                text = {"up":"Команда подъёма: питание " + ("включено." if power_raises else "выключено."),
                        "down":"Команда опускания: питание " + ("выключено." if power_raises else "включено."),
                        "off":"Питание соленоида выключено для ручного перемещения.",
                        "cooling":"Питание выключено: пауза охлаждения. " + ("Ручка внизу." if power_raises else "Ожидается подъём ручки."),
                        "unknown":"Состояние реле неизвестно: доставка сброса не подтверждена. При обрыве связи отключите силовое питание вручную."}.get(value,str(value))
                self.pen_status.set(text + " Положение датчиком не измеряется.")
            elif name == "position":
                state, self.position = value
                latest_position = value
                redraw = True
            elif name == "endstops":
                self.endstop_report = value
            elif name == "calibration":
                self.preserve_draft()
                self.preview_revision += 1
                self.cancel_svg()
                self.zero, self.sheet = value
                self.rebase_editor_history()
                self.invalidate_editor()
                if self.sheet:
                    self.draft_sheet = self.sheet
                    self.paper_text.set(f"Лист: {self.sheet.width:.2f} × {self.sheet.height:.2f} мм")
                    if self.paths:
                        from geometry import validate_paths
                        try:
                            validate_paths(self.paths, self.sheet)
                        except ValueError:
                            self.hint.set("Эскиз сохранён без изменения масштаба, но не помещается в новую область. Измените его размер перед рисованием.")
                        else:
                            self.hint.set("Область задана. Сохранённый эскиз не изменён; проверьте размещение и запустите рисование отдельно.")
                    else:
                        self.hint.set("Область задана. Выберите тестовый квадрат или загрузите SVG.")
                else:
                    self.paper_text.set("Первая точка сохранена. Эскиз сохранён; задайте нижний правый угол." if self.zero
                                        else self.draft_notice())
                redraw = True
            elif name == "progress":
                latest_progress = value
            elif name == "finished":
                latest_progress = None
                self.progress["value"] = 100
                text = "Головка у начала. Установите ручку, не сдвигая оси, и нажмите «Начать рисование»." if value == "startpoint" else \
                    "Рисование завершено: контроллер подтвердил остановку. Автоматического возврата нет."
                if value == "scan":
                    text = "Сканирующий проход завершён. Кадры находятся в окне камеры; головка остаётся на последней точке."
                if value in ("pen_up","pen_pulse"):
                    text = ("Краткий тест завершён: питание выключено, ручка возвращается вниз." if self.pen_power_raises.get() else
                            "Краткий тест завершён: питание выключено. Проверьте, что ручка вернулась вверх.")
                    self.pen_status.set(text)
                elif self.pen_enabled.get():
                    text += (" Питание соленоида отключено, ручка остаётся внизу." if self.pen_power_raises.get() else
                             " Питание соленоида отключено; ожидается подъём ручки.")
                self.hint.set(text)
                self.note(text)
            elif name == "error":
                if isinstance(value, dict):
                    text = str(value.get("message", value.get("error", "Операция прервана")))
                    log_path = value.get("diagnostic_log", value.get("log_path"))
                    if log_path:
                        text += "\n\nДиагностический журнал: " + str(log_path)
                else:
                    text = str(value)
                if "ClearCommError" in text or "PermissionError(13" in text:
                    text = ("Windows прервала доступ к USB-порту. Рисунок сохранён; перед новым запуском восстановите связь и задайте оба угла.\n\n"
                            "Технические подробности: " + text)
                self.note("ОШИБКА: " + text)
                # A preceding disconnect event already invalidated the session.
                # Refresh its controls before the modal dialog pauses this pump.
                self.update_controls()
                messagebox.showerror("Операция остановлена", text, parent=self.root)
            elif name == "log":
                self.note(value)
        if latest_position is not None and self.position is not None:
            state, position = latest_position
            u, v = self.axes().paper(position)
            self.coordinates.set(f"Вправо: {u:.2f} · вниз: {v:.2f} мм · {state}")
        if latest_progress is not None:
            sent, total = latest_progress
            self.progress["value"] = 100 * sent / max(1, total)
            self.hint.set(f"По данным контроллера выполнено {sent:.1f}/{total:.1f} мм штрихов." if isinstance(total,float) else
                          f"В контроллер передано {sent}/{total} участков. Ожидаю завершения движения…")
        while not self.svg_events.empty():
            token, filename, margin, result = self.svg_events.get_nowait()
            if token is not self.svg_token:
                continue
            self.cancel_svg()
            if isinstance(result, Exception):
                self.hint.set("Не удалось загрузить рисунок. Выберите другой SVG.")
                messagebox.showerror("SVG не загружен", str(result), parent=self.root)
            else:
                self.paths = result
                self.file_name = Path(filename).name + f" · {len(result)} контур(ов) · поле {margin:g} мм"
                if getattr(result, "actual_size", False):
                    self.file_name += " · размер 1:1, без растягивания"
                self.loaded()
                redraw = False
        if redraw:
            self.redraw()
        self.update_network_lease()
        self.update_controls()
        if self.dwin_window is not None:
            self.dwin_window.pump()
        self.pump_timer = self.root.after(60, self.pump)

    def close(self):
        if self.closed:
            return
        if self.controller.busy and not messagebox.askokcancel("Закрыть программу?",
                "Операция выполняется. Будет отправлен сброс; ноль потеряется. Закрыть?", parent=self.root):
            return
        self.closed = True
        self.cancel_wifi_search()
        # Auxiliary windows and autosaves can wait for workers. Stop motion
        # before any of those waits, as soon as closing is accepted.
        self.controller.shutdown()
        # The controller only publishes to a queue; it never waits for Tk.
        # Finish its normal cleanup before destroying the Tcl interpreter,
        # while keeping a stalled transport from blocking window close.
        if self.controller.worker.is_alive():
            self.controller.worker.join(timeout=3)
        if self.dwin_window is not None:
            self.dwin_window.close()
        for name in ('pump_timer', 'redraw_timer'):
            identifier = getattr(self, name)
            if identifier is not None:
                self.root.after_cancel(identifier)
                setattr(self, name, None)
        self.cancel_svg()
        if self.editor_window is not None:
            self.editor_window.close()
        if self.camera_window is not None:
            self.camera_window.close()
        if self.pen_observer is not None:
            self.pen_observer.stop.set()
        if self.autosaves:
            self.autosaves.close()
        self.root.destroy()


if __name__ == "__main__":
    App(tk.Tk(),data_dir=Path(__file__).parent/'data').root.mainloop()
