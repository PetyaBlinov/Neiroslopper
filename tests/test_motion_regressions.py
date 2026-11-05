"""Offline motion regressions; no physical serial port is opened."""
import unittest
from unittest.mock import patch

from test_controller import ProtocolWire
from controller import Controller, GrblSession
from geometry import Axes
from machine_profiles import DLC32
from pen_lift import PenLiftSettings


class MotionRegressionTests(unittest.TestCase):
    def test_malformed_coordinate_vectors_are_protocol_errors(self):
        for field in ("WPos:invalid,0,0", "MPos:1", "WCO:0,invalid,0",
                      "WPos:", "WPos:nan,0,0", "WPos:0,inf,0"):
            with self.subTest(field=field), patch("controller.SerialTrace"):
                wire = ProtocolWire()
                controller = Controller(start_worker=False)
                session = GrblSession(wire, controller.cancelled, controller.emit, DLC32)
                session.trusted = True
                wire.queue(f"<Jog|{field}>")
                with self.assertRaisesRegex(RuntimeError, "Некорректная позиция контроллера"):
                    session.read_line()
                self.assertEqual(wire.history, [])

    def test_invalid_motion_status_aborts_before_reporting_error(self):
        controller = Controller(start_worker=False)
        wire = ProtocolWire()
        with patch("controller.SerialTrace"):
            session = GrblSession(wire, controller.cancelled, controller.emit, DLC32)
            session.initialize()
            wire.history.clear()
            controller.session = session
            controller.connected = True
            controller.machine_profile = DLC32
            # The sender has written this jog before parsing its response.
            # A malformed numeric position is a protocol failure, not a
            # harmless invalid value typed into a form.
            wire.command_errors["$J=G91 G21 X1.000 Y0.000 F100.0"] = "<Jog|WPos:invalid,0,0>"
            snapshots = []
            original_emit = controller.emit

            def capture(name, value):
                if name == "error":
                    snapshots.append((controller.connected, wire.closed, list(wire.history)))
                original_emit(name, value)

            controller.emit = capture
            controller.jobs.put(("jog", {"axes": Axes(), "delta": (1, 0), "feed": 100,
                                         "pen": PenLiftSettings(enabled=False)}))
            # End the owning loop deterministically after the failing job.
            controller.jobs.put((None, {}))
            controller.run()
        self.assertEqual(len(snapshots), 1)
        connected, closed, history_at_error = snapshots[0]
        self.assertIn(b"$J=G91 G21 X1.000 Y0.000 F100.0\n", history_at_error)
        self.assertIn(b"\x18", history_at_error)
        self.assertFalse(connected)
        self.assertTrue(closed)


if __name__ == "__main__":
    unittest.main()
