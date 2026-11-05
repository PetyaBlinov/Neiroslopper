"""Offline queued-jog protocol regressions; no device or visible window."""
import sys
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import Controller, GrblSession
from geometry import Axes
from machine_profiles import DLC32
from pen_lift import PenLiftSettings
from test_controller import ProtocolWire


class Clock:
    def __init__(self):
        self.now = 1000.

    def monotonic(self):
        return self.now


class ClockEvent:
    def __init__(self, clock):
        self.clock, self.flag = clock, False

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False

    def wait(self, duration):
        self.clock.now += duration
        return self.flag


class QueuedJogWire(ProtocolWire):
    """ACK permits a still-Idle status before the queued cycle starts.

    Official FluidNC 4.1.1 jog_execute() queues mc_linear then returns Ok;
    protocol_main_loop ACKs before protocol_auto_cycle_start(). StatusReport
    is handled directly by the independent polling task.
    """
    def __init__(self, clock, reports):
        super().__init__()
        self.clock = clock
        self.reports = deque(reports)
        self.status_line = "<Idle|WPos:0,0,0|FS:0,0>"
        self.jog_queued = False
        self.observations = []

    def write(self, data):
        if data.startswith(b"$J="):
            self.jog_queued = True
        if data == b"?" and self.jog_queued:
            if self.reports:
                self.status_line = self.reports.popleft()
            self.observations.append(self.status_line)
        if data == b"M9\n":
            self.observations.append("M9")
        if data.startswith(b"G4") and self.jog_queued:
            if self.status_line.startswith("<Jog|"):
                raise AssertionError("Ordinary G-code was sent while Jog was active")
        return super().write(data)

    def read(self, size=1):
        self.clock.now += .001
        return super().read(size)


class JogProtocolTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.time_patch = patch("controller.time.monotonic", self.clock.monotonic)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.trace_patch = patch("controller.SerialTrace")
        self.trace_patch.start()
        self.addCleanup(self.trace_patch.stop)

    def controller(self, reports):
        owner = Controller(start_worker=False)
        owner.cancelled = ClockEvent(self.clock)
        wire = QueuedJogWire(self.clock, reports)
        session = GrblSession(wire, owner.cancelled, owner.emit, DLC32)
        session.initialize()
        owner.session, owner.connected, owner.machine_profile = session, True, DLC32
        wire.history.clear()
        return owner, session, wire

    def pen(self):
        return PenLiftSettings(enabled=True, active_low=True, confirmed=True,
                               power_raises=True, continuous=False, up_ms=50, down_ms=50)

    def test_delayed_start_does_not_lower_pen_before_target(self):
        target = "<Idle|WPos:0.500,0,0|FS:0,0>"
        owner, _, wire = self.controller([
            "<Idle|WPos:0,0,0|FS:0,0>",
            "<Jog|WPos:0.250,0,0|FS:150,0>", target])
        owner.execute("jog", {"pen": self.pen(), "axes": Axes(),
                              "delta": (.5, 0), "feed": 150})
        self.assertEqual(wire.observations, [
            "<Idle|WPos:0,0,0|FS:0,0>",
            "<Jog|WPos:0.250,0,0|FS:150,0>", target, "M9"])
        self.assertNotIn(b"\x18", wire.history)
        self.assertEqual(sum(data.startswith(b"$J=") for data in wire.history), 1)

    def test_short_jog_already_at_target_needs_no_observed_jog(self):
        owner, _, wire = self.controller(["<Idle|WPos:0.500,0,0|FS:0,0>"])
        owner.execute("jog", {"pen": self.pen(), "axes": Axes(),
                              "delta": (.5, 0), "feed": 150})
        self.assertEqual(wire.observations[-1], "M9")
        self.assertNotIn(b"\x18", wire.history)

    def test_idle_before_target_after_jog_aborts_without_lowering(self):
        owner, _, wire = self.controller([
            "<Jog|WPos:0.200,0,0|FS:150,0>",
            "<Idle|WPos:0.250,0,0|FS:0,0>"])
        with self.assertRaisesRegex(RuntimeError, "до заданной точки"):
            owner.execute("jog", {"pen": self.pen(), "axes": Axes(),
                                  "delta": (.5, 0), "feed": 150})
        self.assertEqual(wire.history[-1], b"\x18")
        self.assertNotIn(b"M9\n", wire.history)
        self.assertEqual(sum(data.startswith(b"$J=") for data in wire.history), 1)

    def test_nonpowered_jog_also_waits_for_expected_target(self):
        target = "<Idle|WPos:1,0,0|FS:0,0>"
        owner, _, wire = self.controller([
            "<Idle|WPos:0,0,0|FS:0,0>",
            "<Jog|WPos:0.500,0,0|FS:150,0>", target])
        owner.execute("jog", {"pen": PenLiftSettings(enabled=False), "axes": Axes(),
                              "delta": (1, 0), "feed": 150})
        self.assertEqual(wire.observations[-1], target)
        self.assertNotIn(b"M8\n", wire.history)

    def test_jog_in_drained_ack_backlog_still_proves_motion_started(self):
        owner, _, wire = self.controller(["<Idle|WPos:0.250,0,0|FS:0,0>"])
        original_write = wire.write

        def ack_then_jog(data):
            result = original_write(data)
            if data.startswith(b"$J="):
                # One read can receive ACK plus the following asynchronous
                # report. status() drains that report before its fresh query.
                wire.queue("<Jog|WPos:0.200,0,0|FS:150,0>")
            return result

        wire.write = ack_then_jog
        with self.assertRaisesRegex(RuntimeError, "до заданной точки"):
            owner.execute("jog", {"pen": self.pen(), "axes": Axes(),
                                  "delta": (.5, 0), "feed": 150})
        self.assertEqual(wire.history[-1], b"\x18")
        self.assertNotIn(b"M9\n", wire.history)

    def test_minimum_jog_is_not_completed_by_old_position(self):
        owner, session, wire = self.controller([
            "<Idle|WPos:0,0,0|FS:0,0>",
            "<Idle|WPos:0.013,0,0|FS:0,0>"])
        session.settings[100] = 80.
        session.command("$J=G91 G21 X0.010 Y0 F150")
        self.assertEqual(session.wait_jog((.01, 0)), (.013, 0.))
        self.assertEqual(len(wire.observations), 2)


if __name__ == "__main__":
    unittest.main()
