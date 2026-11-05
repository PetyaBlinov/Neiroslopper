"""Explicit connections with fake discovery: no hardware or network."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from controller import Controller
from machine_profiles import UNO, DLC32, PROFILE_LABELS, app_profile


def device(label="Neiroslopper", endpoint="tcp://192.168.0.1:23"):
    return SimpleNamespace(label=label, endpoint=endpoint)


class ConnectionUiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        ports = patch("serial.tools.list_ports.comports", return_value=[SimpleNamespace(device="COM5")])
        ports.start()
        self.addCleanup(ports.stop)
        with patch("app.Controller", side_effect=lambda **kw: Controller(start_worker=False, **kw)):
            self.app = App(self.root, data_dir=None)
        self.root.update()
        self.releases = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        self.app.cancel_wifi_search()
        if self.app.wifi_search_thread is not None:
            self.app.wifi_search_thread.join(1)
        self.app.controller.busy = False
        if not self.app.closed:
            self.app.close()

    def mode(self, value):
        self.app.connection_mode.set(value)
        self.app.change_connection_mode()

    def finish_search(self):
        self.app.wifi_search_thread.join(1)
        self.assertFalse(self.app.wifi_search_thread.is_alive())
        self.app.pump_wifi_results()
        self.app.update_controls()

    def test_startup_demo_never_searches_network_or_inherits_hardware_target(self):
        with patch("app.discover_fluidnc") as discover:
            self.app.update_controls()
        discover.assert_not_called()
        self.assertEqual(self.app.connection_mode.get(), "Демо")
        self.assertEqual(tuple(self.app.ports["values"]), ("COM5",))
        self.app.port.set("COM5")
        with patch.object(self.app, "submit", return_value=True) as submit:
            self.app.connect()
        self.assertEqual(submit.call_args.kwargs["port"], "DEMO")

    def test_usb_uses_selected_com_and_has_no_demo_fallback(self):
        self.mode("USB")
        self.app.port.set("tcp://192.168.0.1:23")
        with patch.object(self.app, "submit", return_value=True) as submit:
            self.app.connect()
        self.assertEqual(submit.call_args.kwargs["port"], "COM5")
        self.assertEqual(str(self.app.ports["state"]), "readonly")
        self.app.usb_port.set("")
        self.app.select_connection_target()
        with patch.object(self.app, "submit") as submit:
            self.app.connect()
        submit.assert_not_called()
        self.assertEqual(str(self.app.connect_button["state"]), "disabled")

    def test_wifi_switches_uno_profile_without_starting_search(self):
        self.app.machine_profile.set(PROFILE_LABELS[UNO])
        self.app.change_machine_profile()
        with patch("app.discover_fluidnc") as discover:
            self.mode("Wi-Fi")
        discover.assert_not_called()
        self.assertEqual(app_profile(self.app), DLC32)
        self.assertIn("Сначала подключите ноутбук", self.app.connection_help.get())

    def test_background_discovery_needs_separate_connect_and_remembers_only_success(self):
        self.mode("Wi-Fi")
        seen = []
        def discover(cancel, **kwargs):
            seen.append(threading.get_ident())
            return [device()]
        with patch("app.discover_fluidnc", side_effect=discover), patch.object(self.app, "submit") as submit, \
                patch("app.remember_verified_endpoint") as remember:
            self.app.find_wifi_boards()
            self.finish_search()
            submit.assert_not_called()
            remember.assert_not_called()
            self.assertNotEqual(seen, [threading.get_ident()])
            self.assertEqual(str(self.app.wifi_boards["state"]), "readonly")
            self.app.connect()
            self.assertEqual(submit.call_args.kwargs["port"], "tcp://192.168.0.1:23")
            self.app.controller.events.put(("connected", "tcp://192.168.0.1:23"))
            self.root.after_cancel(self.app.pump_timer)
            self.app.pump_timer = None
            self.app.pump()
            remember.assert_called_once_with(None, "tcp://192.168.0.1:23")

    def test_multiple_boards_require_user_selection(self):
        self.mode("Wi-Fi")
        with patch("app.discover_fluidnc", return_value=[device(), device(endpoint="tcp://192.168.1.8:23")]):
            self.app.find_wifi_boards()
            self.finish_search()
        self.assertEqual(len(self.app.wifi_devices), 2)
        self.assertEqual(self.app.wifi_target.get(), "")
        self.assertEqual(str(self.app.connect_button["state"]), "disabled")
        self.app.wifi_target.set(list(self.app.wifi_devices)[1])
        self.app.select_connection_target()
        self.assertEqual(self.app.port.get(), "tcp://192.168.1.8:23")

    def test_empty_or_failed_search_clears_previous_target(self):
        self.mode("Wi-Fi")
        for result, error in (([device()], None), ([], None), (None, OSError("offline fixture"))):
            with patch("app.discover_fluidnc", return_value=result, side_effect=error):
                self.app.find_wifi_boards()
                self.finish_search()
            if not result:
                self.assertFalse(self.app.wifi_devices)
                self.assertEqual(self.app.port.get(), "")
                self.assertEqual(str(self.app.connect_button["state"]), "disabled")

    def test_search_blocks_connection_and_mode_change_discards_late_result(self):
        self.mode("Wi-Fi")
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        cancellations = []
        def discover(cancel, **kwargs):
            cancellations.append(cancel)
            entered.set()
            release.wait(1)
            return [device()]
        with patch("app.discover_fluidnc", side_effect=discover), patch.object(self.app, "submit") as submit:
            self.app.find_wifi_boards()
            self.assertTrue(entered.wait(.5))
            self.app.connect()
            submit.assert_not_called()
            self.mode("Демо")
            self.assertTrue(cancellations[0].is_set())
            release.set()
            self.finish_search()
        self.assertEqual(self.app.port.get(), "DEMO")
        self.assertFalse(self.app.wifi_devices)

    def test_connected_or_busy_locks_method_profile_and_target(self):
        self.mode("USB")
        for connected, busy in ((True, False), (False, True)):
            self.app.connected, self.app.controller.busy = connected, busy
            self.app.update_controls()
            self.assertTrue(all(str(button["state"]) == "disabled" for button in self.app.connection_mode_buttons))
            self.assertEqual(str(self.app.profile_input["state"]), "disabled")
            self.assertEqual(str(self.app.ports["state"]), "disabled")
            self.mode("Демо")
            self.assertEqual(self.app.connection_mode.get(), "USB")

    def test_close_cancels_search_without_worker_touching_tk(self):
        self.mode("Wi-Fi")
        entered = threading.Event()
        def discover(cancel, **kwargs):
            entered.set()
            cancel.wait(1)
            return [device()]
        with patch("app.discover_fluidnc", side_effect=discover):
            self.app.find_wifi_boards()
            self.assertTrue(entered.wait(.5))
            self.app.close()
            self.app.wifi_search_thread.join(1)
        self.assertFalse(self.app.wifi_search_thread.is_alive())
        self.assertTrue(self.app.wifi_search_cancel.is_set())

    def test_missing_extended_discovery_has_clear_fallback_message(self):
        with patch("app.MDNS_AVAILABLE", False):
            self.mode("Wi-Fi")
            self.assertIn("расширенный поиск", self.app.wifi_status.get())
            with patch("app.discover_fluidnc", return_value=[]):
                self.app.find_wifi_boards()
                self.finish_search()
            self.assertIn("Расширенный поиск", self.app.wifi_status.get())


if __name__ == "__main__":
    unittest.main()
