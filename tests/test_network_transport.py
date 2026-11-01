"""Local-only TCP transport and USB-open tests; no controller is contacted."""
import socket
import sys
import threading
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from network_transport import TcpWire, open_fluidnc_serial, parse_tcp_endpoint


@contextmanager
def loopback_server(reply_parts=(b"ok\r\n",)):
    """One connection on an ephemeral loopback port; never listens on LAN."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    info = {"received": bytearray(), "connections": 0, "error": None}
    done = threading.Event()

    def serve():
        try:
            connection, _ = listener.accept()
            info["connections"] += 1
            with connection:
                connection.settimeout(2)
                while not info["received"].endswith(b"\n"):
                    packet = connection.recv(4096)
                    if not packet:
                        break
                    info["received"].extend(packet)
                for part in reply_parts:
                    connection.sendall(part)
                    time.sleep(.01)
                connection.shutdown(socket.SHUT_WR)
        except Exception as exc:
            info["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield f"tcp://127.0.0.1:{listener.getsockname()[1]}", info, done
    finally:
        listener.close()
        worker.join(3)


class TcpTests(unittest.TestCase):
    def test_fragmented_response_preserves_bytes(self):
        with loopback_server((b"[VER:4.", b"1 FluidNC v4.1.1 :]\r\n", b"ok\r\n")) as (url, info, done):
            wire = TcpWire(url, timeout=.1)
            try:
                self.assertEqual(wire.write(b"$I\n"), 3)
                response = bytearray()
                deadline = time.monotonic() + 2
                while not response.endswith(b"ok\r\n") and time.monotonic() < deadline:
                    response.extend(wire.read(9))
                self.assertEqual(bytes(response), b"[VER:4.1 FluidNC v4.1.1 :]\r\nok\r\n")
                self.assertEqual(bytes(info["received"]), b"$I\n")
                self.assertEqual(wire.peer_address, "127.0.0.1")
                self.assertTrue(done.wait(1))
                self.assertIsNone(info["error"])
            finally:
                wire.close()

    def test_eof_is_reported_and_never_reconnects(self):
        with loopback_server((b"ok\n",)) as (url, info, done):
            wire = TcpWire(url, timeout=.1)
            try:
                wire.write(b"$I\n")
                self.assertEqual(wire.read(3), b"ok\n")
                self.assertTrue(done.wait(1))
                with self.assertRaises(ConnectionError):
                    wire.read(1)
                with self.assertRaises(ConnectionError):
                    wire.read(1)
                self.assertEqual(info["connections"], 1)
                self.assertEqual(bytes(info["received"]), b"$I\n")
            finally:
                wire.close()

    def test_partial_socket_sends_continue_only_the_unsent_suffix(self):
        wire = TcpWire.__new__(TcpWire)
        wire.closed = False
        wire.socket = Mock()
        wire.write_timeout = .5
        wire.socket.send.side_effect = [2, 1, 3]
        with patch("network_transport.select.select", return_value=([], [wire.socket], [])):
            self.assertEqual(wire.write(b"G1 X1\n"), 6)
        sent_arguments = [call.args[0] for call in wire.socket.send.call_args_list]
        self.assertEqual(sent_arguments, [b"G1 X1\n", b" X1\n", b"X1\n"])

    def test_timeout_after_partial_socket_write_does_not_replay_prefix(self):
        wire = TcpWire.__new__(TcpWire)
        wire.closed = False
        wire.socket = Mock()
        wire.write_timeout = .5
        wire.socket.send.return_value = 2
        with patch("network_transport.select.select", side_effect=[([], [wire.socket], []), ([], [], [])]):
            with self.assertRaises(TimeoutError):
                wire.write(b"G1 X1\n")
        self.assertEqual(wire.socket.send.call_count, 1)
        self.assertEqual(wire.socket.send.call_args.args[0], b"G1 X1\n")

    def test_closed_transport_cannot_send_or_read(self):
        wire = TcpWire.__new__(TcpWire)
        wire.closed = True
        wire.socket = None
        with self.assertRaises(OSError):
            wire.write(b"M8\n")
        with self.assertRaises(OSError):
            wire.read(1)

    def test_endpoint_rejects_public_hosts_credentials_and_paths(self):
        for value in ("tcp://8.8.8.8:23", "tcp://user:pass@192.168.0.1:23",
                      "tcp://192.168.0.1:23/file", "tcp://192.168.0.1:23?cmd=M8",
                      "tcp://192.168.0.1", "tcp://192.168.0.1:0", "http://192.168.0.1:80"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_tcp_endpoint(value)

    def test_endpoint_accepts_explicit_board_or_localhost(self):
        self.assertEqual(parse_tcp_endpoint("tcp://192.168.0.1:23").port, 23)
        self.assertEqual(parse_tcp_endpoint("tcp://127.0.0.1:32123").host, "127.0.0.1")
        self.assertEqual(parse_tcp_endpoint("tcp://fluidnc.local:23").url, "tcp://fluidnc.local:23")

    def test_usb_control_lines_are_disabled_before_open(self):
        events = []

        class FakeSerial:
            def __init__(self, **kwargs):
                self.options = kwargs
                self.dtr = None
                self.rts = None
                self.port = kwargs["port"]
                self.closed = False

            def open(self):
                events.append((self.port, self.dtr, self.rts))

            def close(self):
                self.closed = True

        with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=FakeSerial)}):
            wire = open_fluidnc_serial("FAKE-COM", timeout=.05)
        self.assertIsNone(wire.options["port"])
        self.assertEqual(events, [("FAKE-COM", False, False)])
        self.assertEqual(wire.options["baudrate"], 115200)

    def test_failed_usb_open_closes_the_handle(self):
        fake = Mock()
        fake.open.side_effect = OSError("port unavailable")
        serial_module = types.SimpleNamespace(Serial=Mock(return_value=fake))
        with patch.dict(sys.modules, {"serial": serial_module}):
            with self.assertRaises(OSError):
                open_fluidnc_serial("FAKE-COM")
        fake.open.assert_called_once()
        fake.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
