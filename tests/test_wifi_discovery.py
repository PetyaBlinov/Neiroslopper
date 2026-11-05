"""Discovery tests replace every network operation; no LAN or device access."""
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wifi_discovery as d

IDENTITY = "[VER:4.1 FluidNC v4.1.1 (esp32-wifi) :]"


class Wire:
    def __init__(self, reply=None, peer="192.168.1.45"):
        self.reply = list(reply if reply is not None else [
            (IDENTITY + "\r\n[MSG:Machine: Neiroslopper]\r\nok\r\n").encode()])
        self.peer_address, self.history, self.closed = peer, [], False

    def write(self, data):
        self.history.append(data)
        return len(data)

    def read(self, size):
        if self.reply:
            return self.reply.pop(0)
        raise TimeoutError("No further fake reply")

    def close(self):
        self.closed = True


class DiscoveryTests(unittest.TestCase):
    def probe(self, wire):
        with patch.object(d, "TcpWire", return_value=wire):
            return d._probe("tcp://fluidnc.local:23", "mDNS", threading.Event(), time.monotonic() + .2)

    def test_fragmented_verified_identity_is_read_only(self):
        wire = Wire([b"[VER:4.1 Fluid", b"NC v4.1.1 (esp32-wifi) :]\r\n",
                     b"[MSG:Machine: Neiroslopper]\r\nok\r\n"])
        result = self.probe(wire)
        self.assertEqual(result.endpoint, "tcp://192.168.1.45:23")
        self.assertEqual(result.label, "Neiroslopper · 192.168.1.45")
        self.assertEqual(result.identity, IDENTITY)
        self.assertEqual(wire.history, [b"$I\n"])
        self.assertTrue(wire.closed)

    def test_ack_banner_and_unacknowledged_identity_are_not_proof(self):
        for reply in [b"ok\n", b"[VER:1.1h GRBL:]\nok\n", b"Grbl 4.1 [FluidNC]\nok\n",
                      (IDENTITY + "\n").encode(), b"error:9\n", b"ALARM:3\n"]:
            with self.subTest(reply=reply):
                wire = Wire([reply])
                self.assertIsNone(self.probe(wire))
                self.assertEqual(wire.history, [b"$I\n"])
                self.assertTrue(wire.closed)

    def test_peer_must_be_numeric_and_local(self):
        for peer in ("8.8.8.8", "other.example", None):
            with self.subTest(peer=peer):
                self.assertIsNone(self.probe(Wire(peer=peer)))

    def test_alarm_does_not_hide_a_board_that_answers_identity(self):
        wire = Wire([("ALARM:3\r\n" + IDENTITY + "\r\nok\r\n").encode()])
        self.assertIsNotNone(self.probe(wire))
        self.assertEqual(wire.history, [b"$I\n"])

    def test_cancel_before_or_during_open_never_sends_command(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(d, "TcpWire") as factory:
            self.assertIsNone(d._probe("tcp://192.168.0.1:23", "AP", cancel, time.monotonic() + .2))
        factory.assert_not_called()
        cancel.clear()
        wire = Wire()
        def open_cancel(*args, **kwargs):
            cancel.set()
            return wire
        with patch.object(d, "TcpWire", side_effect=open_cancel):
            self.assertIsNone(d._probe("tcp://192.168.0.1:23", "AP", cancel, time.monotonic() + .2))
        self.assertEqual(wire.history, [])
        self.assertTrue(wire.closed)

    def test_service_hints_ignore_unrelated_http_public_ip_and_dwin(self):
        info = types.SimpleNamespace(server="renamed.local.", port=23,
            parsed_addresses=lambda: ["192.168.1.45", "8.8.8.8", "192.168.1.45"])
        self.assertEqual(d._service_candidates(d.SERVICE_TYPES[0], "My plotter", info),
                         ["tcp://192.168.1.45:23"])
        self.assertEqual(d._service_candidates(d.SERVICE_TYPES[1], "Printer", info), [])
        self.assertEqual(d._service_candidates(d.SERVICE_TYPES[1], "FluidNC", info),
                         ["tcp://192.168.1.45:23"])
        info.port = 8888
        self.assertEqual(d._service_candidates(d.SERVICE_TYPES[0], "DWIN", info), [])

    def test_hints_are_verified_and_results_deduplicated(self):
        seen = []
        def probe(endpoint, source, *args):
            seen.append(endpoint)
            return d.DiscoveredFluidNC("Plotter", "tcp://192.168.1.45:23", source, IDENTITY)
        with patch.object(d, "_probe", side_effect=probe), patch.object(d, "_browse_mdns"):
            found = d.discover_fluidnc(threading.Event(), ["tcp://192.168.1.45:23"] * 2 +
                ["tcp://8.8.8.8:23", "tcp://192.168.1.45:8888"], timeout=.1)
        self.assertEqual(len(found), 1)
        self.assertEqual(set(seen), {"tcp://192.168.1.45:23", "tcp://192.168.0.1:23", "tcp://fluidnc.local:23"})

    def test_advertisement_flood_has_fixed_candidate_cap(self):
        seen = []
        def browse(stop, deadline, submit):
            for i in range(1, 80):
                submit(f"tcp://192.168.2.{i}:23", "mDNS")
        with patch.object(d, "_probe", side_effect=lambda endpoint, *args: seen.append(endpoint)), \
                patch.object(d, "_browse_mdns", side_effect=browse):
            self.assertEqual(d.discover_fluidnc(threading.Event(), timeout=.1), [])
        self.assertEqual(len(set(seen)), d.MAX_CANDIDATES)

    def test_deadline_does_not_wait_for_stuck_worker(self):
        release = threading.Event()
        started = time.monotonic()
        try:
            with patch.object(d, "_probe", side_effect=lambda *args: release.wait(1)), patch.object(d, "_browse_mdns"):
                self.assertEqual(d.discover_fluidnc(threading.Event(), timeout=.1), [])
            self.assertLess(time.monotonic() - started, .5)
        finally:
            release.set()

    def test_cancelled_search_does_not_start_network(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(d, "_probe") as probe, patch.object(d, "_browse_mdns") as browse:
            self.assertEqual(d.discover_fluidnc(cancel), [])
        probe.assert_not_called()
        browse.assert_not_called()

    def test_browser_resolves_and_closes_with_fake_zeroconf(self):
        info = types.SimpleNamespace(server="renamed.local.", port=23,
                                     parsed_addresses=lambda: ["192.168.1.45"])
        zc = Mock()
        zc.get_service_info.return_value = info
        def browser(zeroconf, services, listener):
            self.assertEqual(services, list(d.SERVICE_TYPES))
            listener.add_service(zeroconf, d.SERVICE_TYPES[0], "Renamed plotter")
            return Mock()
        fake = types.SimpleNamespace(Zeroconf=Mock(return_value=zc), ServiceBrowser=Mock(side_effect=browser),
                                     ServiceListener=object, IPVersion=types.SimpleNamespace(V4Only=4))
        found = []
        with patch.dict(sys.modules, {"zeroconf": fake}), patch.object(d, "TcpWire") as tcp:
            d._browse_mdns(threading.Event(), time.monotonic() + .06, lambda endpoint, _: found.append(endpoint))
        self.assertEqual(found, ["tcp://192.168.1.45:23"])
        zc.close.assert_called_once()
        tcp.assert_not_called()

    def test_invalid_inputs_do_not_start_search(self):
        with patch.object(d, "_probe") as probe, patch.object(d, "_browse_mdns"):
            for timeout in (None, True, 0, -1, 11, float("nan")):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    d.discover_fluidnc(threading.Event(), timeout=timeout)
            with self.assertRaises(ValueError):
                d.discover_fluidnc(threading.Event(), remembered="tcp://192.168.0.1:23")
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
