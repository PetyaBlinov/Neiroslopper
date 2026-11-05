"""Bounded relay waits with a deterministic clock; no device or wall-clock sleep."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import Controller, GrblSession, DLC32_MAX_ON_SECONDS
from geometry import Axes
from machine_profiles import DLC32
from pen_lift import PenLiftSettings
from test_controller import ProtocolWire


class Clock:
    def __init__(self):
        self.now = 1000.0

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


class StalledWire(ProtocolWire):
    def __init__(self, clock, *, stall_relay=False):
        super().__init__()
        self.clock, self.stall_relay = clock, stall_relay
        self.moving = False
        self.energized_at = None

    def read(self, size=1):
        self.clock.now += .02
        return super().read(size)

    def write(self, data):
        if data == b'M8\n':
            self.energized_at = self.clock.now
            if self.stall_relay:
                self.history.append(data)
                return len(data)
        if data.startswith(b'$J='):
            self.moving = True
        if data == b'?' and self.moving:
            self.status_line = '<Jog|WPos:0,0,0|FS:0,0>'
        return super().write(data)


class PowerBudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.time_patch = patch('controller.time.monotonic', self.clock.monotonic)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.trace_patch = patch('controller.SerialTrace')
        self.trace_patch.start()
        self.addCleanup(self.trace_patch.stop)

    def controller(self, *, stall_relay=False):
        owner = Controller(start_worker=False)
        owner.cancelled = ClockEvent(self.clock)
        wire = StalledWire(self.clock, stall_relay=stall_relay)
        session = GrblSession(wire, owner.cancelled, owner.emit, DLC32)
        session.initialize()
        owner.session, owner.connected, owner.machine_profile = session, True, DLC32
        pen = PenLiftSettings(enabled=True, active_low=True, confirmed=True,
                              power_raises=True, continuous=False, up_ms=50, down_ms=50)
        wire.history.clear()
        return owner, session, wire, pen

    def assert_bounded_failure(self, *, stall_relay=False):
        owner, session, wire, pen = self.controller(stall_relay=stall_relay)
        with self.assertRaises(TimeoutError):
            owner.execute('jog', {'pen': pen, 'axes': Axes(), 'delta': (.1, 0), 'feed': 100})
        elapsed = self.clock.now - wire.energized_at
        self.assertLessEqual(elapsed, DLC32_MAX_ON_SECONDS + .15)
        self.assertEqual(wire.history[-1], b'\x18')
        self.assertTrue(owner.cancelled.is_set())
        return wire

    def test_stuck_jog_does_not_exceed_requested_ten_second_limit(self):
        wire = self.assert_bounded_failure()
        self.assertTrue(any(data.startswith(b'$J=') for data in wire.history))

    def test_missing_relay_ack_is_bounded_before_any_motion(self):
        wire = self.assert_bounded_failure(stall_relay=True)
        self.assertFalse(any(data.startswith(b'$J=') for data in wire.history))

    def test_repeated_on_command_does_not_renew_the_original_deadline(self):
        _, session, wire, pen = self.controller()
        session.set_pen(False, pen)
        original_on_at, original_deadline = session.coil_on_at, session.coil_deadline
        self.clock.now += .5
        session.set_pen(False, pen)
        self.assertEqual(session.coil_on_at, original_on_at)
        self.assertEqual(session.coil_deadline, original_deadline)
        self.clock.now = original_deadline + .001
        history = list(wire.history)
        with self.assertRaises(TimeoutError):
            session.set_pen(False, pen)
        self.assertEqual(wire.history, history)

    def test_missing_off_ack_keeps_deadline_until_worker_aborts(self):
        owner, session, wire, pen = self.controller()
        session.set_pen(False, pen)
        energized_at = session.coil_on_at
        # Make the ON deadline expire before the command's own 8 s timeout.
        self.clock.now += 3
        original_write = wire.write

        def missing_off_ack(data):
            if data == b'M9\n':
                wire.history.append(data)
                return len(data)
            return original_write(data)

        wire.write = missing_off_ack
        owner.jobs.put(('pen_down_hold', {'pen': pen}))
        owner.jobs.put((None, {}))
        owner.run()
        elapsed = self.clock.now - energized_at
        self.assertLessEqual(elapsed, DLC32_MAX_ON_SECONDS + .15)
        self.assertIn(b'M9\n', wire.history)
        self.assertIn(b'\x18', wire.history)
        self.assertFalse(owner.connected)
        self.assertTrue(wire.closed)
        errors = [value for name, value in list(owner.events.queue) if name == 'error']
        self.assertEqual(len(errors), 1)
        self.assertIn('Истекло время питания соленоида', errors[0]['message'])

    def test_explicit_continuous_mode_does_not_inherit_short_pulse_budget(self):
        _, session, wire, pen = self.controller()
        continuous = PenLiftSettings.from_value(dict(pen.as_dict(), continuous=True))
        session.set_pen(False, continuous)
        self.clock.now += 3
        self.assertEqual(session.status()[0], 'Idle')
        self.assertNotIn(b'\x18', wire.history)


if __name__ == '__main__':
    unittest.main()
