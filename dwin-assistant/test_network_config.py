"""No network/hardware: full HTTP YAML replaces lossy TCP log-stream dumps."""
import copy
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "PROGRAM"))
from controller import GrblSession, SimWire, Cancelled
from machine_profiles import DLC32, fluidnc_configuration, settings_ready


class NetworkConfigTests(unittest.TestCase):
    def setUp(self):
        self.full = (ROOT / "firmware/jd_v22_dwin9600.yaml").read_text(encoding="utf-8")
        wire = SimWire()
        wire.port = "tcp://plotter-original-name.local:23"
        wire.peer_address = "192.168.0.1"
        self.session = GrblSession(wire, threading.Event(), Mock(), DLC32)
        self.session.command = Mock(return_value=["$13=0", "$20=0", "$21=0", "$22=0"])

    def http(self, body):
        response = Mock(status=200)
        response.read.return_value = body
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        return opener, response

    def test_truncated_tcp_is_rejected_but_full_runtime_http_is_validated(self):
        truncated = copy.deepcopy(yaml.safe_load(self.full))
        del truncated["report_inches"]
        with self.assertRaises(ValueError):
            fluidnc_configuration(yaml.safe_dump(truncated).splitlines())
        opener, response = self.http(self.full.encode())
        with patch("urllib.request.build_opener", return_value=opener) as factory:
            self.session.refresh_settings()
        self.session.command.assert_called_once_with("$$")
        self.assertTrue(settings_ready(self.session.settings, DLC32))
        self.assertEqual(self.session.settings["_dwin_uart_baud"], 9600)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://192.168.0.1/command?plain=%24Config%2FDump")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 12})
        self.assertEqual(factory.call_args.args[0].proxies, {})
        response.read.assert_called_once_with(120001)

    def test_no_confirmed_numeric_peer_means_no_http_request(self):
        for peer in (None, "another-board.local", "8.8.8.8"):
            with self.subTest(peer=peer), patch("urllib.request.build_opener") as factory:
                self.session.wire.peer_address = peer
                with self.assertRaises(ValueError):
                    self.session.refresh_settings()
                factory.assert_not_called()

    def test_http_truncation_size_and_missing_units_are_not_accepted(self):
        missing_units = copy.deepcopy(yaml.safe_load(self.full))
        del missing_units["report_inches"]
        for body in (self.full[:self.full.index("macros:")].encode(), b"x" * 120001,
                     (yaml.safe_dump(missing_units) + "ok\n").encode()):
            opener, _ = self.http(body)
            with self.subTest(size=len(body)), patch("urllib.request.build_opener", return_value=opener):
                with self.assertRaises(ValueError):
                    self.session.refresh_settings()
                self.assertFalse(settings_ready(self.session.settings, DLC32))

    def test_cancelled_request_does_not_publish_settings(self):
        opener, _ = self.http((self.full + "\nok\n").encode())
        self.session.cancelled.set()
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(Cancelled):
                self.session._network_config_dump()
        opener.open.assert_not_called()

    def test_serial_mks_keeps_original_command_path(self):
        self.session.wire.port = "COM13"
        self.session.command = Mock(side_effect=[[], self.full.splitlines()])
        with patch("urllib.request.build_opener") as factory:
            self.session.refresh_settings()
        factory.assert_not_called()
        self.assertEqual(self.session.command.call_args_list[-1].args, ("$Config/Dump",))
        self.assertTrue(settings_ready(self.session.settings, DLC32))


if __name__ == "__main__":
    unittest.main()
