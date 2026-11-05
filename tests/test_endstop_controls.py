"""Fresh limit-input diagnostics only; no real serial device is opened."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import endstop_summary
from controller import Controller, GrblSession
from machine_profiles import DLC32, UNO
from test_controller import PHYSICAL_CONFIG, ProtocolWire


def nc_configuration():
    config = yaml.safe_load(PHYSICAL_CONFIG)
    # Physical Y drives logical X. Physical X and Z drive the two-motor Y.
    x = config["axes"]["x"]["motor0"]
    x["stepstick"].update(step_pin="i2so.5", direction_pin="i2so.6")
    x.update(limit_neg_pin="gpio.36:high", hard_limits=True)
    y = config["axes"]["y"]["motor0"]
    y["stepstick"].update(step_pin="i2so.1", direction_pin="i2so.2")
    y.update(limit_neg_pin="gpio.35:high", hard_limits=True)
    config["axes"]["y"]["motor1"] = {
        "stepstick": {"step_pin": "i2so.3", "direction_pin": "i2so.4"}}
    return yaml.safe_dump(config)


class EndstopControlsTests(unittest.TestCase):
    def setUp(self):
        trace = patch("controller.SerialTrace")
        trace.start()
        self.addCleanup(trace.stop)

    def session(self, status="<Idle|MPos:10,20,0|WCO:2,3,0|FS:0,0>"):
        wire = ProtocolWire(config=nc_configuration())
        wire.status_line = status
        events = []
        session = GrblSession(wire, threading.Event(),
                              lambda *event: events.append(event), DLC32)
        session.initialize()
        wire.history.clear()
        events.clear()
        return session, wire, events

    def controller(self, session):
        controller = Controller(start_worker=False)
        controller.session = session
        controller.connected = True
        controller.machine_profile = DLC32
        return controller

    def test_active_inputs_are_reported_against_logical_axes(self):
        session, wire, events = self.session()
        wire.status_line = "<Alarm|MPos:10,20,0|Pn:XY|FS:0,0>"
        session.status()
        report = [value for kind, value in events if kind == "endstops"][-1]
        self.assertEqual(report["pins"], "XY")
        self.assertEqual(report["configured"], {"x": True, "y": True})
        self.assertEqual(report["state"], "Alarm")
        self.assertGreater(report["observed_at"], 0)
        self.assertEqual(wire.history, [b"?"])

    def test_absent_Pn_in_fresh_report_means_inputs_released(self):
        session, wire, events = self.session()
        session.active_pins = "XY"  # Old state must not survive the fresh report.
        session.status()
        report = [value for kind, value in events if kind == "endstops"][-1]
        self.assertEqual(report["pins"], "")
        self.assertEqual(wire.history, [b"?"])

    def test_check_action_is_read_only_even_with_unconfirmed_pen(self):
        session, wire, events = self.session()
        controller = self.controller(session)
        controller.execute("check_endstops", {"pen": {"unexpected": "ignored"}})
        self.assertEqual(wire.history, [b"?"])
        self.assertFalse(session.has_actuated)
        self.assertFalse(controller.zero)

    def test_diagnostic_check_does_not_require_motion_ready_configuration(self):
        session, wire, events = self.session()
        session.settings["_fluidnc_ready"] = False
        self.controller(session).execute("check_endstops", {})
        self.assertEqual(wire.history, [b"?"])
        self.assertTrue(any(kind == "endstops" for kind, _ in events))

    def test_fresh_query_replaces_cached_released_state(self):
        session, wire, events = self.session()
        self.assertEqual(session.active_pins, "")
        wire.status_line = "<Alarm|MPos:0,0,0|Pn:Y>"
        self.controller(session).execute("check_endstops", {})
        self.assertEqual(session.active_pins, "Y")
        self.assertEqual(wire.history, [b"?"])

    def test_boot_alarm_is_only_observed_never_automatically_unlocked(self):
        wire = ProtocolWire(config=nc_configuration())
        wire.status_line = "<Alarm|MPos:0,0,0|WCO:0,0,0>"
        wire.queue("ALARM:1")
        session = GrblSession(wire, threading.Event(), lambda *_: None, DLC32)
        session.initialize()
        self.assertEqual(session.state, "Alarm")
        self.assertEqual(wire.history, [b"$I\n", b"$Config/Dump\n", b"?"])
        wire.history.clear()
        self.controller(session).execute("check_endstops", {})
        self.assertEqual(wire.history, [b"?"])
        self.assertFalse(session.has_actuated)

    def test_unassigned_axis_does_not_claim_a_released_switch(self):
        session, _, _ = self.session()
        session.settings["_fluidnc_config"]["axes"]["x"]["motor0"]["limit_neg_pin"] = "NO_PIN"
        report = session.endstop_report()
        label = endstop_summary(report, report["observed_at"])
        self.assertIn("X: не назначен", label)
        self.assertIn("Y: отпущен", label)

    def test_stale_input_status_is_not_displayed_as_released(self):
        report = {"pins": "", "observed_at": 20, "configured": {"x": True, "y": True}}
        self.assertIn("отпущен", endstop_summary(report, 22))
        self.assertIn("устарели", endstop_summary(report, 24))
        self.assertNotIn("отпущен", endstop_summary(report, 24))

    def test_unknown_input_state_is_not_displayed_as_released(self):
        self.assertIn("ещё не получены", endstop_summary(None))
        self.assertIn("ещё не получены", endstop_summary({"pins": None}))

    def test_NC_active_label_mentions_broken_wire_as_well_as_switch(self):
        label = endstop_summary({"pins": "X", "observed_at": 20,
                                 "configured": {"x": True, "y": True}}, 20)
        self.assertIn("X: СРАБОТАЛ / ОБРЫВ", label)
        self.assertIn("Y: отпущен", label)

    def test_malformed_Pn_is_rejected_instead_of_silent_clear(self):
        session, wire, _ = self.session()
        wire.status_line = "<Idle|MPos:10,20,0|Pn:1>"
        with self.assertRaisesRegex(RuntimeError, "Некорректное состояние входов"):
            session.status()
        self.assertEqual(wire.history, [b"?"])

    def test_check_action_is_not_available_for_uno_or_demo(self):
        for profile, simulation in ((UNO, False), (DLC32, True)):
            with self.subTest(profile=profile, simulation=simulation):
                session, wire, _ = self.session()
                controller = self.controller(session)
                controller.machine_profile = profile
                controller.simulation = simulation
                with self.assertRaises(ValueError):
                    controller.execute("check_endstops", {})
                self.assertEqual(wire.history, [])


if __name__ == "__main__":
    unittest.main()
