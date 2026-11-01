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
from machine_profiles import DLC32
from test_controller import ProtocolWire, DEFAULT_CONFIG


class DesktopTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, data_dir=None)

    def tearDown(self):
        if not self.app.closed:
            self.app.close()

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
        self.root.update()
        self.assertIsNotNone(self.app.editor_window)
        self.assertFalse(self.app.connected)
        text = generate_text_paths('Привет, CNC!', height_mm=3, origin=(5, 5))
        self.assertGreater(len(text.paths), 10)
        validate_paths(text.paths, Sheet(210, 297))

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
