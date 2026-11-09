"""Main workspace presentation. All machine actions remain owned by App."""
import tkinter as tk
from tkinter import ttk

from branding import APP_NAME, apply_app_icon
from machine_profiles import PROFILE_LABELS
from ui_theme import BACKGROUND, SURFACE, FIELD, INK, MUTED, CircuitDivider


def section(parent, title, *, pady=(0, 12)):
    frame = ttk.Frame(parent)
    frame.pack(fill="x", pady=pady)
    ttk.Label(frame, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 10))
    return frame


def build_workspace(app):
    a = app
    outer = ttk.Frame(a.root, padding=(22, 16), style="App.TFrame")
    outer.pack(fill="both", expand=True)
    header = ttk.Frame(outer, style="App.TFrame")
    header.pack(fill="x")
    ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
    a.stop_button = ttk.Button(header, text="СТОП · Esc", command=a.stop, style="Danger.TButton")
    a.stop_button.pack(side="right")
    ttk.Button(header, text="Справка", command=a.open_help, style="Header.TButton").pack(side="right", padx=16)
    CircuitDivider(outer).pack(fill="x", pady=(7, 9))

    connection = ttk.Frame(outer, padding=(14, 10), style="Card.TFrame")
    connection.pack(fill="x", pady=(0, 14))
    row = ttk.Frame(connection)
    row.pack(fill="x")
    a.connection_mode_buttons = []
    for mode in ("USB", "Wi-Fi", "Демо"):
        button = ttk.Radiobutton(row, text=mode, value=mode, variable=a.connection_mode,
                                 command=a.change_connection_mode, style="Mode.TRadiobutton")
        button.pack(side="left", padx=(0, 4))
        a.connection_mode_buttons.append(button)
    a.connect_button = ttk.Button(row, text="Подключить", command=a.connect, style="Accent.TButton")
    a.connect_button.pack(side="right", padx=(12, 0))
    a.target_area = ttk.Frame(row)
    a.target_area.pack(side="left", fill="x", expand=True, padx=(8, 0))
    a.usb_connection_frame = ttk.Frame(a.target_area)
    a.ports = ttk.Combobox(a.usb_connection_frame, textvariable=a.usb_port, width=18, state="readonly")
    a.ports.pack(side="left", padx=(0, 8))
    a.ports.bind("<<ComboboxSelected>>", a.select_connection_target)
    a.refresh_button = ttk.Button(a.usb_connection_frame, text="Обновить порты", command=a.refresh_ports, style="Quiet.TButton")
    a.refresh_button.pack(side="left")
    a.wifi_connection_frame = ttk.Frame(a.target_area)
    a.wifi_boards = ttk.Combobox(a.wifi_connection_frame, textvariable=a.wifi_target, width=26, state="readonly")
    a.wifi_boards.pack(side="left", fill="x", expand=True, padx=(0, 8))
    a.wifi_boards.bind("<<ComboboxSelected>>", a.select_connection_target)
    a.wifi_search_button = ttk.Button(a.wifi_connection_frame, text="Найти плату", command=a.find_wifi_boards)
    a.wifi_search_button.pack(side="left")
    a.demo_connection_frame = ttk.Frame(a.target_area)
    ttk.Label(a.demo_connection_frame, text="Без оборудования", style="Muted.TLabel").pack(side="left")
    a.demo_connection_frame.pack(fill="x")
    a.wifi_status_label = ttk.Label(connection, textvariable=a.wifi_status, style="Muted.TLabel", wraplength=900)

    # Footer is packed before the expanding workspace, keeping STOP/start visible
    # even when the optional journal is open at the minimum window height.
    footer = ttk.Frame(outer, style="App.TFrame")
    footer.pack(side="bottom", fill="x", pady=(9, 0))
    status_row = ttk.Frame(footer, style="App.TFrame")
    status_row.pack(fill="x")
    a.diagnostics_button = ttk.Button(status_row, text="Журнал ▴", command=a.toggle_diagnostics, style="Header.TButton")
    a.diagnostics_button.pack(side="right", padx=(12, 0))
    a.status_label = ttk.Label(status_row, textvariable=a.status, style="Status.TLabel", width=1)
    a.status_label.pack(side="left", fill="x", expand=True)
    a.diagnostics_frame = ttk.Frame(footer, style="App.TFrame")
    scrollbar = ttk.Scrollbar(a.diagnostics_frame, orient="vertical")
    scrollbar.pack(side="right", fill="y")
    a.log = tk.Text(a.diagnostics_frame, height=4, font=("Consolas", 9), state="disabled", bg=FIELD,
                    fg=INK, insertbackground=INK, relief="flat", padx=10, pady=8, yscrollcommand=scrollbar.set)
    a.log.pack(fill="both", expand=True)
    scrollbar.configure(command=a.log.yview)

    columns = ttk.Frame(outer, style="App.TFrame")
    columns.pack(fill="both", expand=True)
    sidebar = ttk.Frame(columns, width=320, padding=(10, 6), style="Card.TFrame")
    sidebar.pack(side="left", fill="y", padx=(0, 16))
    sidebar.pack_propagate(False)
    tabs = ttk.Notebook(sidebar)
    tabs.pack(fill="both", expand=True)
    a.test_panel, a.settings_panel = ttk.Frame(tabs), ttk.Frame(tabs)
    tabs.add(a.test_panel, text="Тестирование")
    tabs.add(a.settings_panel, text="Настройки")
    a.sidebar_tabs = tabs
    scroll_targets = []

    def scroll_panel(parent):
        scrollbar = ttk.Scrollbar(parent, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        canvas = tk.Canvas(parent, bg=SURFACE, borderwidth=0, highlightthickness=0, yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=canvas.yview)
        frame = ttk.Frame(canvas, padding=(6, 6, 10, 12))
        window = canvas.create_window(0, 0, window=frame, anchor="nw", width=280)
        frame.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        scroll_targets.append((parent, canvas))
        return frame, canvas

    control_left, a.sidebar_canvas = scroll_panel(a.test_panel)
    settings_left, _ = scroll_panel(a.settings_panel)

    def scroll_sidebar(event):
        widget = event.widget
        while widget is not None:
            for parent, canvas in scroll_targets:
                if widget is parent:
                    canvas.yview_scroll(int(-event.delta / 120), "units")
                    return "break"
            widget = getattr(widget, "master", None)
    a.root.bind("<MouseWheel>", scroll_sidebar)

    jog = section(control_left, "Ручное перемещение")
    row = ttk.Frame(jog)
    row.pack(fill="x")
    for column in (0, 1):
        row.columnconfigure(column, weight=1)
    ttk.Label(row, text="Шаг, мм", style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 5))
    ttk.Label(row, text="Скорость, %", style="Muted.TLabel").grid(row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 5))
    a.step_input = ttk.Combobox(row, textvariable=a.step, values=("0.1", "1", "5", "10", "20", "50", "100"), width=8)
    a.step_input.grid(row=1, column=0, sticky="ew")
    a.feed_input = ttk.Entry(row, textvariable=a.feed, width=6)
    a.feed_input.grid(row=1, column=1, sticky="ew", padx=(10, 0))
    cross = ttk.Frame(jog)
    cross.pack(pady=(16, 8))
    a.jog_buttons = []
    for label, dx, dy, r, c in (("↑", 0, -1, 0, 1), ("←", -1, 0, 1, 0),
                                ("→", 1, 0, 1, 2), ("↓", 0, 1, 2, 1)):
        button = ttk.Button(cross, text=label, width=3, style="Jog.TButton", command=lambda x=dx, y=dy: a.jog(x, y))
        button.grid(row=r, column=c, padx=3, pady=3)
        a.jog_buttons.append(button)
    ttk.Label(jog, textvariable=a.coordinates, wraplength=265, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
    a.jog_lift_button = ttk.Checkbutton(jog, text="Поднимать ручку на время хода", variable=a.jog_lift, command=a.update_controls)
    a.jog_lift_button.pack(anchor="w")
    ttk.Separator(control_left).pack(fill="x", pady=(0, 16))
    pen_test = section(control_left, "Тест ручки", pady=(0, 0))
    a.pen_up_button = ttk.Button(pen_test, text="Проверить подъём и возврат", command=lambda: a.submit("pen_up"))
    a.pen_up_button.pack(fill="x")
    ttk.Label(pen_test, textvariable=a.pen_state_text, style="Muted.TLabel", wraplength=265).pack(anchor="w", pady=(8, 0))
    ttk.Separator(control_left).pack(fill="x", pady=(18, 16))
    drawing_test = section(control_left, "Тестовый рисунок", pady=(0, 0))
    a.test_button = ttk.Button(drawing_test, text="Показать квадрат 10 мм", command=a.load_test)
    a.test_button.pack(fill="x")

    setup = section(settings_left, "Плата и оси")
    a.profile_input = ttk.Combobox(setup, textvariable=a.machine_profile, values=tuple(PROFILE_LABELS.values()), state="readonly", width=25)
    a.profile_input.pack(fill="x", pady=(0, 8))
    a.profile_input.bind("<<ComboboxSelected>>", a.change_machine_profile)
    a.board_setup_button = ttk.Button(setup, text="Настроить плату…", command=a.open_board_setup)
    a.board_setup_button.pack(fill="x", pady=(0, 5))
    a.configure_button = ttk.Button(setup, text="Проверить настройки платы", command=a.configure)
    a.configure_button.pack(fill="x", pady=(0, 5))
    a.check_endstops_button = ttk.Button(setup, text="Проверить концевики", command=a.check_endstops)
    a.check_endstops_button.pack(fill="x")
    ttk.Label(setup, textvariable=a.endstop_text, wraplength=265, style="Muted.TLabel").pack(anchor="w", pady=(6, 8))
    a.unlock_button = ttk.Button(setup, text="Снять аварию MKS…", command=a.unlock_alarm)
    a.unlock_button.pack(fill="x", pady=(0, 8))
    a.hold_button = ttk.Checkbutton(setup, text="Удерживать моторы в простое", variable=a.hold, command=a.change_hold)
    a.hold_button.pack(anchor="w", pady=(0, 4))
    a.axis_widgets = []
    for text, variable in (("Поменять оси X ↔ Y", a.swap), ("Развернуть направление вправо", a.reverse_right),
                           ("Развернуть направление вниз", a.reverse_down)):
        widget = ttk.Checkbutton(setup, text=text, variable=variable, command=a.redraw)
        widget.pack(anchor="w", pady=1)
        a.axis_widgets.append(widget)
    ttk.Separator(settings_left).pack(fill="x", pady=(0, 14))
    pen_box = section(settings_left, "Ручка и соленоид")
    a.pen_widgets = []
    enabled = ttk.Checkbutton(pen_box, text="Автоматический подъём ручки", variable=a.pen_enabled, command=a.apply_pen_settings)
    enabled.pack(anchor="w")
    a.pen_settings_button = ttk.Button(pen_box, text="Параметры реле…", command=a.toggle_pen_settings)
    a.pen_settings_button.pack(fill="x", pady=(7, 0))
    a.pen_advanced = ttk.Frame(pen_box)
    for caption, variable, values, attr, callback in (
            ("Активный уровень", a.pen_active, ("Не проверено", "HIGH", "LOW"), "pen_polarity", a.pen_polarity_changed),
            ("Питание ручку", a.pen_effect, ("Поднимает", "Опускает"), "pen_effect_combo", a.pen_effect_changed)):
        row = ttk.Frame(a.pen_advanced)
        row.pack(fill="x", pady=(8, 2))
        ttk.Label(row, text=caption).pack(side="left")
        combo = ttk.Combobox(row, textvariable=variable, values=values, state="readonly", width=12)
        combo.pack(side="right")
        combo.bind("<<ComboboxSelected>>", callback)
        setattr(a, attr, combo)
    verified = ttk.Checkbutton(a.pen_advanced, text="Полярность и действие проверены", variable=a.pen_confirmed, command=a.apply_pen_settings)
    verified.pack(anchor="w")
    continuous = ttk.Checkbutton(a.pen_advanced, text="Без лимита времени катушки", variable=a.pen_continuous, command=a.apply_pen_settings)
    continuous.pack(anchor="w")
    for caption, variable in (("Подъём, мс", a.pen_up_ms), ("Опускание, мс", a.pen_down_ms)):
        row = ttk.Frame(a.pen_advanced)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=caption).pack(side="left")
        field = ttk.Entry(row, textvariable=variable, width=8)
        field.pack(side="right")
        a.pen_widgets.append(field)
    save = ttk.Button(a.pen_advanced, text="Сохранить параметры реле", command=a.apply_pen_settings)
    save.pack(fill="x", pady=(7, 4))
    help_button = ttk.Button(a.pen_advanced, text="Определить HIGH / LOW…", command=a.open_pen_observer)
    help_button.pack(fill="x", pady=3)
    a.pen_pulse_button = ttk.Button(a.pen_advanced, text="Короткий импульс реле", command=a.pen_pulse)
    a.pen_pulse_button.pack(fill="x", pady=3)
    a.pen_widgets.extend((enabled, verified, continuous, save, help_button, a.pen_settings_button))
    drawing_settings = section(settings_left, "Рисунок")
    row = ttk.Frame(drawing_settings)
    row.pack(fill="x")
    ttk.Label(row, text="Отступ SVG, мм").pack(side="left")
    a.margin_input = ttk.Entry(row, textvariable=a.margin, width=7)
    a.margin_input.pack(side="right")
    extra = section(settings_left, "Дополнительно", pady=(0, 0))
    a.camera_button = ttk.Button(extra, text="Камера и сканер…", command=a.open_camera)
    a.camera_button.pack(fill="x", pady=(0, 5))
    ttk.Button(extra, text="Дисплей DWIN…", command=a.open_dwin).pack(fill="x")

    a.workflow_panel = right = ttk.Frame(columns, padding=(20, 16), style="Card.TFrame")
    right.pack(side="left", fill="both", expand=True)
    a.corners_panel = corners = ttk.Frame(right)
    corners.pack(fill="x", pady=(0, 14))
    heading = ttk.Frame(corners)
    heading.pack(fill="x", pady=(0, 10))
    ttk.Label(heading, text="Область на бумаге", style="Section.TLabel").pack(side="left")
    ttk.Label(heading, textvariable=a.paper_summary, style="Muted.TLabel").pack(side="right")
    corner_row = ttk.Frame(corners)
    corner_row.pack(fill="x")
    a.clear_button = ttk.Button(corner_row, text="Сбросить", command=lambda: a.submit("clear"), style="Quiet.TButton")
    a.clear_button.pack(side="right", padx=(12, 0))
    a.first_button = ttk.Button(corner_row, text="Задать верхний левый угол", command=a.set_zero)
    a.first_button.pack(side="left", fill="x", expand=True, padx=(0, 8))
    a.second_button = ttk.Button(corner_row, text="Задать нижний правый угол", command=a.set_corner)
    a.second_button.pack(side="left", fill="x", expand=True)
    ttk.Separator(right).pack(fill="x", pady=(0, 14))
    toolbar = ttk.Frame(right)
    toolbar.pack(fill="x", pady=(0, 8))
    ttk.Label(toolbar, text="Рисунок", style="Section.TLabel").pack(side="left")
    a.load_button = ttk.Button(toolbar, text="Открыть SVG…", command=a.open_svg)
    a.load_button.pack(side="right")
    a.assistant_button = ttk.Button(toolbar, text="Помощник", command=a.open_assistant)
    a.assistant_button.pack(side="right", padx=(8, 8))
    ttk.Button(toolbar, text="Редактор", command=a.open_editor).pack(side="right", padx=(12, 0))
    a.file_label = ttk.Label(right, text=a.file_name, style="Muted.TLabel", width=1)
    a.file_label.pack(fill="x", pady=(0, 10))

    # Pack controls at the bottom first; preview yields space rather than start.
    actions = ttk.Frame(right)
    actions.pack(side="bottom", fill="x", pady=(12, 0))
    a.draw_button = ttk.Button(actions, text="Начать рисование", command=a.draw, style="Accent.TButton")
    a.draw_button.pack(side="right")
    a.startpoint_button = ttk.Button(actions, text="Подвести к началу", command=a.go_start)
    a.startpoint_button.pack(side="right", padx=(0, 8))
    ttk.Label(actions, textvariable=a.workflow_status, style="Muted.TLabel", wraplength=205).pack(side="left", fill="x", expand=True, padx=(0, 10))
    a.progress = ttk.Progressbar(right, maximum=100)
    a.progress.pack(side="bottom", fill="x", pady=(10, 0))
    a.canvas = tk.Canvas(right, width=1, height=1, bg=FIELD, borderwidth=0, highlightthickness=0)
    a.canvas.pack(fill="both", expand=True)
    a.canvas.bind("<Configure>", lambda event: a.request_redraw())


def show_help(a):
    from display_scaling import ui_scale, scale_widget_tree
    existing = getattr(a, "help_window", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    window = a.help_window = tk.Toplevel(a.root)
    window.title("Neiroslopper · Справка")
    window.configure(bg=BACKGROUND)
    scale = ui_scale(a.root)
    width, height = round(700 * scale), round(590 * scale)
    x = max(0, min(a.root.winfo_screenwidth() - width,
                   a.root.winfo_rootx() + (a.root.winfo_width() - width) // 2))
    y = max(0, min(a.root.winfo_screenheight() - height - 60,
                   a.root.winfo_rooty() + (a.root.winfo_height() - height) // 2))
    window.geometry(f"{width}x{height}+{x}+{y}")
    window.minsize(round(620 * scale), round(480 * scale))
    window._neiroslopper_geometry_scaled = True
    window.bind("<Escape>", lambda event: a.stop())
    apply_app_icon(window)
    ttk.Button(window, text="Закрыть", command=window.destroy).pack(side="bottom", anchor="e", padx=16, pady=(0, 14))
    tabs = ttk.Notebook(window)
    tabs.pack(fill="both", expand=True, padx=16, pady=16)
    help_canvases = {}

    def page(title):
        panel = ttk.Frame(tabs)
        tabs.add(panel, text=title)
        scrollbar = ttk.Scrollbar(panel, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        canvas = tk.Canvas(panel, bg=SURFACE, borderwidth=0, highlightthickness=0, yscrollcommand=scrollbar.set)
        canvas.pack(fill="both", expand=True)
        scrollbar.configure(command=canvas.yview)
        content = ttk.Frame(canvas, padding=16)
        item = canvas.create_window(0, 0, window=content, anchor="nw", width=620)
        content.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        def resize(event):
            canvas.itemconfigure(item, width=event.width)
            for label in content.winfo_children():
                label.configure(wraplength=max(round(200 * scale), event.width - round(32 * scale)))
        canvas.bind("<Configure>", resize)
        help_canvases[str(panel)] = canvas
        return content

    guide, details = page("Как пользоваться"), page("Текущее состояние")
    window.bind("<MouseWheel>", lambda event: help_canvases[tabs.select()].yview_scroll(int(-event.delta / 120), "units"))
    for title, text in (
        ("Подключение", "Выберите USB, Wi-Fi или Демо. Для Wi-Fi сначала подключите ноутбук к сети FluidNC или общему роутеру и нажмите «Найти плату». Выберите найденную плату и отдельно нажмите «Подключить»."),
        ("Тестирование слева", "Стрелка выполняет один ход с заданными шагом и скоростью. Без флажка подъёма поднимите или выньте ручку вручную. Тест ручки выполняет подъём и возврат без движения осей."),
        ("Область на бумаге справа", "Подведите ручку к верхнему левому углу бумаги и сохраните его. Затем подведите к нижнему правому и сохраните второй угол. Эскиз A4 в окне не заменяет реальные углы."),
        ("Рисунок и запуск", "Создайте рисунок в редакторе, откройте SVG или выберите квадрат 10 мм. «Подвести к началу» только перемещает головку. «Начать рисование» запускает работу после отдельного подтверждения."),
        ("Остановка", "СТОП или Esc отправляет программный сброс и сбрасывает углы. Рисунок сохраняется. При потере связи отключайте силовое питание вручную."),
    ):
        ttk.Label(guide, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 5))
        ttk.Label(guide, text=text, wraplength=600, justify="left").pack(anchor="w", pady=(0, 14))
    for variable in (a.connection_help, a.wifi_status, a.wiring_text, a.jog_lift_hint,
                     a.pen_summary, a.pen_status, a.pen_legend, a.hint):
        ttk.Label(details, textvariable=variable, wraplength=600, justify="left").pack(anchor="w", pady=(0, 12))
    scale_widget_tree(window)
