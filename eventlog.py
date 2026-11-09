"""Bounded, optional diagnostics for application actions and connection state.

Adapted from the team's DWIN EventLog in PR #2, commit 0174a3a. The desktop
version uses a shared journal and restricts string values to diagnostic codes.
Prompts, exception messages, filenames, G-code and connection targets do not
belong in this log. Logging failures never interrupt a machine action.
"""
import json
import math
from pathlib import Path
import re
import threading
import time


TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
ACTIONS = frozenset((
    "connect", "disconnect", "configure", "hold", "clear", "unlock",
    "check_endstops", "draw", "startpoint", "pen_up_hold", "pen_down_hold",
    "pen_up", "pen_pulse", "zero", "corner", "jog", "scan", "stop",
    "assistant", "preview", "apply", "undo", "redo", "cancel", "close",
    "voice", "record", "transcribe", "insert",
))
STATES = frozenset((
    "Idle", "Run", "Jog", "Alarm", "Hold", "Door", "Check", "Home",
    "Sleep", "Unknown", "idle", "busy", "ready", "pending", "unknown",
    "up", "down", "off", "cooling", "connected", "disconnected",
    "started", "finished", "cancelled", "failed", "rejected", "accepted",
))
REASONS = frozenset((
    "user", "user_stop", "button", "escape", "window_close", "shutdown",
    "screen_stop", "screen_error", "network_lease_first_loss", "lease_lost",
    "cancelled", "busy", "invalid", "input_error", "worker_error",
    "connection_error", "stale", "not_ready", "readonly", "unknown",
    "accepted", "rejected", "completed", "unavailable", "timeout",
))
STRING_FIELDS = {
    "action": ACTIONS,
    "mode": frozenset(("USB", "Wi-Fi", "Демо", "DEMO")),
    "transport": frozenset(("usb", "wifi", "demo", "serial", "tcp", "none")),
    "state": STATES,
    "kind": STATES | frozenset(("text", "move", "resize", "scale", "rotate", "mirror",
                                "compose", "creative", "help", "status", "refused", "error")),
    "reason": REASONS,
    "caller": frozenset(("ui", "desktop", "dwin", "assistant", "controller", "pump",
                         "submit", "stop", "close", "_dispatch")),
    "error_type": frozenset((
        "Exception", "OSError", "IOError", "ValueError", "TypeError", "RuntimeError",
        "ConnectionError", "TimeoutError", "PermissionError", "FileNotFoundError",
        "SerialException", "SerialTimeoutException", "TransportError", "Cancelled",
        "TclError", "AssertionError", "KeyError", "AttributeError", "ImportError",
        "ModuleNotFoundError", "HTTPError", "URLError", "BrokenPipeError",
        "ConnectionResetError", "ConnectionAbortedError", "UnicodeError",
    )),
    "key_name": frozenset(("left", "right", "up", "down", "stop", "first_corner",
                           "second_corner", "draw", "preview", "assistant", "unknown")),
}
NUMERIC_FIELDS = frozenset((
    "vp", "key", "words_count", "result_code", "errno", "winerror", "count",
    "revision", "duration_ms", "ack_age_s", "heartbeat_age_s", "panel_age_s",
    "ui_age_s", "age_s", "firmware_uptime_ms", "firmware_reset_reason", "board_event_seq",
))
BOOLEAN_FIELDS = frozenset((
    "armed", "linked", "ready", "wanted", "connected", "simulation", "busy",
    "zero", "sheet", "pen_enabled", "success", "cancelled", "wire_connected",
    "tunnel_hello", "ack_owned", "last_owned", "lease_ready", "control_lease", "closed",
))
FIELDS = frozenset(STRING_FIELDS) | NUMERIC_FIELDS | BOOLEAN_FIELDS


def age(now, timestamp):
    """Diagnostic duration; absent/invalid observations remain unknown."""
    try:
        value = now - timestamp
        return round(max(0., value), 3) if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def record_event(owner, event, **fields):
    """Optional on fake apps and old adapters; never alter action behaviour."""
    try:
        logger = getattr(owner, "eventlog", None)
        if logger is not None:
            logger.record(event, **fields)
    except Exception:
        pass


class EventLog:
    def __init__(self, data_dir, *, max_bytes=262144, backups=2):
        self.path = Path(data_dir) / "diagnostics" / "app-events.jsonl" if data_dir is not None else None
        self.max_bytes = max(8192, int(max_bytes))
        self.backups = max(0, min(int(backups), 4))
        self.lock = threading.Lock()

    def record(self, event, **fields):
        """Append a code-only event, returning False if disabled or unavailable."""
        try:
            if self.path is None or not isinstance(event, str) or not TOKEN.fullmatch(event):
                return False
            row = {"event": event, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "monotonic_s": round(time.monotonic(), 3)}
            for key, value in fields.items():
                if key in STRING_FIELDS:
                    if isinstance(value, str) and value in STRING_FIELDS[key]:
                        row[key] = value
                elif key in BOOLEAN_FIELDS:
                    if value is None or type(value) is bool:
                        row[key] = value
                elif key in NUMERIC_FIELDS:
                    if value is None:
                        row[key] = None
                    elif type(value) is int and -(2 ** 63) <= value < 2 ** 63:
                        row[key] = value
                    elif type(value) is float and math.isfinite(value):
                        row[key] = round(value, 3)
            raw = (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
            if len(raw) > min(4096, self.max_bytes):
                return False
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(raw) > self.max_bytes:
                    if self.backups:
                        for index in range(self.backups, 0, -1):
                            source = self.path if index == 1 else self.path.with_name(self.path.name + "." + str(index - 1))
                            target = self.path.with_name(self.path.name + "." + str(index))
                            if source.exists():
                                source.replace(target)
                    else:
                        self.path.write_bytes(b"")
                with self.path.open("ab") as stream:
                    stream.write(raw)
            return True
        except Exception:
            # A full/read-only disk or optional diagnostic error must not stop motion.
            return False
