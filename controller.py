"""Uno/GRBL or official FluidNC transport; one command-owning worker.

STOP is serialized against writes, resets the controller and invalidates calibration.
Connecting to FluidNC reads its identity/configuration without modifying it.
No homing, independent Z, spindle commands, or external G-code are sent.
"""
import math
import queue
import re
import threading
import time
from collections import deque
from geometry import Sheet, route, validate_paths
from pen_lift import PenLiftSettings, PEN_MARKER, COOLDOWN_MS, batches, firmware_profile
from serial_trace import SerialTrace, TransportError
from machine_profiles import (UNO, DLC32, profile_id, settings_ready, fluidnc_identity,
                              fluidnc_configuration, fluidnc_pen_profile, pen_matches, _pin)

# 100% speed in the UI is this machine profile. GRBL remains the final
# per-axis limiter; the UI no longer has an unrelated 600 mm/min ceiling.
MACHINE_MAX_FEED = 3000.0
MACHINE_ACCELERATION = 150.0
MAX_JOG_DISTANCE = 100.0


def board_runtime_info(lines):
    """Optional JD diagnostics; never proof of configuration or motion readiness."""
    markers = [line for line in lines if isinstance(line, str) and line.startswith("[JD-DGUS:")]
    if len(markers) != 1 or not markers[0].endswith("]"):
        return {}
    result = {}
    fields = markers[0][len("[JD-DGUS:"):-1].split(",")
    for token, key, maximum, digits in (
            ("UPTIME_MS", "firmware_uptime_ms", 0xFFFFFFFF, 10),
            ("RESET_REASON", "firmware_reset_reason", 255, 3)):
        values = [field[len(token) + 1:] for field in fields if field.startswith(token + "=")]
        if (len(values) != 1 or not re.fullmatch(r"[0-9]{1," + str(digits) + r"}", values[0]) or
                int(values[0]) > maximum):
            return {}
        result[key] = int(values[0])
    return result


# Bounded lifts reserve time for settling and USB/status traffic. Stock
# FluidNC has no independent solenoid timer, so enforce the host ON budget
# while waiting for replies as well as before starting each transfer.
POWERED_TRAVEL_SECONDS = .55
# Legacy Uno firmware has its own short ON/OFF guard. FluidNC uses the
# separately requested ten-second host limit for complete lifted transfers.
POWERED_HOST_ON_LIMIT = 1.65
DLC32_MAX_ON_SECONDS = 10.0
POWERED_JOG_REPLY_MARGIN = .35
REQUIRED = {
    10: 0, 13: 0, 20: 0, 21: 0, 22: 0,
    110: MACHINE_MAX_FEED, 111: MACHINE_MAX_FEED,
    120: MACHINE_ACCELERATION, 121: MACHINE_ACCELERATION,
}


class Cancelled(Exception):
    pass


class GrblSession:
    def __init__(self, wire, cancelled, callback, machine_profile=UNO):
        self.wire = wire
        self.cancelled = cancelled
        self.callback = callback
        self.machine_profile = profile_id(machine_profile)
        self.simulated_machine_profile = None
        self.work_offset = None
        self.network_interlock = None
        self.write_lock = threading.Lock()
        self.buffer = b""
        self.state = "Unknown"
        self.position = None
        self.settings = {}
        self.trusted = False
        self.last_status = 0.0
        self.status_number = 0
        self.last_jog_status_number = 0
        self.active_pins = None  # Fresh Pn state; None is not evidence of released limits.
        self.pen_firmware = None
        self.pen_down = False
        self.power_raises = False
        self.coil_on = False
        self.coil_on_at = None
        self.coil_deadline = None
        self.pen_off_at = None
        self.last_command = None
        self.powered_travel_notice = False
        self.has_actuated = False
        self.trace = SerialTrace({"type": type(wire).__name__,
                                  "port": getattr(wire, "port", None),
                                  "baudrate": getattr(wire, "baudrate", None),
                                  "thread": threading.current_thread().name})
        self.deferred_lines = deque()
        self.status_pending = False

    def _wire_call(self, phase, method, *args):
        try:
            return method(*args)
        except Exception as exc:
            path = self.trace.failure(phase, self.last_command, exc)
            raise TransportError(phase, self.last_command, exc, path) from exc

    def _available(self):
        return self._wire_call("проверка входного буфера", lambda: self.wire.in_waiting)

    def _write_bytes(self, data):
        self.trace.record("TX", data)
        count = self._wire_call("передача данных", self.wire.write, data)
        if count != len(data):
            exc = OSError(f"Неполная запись: {count!r} из {len(data)} байт; команда не повторялась")
            path = self.trace.failure("неполная передача данных", self.last_command, exc)
            raise TransportError("неполная передача данных", self.last_command, exc, path)

    def write(self, data):
        with self.write_lock:
            if self.cancelled.is_set():
                raise Cancelled()
            self._check_power_budget()
            self._write_bytes(data)
            self._check_power_budget()

    def fluidnc_power_mode(self):
        return (self.machine_profile == DLC32 or
                (isinstance(self.wire, SimWire) and self.simulated_machine_profile == DLC32))

    def host_power_limit(self):
        return DLC32_MAX_ON_SECONDS if self.fluidnc_power_mode() else POWERED_HOST_ON_LIMIT

    def _check_power_budget(self):
        # abort()/close() deliberately bypass this check: an expired budget
        # must never prevent their stop/reset write. This is a host deadline,
        # not a guaranteed cutoff if the OS or physical transport is stuck.
        if self.coil_deadline is not None and time.monotonic() >= self.coil_deadline:
            raise TimeoutError("Истекло время питания соленоида при ожидании контроллера. "
                               "Операция прервана; проверьте соединение и задайте оба угла заново.")

    def abort(self, force=False):
        with self.write_lock:
            self.cancelled.set()
            if self.trusted and (force or self.has_actuated or self.machine_profile == UNO):
                try:
                    self._write_bytes(b"\x18")
                except Exception:
                    pass
            # Sending reset is not confirmation that the device received it.
            self.coil_on = None
            self.coil_on_at = None
            self.coil_deadline = None
            self.pen_down = None
            self.callback("pen", "unknown")

    def close(self):
        with self.write_lock:
            controlled = self.trusted and (self.has_actuated or self.machine_profile == UNO)
            if controlled:
                try:
                    self._write_bytes(b"\x18")
                except Exception:
                    pass
            self.coil_on = None
            self.coil_on_at = None
            self.coil_deadline = None
            self.pen_down = None
            if controlled:
                self.callback("pen", "unknown")
            self._wire_call("закрытие соединения", self.wire.close)

    def read_line(self):
        if self.cancelled.is_set():
            raise Cancelled()
        self._check_power_budget()
        if self.deferred_lines:
            return self.deferred_lines.popleft()
        return self._read_wire_line()

    def _read_wire_line(self):
        if self.cancelled.is_set():
            raise Cancelled()
        self._check_power_budget()
        if b"\n" not in self.buffer:
            data = self._wire_call("чтение ответа", self.wire.read, max(1, self._available()))
            if data:
                self.trace.record("RX", data)
            self.buffer += data
            self._check_power_budget()
        if len(self.buffer) > 20000:
            raise RuntimeError("Некорректный ответ контроллера.")
        if b"\n" not in self.buffer:
            return None
        raw, self.buffer = self.buffer.split(b"\n", 1)
        # Leading indentation is semantic in FluidNC $Config/Dump YAML.
        # Preserve it; only CR/trailing whitespace is transport decoration.
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line.startswith("<") and line.endswith(">"):
            parts = line[1:-1].split("|")
            self.state = parts[0]
            self.position = None
            # FluidNC always reports Pn when any input is active; it omits
            # the field only when all inputs are released (not a mask bit).
            self.active_pins = ""
            machine_position = None
            for part in parts[1:]:
                if part.startswith("Pn:"):
                    pins = part[3:]
                    if not re.fullmatch(r"[A-Za-z]*", pins):
                        raise RuntimeError("Некорректное состояние входов Pn контроллера.")
                    self.active_pins += pins
                if part.startswith(("WPos:", "MPos:", "WCO:")):
                    values = part.split(":", 1)[1].split(",")
                    try:
                        point = float(values[0]), float(values[1])
                    except (ValueError, IndexError) as exc:
                        raise RuntimeError("Некорректная позиция контроллера.") from exc
                    if not all(math.isfinite(v) for v in point):
                        raise RuntimeError("Некорректная позиция контроллера.")
                    if part.startswith("WPos:"):
                        self.position = point
                    elif part.startswith("MPos:"):
                        machine_position = point
                    else:
                        self.work_offset = point
            if self.position is None and machine_position is not None and self.work_offset is not None:
                self.position = tuple(a - b for a, b in zip(machine_position, self.work_offset))
            self.last_status = time.monotonic()
            self.status_number += 1
            if self.state == "Jog":
                self.last_jog_status_number = self.status_number
            self.status_pending = False
            self.callback("position", (self.state, self.position))
            self.callback("endstops", self.endstop_report())
        elif line.startswith("[MSG:PEN_GUARD:"):
            raise RuntimeError("Сработало ограничение включения соленоида. Реле выключено прошивкой; "
                               "движение прервано. Проверьте нагрев, механику и USB; задайте оба угла заново.")
        elif line.startswith("ALARM:") and not self.trusted:
            self.state = "Alarm"
        elif line.startswith(("error:", "ALARM:")):
            context = f" после команды {self.last_command!r}" if self.last_command else ""
            if line == "ALARM:3":
                raise RuntimeError(f"GRBL: {line}{context}. Контроллер сообщил о сбросе во время движения "
                                   "(Abort Cycle); источник сброса не установлен. Проверьте питание и USB, "
                                   "восстановите соединение и задайте оба угла заново.")
            raise RuntimeError(f"GRBL: {line}{context}. Проверьте механику и настройки; ноль сброшен.")
        elif self.trusted and (line.startswith(("Grbl", "[FluidNC v", "[MSG:INFO: FluidNC v", "rst:0x", "ets "))):
            raise RuntimeError("Контроллер перезапустился. Нужно заново выставить обе точки.")
        return line

    def _query_status(self):
        if not self.status_pending:
            self.write(b"?")
            self.status_pending = True

    def _drain_status_backlog(self, deadline):
        """Resolve older queries, preserving every non-status response/ACK."""
        while True:
            if self.cancelled.is_set():
                raise Cancelled()
            buffered = b"\n" in self.buffer or self._available() > 0
            if not buffered and not self.status_pending:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Не получен предыдущий ответ о положении. Проверьте соединение.")
            line = self._read_wire_line()
            if line is not None and not line.startswith("<"):
                self.deferred_lines.append(line)

    def command(self, command, timeout=8):
        if re.search(r"\$J=|^\$(?:H|X)|(?:^|\s)(?:M3|M4|M7|M8|G0?[0-3])(?=\s|[XYZ]|$)", command, re.I):
            self.check_network_interlock()
        if re.search(r"^\$(?:J=|H|X)|(?:^|\s)(?:M[34789]|G(?:0?[0-3]|10|92))(?=\s|[XYZ]|$)", command, re.I):
            self.has_actuated = True
        self.last_command = command
        self.write((command + "\n").encode("ascii"))
        deadline = time.monotonic() + timeout
        next_query = time.monotonic() + 0.25
        query_started = None
        responses = []
        progress_position = self.position
        while time.monotonic() < deadline:
            line = self.read_line()
            if line == "ok":
                return responses
            if line and not line.startswith("<"):
                responses.append(line)
            now = time.monotonic()
            if (line and line.startswith("<") and self.state in ("Run", "Jog")
                    and self.position is not None and self.position != progress_position):
                # An ACK may wait behind a long preceding planner block. A
                # moving controller is not an unresponsive one. A frozen Run
                # report does not renew this deadline, so failures still stop.
                if progress_position is not None:
                    deadline = max(deadline, now + timeout)
                progress_position = self.position
            if now >= next_query:
                self._query_status()
                if query_started is None:
                    query_started = now
                next_query = now + 0.25
            if query_started is not None and now - max(query_started, self.last_status) > 3:
                raise TimeoutError("Контроллер перестал отвечать. Снимите питание моторов, если движение продолжается.")
        raise TimeoutError(f"Нет подтверждения команды {command!r}. Позиция больше не считается достоверной.")

    def status(self):
        deadline = time.monotonic() + 3
        self._drain_status_backlog(deadline)
        number = self.status_number
        self._query_status()
        while time.monotonic() < deadline:
            line = self._read_wire_line()
            if line is not None and not line.startswith("<"):
                self.deferred_lines.append(line)
            if self.status_number > number:
                return self.state, self.position
        raise TimeoutError("Нет ответа о положении. Проверьте соединение и питание.")

    def endstop_report(self):
        """A fresh Pn report, with configured axes separate from pin state."""
        config = self.settings.get("_fluidnc_config", {})
        axes = config.get("axes", {}) if isinstance(config, dict) else {}
        configured = {}
        for name in ("x", "y"):
            axis = axes.get(name, axes.get(name.upper(), {})) if isinstance(axes, dict) else {}
            configured[name] = any(
                isinstance(motor, dict) and any(
                    str(motor.get(pin_key, "NO_PIN")).split(":", 1)[0].strip().lower()
                    not in ("", "no_pin", "none")
                    for pin_key in ("limit_neg_pin", "limit_pos_pin", "limit_all_pin"))
                for key, motor in axis.items()
                if re.fullmatch(r"motor\d+", str(key))) if isinstance(axis, dict) else False
        return {"state": self.state, "pins": self.active_pins,
                "observed_at": self.last_status, "configured": configured}

    def initialize(self):
        info = self.command("$I")
        if self.machine_profile == DLC32:
            if not fluidnc_identity(info):
                raise RuntimeError("Выбран MKS DLC32 / FluidNC, но плата не подтвердила FluidNC. Проверьте профиль и COM-порт.")
            runtime = board_runtime_info(info)
            if runtime:
                self.callback("board_runtime", runtime)
            # A valid FluidNC banner alone cannot prove the relay polarity,
            # dual-X wiring or axis setup. Read the actual controller YAML.
            self.refresh_settings()
            self.trusted = True
            self.status()
            return
        if not any(line.startswith("[VER:1.1") for line in info):
            raise RuntimeError("Нужна прошивка GRBL 1.1 на Arduino Uno; контроллер не подтверждён.")
        self.trusted = True
        markers = [re.fullmatch(PEN_MARKER, line) for line in info]
        markers = [m for m in markers if m]
        if len(markers) > 1:
            raise RuntimeError("Неоднозначный профиль реле в прошивке.")
        if markers:
            m = markers[0]
            self.pen_firmware = {"active_low": m[1] == "LOW", "on_ticks": int(m[2]),
                                 "off_ticks": int(m[3]),"tick_ms_nominal":int(m[4])}
        self.callback("pen_firmware", self.pen_firmware)
        self.refresh_settings()
        startup = self.command("$N")
        if any(re.match(r"\$N[01]=.+", line) for line in startup):
            raise RuntimeError("В GRBL записаны стартовые команды $N. Сначала проверьте и удалите их отдельно: приложение их не меняет.")
        self.status()

    def check_pen(self, profile, *, require_confirmed=True):
        profile.validate(require_ready=require_confirmed)
        self.power_raises = profile.power_raises
        if not profile.enabled:
            return
        if profile.active_low is None:
            raise ValueError("Сначала определите уровень включения реле: HIGH или LOW.")
        if self.pen_firmware is None:
            if self.machine_profile == DLC32:
                raise ValueError("В конфигурации FluidNC не настроен coolant/flood_pin для реле ручки. Подключение доступно для просмотра.")
            raise ValueError("Нужна GRBL-прошивка с управлением ручкой A3.")
        if not pen_matches(self.pen_firmware, profile, self.machine_profile):
            raise ValueError("Полярность реле в пульте не совпадает с фактической конфигурацией платы. Проверьте настройки ручки и выход реле.")

    def set_pen(self, down, profile, *, calibration=False):
        self.check_pen(profile, require_confirmed=not calibration)
        if not profile.enabled:
            raise ValueError("Автоматический подъём ручки отключён.")
        coil_on = profile.coil_for(down)
        if coil_on:
            if (not self.fluidnc_power_mode() and not profile.continuous and
                    not self.coil_on and self.pen_off_at is not None
                    and not isinstance(self.wire, SimWire)):
                remaining = COOLDOWN_MS / 1000 - (time.monotonic() - self.pen_off_at)
                if remaining > 0:
                    self.callback("pen", "cooling")
                    self.command(f"G4 P{remaining:.3f}")
            # Unknown until ack; any failure must reset. Logical DOWN is not
            # equivalent to relay ON when electricity raises this mechanism.
            if not self.coil_on:
                self.coil_on_at = time.monotonic()
            # Repeated M8/status traffic must not renew one energized period.
            self.coil_deadline = (None if profile.continuous else
                                  self.coil_on_at + self.host_power_limit())
            self.coil_on = True
            self.command("M8")
        else:
            was_on = self.coil_on
            self.command("M9")
            if was_on or self.pen_off_at is None:
                self.pen_off_at = time.monotonic()
            self.coil_on = False
            self.coil_on_at = None
            self.coil_deadline = None
        self.pen_down = down
        delay = profile.down_ms if down else profile.up_ms
        self.command(f"G4 P{delay / 1000:.3f}")
        self.callback("pen", "down" if down else "up")

    def power_off_pen(self, profile):
        """Prepare a motors-only jog without pulsing or cooling the relay.

        A logical M9 is safe only with the same confirmed physical polarity
        required by other relay actions. Disabling automatic pen control does
        not authorize touching an unverified output.
        """
        self.check_pen(profile)
        if not profile.enabled:
            if self.coil_on is not False:
                raise ValueError("Сначала подтвердите выключение соленоида перед ручным перемещением.")
            return
        was_on = self.coil_on
        self.command("M9")
        if was_on or self.pen_off_at is None:
            self.pen_off_at = time.monotonic()
        self.coil_on = False
        self.coil_on_at = None
        self.coil_deadline = None
        self.pen_down = profile.power_raises
        self.callback("pen", "off")

    def refresh_settings(self):
        self.settings = {}
        if self.machine_profile == DLC32:
            source = "usb"
            if str(getattr(self.wire, "port", "")).lower().startswith("tcp:"):
                from network_config import (read_runtime_config, RuntimeConfigUnavailable,
                                            RuntimeConfigCancelled)
                try:
                    config_lines = read_runtime_config(self.wire, self.cancelled)
                    source = "http"
                except RuntimeConfigCancelled as exc:
                    raise Cancelled() from exc
                except RuntimeConfigUnavailable:
                    # Compatibility with Telnet-only/HTTP-auth installations:
                    # use the same strict canonical validator, never legacy $$.
                    if self.cancelled.is_set():
                        raise Cancelled()
                    self.callback("log", "HTTP-конфигурация MKS недоступна; читаю YAML по текущему TCP-соединению.")
                    config_lines = self.command("$Config/Dump", timeout=12)
                    source = "tcp"
            else:
                config_lines = self.command("$Config/Dump", timeout=12)
            self.settings = fluidnc_configuration(config_lines)
            self.settings["_fluidnc_config_source"] = source
            self.pen_firmware = fluidnc_pen_profile(self.settings)
            self.callback("pen_firmware", self.pen_firmware)
            if not settings_ready(self.settings, DLC32):
                self.callback("log", self.settings.get("_fluidnc_reason", "Нужна рабочая конфигурация FluidNC."))
        else:
            for line in self.command("$$"):
                match = re.fullmatch(r"\$(\d+)=([-+\d.]+)", line)
                if match:
                    self.settings[int(match[1])] = float(match[2])
        self.callback("settings", self.settings.copy())

    def ready(self):
        self.check_network_interlock()
        if not settings_ready(self.settings, self.machine_profile):
            raise ValueError(self.settings.get("_fluidnc_reason", "Сначала проверьте конфигурацию выбранной платы."))
        state, position = self.status()
        if state != "Idle" or position is None:
            raise ValueError(f"Контроллер не готов: {state}. Для движения нужен Idle и координаты WPos.")
        return position

    def ready_pen(self, profile):
        """Check isolated relay control without requiring XY commissioning.

        The relay still needs a trusted session, a fresh Idle report and a
        confirmed matching output. An incomplete XY configuration cannot make
        a conflicted or unassigned coolant pin usable.
        """
        self.check_network_interlock()
        if not self.trusted:
            raise ValueError("Сначала подтвердите подключение к контроллеру.")
        state, _ = self.status()
        if state != "Idle":
            raise ValueError(f"Контроллер не готов: {state}. Для управления реле нужен Idle.")
        self.check_pen(profile)
        if not profile.enabled:
            raise ValueError("Включите проверенный режим соленоида.")
        if self.machine_profile != DLC32:
            return

        config = self.settings.get("_fluidnc_config", {})
        coolant = config.get("coolant", {}) if isinstance(config, dict) else {}
        if not isinstance(coolant, dict):
            raise ValueError("Не подтверждён фактический выход coolant для реле.")
        pin, flags = _pin(coolant.get("flood_pin"))
        expected_pin, _ = _pin(self.pen_firmware.get("flood_pin"))
        if (pin != expected_pin or set(flags) - {"high", "low"} or
                ("low" in flags) != self.pen_firmware["active_low"] or
                (not settings_ready(self.settings, DLC32) and pin != "gpio.32")):
            raise ValueError("Не подтверждён отдельный выход LASER TTL/GPIO32 для реле.")
        if _pin(coolant.get("mist_pin"))[0] != "no_pin":
            raise ValueError("Для отдельной проверки реле дополнительный выход mist должен быть NO_PIN.")

        def claimed_elsewhere(node, path=()):
            if isinstance(node, dict):
                for key, value in node.items():
                    child = path + (str(key),)
                    if child == ("coolant", "flood_pin"):
                        continue
                    if str(key).endswith("_pin") and _pin(value)[0] == pin:
                        return True
                    if claimed_elsewhere(value, child):
                        return True
            elif isinstance(node, list):
                return any(claimed_elsewhere(value, path + (str(i),))
                           for i, value in enumerate(node))
            return False

        if claimed_elsewhere(config):
            raise ValueError("GPIO реле также занят другим узлом; управление запрещено.")

    def check_network_interlock(self):
        # Stock FluidNC has no JD-DGUS lease service. A fresh response checks
        # connection health, but cannot stop firmware after a broken TCP link.
        if self.machine_profile == DLC32 and str(getattr(self.wire, "port", "")).lower().startswith("tcp:"):
            if getattr(self.wire, "closed", False):
                raise RuntimeError("Wi-Fi-соединение закрыто. Автоматического продолжения нет.")
            if self.trusted and self.last_status and time.monotonic() - self.last_status > 3:
                raise RuntimeError("Нет свежего ответа FluidNC по Wi-Fi. Проверьте связь и состояние станка.")

    def refresh_g54_offset(self):
        """After G54/G92.1/G49 and G10, do not reuse an earlier WCO report.

        FluidNC may use MPos status and emit WCO only periodically. The zero
        operation has explicitly cleared G92/TLO, so its G54 XY offset is the
        complete work offset. No machine movement or configuration write.
        """
        if self.machine_profile != DLC32:
            return
        self._drain_status_backlog(time.monotonic() + 3)
        offsets = []
        for line in self.command("$#"):
            match = re.fullmatch(r"\[G54:([^\]]+)\]", line)
            if match:
                values = match[1].split(",")
                point = float(values[0]), float(values[1])
                if not all(math.isfinite(v) for v in point):
                    raise ValueError("FluidNC вернул некорректное смещение G54.")
                offsets.append(point)
        if len(offsets) != 1:
            self.work_offset = None
            raise ValueError("FluidNC не подтвердил смещение G54 после установки нуля.")
        self.work_offset = offsets[0]

    def set_idle_hold(self, enabled):
        if self.machine_profile == DLC32:
            raise ValueError("Для FluidNC удержание задаётся stepping/idle_ms в config.yaml; пульт не переписывает конфигурацию платы.")
        state, _ = self.status()
        if state not in ("Idle", "Alarm"):
            raise ValueError("Сменить удержание можно только после остановки.")
        value = 255 if enabled else 25
        if self.settings.get(1) != value:
            self.command(f"$1={value}")
            self.refresh_settings()
        if self.settings.get(1) != value:
            raise RuntimeError("Контроллер не подтвердил настройку удержания.")

    def configure(self, hold=True):
        state, _ = self.status()
        if state not in ("Idle", "Alarm"):
            raise ValueError("Настройка доступна только в Idle или Alarm.")
        if self.machine_profile == DLC32:
            self.refresh_settings()
            if not settings_ready(self.settings, DLC32):
                self.callback("log", self.settings.get("_fluidnc_reason", "Конфигурация пока не готова к движению."))
                return
            state, point = self.status()
            if state != "Idle" or point is None:
                raise ValueError("Конфигурация прочитана, но для работы нужны Idle и рабочие координаты; автоматической разблокировки нет.")
            self.callback("log", "Конфигурация FluidNC проверена. Настройки платы не изменялись.")
            return
        self.callback("log", "Исходные настройки: " + " ".join(f"${k}={v:g}" for k, v in sorted(self.settings.items())))
        for key, value in REQUIRED.items():
            self.command(f"${key}={value}")
        self.set_idle_hold(hold)
        if state == "Alarm":
            self.command("$X")
        self.refresh_settings()
        self.ready()

    def unlock_alarm(self):
        """Explicit MKS-only recovery; never moves, powers the relay, or homes.

        The caller separately confirms that the physical cause was removed.
        Reading a fresh status is essential: a cached clear limit is not proof
        that the NC switches are currently closed.
        """
        if self.machine_profile != DLC32:
            raise ValueError("Отдельное снятие аварии предусмотрено только для MKS DLC32.")
        self.check_network_interlock()
        if not settings_ready(self.settings, DLC32):
            raise ValueError("Сначала подключитесь и проверьте конфигурацию MKS.")
        state, _ = self.status()
        if self.active_pins is None or self.active_pins:
            pins = self.active_pins or "неизвестно"
            raise ValueError("Снятие аварии запрещено: активны входы Pn=" + pins +
                             ". Сначала физически освободите концевики и устраните причину.")
        if state == "Idle":
            self.callback("log", "MKS уже в Idle; команда $X не отправлялась. Углы нужно задать заново.")
            return
        if state != "Alarm":
            raise ValueError(f"Снять аварию можно только в Alarm, получено {state}; движения не выполнялись.")
        # Recheck the ownership immediately before the state-changing command.
        self.check_network_interlock()
        self.command("$X")
        state, _ = self.status()
        if state != "Idle" or self.active_pins is None or self.active_pins:
            raise RuntimeError("MKS не подтвердила Idle с освобождёнными входами после $X; продолжение запрещено.")
        self.callback("log", "Авария MKS снята явной командой. Движений не было; задайте оба угла листа заново.")

    def synchronize(self, timeout):
        # GRBL 'ok' on a move means queued, NOT completed. Dwell drains planner.
        self.command("G4 P0", timeout=timeout)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state, position = self.status()
            if state == "Idle" and position is not None:
                return position
            # A status requested during the dwell may arrive just after its ack.
            if state != "Run":
                raise RuntimeError(f"Движение не завершено: {state}.")
            if self.cancelled.wait(0.1):
                raise Cancelled()
        raise TimeoutError("Не получено подтверждение Idle после движения.")

    def wait_jog(self, expected_position, timeout=30):
        # Normal G-code is forbidden while Jog is active: do not send G4 here.
        # FluidNC acknowledges the queued jog before cycleStart changes Idle
        # to Jog. A fresh Idle report alone is therefore not completion.
        tolerances = []
        for key in (100, 101):
            steps = float(self.settings.get(key, 0))
            if not math.isfinite(steps) or steps <= 0:
                raise ValueError("Не подтверждён масштаб осей для проверки ручного движения.")
            # Account for step rounding and the 0.001 mm status precision.
            tolerances.append(min(.02, .5 / steps + .001))
        started = self.state == "Jog"
        before_wait = self.status_number
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state, position = self.status()
            started = started or self.last_jog_status_number > before_wait
            if state == "Idle" and position is not None:
                if (math.dist(position, expected_position) <= .02 and
                        all(abs(actual - expected) <= tolerance
                            for actual, expected, tolerance in zip(position, expected_position, tolerances))):
                    return position
                if started:
                    raise RuntimeError("Ручное движение остановилось до заданной точки; задайте оба угла заново.")
            elif state == "Jog":
                started = True
            else:
                raise RuntimeError(f"Ручное движение прервано: {state}.")
            if self.cancelled.wait(0.1):
                raise Cancelled()
        raise TimeoutError("Ручное движение не завершилось вовремя.")


class SimWire:
    """Serial-level simulator; it never opens a device or touches USB."""
    def __init__(self, power_raises=False, continuous=False):
        self.rx = bytearray()
        self.pending = None
        self.until = 0
        self.position = (0.0, 0.0)
        self.settings = {**REQUIRED, 1: 255, 100: 40, 101: 40}
        self.closed = False
        self.history = []
        self.motion_state = "Run"
        self.power_raises = power_raises
        self.continuous = continuous
        self.coil_on = False
        self.pen_down = power_raises
        self.ink_moves = []

    def respond(self, line):
        self.rx.extend((line + "\r\n").encode("ascii"))

    @property
    def in_waiting(self):
        return len(self.rx)

    def finish(self):
        if self.pending is not None and time.monotonic() >= self.until:
            self.position, self.pending = self.pending, None

    def write(self, data):
        if self.closed:
            raise OSError("Simulator closed")
        self.history.append(data)
        self.finish()
        if data == b"\x18":
            self.coil_on = False
            self.pen_down = self.power_raises
            self.pending = None
            self.rx.clear()
            self.respond("Grbl 1.1h ['$' for help]")
            return len(data)
        if data == b"?":
            state = self.motion_state if self.pending else "Idle"
            self.respond(f"<{state}|WPos:{self.position[0]:.3f},{self.position[1]:.3f},0|FS:0,0>")
            return len(data)
        command = data.decode("ascii").strip()
        if self.pending and self.motion_state == "Jog" and not command.startswith("$J="):
            self.respond("error:9")
            return len(data)
        if command == "$I":
            self.respond("[VER:1.1h.SIMULATOR:]")
            on_ticks, off_ticks = (0, 0) if self.continuous else (60, 61)
            self.respond(f"[PEN:A3,ACTIVE=HIGH,ON_TICKS={on_ticks},OFF_TICKS={off_ticks},TICK_MS_NOMINAL=32]")
        elif command == "$$":
            for key, value in self.settings.items():
                self.respond(f"${key}={value}")
        elif command == "$N":
            self.respond("$N0=")
            self.respond("$N1=")
        elif re.fullmatch(r"\$\d+=.+", command):
            key, value = command[1:].split("=")
            self.settings[int(key)] = float(value)
        elif command.startswith("G10"):
            self.position = (0.0, 0.0)
        elif command.startswith(("$J=", "G1 ")):
            self.ink_moves.append((command, self.pen_down))
            self.motion_state = "Jog" if command.startswith("$J=") else "Run"
            self.position = self.pending or self.position
            numbers = dict((k, float(v)) for k, v in re.findall(r"([XYF])([-+\d.]+)", command))
            point = numbers.get("X", 0), numbers.get("Y", 0)
            if command.startswith("$J="):
                point = self.position[0] + point[0], self.position[1] + point[1]
            self.pending = point
            self.until = time.monotonic() + 0.03
        elif command in ("M8", "M9"):
            if self.pending:
                self.position, self.pending = self.pending, None
            self.coil_on = command == "M8"
            self.pen_down = self.coil_on != self.power_raises
        elif command.startswith("G4 P"):
            # Delayed ack allows exercising completion logic.
            if self.pending:
                self.position, self.pending = self.pending, None
        self.respond("ok")
        return len(data)

    def read(self, size):
        if not self.rx:
            time.sleep(0.01)
        result = bytes(self.rx[:size])
        del self.rx[:size]
        return result

    def close(self):
        self.closed = True


class Controller:
    def __init__(self, start_worker=True, network_interlock=None):
        self.events = queue.Queue()
        self.jobs = queue.Queue()
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.busy = False
        self.closed = False
        self.session = None
        self.connected = False
        self.zero = False
        self.sheet = None
        self.axes = None
        self.pen = PenLiftSettings()
        self.pen_settings = self.pen  # The physical profile never inherits DEMO confirmation.
        self.simulation = False
        self.machine_profile = UNO
        self.network_interlock = network_interlock
        self.worker = threading.Thread(target=self.run, daemon=True)
        if start_worker:
            self.worker.start()

    def emit(self, name, value):
        self.events.put((name, value))

    def error_detail(self, exc):
        """Capture diagnostics before abort/disconnect destroys session context."""
        path = getattr(exc, "trace_path", None)
        if path is None and self.session is not None and not isinstance(exc, Cancelled):
            try:
                path = self.session.trace.failure("операция контроллера", self.session.last_command, exc)
            except Exception:
                pass
        if path:
            return {"message": str(exc), "diagnostic_log": str(path)}
        return str(exc)

    def submit(self, action, **params):
        with self.lock:
            if self.busy or self.closed:
                return False
            if action == "connect":
                self.cancelled.clear()
            self.busy = True
        self.jobs.put((action, params))
        return True

    def stop(self):
        # Mark first, so no later ordinary write can overtake the reset.
        self.cancelled.set()
        if self.session:
            self.session.abort(force=True)

    def shutdown(self):
        """Stop accepting work and let the owning worker close its session."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self.cancelled.set()
        if self.session and self.session.has_actuated:
            self.session.abort()
        if self.worker.is_alive():
            self.jobs.put((None, {}))
        else:
            self.disconnect()

    def disconnect(self):
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass
        self.session = None
        self.connected = self.zero = False
        self.simulation = False
        self.pen = self.pen_settings
        self.sheet = self.axes = None
        self.emit("disconnected", None)

    def run(self):
        while True:
            try:
                action, params = self.jobs.get(timeout=0.25)
            except queue.Empty:
                if self.session:
                    try:
                        if self.cancelled.is_set():
                            raise Cancelled()
                        state, _ = self.session.status()
                        if state not in ("Idle", "Alarm") or (self.zero and state == "Alarm"):
                            raise RuntimeError(f"Неожиданное состояние {state}; калибровка сброшена.")
                    except Exception as exc:
                        detail = self.error_detail(exc)
                        if self.session:
                            self.session.abort()
                        self.disconnect()
                        if not isinstance(exc, Cancelled):
                            self.emit("error", detail)
                continue
            if self.closed or action is None:
                self.disconnect()
                with self.lock:
                    self.busy = False
                return
            try:
                self.execute(action, params)
            except ValueError as exc:
                detail = self.error_detail(exc)
                if self.session and self.session.has_actuated and self.session.coil_on is not False:
                    self.session.abort()
                    self.disconnect()
                self.emit("error", detail)
            except Exception as exc:
                detail = self.error_detail(exc)
                if self.session:
                    self.session.abort()
                self.disconnect()
                if not isinstance(exc, Cancelled):
                    self.emit("error", detail)
            finally:
                with self.lock:
                    self.busy = False
                self.emit("done", action)

    def execute(self, action, params):
        if self.closed:
            raise Cancelled()
        if action == "disconnect":
            self.disconnect()
            return
        profile = self.pen_settings if action in ("configure","hold","clear","unlock","check_endstops") else PenLiftSettings.from_value(params.get("pen", self.pen_settings))
        if action == "connect":
            self.disconnect()
            if self.cancelled.is_set():
                raise Cancelled()
            port = params["port"]
            self.simulation = port == "DEMO"
            selected = profile_id(params.get("machine_profile", UNO))
            # DEMO uses the long-standing serial simulator, never a fake
            # identity/configuration pretending to validate a physical DLC32.
            self.machine_profile = UNO if self.simulation else selected
            self.pen_settings = profile
            self.pen = profile.for_demo() if self.simulation else profile
            if port == "DEMO":
                wire = SimWire(power_raises=self.pen.power_raises, continuous=self.pen.continuous)
            elif port.lower().startswith("tcp:"):
                if self.machine_profile != DLC32:
                    raise ValueError("Сетевое подключение поддерживается только профилем MKS DLC32 / FluidNC.")
                from network_transport import TcpWire
                wire = TcpWire(port, timeout=.05, write_timeout=.5)
            else:
                if self.machine_profile == DLC32:
                    from network_transport import open_fluidnc_serial
                    wire = open_fluidnc_serial(port, timeout=0.05, write_timeout=0.5)
                else:
                    import serial
                    wire = serial.Serial(port, 115200, timeout=0.05, write_timeout=0.5)
            self.session = GrblSession(wire, self.cancelled, self.emit, self.machine_profile)
            self.session.simulated_machine_profile = selected if self.simulation else None
            self.session.network_interlock = self.network_interlock
            if port != "DEMO" and not port.lower().startswith("tcp:"):
                if self.cancelled.wait(2.5):
                    raise Cancelled()
                wire.reset_input_buffer()
            self.session.initialize()
            self.session.power_raises = self.pen.power_raises
            self.session.pen_down = None if self.machine_profile == DLC32 else self.pen.power_raises
            if self.machine_profile == UNO:
                self.session.set_idle_hold(params.get("hold", True))
            self.connected = True
            self.emit("connected", port)
            return
        if not self.connected or not self.session:
            raise ValueError("Сначала подключитесь к контроллеру или DEMO.")
        s = self.session
        self.pen_settings = profile
        self.pen = profile.for_demo() if self.simulation else profile
        if isinstance(s.wire, SimWire):
            # The DEMO mechanism must follow profile edits, not retain the
            # connection-time default and mislabel all ink/travel movements.
            s.wire.power_raises = self.pen.power_raises
            # Firmware identity remains the connection-time report. Silently
            # rewriting it hid mismatches in both DEMO and regression tests.
            s.wire.pen_down = s.wire.coil_on != self.pen.power_raises
        if action == "check_endstops":
            if self.machine_profile != DLC32 or self.simulation:
                raise ValueError("Проверка входов этой кнопкой доступна для подключённой MKS DLC32.")
            s.status()
            self.emit("log", "Концевики прочитаны свежим запросом; движения и переключения реле не выполнялись.")
            return
        if action == "unlock":
            if self.machine_profile != DLC32 or self.simulation:
                raise ValueError("Снятие аварии доступно только для подключённой MKS DLC32.")
            if params.get("confirmed") is not True:
                raise ValueError("Подтвердите физическое устранение причины аварии перед снятием блокировки.")
            self.zero = False
            self.sheet = self.axes = None
            self.emit("calibration", (False, None))
            s.unlock_alarm()
            return
        if action == "configure":
            self.zero = False
            self.sheet = None
            self.emit("calibration", (False, None))
            s.configure(params.get("hold", True))
            if self.machine_profile == UNO:
                self.emit("log", "Режим без концевиков настроен. Шаги/мм не изменены; профиль подачи и ускорения применён.")
            return
        if action == "hold":
            s.set_idle_hold(params["hold"])
            self.zero = False
            self.sheet = self.axes = None
            self.emit("calibration", (False, None))
            self.emit("log", "Удержание " + ("включено" if params["hold"] else "отключено")
                      + ". Режим применяется при следующем движении. Задайте оба угла заново.")
            return
        if action == "clear":
            self.zero = False
            self.sheet = self.axes = None
            self.emit("calibration", (False, None))
            return
        if action in ("draw", "startpoint"):
            if not self.zero or not self.sheet:
                raise ValueError("Сначала сохраните оба угла листа.")
            validate_paths(params.get("paths"), self.sheet)
        if action in ("pen_up_hold", "pen_down_hold", "pen_up", "pen_pulse"):
            s.ready_pen(self.pen)
        if action in ("pen_up_hold", "pen_down_hold"):
            if not self.pen.enabled:
                raise ValueError("Включите проверенный режим соленоида.")
            down = action == "pen_down_hold"
            if (not down or self.pen.coil_for(down)) and not self.pen.continuous:
                raise ValueError("Постоянное управление ручкой требует проверенного непрерывного профиля.")
            # Deliberate manual state, no axis moves and no test pulse. The
            # existing matching-profile check remains authoritative; normal
            # jog/draw paths still set the required pen state themselves.
            s.set_pen(down, self.pen)
            self.emit("log", "Ручка: команда " + ("опускания." if down else "подъёма до следующей команды.") +
                      " Положение датчиком не измеряется.")
            return
        if action in ("pen_up", "pen_pulse"):
            if not self.pen.enabled:
                raise ValueError("Включите проверенный режим соленоида.")
            if self.pen.power_raises:
                try:
                    # No axis moves and no unbounded powered-UP idle state.
                    # A test always finishes OFF/DOWN for this mechanism.
                    s.set_pen(True, self.pen, calibration=True)
                    s.set_pen(False, self.pen, calibration=True)
                    if action == "pen_pulse":
                        s.command("G4 P0.250")
                    s.set_pen(True, self.pen, calibration=True)
                except Exception:
                    s.abort()
                    raise
            elif action == "pen_up":
                s.set_pen(False, self.pen, calibration=True)
            else:
                try:
                    s.set_pen(False, self.pen, calibration=True)
                    s.set_pen(True, self.pen, calibration=True)
                    s.command("G4 P0.250")
                    s.set_pen(False, self.pen, calibration=True)
                except Exception:
                    s.abort()
                    raise
            self.emit("finished", action)
            return
        position = s.ready()
        s.check_pen(self.pen)
        axes = params["axes"]
        if self.zero and axes != self.axes:
            raise ValueError("Направления осей изменились. Сначала сбросьте обе точки.")
        if action == "zero":
            s.command("G21 G90 G94 G54 G92.1 G49")
            s.command("G10 L20 P1 X0 Y0")
            s.refresh_g54_offset()
            s.status()
            self.zero, self.sheet, self.axes = True, None, axes
            self.emit("calibration", (True, None))
            return
        if action == "corner":
            if not self.zero:
                raise ValueError("Сначала сохраните верхний левый угол.")
            self.sheet = Sheet(*axes.paper(position))
            self.emit("calibration", (True, self.sheet))
            return
        feed = float(params.get("feed", MACHINE_MAX_FEED / 2))
        max_feed = min(float(s.settings.get(110, 0)), float(s.settings.get(111, 0)))
        if not math.isfinite(feed) or feed <= 0 or max_feed <= 0 or feed > max_feed + 0.001:
            raise ValueError("Некорректная скорость: она должна быть положительной и не выше максимума GRBL.")
        if action == "jog":
            lift_pen = params.get("lift_pen", True)
            if type(lift_pen) is not bool:
                raise ValueError("Режим подъёма при ручном перемещении должен быть включён или выключен.")
            delta = params["delta"]
            distance = math.hypot(*delta)
            if not all(math.isfinite(v) for v in delta) or not 0.01 <= distance <= MAX_JOG_DISTANCE + 0.001:
                raise ValueError(f"Ход за нажатие должен быть от 0,01 до {MAX_JOG_DISTANCE:g} мм.")
            dx, dy = axes.machine(delta)
            target = axes.paper((position[0] + dx, position[1] + dy))
            if self.sheet and not self.sheet.contains(target):
                raise ValueError("Этот шаг выходит за границы заданного листа.")
            if not lift_pen:
                s.power_off_pen(self.pen)
            elif self.pen.enabled and self.pen.power_raises:
                self.powered_travel(axes.paper(position), target, axes, feed, jog=True)
                return
            elif self.pen.enabled:
                s.set_pen(False, self.pen)
            s.command(f"$J=G91 G21 X{dx:.3f} Y{dy:.3f} F{feed:.1f}")
            s.wait_jog((position[0] + dx, position[1] + dy),
                       timeout=max(30, distance / feed * 120 + 15))
            return
        if action == "scan":
            if not self.zero or not self.sheet:
                raise ValueError("Для сканирования сначала задайте проверенную область двумя углами.")
            points, take_photo = params.get("points"), params.get("capture")
            if not isinstance(points, (list, tuple)) or not 1 <= len(points) <= 200 or not callable(take_photo):
                raise ValueError("Некорректный план сканирования.")
            # Check the ENTIRE route before the first move, not point by point after starting.
            for point in points:
                if not isinstance(point, (tuple, list)) or len(point) != 2 or any(type(v) not in (int, float) for v in point) or not self.sheet.contains(point):
                    raise ValueError("План сканирования выходит за проверенную область.")
            current = axes.paper(position)
            if not self.sheet.contains(current):
                raise ValueError("Текущее положение вне области. Повторите ручную установку углов.")
            if self.pen.enabled and not self.pen.power_raises:
                s.set_pen(False, self.pen)
            s.command("G21 G90 G94 G54")
            for index, point in enumerate(points):
                distance = math.dist(current, point)
                timeout = max(15, distance / feed * 120 + 15)
                if self.pen.enabled and self.pen.power_raises:
                    actual = self.powered_travel(current, point, axes, feed)
                else:
                    x, y = axes.machine(point)
                    s.command(f"G1 X{x:.3f} Y{y:.3f} F{feed:.1f}", timeout=timeout)
                    actual = axes.paper(s.synchronize(timeout))
                if math.dist(actual, point) > .02:
                    raise RuntimeError("Контроллер не подтвердил точку сканирования.")
                if self.cancelled.wait(.3):
                    raise Cancelled()
                # The callback has a bounded camera timeout and never sends serial commands.
                take_photo(index, tuple(actual))
                if self.cancelled.is_set():
                    raise Cancelled()
                self.emit("progress", (index + 1, len(points)))
                current = point
            self.emit("finished", action)
            return
        if action in ("draw", "startpoint"):
            paths = params["paths"]
            current = axes.paper(position)
            if not self.sheet.contains(current):
                raise ValueError("Текущее положение вне листа. Повторите установку двух углов.")
            if action == "startpoint":
                paths = [[paths[0][0]]]
            if self.pen.enabled:
                self.draw_with_lift(paths, current, axes, feed, action)
                self.emit("finished", action)
                return
            moves = list(route(paths, current))
            distance = sum(math.dist(a, b) for a, b, _ in moves)
            timeout = max(15, distance / feed * 120 + 15)
            s.command("G21 G90 G94 G54")
            for index, (_, point, _) in enumerate(moves):
                x, y = axes.machine(point)
                s.command(f"G1 X{x:.3f} Y{y:.3f} F{feed:.1f}", timeout=timeout)
                self.emit("progress", (index + 1, len(moves)))
            s.synchronize(timeout)
            self.emit("finished", action)
            return
        raise ValueError("Неизвестная операция.")

    def powered_travel(self, current, target, axes, feed, *, jog=False):
        """Lift through a transfer; an arrow jog is always one planner move.

        FluidNC uses one whole transfer with its requested ON limit. The Uno
        retains short drawing batches for its independent firmware guard.
        All paths confirm full stop before lowering and stop on link failure.
        """
        s = self.session
        distance = math.dist(current, target)
        if distance <= .0005:
            s.set_pen(True, self.pen)
            return tuple(current)
        if self.pen.continuous or jog or s.fluidnc_power_mode():
            if not self.pen.continuous:
                acceleration = min(float(s.settings.get(120, 0)), float(s.settings.get(121, 0)))
                velocity = feed / 60
                if not math.isfinite(acceleration) or acceleration <= 0:
                    raise ValueError("Для поднятого перемещения нужно подтверждённое ускорение обеих осей.")
                # One click is one jog, never a loop of short relay cycles.
                # Reject before M8 when a whole rest-to-rest move cannot fit
                # the existing bounded ON period. Reserve coordinate rounding
                # and reply/stop margin; the runtime deadline remains active.
                padded_distance = distance + .004
                if padded_distance <= velocity * velocity / acceleration:
                    motion_seconds = 2 * math.sqrt(padded_distance / acceleration)
                else:
                    motion_seconds = padded_distance / velocity + velocity / acceleration
                required = self.pen.up_ms / 1000 + motion_seconds + POWERED_JOG_REPLY_MARGIN
                limit = s.host_power_limit()
                if required > limit:
                    raise ValueError(f"Перемещение требует слишком долгого питания соленоида "
                                     f"(лимит {limit:g} с). Уменьшите расстояние или поднимите "
                                     "ручку вручную для перемещения без автоподъёма. Ход не начинался.")
            timeout = max(30, distance / feed * 120 + 15)
            try:
                s.set_pen(False, self.pen)
                if not s.coil_on or s.pen_down:
                    raise RuntimeError("Подъём ручки не подтверждён; движение отменено.")
                x, y = axes.machine(target)
                if jog:
                    cx, cy = axes.machine(current)
                    command = f"$J=G91 G21 X{x-cx:.3f} Y{y-cy:.3f} F{feed:.1f}"
                else:
                    command = f"G1 X{x:.3f} Y{y:.3f} F{feed:.1f}"
                # Exactly one planner movement; bounded long jogs have already
                # been rejected, explicitly continuous ones retain full travel.
                s.command(command, timeout=timeout)
                actual = s.wait_jog((x, y), timeout=timeout) if jog else s.synchronize(timeout)
                actual = axes.paper(actual)
                if math.dist(actual, target) > .02:
                    raise RuntimeError("Контроллер не подтвердил точку поднятого перехода; задайте оба угла заново.")
                s.set_pen(True, self.pen)
                return tuple(actual)
            except Exception:
                s.abort()
                raise
        acceleration = min(float(s.settings.get(120, 0)), float(s.settings.get(121, 0)))
        velocity = feed / 60
        if not math.isfinite(acceleration) or acceleration <= 0:
            raise ValueError("Для поднятых переходов нужно подтверждённое ускорение обеих осей GRBL.")
        duration = POWERED_TRAVEL_SECONDS
        # Symmetric triangular/trapezoidal rest-to-rest profile. Minimum axis
        # acceleration is conservative for any XY direction; reserve 4 microns
        # for the two rounded coordinates (GRBL commands use 0.001 mm).
        if duration <= 2 * velocity / acceleration:
            maximum_distance = acceleration * duration * duration / 4
        else:
            maximum_distance = velocity * (duration - velocity / acceleration)
        maximum_distance -= .004
        if maximum_distance < .005:
            raise ValueError("Скорость слишком мала для коротких поднятых переходов с этим соленоидом; увеличьте скорость.")
        count = max(1, math.ceil(distance / maximum_distance))
        if count > 1 and not s.powered_travel_notice:
            self.emit("log", "Длинные переходы разделены остановками для охлаждения соленоида. "
                      "При остановках питание выключается и ручка касается бумаги: возможны точки. "
                      "Во время самого перехода ручка поднята.")
            s.powered_travel_notice = True
        start = tuple(current)
        try:
            for index in range(1, count + 1):
                endpoint = tuple(target) if index == count else tuple(
                    start[axis] + (target[axis] - start[axis]) * index / count for axis in (0, 1))
                s.set_pen(False, self.pen)
                # Do not start a move if host delays already consumed the ON
                # budget. Reset also stops motors; never continue pen-down.
                if (s.coil_on_at is None or not s.coil_on or s.pen_down or
                        time.monotonic() - s.coil_on_at + duration > s.host_power_limit()):
                    raise TimeoutError("Подъём задержался: переход отменён до движения. Проверьте соединение и соленоид.")
                x, y = axes.machine(endpoint)
                if jog:
                    cx, cy = axes.machine(current)
                    command = f"$J=G91 G21 X{x-cx:.3f} Y{y-cy:.3f} F{feed:.1f}"
                else:
                    command = f"G1 X{x:.3f} Y{y:.3f} F{feed:.1f}"
                s.command(command, timeout=8)
                actual = s.wait_jog((x, y), timeout=8) if jog else s.synchronize(8)
                # Confirm full stop before OFF: relay-off synchronization can
                # otherwise lower the nib while an axis planner is still busy.
                s.set_pen(True, self.pen)
                actual = axes.paper(actual)
                if math.dist(actual, endpoint) > .02:
                    raise RuntimeError("Контроллер не подтвердил точку поднятого перехода; задайте оба угла заново.")
                current = tuple(actual)
            return tuple(current)
        except Exception:
            s.abort()
            raise

    def draw_power_raises(self, paths, current, axes, feed, action):
        """Unpowered strokes, raised transfers; OFF after each full stop."""
        s = self.session
        total = sum(math.dist(a, b) for path in paths for a, b in zip(path, path[1:]))
        drawn = 0.
        try:
            s.command("G21 G90 G94 G54")
            s.set_pen(True, self.pen)
            for path in paths:
                if math.dist(current, path[0]) > .0005:
                    current = self.powered_travel(current, path[0], axes, feed)
                if action == "startpoint":
                    break
                # Lower and settle exactly at the stroke/dot position. No
                # duty-cycle batching is needed while drawing unpowered.
                s.set_pen(True, self.pen)
                span = 0.
                for point in path[1:]:
                    length = math.dist(current, point)
                    if length <= .0005:
                        continue
                    x, y = axes.machine(point)
                    timeout = max(15, length / feed * 120 + 15)
                    s.command(f"G1 X{x:.3f} Y{y:.3f} F{feed:.1f}", timeout=timeout)
                    current = tuple(point)
                    span += length
                    drawn += length
                    self.emit("progress", (drawn, total))
                if span:
                    actual = axes.paper(s.synchronize(max(15, span / feed * 120 + 15)))
                    if math.dist(actual, current) > .02:
                        raise RuntimeError("Контроллер не подтвердил конец штриха; задайте оба угла заново.")
                    current = tuple(actual)
            s.set_pen(True, self.pen)
            s.synchronize(15)
        except Exception:
            s.abort()
            raise

    def draw_with_lift(self, paths, current, axes, feed, action):
        if self.pen.power_raises:
            return self.draw_power_raises(paths, current, axes, feed, action)
        s = self.session
        total = sum(math.dist(a,b) for path in paths for a,b in zip(path,path[1:]))
        drawn = 0.
        def move(point):
            nonlocal current
            distance = math.dist(current, point)
            x,y = axes.machine(point)
            timeout = max(15, distance / feed * 120 + 15)
            s.command(f"G1 X{x:.3f} Y{y:.3f} F{feed:.1f}", timeout=timeout)
            current = point
            return timeout
        try:
            s.set_pen(False, self.pen)
            s.command("G21 G90 G94 G54")
            for path in paths:
                if math.dist(current,path[0]) > .0005:
                    timeout = move(path[0])
                    s.synchronize(timeout)  # Still UP at the start of the stroke.
                if action == "startpoint":
                    break
                if not any(math.dist(a, b) > 1e-9 for a, b in zip(path, path[1:])):
                    # A singleton or repeated point is a dot: preserve it with
                    # one settled contact, using the same cooldown and guard.
                    s.set_pen(True, self.pen)
                    s.set_pen(False, self.pen)
                    continue
                stroke_batches = [path[1:]] if self.pen.continuous else batches(path,feed)
                for batch in stroke_batches:
                    s.set_pen(True,self.pen)
                    span = 0.
                    for point in batch:
                        span += math.dist(current,point)
                        move(point)
                    s.synchronize(max(15,span/feed*120+15))
                    s.set_pen(False,self.pen)
                    drawn += span
                    self.emit("progress", (drawn,total))
            s.set_pen(False,self.pen)
            s.synchronize(15)
        except Exception:
            s.abort() # Do not leave coil energized after a transport/geometry error.
            raise
