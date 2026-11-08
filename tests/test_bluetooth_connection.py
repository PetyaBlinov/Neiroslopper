"""Classic SPP port choices and UI intents; no pairing, radio or serial I/O."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from bluetooth_ports import bluetooth_choices
from controller import Controller
from machine_profiles import UNO, DLC32, PROFILE_LABELS, app_profile
from tk_test_support import close_tk_app


def port(device="COM9", description="Standard Serial over Bluetooth link (COM9)",
         hwid=r"BTHENUM\{00001101-0000-1000-8000-00805f9b34fb}", **kwargs):
    return SimpleNamespace(device=device, description=description, hwid=hwid, **kwargs)


class BluetoothPortTests(unittest.TestCase):
    def test_bluetooth_metadata_is_distinct_from_usb_and_explicit_incoming(self):
        choices = bluetooth_choices([
            port(), port("COM5", "USB-SERIAL CH340 (COM5)", "USB VID:PID=1A86:7523", vid=0x1a86),
            port("COM10", "Bluetooth Incoming (COM10)"), port("COM11", "Bluetooth входящий"),
        ])
        self.assertEqual([item.device for item in choices], ["COM9"])
        self.assertTrue(choices[0].detected)
        self.assertNotIn("(COM9)", choices[0].label)

    def test_unknown_driver_is_honest_fallback_and_names_sort_numerically(self):
        choices = bluetooth_choices([
            port("COM12"), port("COM3"), SimpleNamespace(device="COM2"),
            port("COM3"), port("LPT1"), port("COM0"),
        ])
        self.assertEqual([item.device for item in choices], ["COM3", "COM12", "COM2"])
        self.assertFalse(choices[-1].detected)
        self.assertIn("тип порта не определён", choices[-1].label)


class BluetoothConnectionTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.ports = patch("serial.tools.list_ports.comports", return_value=[])
        self.enumerate = self.ports.start()
        self.addCleanup(self.ports.stop)
        with patch("app.Controller", side_effect=lambda **kw: Controller(start_worker=False, **kw)):
            self.app = App(self.root, data_dir=None)
        self.root.update()

    def tearDown(self):
        self.app.controller.busy = False
        close_tk_app(self)

    def bluetooth(self, values=()):
        self.enumerate.return_value = list(values)
        self.app.connection_mode.set("Bluetooth")
        self.app.change_connection_mode()

    def test_startup_does_not_open_or_pair_anything_and_mode_is_explicit(self):
        self.assertEqual(self.app.connection_mode.get(), "Демо")
        with patch("serial.Serial") as serial, patch("app.discover_fluidnc") as discover:
            self.bluetooth([port()])
        serial.assert_not_called()
        discover.assert_not_called()
        self.assertFalse(self.app.connected)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)
        self.assertEqual(self.app.port.get(), "COM9")
        with patch.object(self.app, "submit", return_value=True) as submit:
            self.app.connect()
        self.assertEqual(submit.call_args.args, ("connect",))
        self.assertEqual(submit.call_args.kwargs["port"], "COM9")
        self.assertEqual(submit.call_args.kwargs["machine_profile"], DLC32)

    def test_no_bluetooth_port_has_no_demo_or_usb_fallback(self):
        self.app.usb_port.set("COM5")
        self.bluetooth([port("COM5", "USB CH340", "USB VID:PID=1A86:7523")])
        self.assertEqual(self.app.port.get(), "")
        self.assertEqual(str(self.app.connect_button["state"]), "disabled")
        with patch.object(self.app, "submit") as submit:
            self.app.connect()
        submit.assert_not_called()
        self.assertIn("сопряжение", self.app.bluetooth_status.get())

    def test_multiple_or_unknown_ports_require_user_selection(self):
        for values in ([port("COM9"), port("COM10")], [SimpleNamespace(device="COM8")]):
            self.bluetooth(values)
            self.assertEqual(self.app.bluetooth_port.get(), "")
            self.assertEqual(self.app.port.get(), "")
            self.assertEqual(str(self.app.connect_button["state"]), "disabled")
            label = tuple(self.app.bluetooth_ports["values"])[-1]
            self.app.bluetooth_port.set(label)
            self.app.select_connection_target()
            self.assertEqual(str(self.app.connect_button["state"]), "normal")
            self.assertEqual(self.app.port.get(), self.app.bluetooth_targets[label])

    def test_removing_device_invalidates_selection(self):
        self.bluetooth([port()])
        self.enumerate.return_value = []
        self.app.refresh_bluetooth_ports()
        self.assertEqual(self.app.bluetooth_port.get(), "")
        self.assertEqual(self.app.port.get(), "")
        self.assertEqual(str(self.app.connect_button["state"]), "disabled")

    def test_bluetooth_mode_selects_mks_and_uno_profile_returns_to_usb(self):
        self.app.machine_profile.set(PROFILE_LABELS[UNO])
        self.app.change_machine_profile()
        self.bluetooth([port()])
        self.assertEqual(app_profile(self.app), DLC32)
        self.assertIn("прошивка", self.app.connection_help.get())
        self.app.machine_profile.set(PROFILE_LABELS[UNO])
        self.app.change_machine_profile()
        self.assertEqual(self.app.connection_mode.get(), "USB")

    def test_connected_or_busy_locks_bluetooth_target_and_refresh(self):
        self.bluetooth([port()])
        for connected, busy in ((True, False), (False, True)):
            self.app.connected, self.app.controller.busy = connected, busy
            self.app.update_controls()
            self.assertEqual(str(self.app.bluetooth_ports["state"]), "disabled")
            self.assertEqual(str(self.app.refresh_bluetooth_button["state"]), "disabled")
            self.enumerate.reset_mock()
            self.app.refresh_bluetooth_ports()
            self.enumerate.assert_not_called()
            self.app.connection_mode.set("USB")
            self.app.change_connection_mode()
            self.assertEqual(self.app.connection_mode.get(), "Bluetooth")

    def test_driver_error_clears_stale_target_without_connection_attempt(self):
        self.bluetooth([port()])
        self.enumerate.side_effect = OSError("offline fixture")
        self.app.refresh_bluetooth_ports()
        self.assertEqual(self.app.bluetooth_port.get(), "")
        self.assertEqual(str(self.app.connect_button["state"]), "disabled")
        self.assertIn("Не удалось", self.app.bluetooth_status.get())
        self.assertEqual(self.app.controller.jobs.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
