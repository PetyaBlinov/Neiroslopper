"""Explicitly enabled PC companion connection for the optional DWIN panel."""
import queue
import time
import tkinter as tk
from tkinter import ttk, messagebox

from dwin_bridge import (SerialBridge, CommandRouter, snapshot, link_state, write_words,
                         STATUS_VP, STEP_VP, SPEED_VP, RESULT_VP, LINK_VP, STEPS)
from network_transport import parse_tcp_endpoint
from ui_theme import INK


def display_endpoint(main):
    """The display tunnel belongs to the same MKS as its motion channel."""
    endpoint = parse_tcp_endpoint(main)
    if endpoint.port != 23:
        raise ValueError("Канал станка MKS должен использовать TCP 23")
    host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
    return f"tcp://{host}:8888"


def display_ports(port_infos, *, main_selected="", main_active=""):
    """Exclude explicit motor ports, not every board with an Arduino USB ID.

    A second Uno held in reset is a valid USB–UART adapter. Identical USB
    vendor/product IDs cannot distinguish that adapter from the motor Uno.
    """
    excluded = {str(value).casefold() for value in (main_selected, main_active) if value}
    seen = set()
    ports = []
    for info in port_infos:
        device = str(info.device)
        key = device.casefold()
        if key not in excluded and key not in seen:
            ports.append(device)
            seen.add(key)
    return ports


class DwinWindow:
    native = False

    def __init__(self, app, *, show=True):
        self.app = app
        self.closed = False
        self.bridge = SerialBridge()
        self.router = CommandRouter(app)
        self.last_status = 0
        self.last_result = 0
        self.armed_since = float("inf")
        self.last_selectors = None
        self.window = tk.Toplevel(app.root)
        if not show:
            self.window.withdraw()
        self.window.title("DWIN · экран плоттера")
        self.window.geometry("700x580")
        self.window.minsize(640, 540)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.port = tk.StringVar()
        self.baud = tk.StringVar(value="115200")
        self.armed = tk.BooleanVar(value=False)
        self.poll_mode = tk.BooleanVar(value=True)
        self.info = tk.StringVar(value="Экран не подключён. Управление выключено.")
        body = ttk.Frame(self.window, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="DWIN 800 × 480", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(body, text="MKS по Wi-Fi: tcp://адрес-платы:8888. DWIN подключается напрямую к MKS, без Arduino. Отдельный COM оставлен для прежней схемы.",
                  wraplength=610).pack(anchor="w", pady=(4, 12))
        row = ttk.Frame(body)
        row.pack(fill="x")
        self.ports = ttk.Combobox(row, textvariable=self.port, state="normal", width=28)
        self.ports.pack(side="left")
        ttk.Button(row, text="Обновить", command=self.refresh).pack(side="left", padx=6)
        ttk.Combobox(row, textvariable=self.baud, state="readonly", width=9,
                     values=("9600", "19200", "38400", "57600", "115200")).pack(side="left")
        self.button = ttk.Button(row, text="Подключить экран", command=self.toggle)
        self.button.pack(side="left", padx=6)
        ttk.Checkbutton(body, text="Разрешить команды с экрана", variable=self.armed,
                        command=self.arm).pack(anchor="w", pady=(14, 6))
        self.poll_button = ttk.Checkbutton(body, text="Опрашивать кнопки (рекомендуется без изменения CFG)",
                                           variable=self.poll_mode)
        self.poll_button.pack(anchor="w")
        ttk.Label(body, text="Опрос работает без изменения системной конфигурации дисплея. Снимайте галочку только при проверенной автоотправке DGUS. Переключение — после отключения.",
                  wraplength=610).pack(anchor="w", pady=4)
        ttk.Label(body, textvariable=self.info, wraplength=610, foreground=INK).pack(anchor="w", pady=10)
        ttk.Separator(body).pack(fill="x", pady=8)
        ttk.Label(body, text=("• Первый раз проверьте экран в DEMO. Соединение само не разрешает движение.\n"
                             "• Один клик стрелки — один шаг; удержание не запускает непрерывный ход.\n"
                             "• Начало рисования подтверждается в окне компьютера.\n"
                             "• СТОП работает и без галочки, пока живы экран, компьютер и связь.\n"
                             "• MKS: основной канал — TCP 23, экран — TCP 8888 той же платы.\n"
                             "• Закрытие сетевого канала экрана останавливает управление станком.\n"
                             "• DWIN: TX2 → GPIO33, RX2 ← GPIO23; общее GND и отдельные 5 В.\n"
                             "• Питание и допустимые уровни UART проверяются по паспорту панели."),
                  wraplength=610, justify="left").pack(anchor="w")
        self.refresh()

    def refresh(self):
        from serial.tools import list_ports
        ports = display_ports(list_ports.comports(), main_selected=self.app.port.get(),
                              main_active=self.app.port_name)
        main = self.app.port_name or self.app.port.get()
        if str(main).lower().startswith("tcp://"):
            try:
                ports.insert(0, display_endpoint(main))
            except ValueError:
                pass
        self.ports["values"] = ports
        running = self.bridge.thread is not None and self.bridge.thread.is_alive()
        # Refresh must not change the displayed identity of an open port.
        # A conflicting new main selection is rejected by App.connect.
        if (not running and not self.port.get().lower().startswith("tcp://") and
                self.port.get().casefold() not in {p.casefold() for p in ports}):
            self.port.set(ports[0] if ports else "")

    def arm(self):
        if self.armed.get() and not self.bridge.connected.is_set():
            self.armed.set(False)
            self.info.set("Сначала подключите экран. Движения не выполнялись.")
        else:
            self.info.set("Управление экраном разрешено; действуют проверки обычного пульта." if self.armed.get()
                          else "Команды выключены; СТОП остаётся доступен.")
        self.armed_since = time.monotonic() if self.armed.get() else float("inf")
        self.last_status = 0

    def toggle(self):
        if self.bridge.thread is not None and self.bridge.thread.is_alive():
            self.armed.set(False)
            self.bridge.close()
            self.info.set("Отключение экрана…")
            return
        self.armed.set(False)
        try:
            if self.port.get().lower().startswith("tcp://"):
                main = self.app.port_name or self.app.port.get()
                if parse_tcp_endpoint(self.port.get()) != parse_tcp_endpoint(display_endpoint(main)):
                    raise ValueError("Экран и станок должны быть подключены к одной MKS: порты 8888 и 23")
                self.baud.set("115200")
            self.bridge.start(self.port.get(), forbidden_ports=(self.app.port.get(), self.app.port_name),
                              baudrate=int(self.baud.get()), touch_mode="poll" if self.poll_mode.get() else "upload")
        except (ValueError, OSError) as exc:
            messagebox.showerror("DWIN", str(exc), parent=self.window)
            return
        self.info.set("Подключаю экран. Управление останется выключено.")
        self.button.configure(text="Отключить экран")
        self.ports.configure(state="disabled")
        self.poll_button.configure(state="disabled")

    def pump(self):
        if self.closed:
            return
        while True:
            try:
                kind, text = self.bridge.notices.get_nowait()
            except queue.Empty:
                break
            if kind == "connected":
                self.info.set(f"{text}: порт открыт. Проверка ответа — кнопкой экрана; разрешите команды отдельно.")
                self.last_status = 0
                self.last_result = 0
                self.last_selectors = None
            elif kind in ("error", "disconnected"):
                self.armed.set(False)
                self.armed_since = float("inf")
                self.last_selectors = None
                self.last_result = 2
                self.button.configure(text="Подключить экран")
                self.ports.configure(state="normal")
                self.poll_button.configure(state="normal")
                if kind == "error":
                    self.info.set("Связь с экраном потеряна: " + text + ". Автопереподключение отключено.")
                    self.app.note("DWIN: " + text)
                elif self.bridge.error is None:
                    self.info.set("Экран отключён. Повторное подключение и разрешение — вручную.")
        for event in self.bridge.pending():
            try:
                self.last_result, text = self.router.dispatch(event, armed=self.armed.get() and event.received >= self.armed_since,
                                                               linked=self.bridge.connected.is_set())
            except Exception as exc:
                self.last_result, text = 6, "DWIN: команда отклонена: " + str(exc)
            if text:
                self.info.set(text)
                self.app.note("DWIN: " + text)
            if event.vp in (STEP_VP, SPEED_VP) and self.last_result != 0:
                # A native DGUS selector writes its local VP before the PC
                # can reject it (unarmed/busy/invalid). Restore the actual PC
                # values so a large shown step is never a small real step.
                self.last_selectors = None
            self.last_status = 0
        now = time.monotonic()
        if self.bridge.connected.is_set() and now - self.last_status >= 0.4:
            self.last_status = now
            frames = [write_words(STATUS_VP, snapshot(self.app, self.armed.get())),
                      write_words(RESULT_VP, (self.last_result,
                                             link_state(self.app),
                                             int(self.armed.get())))]
            try:
                step = float(self.app.step.get().replace(",", "."))
                speed = round(float(self.app.feed.get().replace(",", ".")))
                selectors = (STEPS.index(step) if step in STEPS else None,
                             speed if 1 <= speed <= 100 else None)
                # Do not overwrite freshly touched panel selectors every
                # telemetry cycle before their poll response can reach us.
                previous = self.last_selectors
                if selectors[0] is not None and (previous is None or selectors[0] != previous[0]):
                    frames.append(write_words(STEP_VP, (selectors[0],)))
                if selectors[1] is not None and (previous is None or selectors[1] != previous[1]):
                    frames.append(write_words(SPEED_VP, (selectors[1],)))
                self.last_selectors = selectors
            except ValueError:
                pass
            self.bridge.publish(frames)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.armed.set(False)
        self.bridge.close()
        self.window.destroy()
        self.app.dwin_window = None


class NativeDwinWindow:
    """PC companion for a panel controlled by the board, not a second UART writer.

    Construction only creates widgets. The explicit start/reconnect action
    opens the single versioned event channel; it never claims motion.
    """
    native = True

    def __init__(self, app, *, show=True):
        self.app, self.closed = app, False
        self.bridge = self.panel_assistant = None
        self.window = tk.Toplevel(app.root)
        if not show:
            self.window.withdraw()
        self.window.title('DWIN · панель MKS')
        self.window.protocol('WM_DELETE_WINDOW', self.window.withdraw)
        from branding import apply_app_icon
        from display_scaling import ui_scale, scale_widget_tree
        apply_app_icon(self.window)
        scale = ui_scale(self.window)
        self.window.geometry(f'{round(540 * scale)}x{round(250 * scale)}')
        self.window._neiroslopper_geometry_scaled = True
        body = ttk.Frame(self.window, padding=16)
        body.pack(fill='both', expand=True)
        self.info = tk.StringVar(value='Канал DWIN не подключён')
        ttk.Label(body, textvariable=self.info, wraplength=490).pack(anchor='w', pady=(0, 14))
        self.helper_enabled = tk.BooleanVar(value=False)
        self.mic_enabled = tk.BooleanVar(value=False)
        self.armed = tk.BooleanVar(value=False)  # Legacy window interface; never used for native control.
        ttk.Checkbutton(body, text='Разрешить вопросы помощнику с экрана', variable=self.helper_enabled,
                        command=self.permissions_changed).pack(anchor='w')
        ttk.Checkbutton(body, text='Разрешить запись микрофона ПК с экрана', variable=self.mic_enabled,
                        command=self.permissions_changed).pack(anchor='w', pady=(5, 12))
        row = ttk.Frame(body)
        row.pack(fill='x')
        ttk.Button(row, text='Подключить канал DWIN', command=self.reconnect).pack(side='left')
        ttk.Button(row, text='Скрыть', command=self.window.withdraw, style='Quiet.TButton').pack(side='right')
        scale_widget_tree(self.window)

    def start(self):
        from dwin_native import NativeDwinClient
        session = self.app.controller.session
        peer = getattr(getattr(session, 'wire', None), 'peer_address', None)
        if not peer or not self.app.network_controller() or not getattr(session, 'native_panel', False):
            self.info.set('Native DWIN: сначала подключите MKS по Wi-Fi.')
            return False
        host = f'[{peer}]' if ':' in peer else peer
        self.bridge = NativeDwinClient(f'tcp://{host}:8888', expected_peer=peer,
                                       interactive=getattr(session, 'panel_assistant', False))
        from dwin_assistant import PanelAssistant
        self.panel_assistant = PanelAssistant(self.bridge.panel_answer, review_voice=self.review_voice)
        self.info.set('Проверяю версию канала DWIN…')
        self.bridge.start()
        return True

    def reconnect(self):
        if self.app.controller.busy or self.app.controller.cancelled.is_set():
            self.info.set('Дождитесь остановки и подключитесь к MKS заново.')
            return
        self.close_assistant()
        if self.bridge is not None:
            self.bridge.close()
        self.start()

    def review_voice(self, text):
        if self.closed or not self.mic_enabled.get():
            return False
        self.app.open_assistant()
        window = self.app.assistant_window
        insert = getattr(window, 'insert_voice_text', None)
        return bool(callable(insert) and insert(text))

    def permissions_changed(self):
        assistant = self.panel_assistant
        if assistant is not None and ((assistant.voice_pending and not self.mic_enabled.get()) or
                                      (not assistant.voice_pending and not self.helper_enabled.get())):
            assistant.cancel()

    def close_assistant(self):
        if self.panel_assistant is not None:
            self.panel_assistant.close()
            self.panel_assistant = None

    def service_assistant(self, bridge):
        from dwin_assistant import ERROR, sheet_segments
        from dwin_native import ASK, MIC_START, MIC_STOP, CANCEL, VIEW_SHEET
        assistant = self.panel_assistant
        if bridge.panel_interrupted():
            assistant.cancel()
        for request_id, action, text in bridge.panel_requests():
            if action == CANCEL:
                assistant.cancel(request_id)
            elif action == MIC_STOP:
                if not assistant.stop_voice(request_id):
                    bridge.panel_answer(request_id, ERROR, 'Запись не началась. Повторите запрос.')
            elif action == VIEW_SHEET:
                try:
                    position = self.app.position
                    start = self.app.axes().paper(position) if position is not None and self.app.zero else None
                    box, lines = sheet_segments(self.app.editor_snapshot(), start_position=start)
                    bridge.panel_sheet(request_id, box, lines)
                except (ValueError, TypeError):
                    bridge.panel_sheet(request_id)
            elif action == ASK:
                if not self.helper_enabled.get():
                    bridge.panel_answer(request_id, ERROR, 'Разрешите вопросы с экрана в окне DWIN на ПК.')
                elif not assistant.start(request_id, text, self.app.editor_snapshot()):
                    bridge.panel_answer(request_id, ERROR, 'Помощник занят. Повторите запрос.')
            elif action == MIC_START:
                if not self.mic_enabled.get():
                    bridge.panel_answer(request_id, ERROR, 'Разрешите микрофон ПК в окне DWIN.')
                elif not assistant.start_voice(request_id, self.app.editor_snapshot()):
                    bridge.panel_answer(request_id, ERROR, 'Микрофон занят. Повторите запрос.')
        if not self.mic_enabled.get() and assistant.voice_pending:
            assistant.cancel()
        assistant.pump()

    def pump(self):
        if self.closed or self.bridge is None:
            return
        from eventlog import record_event
        bridge = self.bridge
        while True:
            try:
                kind, text = bridge.notices.get_nowait()
            except queue.Empty:
                break
            self.info.set(text)
            self.app.note('DWIN · ' + text)
            record_event(self.app, 'dwin.native.link', kind=kind, **bridge.diagnostic_state())
        for event in bridge.pending():
            record_event(self.app, 'dwin.native.event', vp=event.vp, key=event.key,
                         result_code=event.result_code, board_event_seq=event.seq,
                         firmware_uptime_ms=event.uptime_ms)
        board = bridge.snapshot()
        self.app.apply_panel_state(board)
        if board:
            bridge.answer_probe()
            text = ('MKS управляет панелью · ПК получает состояние и запросы' if board.confirmed else
                    'MKS не подтвердила профиль локального управления')
            if self.info.get() != text:
                self.info.set(text)
            bridge.publish_status(bool(self.app.paths), min(100, max(0, round(float(self.app.progress['value'])))))
            self.service_assistant(bridge)
        elif self.panel_assistant is not None:
            self.panel_assistant.cancel()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.close_assistant()
        if self.bridge is not None:
            self.bridge.close()
        self.window.destroy()
        self.app.dwin_window = None
