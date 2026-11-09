"""Supported connection methods and USB choices; no hardware is opened."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from controller import Controller
from serial_ports import usb_port_choices
from tk_test_support import close_tk_app


class SerialChoiceTests(unittest.TestCase):
    def test_usb_picker_excludes_known_wireless_services_in_any_metadata_field(self):
        ports = [
            SimpleNamespace(device="COM5", hwid="USB VID:PID=1A86:7523", description="CH340"),
            SimpleNamespace(device="COM8", hwid=r"BTHENUM\fixture"),
            SimpleNamespace(device="COM9", description="Standard Serial over Bluetooth link"),
            SimpleNamespace(device="COM10", manufacturer="RFCOMM service"),
            SimpleNamespace(device="COM11", hwid="BTHMODEM"),
        ]
        self.assertEqual(usb_port_choices(ports), ["COM5"])

    def test_plain_serial_names_remain_supported_without_duplicates_or_invalid_names(self):
        ports = [SimpleNamespace(device=name) for name in
                 ("COM12", "com3", "COM3", "LPT1", "COM0", "tcp://host:23", "", None)]
        self.assertEqual(usb_port_choices(ports), ["COM3", "COM12"])


class ConnectionModeTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.port_patch = patch("serial.tools.list_ports.comports", return_value=[])
        self.enumerate = self.port_patch.start()
        self.addCleanup(self.port_patch.stop)
        with patch("app.Controller", side_effect=lambda **kw: Controller(start_worker=False, **kw)):
            self.app = App(self.root, data_dir=None)
        self.root.update()

    def tearDown(self):
        self.app.controller.busy = False
        close_tk_app(self)

    def mode(self, value):
        self.app.connection_mode.set(value)
        self.app.change_connection_mode()

    def test_startup_offers_only_usb_wifi_and_demo_without_opening_anything(self):
        self.assertEqual([button.cget("text") for button in self.app.connection_mode_buttons],
                         ["USB", "Wi-Fi", "Демо"])
        self.assertEqual(self.app.connection_mode.get(), "Демо")
        self.assertFalse(hasattr(self.app, "bluetooth_port"))
        self.assertFalse(hasattr(self.app, "refresh_bluetooth_ports"))
        self.assertFalse(self.app.connected)
        self.assertIsNone(self.app.controller.session)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_obsolete_mode_is_rejected_instead_of_falling_back_to_usb_or_demo(self):
        self.app.usb_port.set("COM5")
        self.app.connection_mode.set("Bluetooth")
        self.app.update_controls()
        with patch.object(self.app, "submit") as submit:
            self.assertFalse(self.app.connect())
        submit.assert_not_called()
        self.assertEqual(str(self.app.connect_button["state"]), "disabled")
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_obsolete_view_state_normalizes_without_search_or_connection(self):
        for active in ("Демо", "Bluetooth"):
            with self.subTest(active=active):
                self.app.active_connection_mode = active
                with patch("serial.Serial") as serial, patch("app.discover_fluidnc") as discover:
                    self.mode("Bluetooth")
                serial.assert_not_called()
                discover.assert_not_called()
                self.assertEqual(self.app.connection_mode.get(), "Демо")
                self.assertEqual(self.app.port.get(), "DEMO")
                self.assertFalse(self.app.connected)
                self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_usb_target_must_be_one_of_the_eligible_listed_ports(self):
        self.enumerate.return_value = [
            SimpleNamespace(device="COM5", description="USB CH340"),
            SimpleNamespace(device="COM9", hwid="BTHENUM"),
        ]
        self.app.refresh_ports()
        self.mode("USB")
        self.assertEqual(tuple(self.app.ports["values"]), ("COM5",))
        self.assertEqual(self.app.connection_target(), "COM5")
        self.app.usb_port.set("COM9")
        self.app.select_connection_target()
        self.assertEqual(self.app.port.get(), "")
        with patch.object(self.app, "submit") as submit:
            self.assertFalse(self.app.connect())
        submit.assert_not_called()
        self.assertEqual(str(self.app.connect_button["state"]), "disabled")

    def test_removing_or_failing_usb_device_clears_the_target(self):
        self.enumerate.return_value = [SimpleNamespace(device="COM5")]
        self.app.refresh_ports()
        self.mode("USB")
        self.assertEqual(self.app.port.get(), "COM5")
        for error in (None, OSError("fixture enumeration error")):
            with self.subTest(error=error):
                self.enumerate.return_value = []
                self.enumerate.side_effect = error
                self.app.refresh_ports()
                self.assertEqual(self.app.usb_port.get(), "")
                self.assertEqual(self.app.port.get(), "")
                self.assertEqual(str(self.app.connect_button["state"]), "disabled")
        self.assertIn("Не удалось", self.app.status.get())
        self.assertEqual(self.app.controller.jobs.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
