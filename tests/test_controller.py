"""Sender regressions using a protocol fake; no serial device is opened."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import Cancelled, Controller, GrblSession
from geometry import Axes
from machine_profiles import DLC32, settings_ready
from pen_lift import PenLiftSettings
from serial_trace import TransportError


PHYSICAL_CONFIG = """name: Existing Plotter
board: MKS DLC32 v2.2
report_inches: false
kinematics:
  Cartesian: {}
stepping:
  engine: I2S_STATIC
  idle_ms: 255
i2so:
  bck_pin: gpio.16
  ws_pin: gpio.17
  data_pin: gpio.21
axes:
  shared_stepper_disable_pin: i2so.0
  x:
    steps_per_mm: 40
    max_rate_mm_per_min: 1200
    acceleration_mm_per_sec2: 100
    max_travel_mm: 297
    soft_limits: false
    motor0:
      hard_limits: false
      stepstick:
        step_pin: i2so.1
        direction_pin: i2so.2
  y:
    steps_per_mm: 40
    max_rate_mm_per_min: 1200
    acceleration_mm_per_sec2: 100
    max_travel_mm: 210
    soft_limits: false
    motor0:
      hard_limits: false
      stepstick:
        step_pin: i2so.5
        direction_pin: i2so.6
coolant:
  flood_pin: gpio.32:low
  mist_pin: NO_PIN
start:
  must_home: false
  check_limits: false
"""

DEFAULT_CONFIG = """name: Default (Test Drive no I/O)
board: None
report_inches: false
stepping:
  engine: RMT
axes:
  x:
    steps_per_mm: 80
    max_rate_mm_per_min: 1000
    acceleration_mm_per_sec2: 100
    motor0:
      null_motor: {}
  y:
    steps_per_mm: 80
    max_rate_mm_per_min: 1000
    acceleration_mm_per_sec2: 100
    motor0:
      null_motor: {}
"""


class ProtocolWire:
    """Deterministic serial-shaped controller, deliberately not SimWire/GRBL."""
    def __init__(self, config=PHYSICAL_CONFIG, port="FAKE-USB"):
        self.port = port
        self.baudrate = 115200
        self.closed = False
        self.rx = bytearray()
        self.history = []
        self.config = config
        self.identity = "[VER:4.1 FluidNC v4.1.1 (esp32-wifi) :]"
        self.status_line = "<Idle|MPos:10,20,0|WCO:2,3,0|FS:0,0>"
        self.g54 = ["[G54:2.000,3.000,0.000]"]
        self.command_errors = {}
        self.short_write = False

    def queue(self, *lines):
        self.rx.extend(("\r\n".join(lines) + "\r\n").encode("ascii"))

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, size=1):
        data = bytes(self.rx[:size])
        del self.rx[:size]
        return data

    def write(self, data):
        self.history.append(data)
        if self.short_write:
            return len(data) - 1
        if data == b"?":
            self.queue(self.status_line)
        elif data != b"\x18":
            command = data.decode("ascii").strip()
            if command == "$$":
                raise AssertionError("Stock FluidNC setup must be read as YAML, not legacy $$")
            if command in self.command_errors:
                self.queue(self.command_errors[command])
            elif command == "$I":
                self.queue(self.identity, "ok")
            elif command == "$Config/Dump":
                self.queue(*self.config.splitlines(), "ok")
            elif command == "$#":
                self.queue(*self.g54, "ok")
            else:
                self.queue("ok")
        return len(data)

    def close(self):
        self.closed = True


class SenderTests(unittest.TestCase):
    def setUp(self):
        # Expected failures must not create diagnostic files in the user's data.
        self.trace_patch = patch("controller.SerialTrace")
        self.trace_patch.start()
        self.addCleanup(self.trace_patch.stop)

    def session(self, wire=None):
        wire = wire or ProtocolWire()
        callbacks = []
        session = GrblSession(wire, threading.Event(), lambda *args: callbacks.append(args), DLC32)
        return session, wire, callbacks

    def test_stock_initialize_is_read_only_and_does_not_require_dwin(self):
        session, wire, callbacks = self.session(ProtocolWire(port="tcp://127.0.0.1:23"))
        session.network_interlock = lambda: False  # Old JD lease is intentionally absent.
        session.initialize()
        self.assertTrue(session.trusted)
        self.assertTrue(settings_ready(session.settings, DLC32))
        self.assertEqual(wire.history, [b"$I\n", b"$Config/Dump\n", b"?"])
        self.assertFalse(session.has_actuated)
        self.assertEqual(session.pen_firmware["flood_pin"], "gpio.32:low")
        self.assertTrue(any(kind == "settings" for kind, _ in callbacks))

    def test_default_test_drive_connects_but_cannot_jog(self):
        session, wire, _ = self.session(ProtocolWire(config=DEFAULT_CONFIG))
        session.initialize()
        self.assertTrue(session.trusted)
        self.assertFalse(settings_ready(session.settings, DLC32))
        controller = Controller(start_worker=False)
        controller.session = session
        controller.connected = True
        controller.machine_profile = DLC32
        with self.assertRaises(ValueError):
            controller.execute("jog", {"axes": Axes(), "delta": (1, 0), "feed": 100,
                                       "pen": PenLiftSettings(enabled=False)})
        self.assertFalse(any(b"$J=" in item or b"G1 " in item or b"M8" in item for item in wire.history))
        self.assertFalse(session.has_actuated)

    def test_diagnostic_disconnect_does_not_reset_the_board(self):
        session, wire, _ = self.session()
        session.initialize()
        session.close()
        self.assertTrue(wire.closed)
        self.assertNotIn(b"\x18", wire.history)

    def test_disconnect_after_control_sends_reset(self):
        session, wire, _ = self.session()
        session.initialize()
        session.command("M9")
        self.assertTrue(session.has_actuated)
        session.close()
        self.assertEqual(wire.history[-1], b"\x18")

    def test_short_write_is_an_error_and_command_is_not_replayed(self):
        session, wire, _ = self.session()
        wire.short_write = True
        with self.assertRaises(TransportError):
            session.command("G1 X1 F100")
        self.assertEqual(wire.history, [b"G1 X1 F100\n"])

    def test_stop_blocks_any_later_motion_write(self):
        session, wire, _ = self.session()
        session.trusted = True
        controller = Controller(start_worker=False)
        controller.session = session
        # Both owner and session must observe the same cancellation event.
        session.cancelled = controller.cancelled
        controller.stop()
        with self.assertRaises(Cancelled):
            session.write(b"G1 X99 F100\n")
        self.assertEqual(wire.history, [b"\x18"])

    def test_stop_serializes_with_an_in_flight_write(self):
        session, wire, _ = self.session()
        entered, release = threading.Event(), threading.Event()
        original = wire.write

        def blocking_write(data):
            if data == b"G1 X1\n":
                entered.set()
                if not release.wait(2):
                    raise AssertionError("Test did not release the fake write")
            return original(data)

        wire.write = blocking_write
        session.trusted = True
        controller = Controller(start_worker=False)
        controller.session = session
        session.cancelled = controller.cancelled
        writer = threading.Thread(target=lambda: session.write(b"G1 X1\n"))
        stopper = threading.Thread(target=controller.stop)
        writer.start()
        self.assertTrue(entered.wait(1))
        stopper.start()
        self.assertTrue(controller.cancelled.wait(1))
        release.set()
        writer.join(2)
        stopper.join(2)
        self.assertFalse(writer.is_alive() or stopper.is_alive())
        with self.assertRaises(Cancelled):
            session.write(b"G1 X2\n")
        self.assertEqual(wire.history, [b"G1 X1\n", b"\x18"])

    def test_power_raises_uses_M8_for_up_and_M9_for_down(self):
        session, wire, _ = self.session()
        session.initialize()
        settings = PenLiftSettings(enabled=True, active_low=True, confirmed=True,
                                   power_raises=True, continuous=True, up_ms=50, down_ms=50)
        wire.history.clear()
        session.set_pen(False, settings)
        self.assertFalse(session.pen_down)
        session.set_pen(True, settings)
        self.assertTrue(session.pen_down)
        self.assertEqual(wire.history, [b"M8\n", b"G4 P0.050\n", b"M9\n", b"G4 P0.050\n"])
        self.assertFalse(session.coil_on)

    def test_incorrect_relay_polarity_never_energizes_the_coil(self):
        session, wire, _ = self.session()
        session.initialize()
        settings = PenLiftSettings(enabled=True, active_low=False, confirmed=True,
                                   power_raises=True, continuous=True)
        wire.history.clear()
        with self.assertRaises(ValueError):
            session.set_pen(False, settings)
        self.assertEqual(wire.history, [])

    def test_MPos_is_converted_using_WCO_and_cached_for_later_reports(self):
        session, wire, _ = self.session()
        wire.queue("<Idle|MPos:10,20,0|WCO:2,3,0>", "<Idle|MPos:11,22,0>")
        session._read_wire_line()
        self.assertEqual(session.position, (8.0, 17.0))
        session._read_wire_line()
        self.assertEqual(session.position, (9.0, 19.0))

    def test_MPos_without_any_WCO_does_not_invent_a_work_position(self):
        session, wire, _ = self.session()
        wire.queue("<Idle|MPos:10,20,0>")
        session._read_wire_line()
        self.assertIsNone(session.position)

    def test_g54_refresh_reads_offsets_without_motion(self):
        session, wire, _ = self.session()
        session.refresh_g54_offset()
        self.assertEqual(session.work_offset, (2.0, 3.0))
        self.assertEqual(wire.history, [b"$#\n"])

    def test_ambiguous_g54_response_is_rejected(self):
        session, wire, _ = self.session()
        wire.g54.append("[G54:9,9,0]")
        with self.assertRaises(ValueError):
            session.refresh_g54_offset()
        self.assertIsNone(session.work_offset)

    def test_runtime_reboot_banners_invalidate_the_session(self):
        for banner in ("[FluidNC v4.1.1 (esp32-wifi)]",
                       "[MSG:INFO: FluidNC v4.1.1 https://github.com/bdring/FluidNC]",
                       "rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)",
                       "Grbl 1.1h ['$' for help]"):
            with self.subTest(banner=banner):
                session, wire, _ = self.session()
                session.trusted = True
                wire.queue(banner)
                with self.assertRaises(RuntimeError):
                    session._read_wire_line()
                self.assertEqual(wire.history, [])

    def test_legacy_identity_is_not_accepted_as_FluidNC(self):
        session, wire, _ = self.session()
        wire.identity = "[VER:1.1h.20190825:]"
        with self.assertRaises(RuntimeError):
            session.initialize()
        self.assertFalse(session.trusted)
        self.assertEqual(wire.history, [b"$I\n"])

    def test_firmware_error_does_not_retry_a_motion(self):
        session, wire, _ = self.session()
        wire.command_errors["G1 X1 F100"] = "error:20"
        with self.assertRaises(RuntimeError):
            session.command("G1 X1 F100")
        self.assertEqual(wire.history, [b"G1 X1 F100\n"])


if __name__ == "__main__":
    unittest.main()
