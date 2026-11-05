"""One click is one finite jog; no machine or wall-clock delay is used."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import Controller, GrblSession, DLC32_MAX_ON_SECONDS, POWERED_HOST_ON_LIMIT
from geometry import Axes
from machine_profiles import DLC32, UNO
from pen_lift import PenLiftSettings, firmware_profile
from test_protocol_regressions import Clock, ClockEvent, QueuedJogWire


class JogClickTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for target, replacement in (("controller.time.monotonic", self.clock.monotonic),
                                     ("controller.SerialTrace", None)):
            mock = patch(target, replacement) if replacement is not None else patch(target)
            mock.start()
            self.addCleanup(mock.stop)

    def controller(self, target=(10, 0)):
        owner = Controller(start_worker=False)
        owner.cancelled = ClockEvent(self.clock)
        x, y = target
        wire = QueuedJogWire(self.clock, [
            f"<Jog|WPos:{x / 2},{y / 2},0|FS:150,0>",
            f"<Idle|WPos:{x},{y},0|FS:0,0>"])
        session = GrblSession(wire, owner.cancelled, owner.emit, DLC32)
        session.initialize()
        # Actual machine's current speed/acceleration/scale; no real YAML read.
        session.settings.update({100: 80, 101: 80, 110: 300, 111: 300, 120: 10, 121: 10})
        owner.session, owner.connected, owner.machine_profile = session, True, DLC32
        wire.history.clear()
        pen = PenLiftSettings(enabled=True, active_low=True, confirmed=True,
                              power_raises=True, continuous=False, up_ms=250, down_ms=250)
        return owner, session, wire, pen

    def params(self, pen, *, lift_pen=False, delta=(10, 0), axes=Axes()):
        return {"pen": pen, "axes": axes, "delta": delta, "feed": 150, "lift_pen": lift_pen}

    def jogs(self, wire):
        return [data for data in wire.history if data.startswith(b"$J=")]

    def test_ten_mm_without_auto_lift_is_one_whole_jog(self):
        owner, session, wire, pen = self.controller()
        owner.execute("jog", self.params(pen))
        self.assertEqual(self.jogs(wire), [b"$J=G91 G21 X10.000 Y0.000 F150.0\n"])
        self.assertEqual(wire.history.count(b"M9\n"), 1)
        self.assertNotIn(b"M8\n", wire.history)
        self.assertFalse(any(data.startswith(b"G4") for data in wire.history))
        self.assertNotIn(b"\x18", wire.history)
        self.assertFalse(session.coil_on)
        self.assertEqual(session.position, (10., 0.))

    def test_bounded_auto_lift_rejects_over_ten_second_move_before_actuation(self):
        owner, _, wire, pen = self.controller(target=(100, 0))
        with self.assertRaisesRegex(ValueError, "лимит 10 с"):
            owner.execute("jog", self.params(pen, lift_pen=True, delta=(100, 0)))
        self.assertEqual(wire.history, [b"?"])
        self.assertFalse(owner.cancelled.is_set())

    def test_ten_mm_auto_lift_can_take_several_seconds_without_fragmentation(self):
        owner, session, wire, pen = self.controller()
        wire.reports.clear()
        wire.reports.extend(["<Jog|WPos:1,0,0|FS:150,0>",
                             "<Jog|WPos:4,0,0|FS:150,0>",
                             "<Jog|WPos:8,0,0|FS:150,0>",
                             "<Idle|WPos:10,0,0|FS:0,0>"])
        original_write = wire.write

        def delayed_completion(data):
            if data == b"?" and wire.jog_queued:
                # Several fresh moving reports over the ~4.25 second transfer.
                # Every reply still arrives inside its own status deadline.
                self.clock.now += 1
            return original_write(data)

        wire.write = delayed_completion
        owner.execute("jog", self.params(pen, lift_pen=True))
        self.assertEqual(self.jogs(wire), [b"$J=G91 G21 X10.000 Y0.000 F150.0\n"])
        self.assertEqual(wire.history.count(b"M8\n"), 1)
        self.assertEqual(wire.history.count(b"M9\n"), 1)
        self.assertNotIn(b"\x18", wire.history)
        self.assertFalse(session.coil_on)
        self.assertFalse(pen.continuous)

    def test_consecutive_ten_mm_transfers_have_no_added_cooling_dwell(self):
        owner, _, wire, pen = self.controller()
        owner.execute("jog", self.params(pen, lift_pen=True))
        wire.jog_queued = False
        wire.reports.extend(["<Jog|WPos:15,0,0|FS:150,0>", "<Idle|WPos:20,0,0|FS:0,0>"])
        owner.execute("jog", self.params(pen, lift_pen=True))
        self.assertEqual(len(self.jogs(wire)), 2)
        self.assertEqual(wire.history.count(b"M8\n"), 2)
        self.assertEqual(wire.history.count(b"M9\n"), 2)
        dwells = [float(data.decode().strip().split("P", 1)[1])
                  for data in wire.history if data.startswith(b"G4 P")]
        self.assertEqual(dwells, [.25, .25, .25, .25])

    def test_drawing_transfer_is_one_g1_and_lowers_only_after_stop(self):
        owner, session, wire, pen = self.controller()
        owner.pen = pen
        stopped = []

        def synchronize(timeout):
            self.clock.now += 4.25
            self.assertNotIn(b"M9\n", wire.history)
            stopped.append(True)
            return (10., 0.)

        session.synchronize = synchronize
        self.assertEqual(owner.powered_travel((0, 0), (10, 0), Axes(), 150), (10., 0.))
        self.assertEqual(stopped, [True])
        self.assertEqual([data for data in wire.history if data.startswith(b"G1 ")],
                         [b"G1 X10.000 Y0.000 F150.0\n"])
        self.assertEqual(wire.history.count(b"M8\n"), 1)
        self.assertEqual(wire.history.count(b"M9\n"), 1)

    def test_legacy_uno_retains_short_guard_and_drawing_batches(self):
        owner, session, wire, pen = self.controller()
        owner.pen = pen
        session.machine_profile = owner.machine_profile = UNO
        session.pen_firmware = firmware_profile(True)
        self.assertEqual(session.host_power_limit(), POWERED_HOST_ON_LIMIT)

        def synchronize(timeout):
            import re
            command = next(data.decode() for data in reversed(wire.history) if data.startswith(b"G1 "))
            coordinates = dict((axis, float(value)) for axis, value in re.findall(r"([XY])([-\d.]+)", command))
            return coordinates["X"], coordinates["Y"]

        session.synchronize = synchronize
        owner.powered_travel((0, 0), (10, 0), Axes(), 150)
        self.assertEqual(len([data for data in wire.history if data.startswith(b"G1 ")]), 14)
        self.assertEqual(wire.history.count(b"M8\n"), 14)

    def test_demo_uses_selected_machine_power_policy_without_fake_identity(self):
        pen = PenLiftSettings(enabled=True, active_low=False, confirmed=True,
                              power_raises=True, continuous=False)
        for selected, limit, succeeds in ((DLC32, DLC32_MAX_ON_SECONDS, True),
                                           (UNO, POWERED_HOST_ON_LIMIT, False)):
            with self.subTest(selected=selected):
                owner = Controller(start_worker=False)
                owner.cancelled = ClockEvent(self.clock)
                owner.execute("connect", {"port": "DEMO", "machine_profile": selected, "pen": pen})
                session, wire = owner.session, owner.session.wire
                self.assertEqual(session.machine_profile, UNO)
                self.assertEqual(session.host_power_limit(), limit)
                wire.history.clear()
                if succeeds:
                    owner.execute("jog", self.params(pen, lift_pen=True))
                    self.assertEqual(len(self.jogs(wire)), 1)
                    self.assertEqual(wire.history.count(b"M8\n"), 1)
                else:
                    with self.assertRaises(ValueError):
                        owner.execute("jog", self.params(pen, lift_pen=True))
                    self.assertNotIn(b"M8\n", wire.history)

    def test_bounded_one_mm_auto_lift_is_one_jog_and_one_pulse(self):
        owner, _, wire, pen = self.controller(target=(1, 0))
        owner.execute("jog", self.params(pen, lift_pen=True, delta=(1, 0)))
        self.assertEqual(self.jogs(wire), [b"$J=G91 G21 X1.000 Y0.000 F150.0\n"])
        self.assertEqual(wire.history.count(b"M8\n"), 1)
        self.assertEqual(wire.history.count(b"M9\n"), 1)
        self.assertNotIn(b"\x18", wire.history)

    def test_explicit_continuous_auto_lift_keeps_one_ten_mm_jog(self):
        owner, _, wire, pen = self.controller()
        pen = PenLiftSettings.from_value(dict(pen.as_dict(), continuous=True))
        owner.execute("jog", self.params(pen, lift_pen=True))
        self.assertEqual(len(self.jogs(wire)), 1)
        self.assertIn(b"X10.000", self.jogs(wire)[0])
        self.assertEqual(wire.history.count(b"M8\n"), 1)
        self.assertEqual(wire.history.count(b"M9\n"), 1)

    def test_motor_only_preserves_axis_transform_and_requested_sign(self):
        for axes, delta, target in ((Axes(right_sign=-1), (10, 0), (-10, 0)),
                                    (Axes(), (0, -10), (0, -10)),
                                    (Axes(swap=True), (10, 0), (0, 10))):
            with self.subTest(axes=axes, delta=delta):
                owner, _, wire, pen = self.controller(target)
                owner.execute("jog", self.params(pen, delta=delta, axes=axes))
                x, y = target
                self.assertEqual(self.jogs(wire),
                                 [f"$J=G91 G21 X{x:.3f} Y{y:.3f} F150.0\n".encode()])
                self.assertNotIn(b"M8\n", wire.history)

    def test_unconfirmed_enabled_relay_does_not_get_unchecked_m9(self):
        owner, _, wire, pen = self.controller()
        pen = PenLiftSettings.from_value(dict(pen.as_dict(), confirmed=False))
        with self.assertRaises(ValueError):
            owner.execute("jog", self.params(pen))
        self.assertFalse(any(data.startswith((b"M", b"$J=")) for data in wire.history))

    def test_disabled_pen_has_no_relay_commands(self):
        owner, _, wire, _ = self.controller()
        owner.execute("jog", self.params(PenLiftSettings(enabled=False)))
        self.assertEqual(len(self.jogs(wire)), 1)
        self.assertFalse(any(data.startswith((b"M", b"G4")) for data in wire.history))

    def test_error_never_retries_the_single_jog(self):
        owner, _, wire, pen = self.controller()
        wire.command_errors["$J=G91 G21 X10.000 Y0.000 F150.0"] = "ALARM:3"
        owner.jobs.put(("jog", self.params(pen)))
        owner.jobs.put((None, {}))
        owner.run()
        self.assertEqual(len(self.jogs(wire)), 1)
        self.assertIn(b"\x18", wire.history)
        self.assertFalse(owner.connected)
        self.assertTrue(wire.closed)

    def test_non_boolean_lift_mode_is_rejected_before_actuation(self):
        for mode in (None, 0, 1, "false"):
            with self.subTest(mode=mode):
                owner, _, wire, pen = self.controller()
                with self.assertRaises(ValueError):
                    owner.execute("jog", self.params(pen, lift_pen=mode))
                self.assertFalse(any(data.startswith((b"M", b"$J=")) for data in wire.history))


if __name__ == "__main__":
    unittest.main()
