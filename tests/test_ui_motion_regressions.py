"""Offline regressions for UI axis roles; never opens a hardware transport."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from controller import Controller
from machine_profiles import UNO, DLC32, PROFILE_LABELS, app_profile


class MotionAxisUiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        with patch("app.Controller", side_effect=lambda **kw: Controller(start_worker=False, **kw)):
            self.app = App(self.root, data_dir=None)
        self.root.update()

    def tearDown(self):
        self.app.close()

    def test_new_fluidnc_window_uses_logical_x_for_carriage(self):
        self.assertEqual(app_profile(self.app), DLC32)
        x, y = self.app.axes().machine((1, 0))
        self.assertEqual(y, 0, "Horizontal UI motion must address logical X (carriage), not logical Y (portal)")
        self.assertEqual(abs(x), 1)

    def test_up_arrow_targets_logical_portal_axis(self):
        with patch.object(self.app, "submit", return_value=True) as submit:
            self.app.jog(0, -1)
        action = submit.call_args.args[0]
        delta = submit.call_args.kwargs["delta"]
        self.assertEqual(action, "jog")
        x, y = self.app.axes().machine(delta)
        self.assertEqual(x, 0, "Up/down must address logical Y (portal), not logical X (carriage)")
        self.assertEqual(abs(y), 1)

    def test_switching_profiles_uses_role_defaults_and_remembers_user_directions(self):
        self.app.reverse_right.set(False)
        self.app.reverse_down.set(True)
        self.app.machine_profile.set(PROFILE_LABELS[UNO])
        self.app.change_machine_profile()
        self.assertEqual((self.app.swap.get(), self.app.reverse_right.get(), self.app.reverse_down.get()),
                         (True, True, False))
        self.app.swap.set(False)
        self.app.reverse_down.set(True)
        self.app.machine_profile.set(PROFILE_LABELS[DLC32])
        self.app.change_machine_profile()
        self.assertEqual((self.app.swap.get(), self.app.reverse_right.get(), self.app.reverse_down.get()),
                         (False, False, True))
        self.app.machine_profile.set(PROFILE_LABELS[UNO])
        self.app.change_machine_profile()
        self.assertEqual((self.app.swap.get(), self.app.reverse_right.get(), self.app.reverse_down.get()),
                         (False, True, True))

    def test_reconnection_does_not_overwrite_user_axis_choices(self):
        self.app.swap.set(True)
        self.app.reverse_right.set(False)
        self.app.reverse_down.set(True)
        expected = self.app.axes()
        with patch.object(self.app, "submit", return_value=True) as submit:
            self.app.connect()
            self.app.connected = True
            self.app.connect()
            self.app.connected = False
            self.app.connect()
        self.assertEqual([c.args[0] for c in submit.call_args_list], ["connect", "disconnect", "connect"])
        self.assertEqual(self.app.axes(), expected)

    def test_disconnect_controls_are_updated_before_the_error_dialog_opens(self):
        self.app.connected = True
        with patch.object(self.app, "machine_ready", return_value=True), \
                patch.object(self.app, "pen_ready", return_value=True):
            self.app.update_controls()
        self.assertTrue(all(str(button["state"]) == "normal" for button in self.app.jog_buttons))
        self.root.after_cancel(self.app.pump_timer)
        self.app.pump_timer = None
        self.app.controller.events.put(("disconnected", None))
        self.app.controller.events.put(("error", "Offline transport failure fixture"))
        snapshots = []
        def observe_dialog(*args, **kwargs):
            snapshots.append((self.app.connected, str(self.app.connect_button["text"]),
                              tuple(str(button["state"]) for button in self.app.jog_buttons)))
        with patch("app.messagebox.showerror", side_effect=observe_dialog):
            self.app.pump()
        self.assertEqual(snapshots, [(False, "Подключить", ("disabled",) * 4)])


if __name__ == "__main__":
    unittest.main()
