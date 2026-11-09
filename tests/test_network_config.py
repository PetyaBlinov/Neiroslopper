"""LAN YAML and optional runtime diagnostics; fakes or loopback only."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import Cancelled, GrblSession, board_runtime_info
from machine_profiles import DLC32, settings_ready
from network_config import (CONFIG_PATH, MAX_CONFIG_BYTES, read_runtime_config,
                            RuntimeConfigCancelled, RuntimeConfigUnavailable)
from test_controller import ProtocolWire, PHYSICAL_CONFIG


class FakeResponse:
    def __init__(self, body=b"name: test\nok\r\n\r\n", *, status=200, headers=None):
        self.body = body
        self.position = 0
        self.status = status
        self.headers = headers or {}
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def begin(self):
        pass

    def read1(self, size):
        result = self.body[self.position:self.position + size]
        self.position += len(result)
        return result

    def close(self):
        self.closed = True


class HttpConfigurationTests(unittest.TestCase):
    def wire(self, peer="192.168.1.23"):
        return SimpleNamespace(port="tcp://fluidnc.local:23", peer_address=peer, closed=False)

    def reader(self, response=None, peer="192.168.1.23"):
        response = response or FakeResponse()
        connection = Mock()
        connection.getpeername.return_value = (peer, 80)
        return connection, response

    def test_reads_fixed_command_from_numeric_peer_without_proxies_or_dns_name(self):
        connection, response = self.reader()
        with patch("network_config.socket.socket", return_value=connection) as factory, \
                patch("network_config.http.client.HTTPResponse", return_value=response):
            lines = read_runtime_config(self.wire(), threading.Event())
        self.assertEqual(lines, ["name: test"])
        connection.connect.assert_called_once_with(("192.168.1.23", 80))
        connection.sendall.assert_called_once_with(
            (f"GET {CONFIG_PATH} HTTP/1.1\r\nHost: 192.168.1.23\r\n"
             "Accept: text/plain\r\nConnection: close\r\n\r\n").encode("ascii"))
        self.assertTrue(response.closed)
        connection.close.assert_called_once()

    def test_invalid_missing_public_closed_and_non_tcp_peers_never_open_http(self):
        invalid = [self.wire(None), self.wire("fluidnc.local"), self.wire("8.8.8.8"),
                   SimpleNamespace(port="COM5", peer_address="192.168.1.23"),
                   SimpleNamespace(port="tcp://192.168.1.23:23", peer_address="192.168.1.23", closed=True)]
        for wire in invalid:
            with self.subTest(wire=wire), patch("network_config.socket.socket") as factory:
                with self.assertRaises(ValueError):
                    read_runtime_config(wire, threading.Event())
                factory.assert_not_called()

    def test_unexpected_http_peer_is_rejected_before_request(self):
        connection, response = self.reader(peer="192.168.1.99")
        with patch("network_config.socket.socket", return_value=connection), \
                patch("network_config.http.client.HTTPResponse", return_value=response):
            with self.assertRaisesRegex(ValueError, "не от подключённой"):
                read_runtime_config(self.wire(), threading.Event())
        connection.sendall.assert_not_called()
        connection.close.assert_called_once()

    def test_ipv6_peer_is_pinned_and_not_replaced_by_original_hostname(self):
        connection, response = self.reader(peer="fd00::7")
        with patch("network_config.socket.socket", return_value=connection), \
                patch("network_config.http.client.HTTPResponse", return_value=response):
            self.assertEqual(read_runtime_config(self.wire("fd00::7"), threading.Event()), ["name: test"])
        connection.connect.assert_called_once_with(("fd00::7", 80))
        self.assertIn(b"Host: [fd00::7]\r\n", connection.sendall.call_args.args[0])

    def test_network_and_auth_failure_are_available_for_console_fallback(self):
        for status in (401, 403, 404, 500, 503):
            connection, response = self.reader(FakeResponse(status=status))
            with self.subTest(status=status), patch("network_config.socket.socket", return_value=connection), \
                    patch("network_config.http.client.HTTPResponse", return_value=response):
                with self.assertRaises(RuntimeConfigUnavailable):
                    read_runtime_config(self.wire(), threading.Event())
                self.assertTrue(response.closed)
        connection, response = self.reader()
        connection.connect.side_effect = OSError("fixture offline")
        with patch("network_config.socket.socket", return_value=connection), \
                patch("network_config.http.client.HTTPResponse", return_value=response):
            with self.assertRaises(RuntimeConfigUnavailable):
                read_runtime_config(self.wire(), threading.Event())
        connection.close.assert_called_once()

    def test_redirect_is_rejected_without_a_second_connection(self):
        connection, response = self.reader(FakeResponse(status=302, headers={"Location": "http://8.8.8.8"}))
        with patch("network_config.socket.socket", return_value=connection) as factory, \
                patch("network_config.http.client.HTTPResponse", return_value=response):
            with self.assertRaisesRegex(ValueError, "перенаправила"):
                read_runtime_config(self.wire(), threading.Event())
            self.assertEqual(factory.call_count, 1)

    def test_bounded_complete_plain_utf8_body_is_required(self):
        invalid = [FakeResponse(b"x" * (MAX_CONFIG_BYTES + 1)),
                   FakeResponse(headers={"Content-Length": str(MAX_CONFIG_BYTES + 1)}),
                   FakeResponse(b"name: x", headers={"Content-Length": "99"}),
                   FakeResponse(headers={"Content-Length": "3,3"}),
                   FakeResponse(b"\xff\xfe"), FakeResponse(b"name: x\x00"),
                   FakeResponse(b"ok\n\n"), FakeResponse(headers={"Content-Encoding": "gzip"})]
        for response in invalid:
            connection, _ = self.reader(response)
            with self.subTest(body_size=len(response.body), headers=response.headers), \
                    patch("network_config.socket.socket", return_value=connection), \
                    patch("network_config.http.client.HTTPResponse", return_value=response):
                with self.assertRaises(ValueError):
                    read_runtime_config(self.wire(), threading.Event())
                self.assertTrue(response.closed)
                connection.close.assert_called_once()

    def test_cancellation_before_and_during_read_discards_body(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch("network_config.socket.socket") as factory:
            with self.assertRaises(RuntimeConfigCancelled):
                read_runtime_config(self.wire(), cancelled)
            factory.assert_not_called()
        cancelled.clear()
        connection, response = self.reader()
        original = response.read1
        def cancel_after_chunk(size):
            cancelled.set()
            return original(size)
        response.read1 = cancel_after_chunk
        with patch("network_config.socket.socket", return_value=connection), \
                patch("network_config.http.client.HTTPResponse", return_value=response):
            with self.assertRaises(RuntimeConfigCancelled):
                read_runtime_config(self.wire(), cancelled)
        self.assertTrue(response.closed)

    def test_total_deadline_does_not_renew_per_chunk(self):
        connection, response = self.reader()
        clock = [0.]
        def slow_chunk(size):
            clock[0] += .4
            return b"x"
        response.read1 = slow_chunk
        with patch("network_config.socket.socket", return_value=connection), \
                patch("network_config.http.client.HTTPResponse", return_value=response), \
                patch("network_config.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(RuntimeConfigUnavailable):
                read_runtime_config(self.wire(), threading.Event(), timeout=1)
        self.assertLess(clock[0], 1.5)
        timeouts = [call.args[0] for call in connection.settimeout.call_args_list]
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))

    def test_real_loopback_http_connection_close_retains_response_body(self):
        received = []
        body = PHYSICAL_CONFIG.encode("utf-8")
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch("network_config.HTTP_PORT", server.server_address[1]):
                lines = read_runtime_config(self.wire("127.0.0.1"), threading.Event(), timeout=2)
            self.assertEqual(lines, PHYSICAL_CONFIG.splitlines())
            self.assertEqual(received, [CONFIG_PATH])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_real_loopback_chunked_body_is_complete_and_truncation_is_not_a_fallback(self):
        body = PHYSICAL_CONFIG.encode("utf-8")
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            truncated = False
            def do_GET(self):
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                for piece in (body[:23], body[23:]):
                    self.wfile.write(f"{len(piece):X}\r\n".encode("ascii") + piece + b"\r\n")
                if not type(self).truncated:
                    self.wfile.write(b"0\r\n\r\n")
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch("network_config.HTTP_PORT", server.server_address[1]):
                self.assertEqual(read_runtime_config(self.wire("127.0.0.1"), threading.Event(), timeout=2),
                                 PHYSICAL_CONFIG.splitlines())
                Handler.truncated = True
                with self.assertRaisesRegex(ValueError, "оборвана"):
                    read_runtime_config(self.wire("127.0.0.1"), threading.Event(), timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_real_loopback_trickling_chunk_header_cannot_renew_total_deadline(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_GET(self):
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    # Never completes the first chunk-size line. Each byte is
                    # faster than the socket timeout but the total exceeds it.
                    for _ in range(30):
                        self.wfile.write(b"0")
                        self.wfile.flush()
                        time.sleep(.04)
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            started = time.monotonic()
            with patch("network_config.HTTP_PORT", server.server_address[1]):
                with self.assertRaises(RuntimeConfigUnavailable):
                    read_runtime_config(self.wire("127.0.0.1"), threading.Event(), timeout=.25)
            self.assertLess(time.monotonic() - started, .8)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


class RuntimeAndSessionTests(unittest.TestCase):
    def setUp(self):
        self.trace = patch("controller.SerialTrace")
        self.trace.start()
        self.addCleanup(self.trace.stop)

    def session(self, *, tcp=True):
        wire = ProtocolWire(port="tcp://fluidnc.local:23" if tcp else "FAKE-USB")
        wire.peer_address = "192.168.1.23"
        events = []
        return GrblSession(wire, threading.Event(), lambda *args: events.append(args), DLC32), wire, events

    def test_http_primary_validates_canonical_yaml_without_legacy_settings_or_motion(self):
        session, wire, events = self.session()
        with patch("network_config.read_runtime_config", return_value=PHYSICAL_CONFIG.splitlines()):
            session.initialize()
        self.assertTrue(session.trusted)
        self.assertTrue(settings_ready(session.settings, DLC32))
        self.assertEqual(session.settings["_fluidnc_config_source"], "http")
        self.assertEqual(wire.history, [b"$I\n", b"?"])
        self.assertFalse(session.has_actuated)

    def test_unavailable_http_falls_back_to_same_console_with_same_validator(self):
        for failure in (RuntimeConfigUnavailable("offline"), RuntimeConfigUnavailable("HTTP401")):
            session, wire, events = self.session()
            with self.subTest(failure=str(failure)), patch("network_config.read_runtime_config", side_effect=failure):
                session.initialize()
            self.assertTrue(settings_ready(session.settings, DLC32))
            self.assertEqual(session.settings["_fluidnc_config_source"], "tcp")
            self.assertEqual(wire.history, [b"$I\n", b"$Config/Dump\n", b"?"])
            self.assertFalse(session.has_actuated)

    def test_malformed_http_or_cancelled_read_cannot_fall_back_or_mark_ready(self):
        for error, expected in ((ValueError("invalid content"), ValueError),
                                (RuntimeConfigCancelled(), Cancelled)):
            session, wire, _ = self.session()
            with self.subTest(error=type(error).__name__), patch("network_config.read_runtime_config", side_effect=error):
                with self.assertRaises(expected):
                    session.initialize()
            self.assertFalse(session.trusted)
            self.assertEqual(wire.history, [b"$I\n"])
            self.assertFalse(session.has_actuated)
        session, wire, _ = self.session()
        with patch("network_config.read_runtime_config", return_value=["axes: ["]):
            with self.assertRaises(ValueError):
                session.initialize()
        self.assertFalse(session.trusted)
        self.assertEqual(wire.history, [b"$I\n"])

    def test_http_not_queried_for_wrong_identity_or_usb_session(self):
        session, wire, _ = self.session()
        wire.identity = "[VER:1.1h:]"
        with patch("network_config.read_runtime_config") as read:
            with self.assertRaises(RuntimeError):
                session.initialize()
            read.assert_not_called()
        session, wire, _ = self.session(tcp=False)
        with patch("network_config.read_runtime_config") as read:
            session.initialize()
            read.assert_not_called()
        self.assertEqual(session.settings["_fluidnc_config_source"], "usb")
        self.assertEqual(wire.history, [b"$I\n", b"$Config/Dump\n", b"?"])

    def test_runtime_marker_is_optional_strict_and_never_authorizes_configuration(self):
        valid = "[JD-DGUS:VERSION=2.1,UPTIME_MS=4294967295,RESET_REASON=255]"
        self.assertEqual(board_runtime_info([valid]),
                         {"firmware_uptime_ms": 4294967295, "firmware_reset_reason": 255})
        for lines in ([], [valid, valid], [valid[:-1]],
                      [valid.replace("4294967295", "4294967296")],
                      [valid.replace("RESET_REASON=255", "RESET_REASON=256")],
                      [valid.replace("UPTIME_MS=4294967295", "UPTIME_MS=-1")],
                      [valid.replace("RESET_REASON=255", "RESET_REASON=1,RESET_REASON=no")],
                      [valid.replace("UPTIME_MS=4294967295", "UPTIME_MS=" + "9" * 100)],
                      [valid.replace(",RESET_REASON=255", "")]):
            with self.subTest(lines=lines):
                self.assertEqual(board_runtime_info(lines), {})
        session, wire, events = self.session(tcp=False)
        wire.identity += "\r\n" + valid
        wire.config = "name: test\nboard: None\n"
        session.initialize()
        self.assertIn(("board_runtime", {"firmware_uptime_ms": 4294967295,
                                       "firmware_reset_reason": 255}), events)
        self.assertFalse(settings_ready(session.settings, DLC32))
        self.assertFalse(session.has_actuated)


if __name__ == "__main__":
    unittest.main()
