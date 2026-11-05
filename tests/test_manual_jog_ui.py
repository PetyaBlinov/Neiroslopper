"""Manual jog choices with a hidden Tk window; no serial connection or motion."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from controller import Controller
from geometry import Sheet
from machine_profiles import UNO, DLC32, PROFILE_LABELS


class ManualJogUiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        with patch("app.Controller", side_effect=lambda **kw: Controller(start_worker=False, **kw)):
            self.app = App(self.root, data_dir=None)
        self.root.update()

    def tearDown(self):
        self.app.controller.busy = False
        self.app.close()

    def test_default_is_motors_only_with_manual_nib_instruction(self):
        self.assertFalse(self.app.jog_lift.get())
        self.assertIn("только моторы", self.app.jog_lift_hint.get())
        self.assertIn("поднимите или выньте ручку вручную", self.app.jog_lift_hint.get())

    def test_fluidnc_lift_displays_ten_second_limit_without_enabling_continuous_mode(self):
        self.app.machine_profile.set(PROFILE_LABELS[DLC32])
        self.app.jog_lift.set(True)
        self.app.update_controls()
        self.assertIn("10 с", self.app.jog_lift_hint.get())
        self.assertIn("после остановки питание выключается", self.app.jog_lift_hint.get())
        self.assertIn("катушка до 10 с", self.app.pen_summary.get())
        self.assertNotIn("краткого питания", self.app.jog_lift_hint.get())
        self.assertIn("до 10 с", self.app.pen_legend.get())
        self.assertNotIn("В паузах охлаждения", self.app.pen_legend.get())
        self.assertFalse(self.app.pen_continuous.get())

    def test_uno_lift_keeps_its_existing_short_power_description(self):
        self.app.machine_profile.set(PROFILE_LABELS[UNO])
        self.app.jog_lift.set(True)
        self.app.update_controls()
        self.assertIn("краткого питания", self.app.jog_lift_hint.get())
        self.assertNotIn("10 с", self.app.jog_lift_hint.get())
        self.assertNotIn("10 с", self.app.pen_summary.get())
        self.assertIn("В паузах охлаждения", self.app.pen_legend.get())

    def test_explicit_continuous_mode_is_not_labelled_as_ten_second_mode(self):
        self.app.machine_profile.set(PROFILE_LABELS[DLC32])
        self.app.pen_continuous.set(True)
        self.app.jog_lift.set(True)
        self.app.update_controls()
        self.assertNotIn("10 с", self.app.jog_lift_hint.get())
        self.assertIn("непрерывный ход", self.app.pen_summary.get())

    def test_fluidnc_demo_keeps_the_selected_machine_power_description(self):
        self.app.machine_profile.set(PROFILE_LABELS[DLC32])
        self.app.connected, self.app.simulation = True, True
        self.app.jog_lift.set(True)
        self.app.update_controls()
        self.assertIn("10 с", self.app.jog_lift_hint.get())
        self.assertIn("катушка до 10 с", self.app.pen_summary.get())

    def test_fluidnc_drawing_confirmation_describes_whole_transfers_and_limit(self):
        self.app.machine_profile.set(PROFILE_LABELS[DLC32])
        self.app.zero, self.app.sheet = True, Sheet(20, 20)
        for power_raises in (False, True):
            self.app.pen_power_raises.set(power_raises)
            with patch("app.messagebox.askokcancel", return_value=False) as confirm:
                self.app.draw()
            text = confirm.call_args.args[1]
            self.assertIn("Переходы выполняются целиком", text)
            self.assertIn("10 с", text)
            self.assertNotIn("Будут паузы охлаждения", text)
        self.assertFalse(self.app.pen_continuous.get())

    def test_uno_drawing_confirmation_keeps_existing_guard_description(self):
        self.app.machine_profile.set(PROFILE_LABELS[UNO])
        self.app.zero, self.app.sheet = True, Sheet(20, 20)
        self.app.pen_power_raises.set(False)
        with patch("app.messagebox.askokcancel", return_value=False) as confirm:
            self.app.draw()
        text = confirm.call_args.args[1]
        self.assertIn("Будут паузы охлаждения", text)
        self.assertNotIn("10 с", text)

    def test_one_arrow_click_submits_one_job_with_selected_lift_policy(self):
        self.app.step.set("10")
        for selected in (False, True):
            with self.subTest(lift_pen=selected):
                self.app.jog_lift.set(selected)
                with patch.object(self.app.controller, "submit", return_value=True) as submit:
                    self.assertTrue(self.app.jog(0, -1))
                self.assertEqual(submit.call_count, 1)
                self.assertEqual(submit.call_args.args, ("jog",))
                self.assertEqual(submit.call_args.kwargs["delta"], (0, -10))
                self.assertIs(submit.call_args.kwargs["lift_pen"], selected)

    def test_direct_submit_from_display_inherits_checkbox_without_overriding_explicit_policy(self):
        for selected in (False, True):
            self.app.jog_lift.set(selected)
            with patch.object(self.app.controller, "submit", return_value=True) as submit:
                self.app.submit("jog", delta=(1, 0), feed=100)
            self.assertIs(submit.call_args.kwargs["lift_pen"], selected)
        with patch.object(self.app.controller, "submit", return_value=True) as submit:
            self.app.submit("jog", delta=(1, 0), feed=100, lift_pen=False)
        self.assertIs(submit.call_args.kwargs["lift_pen"], False)

    def test_rapid_arrow_invocations_queue_only_one_job_and_lock_checkbox(self):
        self.app.connected = True
        with patch.object(self.app, "machine_ready", return_value=True), \
                patch.object(self.app, "pen_ready", return_value=True):
            self.app.update_controls()
            self.app.jog_buttons[0].invoke()
            self.app.jog_buttons[0].invoke()
        self.assertEqual(self.app.controller.jobs.qsize(), 1)
        self.assertEqual(str(self.app.jog_lift_button["state"]), "disabled")
        self.assertTrue(all(str(button["state"]) == "disabled" for button in self.app.jog_buttons))

    def test_unconfirmed_enabled_relay_still_blocks_arrows(self):
        self.app.connected = True
        self.app.pen_enabled.set(True)
        self.app.pen_confirmed.set(False)
        with patch.object(self.app, "machine_ready", return_value=True):
            for selected in (False, True):
                self.app.jog_lift.set(selected)
                self.app.update_controls()
                self.assertTrue(all(str(button["state"]) == "disabled" for button in self.app.jog_buttons))

    def test_disabling_auto_pen_allows_ready_motor_test_and_clears_lift_checkbox(self):
        self.app.connected = True
        self.app.jog_lift.set(True)
        self.app.pen_enabled.set(False)
        with patch.object(self.app, "machine_ready", return_value=True):
            self.app.update_controls()
        self.assertFalse(self.app.jog_lift.get())
        self.assertEqual(str(self.app.jog_lift_button["state"]), "disabled")
        self.assertTrue(all(str(button["state"]) == "normal" for button in self.app.jog_buttons))

    def test_checkbox_does_not_change_relay_profile_or_paper_calibration(self):
        self.app.connected, self.app.zero = True, True
        before_pen = self.app.read_pen_settings()
        with patch.object(self.app, "machine_ready", return_value=True), \
                patch.object(self.app, "pen_ready", return_value=True):
            self.app.jog_lift_button.invoke()
        self.assertTrue(self.app.jog_lift.get())
        self.assertEqual(self.app.read_pen_settings(), before_pen)
        self.assertTrue(self.app.zero)
        self.assertEqual(str(self.app.jog_lift_button["state"]), "normal")

    def test_off_event_reports_power_without_claiming_physical_pen_direction(self):
        self.app.connected = True
        self.app.relay_state_unknown = True
        self.root.after_cancel(self.app.pump_timer)
        self.app.pump_timer = None
        self.app.controller.events.put(("pen", "off"))
        with patch.object(self.app, "machine_ready", return_value=True), \
                patch.object(self.app, "pen_ready", return_value=True):
            self.app.pump()
        self.assertIn("Питание соленоида выключено для ручного перемещения", self.app.pen_status.get())
        self.assertNotIn("Ручка внизу", self.app.pen_status.get())
        self.assertFalse(self.app.relay_state_unknown)
        self.assertEqual(self.app.dwin_pen_state, 0)


if __name__ == "__main__":
    unittest.main()
