"""Manual LAN choice complements discovery without opening any network/device."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from controller import Controller
from tk_test_support import close_tk_app


class WifiConnectionFlowTests(unittest.TestCase):
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
        self.app.svg_loading = False
        close_tk_app(self)

    def wifi(self):
        self.app.connection_mode.set("Wi-Fi")
        self.app.change_connection_mode()

    def choice(self):
        return (dict(self.app.wifi_devices), self.app.wifi_target.get(),
                tuple(self.app.wifi_boards["values"]), self.app.port.get(),
                tuple(self.app.remembered_wifi))

    def test_wifi_mode_and_manual_dialog_do_not_search_connect_or_remember(self):
        with patch("app.discover_fluidnc") as discovery, \
                patch("network_transport.TcpWire") as wire, \
                patch("app.remember_verified_endpoint") as remember, \
                patch.object(self.app.controller, "submit") as submit:
            self.wifi()
            with patch("app.ask_board_address", return_value="192.168.1.45") as ask:
                self.app.open_wifi_address()
        ask.assert_called_once()
        discovery.assert_not_called()
        wire.assert_not_called()
        remember.assert_not_called()
        submit.assert_not_called()
        self.assertEqual(self.app.connection_target(), "tcp://192.168.1.45:23")
        self.assertIn("по адресу", self.app.wifi_target.get())
        self.assertEqual(self.app.remembered_wifi, [])
        self.assertFalse(self.app.connected)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_supported_manual_forms_select_their_canonical_lan_endpoint(self):
        self.wifi()
        addresses = (
            ("192.168.1.45", "tcp://192.168.1.45:23"),
            ("fluidnc.local", "tcp://fluidnc.local:23"),
            ("plotter.local:2323", "tcp://plotter.local:2323"),
            ("tcp://192.168.2.42:2323", "tcp://192.168.2.42:2323"),
            ("tcp://[fd00::42]:23", "tcp://[fd00::42]:23"),
        )
        with patch("network_transport.TcpWire") as wire, patch("app.discover_fluidnc") as discovery:
            for address, expected in addresses:
                with self.subTest(address=address):
                    self.assertTrue(self.app.use_wifi_address(address))
                    self.assertEqual(self.app.connection_target(), expected)
                    self.assertEqual(self.app.port.get(), expected)
                    self.assertEqual(str(self.app.connect_button["state"]), "normal")
        wire.assert_not_called()
        discovery.assert_not_called()
        self.assertEqual(self.app.remembered_wifi, [])
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_bad_manual_address_is_rejected_atomically_without_losing_a_valid_choice(self):
        self.wifi()
        self.assertTrue(self.app.use_wifi_address("192.168.1.45"))
        before = self.choice()
        for address in (None, "", "8.8.8.8", "tcp://8.8.8.8:23", "http://192.168.1.45/",
                        "tcp://user:password@192.168.1.45:23", "tcp://192.168.1.45:23/path",
                        "192.168.1.45:0", "192.168.1.45:65536", "999.999.0.1"):
            with self.subTest(address=address), patch("network_transport.TcpWire") as wire:
                self.app.wifi_status.set("previous-status")
                self.assertFalse(self.app.use_wifi_address(address))
                self.assertEqual(self.choice(), before)
                self.assertTrue(self.app.wifi_status.get())
                self.assertNotEqual(self.app.wifi_status.get(), "previous-status")
                wire.assert_not_called()
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_cancelled_address_dialog_preserves_the_existing_choice(self):
        self.wifi()
        self.app.use_wifi_address("192.168.1.45")
        before = self.choice()
        with patch("app.ask_board_address", return_value=None), \
                patch.object(self.app, "use_wifi_address") as use:
            self.app.open_wifi_address()
        use.assert_not_called()
        self.assertEqual(self.choice(), before)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_manual_entry_is_locked_during_another_connection_or_operation(self):
        self.wifi()
        self.app.use_wifi_address("192.168.1.45")
        before = self.choice()
        cases = ((self.app, "connected", True),
                 (self.app.controller, "busy", True),
                 (self.app, "svg_loading", True),
                 (self.app, "closed", True),
                 (self.app, "wifi_searching", True))
        for owner, field, value in cases:
            with self.subTest(field=field), patch.object(owner, field, value), \
                    patch("app.ask_board_address") as ask:
                self.assertFalse(self.app.use_wifi_address("192.168.2.90"))
                self.app.open_wifi_address()
                ask.assert_not_called()
                self.assertEqual(self.choice(), before)
        with patch.object(self.app, "wifi_worker_active", return_value=True), \
                patch("app.ask_board_address") as ask:
            self.assertFalse(self.app.use_wifi_address("192.168.2.90"))
            self.app.open_wifi_address()
            ask.assert_not_called()
            self.assertEqual(self.choice(), before)

    def test_address_entry_requires_wifi_mode_and_does_not_change_usb_or_demo(self):
        for mode in ("Демо", "USB"):
            with self.subTest(mode=mode):
                self.app.connection_mode.set(mode)
                self.app.change_connection_mode()
                before = self.choice()
                with patch("app.ask_board_address") as ask:
                    self.assertFalse(self.app.use_wifi_address("192.168.1.45"))
                    self.app.open_wifi_address()
                ask.assert_not_called()
                self.assertEqual(self.app.connection_mode.get(), mode)
                self.assertEqual(self.choice(), before)
                self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_manual_choice_is_compatible_with_discovery_and_requires_explicit_connect(self):
        self.wifi()
        self.app.wifi_devices = {"Existing discovered plotter": "tcp://192.168.1.30:23"}
        self.app.wifi_boards["values"] = tuple(self.app.wifi_devices)
        self.assertTrue(self.app.use_wifi_address("192.168.1.45"))
        self.assertEqual(self.app.wifi_devices["Existing discovered plotter"], "tcp://192.168.1.30:23")
        self.assertEqual(self.app.connection_target(), "tcp://192.168.1.45:23")
        self.assertEqual(self.app.controller.jobs.qsize(), 0)
        with patch.object(self.app, "submit", return_value=True) as submit:
            self.app.connect()
        self.assertEqual(submit.call_count, 1)
        args, kwargs = submit.call_args
        self.assertEqual(args, ("connect",))
        self.assertEqual(kwargs["port"], "tcp://192.168.1.45:23")
        self.assertFalse(self.app.connected)
        self.assertEqual(self.app.remembered_wifi, [])

    def test_runtime_event_is_readonly_code_only_and_cleared_on_disconnect(self):
        from eventlog import EventLog
        before = (self.app.paths, self.app.preview_revision, self.app.zero, self.app.sheet,
                  self.app.position, dict(self.app.settings))
        with tempfile.TemporaryDirectory() as directory:
            self.app.eventlog = EventLog(directory)
            self.root.after_cancel(self.app.pump_timer)
            self.app.pump_timer = None
            self.app.controller.events.put(("board_runtime", {
                "firmware_uptime_ms": 125000, "firmware_reset_reason": 3,
                "private": "password=secret",
            }))
            self.app.pump()
            text = self.app.board_runtime_text.get()
            self.assertIn("2 мин 5 с", text)
            self.assertIn("перезапуска: 3", text)
            self.assertEqual((self.app.paths, self.app.preview_revision, self.app.zero,
                              self.app.sheet, self.app.position, dict(self.app.settings)), before)
            self.assertEqual(self.app.controller.jobs.qsize(), 0)
            self.assertFalse(self.app.connected)
            for invalid in (None, {}, {"firmware_uptime_ms": True, "firmware_reset_reason": 3},
                            {"firmware_uptime_ms": -1, "firmware_reset_reason": 3},
                            {"firmware_uptime_ms": 125000, "firmware_reset_reason": 256}):
                self.app.controller.events.put(("board_runtime", invalid))
                self.app.pump()
                self.assertEqual(self.app.board_runtime_text.get(), text)
            content = self.app.eventlog.path.read_text(encoding="ascii")
            rows = [json.loads(line) for line in content.splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event"], "board.runtime")
            self.assertEqual(rows[0]["firmware_uptime_ms"], 125000)
            self.assertEqual(rows[0]["firmware_reset_reason"], 3)
            self.assertNotIn("secret", content)
            self.app.controller.events.put(("disconnected", None))
            self.app.pump()
            self.assertIn("ещё не получены", self.app.board_runtime_text.get())
            self.assertEqual(self.app.controller.jobs.qsize(), 0)
            self.app.eventlog = EventLog(None)


if __name__ == "__main__":
    unittest.main()
