"""Desktop workflow with the built-in simulator, never a hardware transport."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from controller import Controller, GrblSession
from fluidnc_setup import generate_config
from geometry import Sheet
from machine_profiles import (DLC32, PROFILE_LABELS, fluidnc_configuration,
                              fluidnc_pen_profile, settings_ready)
from test_controller import ProtocolWire, DEFAULT_CONFIG
from test_wiring_setup import commissioned_parameters
from tk_test_support import close_tk_app
import yaml


class DesktopTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, data_dir=None)
        # Flush initial theme/icon callbacks while this hidden root is alive.
        self.root.update()

    def tearDown(self):
        close_tk_app(self)

    def test_close_finishes_worker_before_destroying_tk(self):
        entered, release = threading.Event(), threading.Event()

        def execute(action, params):
            entered.set()
            release.wait(2)

        controller = self.app.controller
        with patch.object(controller, "execute", side_effect=execute):
            self.assertTrue(controller.submit("offline_fixture"))
            self.assertTrue(entered.wait(1))
            # Fake queue job; no transport or controller command is opened.
            timer = threading.Timer(.1, release.set)
            timer.daemon = True
            timer.start()
            destroyed_after_worker = []
            original_destroy = self.root.destroy

            def destroy():
                destroyed_after_worker.append(not controller.worker.is_alive())
                original_destroy()

            try:
                with patch("app.messagebox.askokcancel", return_value=True), \
                        patch.object(self.root, "destroy", side_effect=destroy):
                    self.app.close()
            finally:
                release.set()
                timer.join(1)
        self.assertEqual(destroyed_after_worker, [True])
        self.assertFalse(controller.worker.is_alive())

    def wait_for(self, predicate, timeout=8):
        stop = time.monotonic() + timeout
        while time.monotonic() < stop:
            self.root.update()
            if predicate():
                return
            time.sleep(.02)
        self.fail('Desktop workflow timed out: ' + self.app.status.get())

    def test_new_window_is_offline_and_does_not_assume_relay_polarity(self):
        self.assertEqual(self.app.port.get(), 'DEMO')
        self.assertFalse(self.app.connected)
        self.assertEqual(self.app.pen_active.get(), 'Не проверено')
        self.assertFalse(self.app.pen_confirmed.get())
        self.assertFalse(self.app.pen_continuous.get())
        self.assertTrue(self.app.pen_power_raises.get())
        self.assertEqual(str(self.app.draw_button['state']), 'disabled')
        self.assertEqual(str(self.app.load_button['state']), 'normal')
        self.assertEqual((self.app.draft_sheet.width, self.app.draft_sheet.height), (210, 297))
        self.assertTrue(all(str(w['state']) == 'disabled' for w in self.app.jog_buttons))

    def test_editor_and_cyrillic_paths_still_work_without_a_machine(self):
        from editor_tools.text_paths import generate_text_paths
        from geometry import Sheet, validate_paths
        self.app.open_editor()
        self.app.editor_window.root.withdraw()
        self.root.update()
        self.assertIsNotNone(self.app.editor_window)
        self.assertFalse(self.app.connected)
        text = generate_text_paths('Привет, CNC!', height_mm=3, origin=(5, 5))
        self.assertGreater(len(text.paths), 10)
        validate_paths(text.paths, Sheet(210, 297))

    def fluidnc_idle_view(self, *, commissioning):
        """Populate only the UI view model; no connection or hardware action."""
        config = generate_config(commissioned_parameters(
            endstops='none', no_endstops_confirmed=True))
        config['meta'] = ('NEIROSLOPPER_COMMISSIONING directions and scale unverified'
                          if commissioning else 'Offline TEST fixture')
        self.app.settings = fluidnc_configuration(yaml.safe_dump(config))
        self.app.pen_firmware = fluidnc_pen_profile(self.app.settings)
        self.app.machine_profile.set(PROFILE_LABELS[DLC32])
        self.app.connected = True
        self.app.simulation = False
        self.app.position = (0.0, 0.0)
        self.app.status.set('Idle · offline test fixture')
        self.app.pen_enabled.set(True)
        self.app.pen_active.set('LOW')
        self.app.pen_power_raises.set(True)
        self.app.pen_continuous.set(False)
        self.app.pen_confirmed.set(True)
        self.app.controller.cancelled.clear()
        self.app.update_controls()
        self.assertEqual(self.root.state(), 'withdrawn')
        self.assertIsNone(self.app.controller.session)

    def test_commissioning_xy_blocks_motion_but_verified_relay_controls_are_enabled(self):
        self.fluidnc_idle_view(commissioning=True)
        self.assertFalse(settings_ready(self.app.settings, DLC32))
        self.assertTrue(self.app.pen_ready())
        for button in (self.app.pen_up_button, self.app.pen_pulse_button):
            self.assertEqual(str(button['state']), 'normal')
        for button in (*self.app.jog_buttons, self.app.first_button,
                       self.app.draw_button, self.app.startpoint_button):
            self.assertEqual(str(button['state']), 'disabled')

    def test_test_profile_enables_jog_then_drawing_only_after_both_corners_and_paths(self):
        self.fluidnc_idle_view(commissioning=False)
        self.assertTrue(settings_ready(self.app.settings, DLC32))
        self.assertTrue(all(str(button['state']) == 'normal' for button in self.app.jog_buttons))
        self.assertEqual(str(self.app.first_button['state']), 'normal')
        self.assertEqual(str(self.app.second_button['state']), 'disabled')
        self.assertEqual(str(self.app.draw_button['state']), 'disabled')
        self.assertEqual(str(self.app.startpoint_button['state']), 'disabled')

        # A drawing alone cannot replace the physical two-corner calibration.
        self.app.paths = [[(2.0, 2.0), (5.0, 5.0)]]
        self.app.zero = True
        self.app.update_controls()
        self.assertEqual(str(self.app.second_button['state']), 'normal')
        self.assertEqual(str(self.app.draw_button['state']), 'disabled')
        self.assertEqual(str(self.app.startpoint_button['state']), 'disabled')

        self.app.sheet = Sheet(20, 20)
        self.app.paths = []
        self.app.update_controls()
        self.assertEqual(str(self.app.draw_button['state']), 'disabled')
        self.assertEqual(str(self.app.startpoint_button['state']), 'disabled')

        self.app.paths = [[(2.0, 2.0), (5.0, 5.0)]]
        self.app.update_controls()
        self.assertEqual(str(self.app.draw_button['state']), 'normal')
        self.assertEqual(str(self.app.startpoint_button['state']), 'normal')
        self.assertIsNone(self.app.controller.session)

    def test_demo_calibration_drawing_and_stop_preserve_workflow(self):
        self.app.pen_enabled.set(False)
        self.app.connect()
        self.wait_for(lambda: self.app.connected and not self.app.controller.busy)
        self.assertTrue(self.app.simulation)
        self.app.set_zero()
        self.wait_for(lambda: self.app.zero and not self.app.controller.busy)
        self.app.jog(10, 10)
        self.wait_for(lambda: not self.app.controller.busy)
        self.app.set_corner()
        self.wait_for(lambda: self.app.sheet is not None and not self.app.controller.busy)
        self.assertAlmostEqual(self.app.sheet.width, 10)
        self.assertAlmostEqual(self.app.sheet.height, 10)
        self.app.load_test()
        self.assertTrue(self.app.paths)
        with patch('app.messagebox.askokcancel', return_value=True):
            self.app.draw()
        self.wait_for(lambda: not self.app.controller.busy)
        self.assertTrue(self.app.controller.session.wire.ink_moves)
        self.app.stop()
        self.assertFalse(self.app.zero)
        self.assertTrue(self.app.paths)


class DiagnosticLifecycleTests(unittest.TestCase):
    def test_app_shutdown_after_inspection_does_not_send_reset(self):
        wire = ProtocolWire(config=DEFAULT_CONFIG)
        session = GrblSession(wire, threading.Event(), lambda *a: None, DLC32)
        session.initialize()
        controller = Controller(start_worker=False)
        controller.session = session
        controller.connected = True
        controller.shutdown()
        self.assertTrue(wire.closed)
        self.assertNotIn(b'\x18', wire.history)


if __name__ == '__main__':
    unittest.main()
