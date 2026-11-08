"""Workspace layout and optional diagnostics, without a hardware transport."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from controller import Controller
from geometry import Sheet
from tk_test_support import close_tk_app


def inside(widget, ancestor):
    """Check ownership rather than a particular packing or pixel arrangement."""
    while widget is not None:
        if widget is ancestor:
            return True
        widget = getattr(widget, "master", None)
    return False


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class WorkspaceLayoutTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        with patch("app.Controller", side_effect=lambda **kw: Controller(start_worker=False, **kw)), \
                patch("serial.tools.list_ports.comports", return_value=[]):
            self.app = App(self.root, data_dir=None)
        self.root.update()

    def tearDown(self):
        self.app.controller.busy = False
        close_tk_app(self)

    def test_manual_controls_and_settings_are_separate_from_drawing_workflow(self):
        tabs = self.app.sidebar_tabs
        self.assertEqual([tabs.tab(tab, "text") for tab in tabs.tabs()],
                         ["Тестирование", "Настройки"])
        for button in self.app.jog_buttons:
            self.assertTrue(inside(button, self.app.test_panel))
            self.assertFalse(inside(button, self.app.workflow_panel))
        self.assertTrue(inside(self.app.configure_button, self.app.settings_panel))
        self.assertTrue(inside(self.app.corners_panel, self.app.workflow_panel))
        for button in (self.app.first_button, self.app.second_button):
            self.assertTrue(inside(button, self.app.corners_panel))
            self.assertFalse(inside(button, self.app.test_panel))
        for button in (self.app.load_button, self.app.draw_button):
            self.assertTrue(inside(button, self.app.workflow_panel))

    def test_main_window_has_no_numbered_step_banner(self):
        texts = []
        for widget in descendants(self.root):
            if "text" in widget.keys():
                texts.append(str(widget.cget("text")))
        self.assertFalse(any("Задайте лист" in text and "Подготовьте рисунок" in text
                             for text in texts))
        self.assertFalse(str(self.app.draw_button.cget("text")).lstrip().startswith("4"))

    def test_opening_diagnostics_only_changes_visibility(self):
        self.assertEqual(self.app.diagnostics_frame.winfo_manager(), "")
        self.app.zero = True
        self.app.sheet = Sheet(120, 80)
        self.app.paths = [[(1.0, 2.0), (3.0, 4.0)]]
        paper, paths = self.app.sheet, self.app.paths
        queued = self.app.controller.jobs.qsize()
        cancelled = self.app.controller.cancelled.is_set()
        self.app.toggle_diagnostics()
        self.root.update_idletasks()
        self.assertNotEqual(self.app.diagnostics_frame.winfo_manager(), "")
        self.app.toggle_diagnostics()
        self.root.update_idletasks()
        self.assertEqual(self.app.diagnostics_frame.winfo_manager(), "")
        self.assertTrue(self.app.zero)
        self.assertIs(self.app.sheet, paper)
        self.assertIs(self.app.paths, paths)
        self.assertEqual(self.app.controller.jobs.qsize(), queued)
        self.assertEqual(self.app.controller.cancelled.is_set(), cancelled)
        self.assertIsNone(self.app.controller.session)
        self.assertFalse(self.app.connected)

    def test_help_reuses_window_and_keeps_stop_shortcut_without_machine_actions(self):
        self.app.open_help()
        window = self.app.help_window
        self.app.open_help()
        self.assertIs(self.app.help_window, window)
        self.assertTrue(window.bind("<Escape>"))
        self.assertEqual(self.app.controller.jobs.qsize(), 0)
        self.assertIsNone(self.app.controller.session)
        window.destroy()
        self.app.open_help()
        self.assertIsNot(self.app.help_window, window)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_key_actions_fit_minimum_window_with_optional_journal(self):
        self.root.geometry("1100x700")
        self.root.deiconify()
        for mode in ("Демо", "USB", "Wi-Fi"):
            self.app.connection_mode.set(mode)
            self.app.change_connection_mode()
            for journal in (False, True):
                with self.subTest(mode=mode, journal=journal):
                    if bool(self.app.diagnostics_frame.winfo_manager()) != journal:
                        self.app.toggle_diagnostics()
                    self.root.update()
                    for widget in (self.app.stop_button, self.app.connect_button,
                                   self.app.first_button, self.app.second_button,
                                   self.app.startpoint_button, self.app.draw_button):
                        self.assertTrue(widget.winfo_ismapped())
                        x = widget.winfo_rootx() - self.root.winfo_rootx()
                        y = widget.winfo_rooty() - self.root.winfo_rooty()
                        self.assertGreaterEqual(x, 0)
                        self.assertGreaterEqual(y, 0)
                        self.assertLessEqual(x + widget.winfo_width(), self.root.winfo_width())
                        self.assertLessEqual(y + widget.winfo_height(), self.root.winfo_height())
                    self.assertGreater(self.app.canvas.winfo_height(), 30)


if __name__ == "__main__":
    unittest.main()
