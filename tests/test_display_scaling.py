"""Initial display scaling with fake Windows APIs, never process DPI changes."""
import ctypes
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from display_scaling import configure_process_dpi, ui_scale, scale_widget_tree, scale_toplevel


class ProcessDpiTests(unittest.TestCase):
    def test_modern_mode_is_requested_before_any_ui_without_legacy_calls(self):
        modern, legacy, old = Mock(return_value=1), Mock(), Mock()
        result = configure_process_dpi(platform="win32", user32=SimpleNamespace(
            SetProcessDpiAwarenessContext=modern, SetProcessDPIAware=legacy),
            shcore=SimpleNamespace(SetProcessDpiAwareness=old))
        self.assertEqual(result, "per-monitor-v2")
        self.assertEqual(modern.call_count, 1)
        self.assertEqual(modern.call_args.args[0].value, ctypes.c_void_p(-4).value)
        legacy.assert_not_called()
        old.assert_not_called()

    def test_existing_host_awareness_is_respected(self):
        legacy, old = Mock(), Mock()
        with patch("display_scaling.ctypes.get_last_error", return_value=5):
            result = configure_process_dpi(platform="win32", user32=SimpleNamespace(
                SetProcessDpiAwarenessContext=Mock(return_value=0), SetProcessDPIAware=legacy),
                shcore=SimpleNamespace(SetProcessDpiAwareness=old))
        self.assertEqual(result, "already-set")
        legacy.assert_not_called()
        old.assert_not_called()

    def test_unsupported_modern_context_falls_back_to_supported_older_api(self):
        old, legacy = Mock(return_value=0), Mock()
        with patch("display_scaling.ctypes.get_last_error", return_value=87):
            result = configure_process_dpi(platform="win32", user32=SimpleNamespace(
                SetProcessDpiAwarenessContext=Mock(return_value=0), SetProcessDPIAware=legacy),
                shcore=SimpleNamespace(SetProcessDpiAwareness=old))
        self.assertEqual(result, "per-monitor")
        old.assert_called_once_with(2)
        legacy.assert_not_called()

    def test_unrelated_failure_does_not_try_changing_awareness_again(self):
        old, legacy = Mock(), Mock()
        with patch("display_scaling.ctypes.get_last_error", return_value=8):
            result = configure_process_dpi(platform="win32", user32=SimpleNamespace(
                SetProcessDpiAwarenessContext=Mock(return_value=0), SetProcessDPIAware=legacy),
                shcore=SimpleNamespace(SetProcessDpiAwareness=old))
        self.assertEqual(result, "unchanged")
        old.assert_not_called()
        legacy.assert_not_called()

    def test_missing_apis_allow_legacy_but_non_windows_calls_none(self):
        legacy = Mock(return_value=1)
        user32 = SimpleNamespace(SetProcessDPIAware=legacy)
        self.assertEqual(configure_process_dpi(platform="win32", user32=user32, shcore=SimpleNamespace()), "system")
        legacy.assert_called_once_with()
        legacy.reset_mock()
        self.assertEqual(configure_process_dpi(platform="linux", user32=user32), "unsupported")
        legacy.assert_not_called()

    def test_call_exception_does_not_bypass_existing_awareness(self):
        denied = OSError("host context already chosen")
        denied.winerror = 5
        old, legacy = Mock(), Mock()
        result = configure_process_dpi(platform="win32", user32=SimpleNamespace(
            SetProcessDpiAwarenessContext=Mock(side_effect=denied), SetProcessDPIAware=legacy),
            shcore=SimpleNamespace(SetProcessDpiAwareness=old))
        self.assertEqual(result, "already-set")
        old.assert_not_called()
        legacy.assert_not_called()


class ScaleFactorTests(unittest.TestCase):
    def test_initial_dpi_ratio_rounding_and_bounds(self):
        for dpi, expected in ((96, 1), (192, 2), (191.9, 2), (144, 1.5), (72, 1), (600, 4), (float("nan"), 1)):
            with self.subTest(dpi=dpi):
                self.assertEqual(ui_scale(SimpleNamespace(winfo_fpixels=lambda value: dpi)), expected)


class WidgetScaleTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()
        self.root = None

    def test_pixel_layout_scales_once_but_character_widths_and_fonts_do_not(self):
        from tkinter import ttk
        frame = ttk.Frame(self.root, width=100, height=80, padding=(4, 6))
        frame.pack(padx=3, pady=(2, 5), ipadx=1)
        canvas = self.tk.Canvas(frame, width=90, height=50)
        canvas.grid(row=0, column=0, padx=2)
        entry = ttk.Entry(frame, width=12)
        entry.grid(row=1, column=0)
        label = ttk.Label(frame, text="text", wraplength=100, font=("Segoe UI", 10))
        label.grid(row=2, column=0)
        text = self.tk.Text(frame, width=20, height=3, padx=3, pady=4)
        text.grid(row=3, column=0)
        font = label.cget("font")
        with patch("display_scaling.ui_scale", return_value=2):
            scale_widget_tree(self.root)
            scale_widget_tree(self.root)
        self.assertEqual(int(frame.cget("width")), 200)
        self.assertEqual(tuple(int(str(value)) for value in frame.tk.splitlist(frame.cget("padding"))), (8, 12))
        self.assertEqual(int(canvas.cget("width")), 180)
        self.assertEqual(int(canvas.cget("height")), 100)
        self.assertEqual(int(entry.cget("width")), 12)
        self.assertEqual(int(text.cget("width")), 20)
        self.assertEqual(int(text.cget("height")), 3)
        self.assertEqual(int(text.cget("padx")), 6)
        self.assertEqual(int(text.cget("pady")), 8)
        self.assertEqual(int(label.cget("wraplength")), 200)
        self.assertEqual(label.cget("font"), font)
        self.assertEqual(int(frame.pack_info()["padx"]), 6)
        self.assertEqual(tuple(map(int, frame.pack_info()["pady"])), (4, 10))
        self.assertEqual(int(canvas.grid_info()["padx"]), 4)

    def test_owned_toplevel_has_independent_one_time_geometry_and_widget_scaling(self):
        window = self.tk.Toplevel(self.root)
        window.withdraw()
        window.geometry("300x200+10+20")
        window.minsize(200, 100)
        canvas = self.tk.Canvas(window, width=50, height=40)
        canvas.pack()
        self.root.update_idletasks()
        with patch("display_scaling.ui_scale", return_value=2), \
                patch.object(window, "geometry", wraps=window.geometry) as geometry:
            scale_widget_tree(self.root)
            self.assertEqual(int(canvas.cget("width")), 50)
            scale_toplevel(window)
            scale_toplevel(window)
            geometry.assert_any_call("600x400+10+20")
            self.assertEqual(geometry.call_count, 2)
        self.root.update_idletasks()
        self.assertEqual(window.minsize(), (400, 200))
        self.assertEqual(int(canvas.cget("width")), 100)
        window.destroy()

    def test_style_only_padding_stays_unset_on_widget(self):
        from tkinter import ttk
        style = ttk.Style(self.root)
        style.configure("DpiTest.TButton", padding=(6, 10))
        button = ttk.Button(self.root, text="Action", style="DpiTest.TButton")
        button.pack()
        self.assertEqual(str(button.cget("padding")), "")
        with patch("display_scaling.ui_scale", return_value=2):
            scale_widget_tree(self.root)
        self.assertEqual(str(button.cget("padding")), "")
        self.assertEqual(tuple(int(str(value)) for value in style.lookup("DpiTest.TButton", "padding")), (6, 10))

    def test_prescaled_dialog_geometry_still_scales_unscaled_widgets(self):
        window = self.tk.Toplevel(self.root)
        window.withdraw()
        window._neiroslopper_geometry_scaled = True
        canvas = self.tk.Canvas(window, width=50, height=40)
        canvas.pack()
        with patch("display_scaling.ui_scale", return_value=2), \
                patch.object(window, "geometry", wraps=window.geometry) as geometry:
            scale_toplevel(window)
            scale_toplevel(window)
            geometry.assert_not_called()
        self.assertEqual(int(canvas.cget("width")), 100)
        window.destroy()


if __name__ == "__main__":
    unittest.main()
