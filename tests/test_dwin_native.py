"""Offline checks for the native board/desktop handoff."""
from pathlib import Path
import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "PROGRAM"))
import dwin_native as protocol
from controller import Controller, GrblSession, Cancelled, native_panel_capabilities
from geometry import Axes
from machine_profiles import DLC32, PROFILE_KEY


def verified_settings():
    # Configuration parser has its independent canonical-YAML regression suite.
    return {PROFILE_KEY: DLC32, '_fluidnc_ready': True, '_fluidnc_wiring': 'neiroslopper_ttl',
            13: 0, **{key: 40 for key in (100, 101, 110, 111, 120, 121)}}


def packet(kind, body=b""):
    return protocol.MAGIC + bytes((kind,)) + len(body).to_bytes(2, "big") + body


def board_state(*, owner=False, pending=22, boot=4):
    flags = 1 | 2 | 4 | 32 | (8 if owner else 0)
    return protocol.STATE_STRUCT.pack(flags, 1, 50, 2000, 3000, 1000, 1000,
                                      210000, 297000, 5, boot, pending, 13 if pending else 0, 10 if pending else 0)


class FakeWire:
    peer_address = "192.168.1.8"

    def __init__(self):
        self.incoming = queue.Queue()
        self.outgoing = []
        self.closed = False
        banner = packet(protocol.HELLO, protocol.HELLO_TEXT)
        self.incoming.put(banner[:4])
        self.incoming.put(banner[4:] + packet(protocol.STATE, board_state()) +
                          packet(protocol.EVENT, protocol.EVENT_STRUCT.pack(3, 250, 13, 10, 0x1000)))

    def read(self, count):
        try:
            return self.incoming.get(timeout=.01)
        except queue.Empty:
            return b""

    def write(self, data):
        kind, body = data[3], data[6:]
        self.outgoing.append((kind, body))
        if kind == protocol.HEARTBEAT:
            if body == bytes((protocol.CLAIM,)):
                self.incoming.put(packet(protocol.STATE, board_state(owner=True)) +
                                  packet(protocol.ACK, bytes((1, 0))))
            elif body == bytes((protocol.RENEW,)):
                self.incoming.put(packet(protocol.ACK, bytes((1, 0))))
        elif kind == protocol.RELEASE:
            self.incoming.put(packet(protocol.STATE, board_state()) + packet(protocol.ACK, bytes((0, 0))))
        return len(data)

    def close(self):
        self.closed = True


class NativeDwinTests(unittest.TestCase):
    def client(self, *, interactive=False):
        wire = FakeWire()
        client = protocol.NativeDwinClient("tcp://192.168.1.8:8888", expected_peer=wire.peer_address,
                                           wire_factory=lambda *args, **kwargs: wire, interactive=interactive)
        client.start()
        self.addCleanup(client.thread.join, 1)
        self.addCleanup(client.close)
        self.assertTrue(client.connected.wait(1))
        return client, wire

    def test_observer_receives_waiting_request_without_acquiring_motion(self):
        client, wire = self.client()
        self.assertEqual(client.snapshot().request_id, 22)
        self.assertTrue(client.snapshot().has_sheet)
        self.assertEqual(client.snapshot().origin_mm, (1, 1))
        self.assertFalse(client.lease_ready())
        for _ in range(30):
            logs = client.pending()
            if logs:
                break
            time.sleep(.01)
        self.assertEqual([(row.key, row.result_code) for row in logs], [(13, 10)])
        self.assertTrue(all(kind == protocol.PC_STATUS for kind, _ in wire.outgoing))

    def test_explicit_claim_and_release_and_duplicate_response(self):
        client, wire = self.client()
        self.assertFalse(client.respond(23, 0))
        self.assertTrue(client.claim())
        self.assertTrue(client.lease_ready())
        self.assertTrue(client.respond(22, 0))
        self.assertFalse(client.respond(22, 0))
        self.assertTrue(client.release())
        self.assertFalse(client.lease_ready())
        self.assertEqual(sum(kind == protocol.PC_RESULT for kind, _ in wire.outgoing), 1)

    def test_link_probe_requires_responsive_gui_and_leaves_request_pending(self):
        client, wire = self.client()
        challenge = (19).to_bytes(4, "big")
        wire.incoming.put(packet(protocol.PC_PROBE, challenge))
        for _ in range(50):
            with client._condition:
                if client._probe == challenge:
                    break
            time.sleep(.01)
        self.assertNotIn((protocol.PC_PONG, challenge), wire.outgoing)
        client.answer_probe()
        for _ in range(50):
            if (protocol.PC_PONG, challenge) in wire.outgoing:
                break
            time.sleep(.01)
        self.assertIn((protocol.PC_PONG, challenge), wire.outgoing)
        self.assertEqual(client.snapshot().request_id, 22)
        self.assertFalse(client.lease_ready())

    def test_assistant_and_preview_frames_do_not_acquire_motion(self):
        client, wire = self.client(interactive=True)
        text = "Как задать лист?"
        wire.incoming.put(packet(protocol.PANEL_QUERY, b"\x00\x00\x00\x09\x01" + text.encode("cp1251")))
        requests = []
        for _ in range(50):
            requests = client.panel_requests()
            if requests:
                break
            time.sleep(.01)
        self.assertEqual(requests, [(9, protocol.ASK, text)])
        client.panel_answer(9, 5, "Отметьте два угла.")
        client.panel_sheet(10, (1, 2, 416, 270), ((1, 2, 10, 20),) * protocol.MAX_SEGMENTS)
        for _ in range(50):
            if any(kind == protocol.PANEL_SHEET for kind, _ in wire.outgoing):
                break
            time.sleep(.01)
        outgoing = dict(wire.outgoing)
        self.assertIn(protocol.PANEL_READY, outgoing)
        self.assertEqual(outgoing[protocol.PANEL_ANSWER][5:].decode("cp1251"), "Отметьте два угла.")
        self.assertEqual(len(outgoing[protocol.PANEL_SHEET]), 12 + 8 * protocol.MAX_SEGMENTS)
        self.assertNotIn(protocol.HEARTBEAT, outgoing)
        self.assertEqual(client.snapshot().request_id, 22)

    def test_confirmed_request_rechecked_after_ownership_before_motion(self):
        controller = Controller(start_worker=False)
        wire = SimpleNamespace(peer_address="192.168.1.8", port='tcp://192.168.1.8:23')
        session = SimpleNamespace(native_panel=True, native_owned=False, wire=wire,
                                  coil_on=False, abort=Mock(), refresh_g54_offset=Mock(),
                                  work_offset=(1.0, 1.0), settings=verified_settings())
        controller.session = session
        controller.execute = Mock()
        panel = Mock(peer_address=wire.peer_address)
        panel.snapshot.return_value = protocol.NativeState.decode(board_state(owner=True), time.monotonic())
        params = dict(panel_client=panel, axes=Axes(), panel_request=(4, 3, 22, 13))
        with self.assertRaisesRegex(ValueError, "изменились"):
            controller.execute_job("draw", params)
        controller.execute.assert_not_called()
        panel.release.assert_called_once()
        session.abort.assert_not_called()
        params["panel_request"] = (4, 5, 22, 13)
        controller.execute_job("draw", params)
        controller.execute.assert_called_once()
        panel.respond.assert_called_once_with(22, 0)

    def test_passive_close_does_not_reset_local_motion(self):
        wire = Mock()
        session = GrblSession(wire, threading.Event(), Mock())
        session.trusted = session.native_panel = True
        session.close()
        wire.write.assert_not_called()
        wire.close.assert_called_once()

    def test_fragmented_parser_rejects_unknown_sizes_and_unbounded_noise(self):
        parser = protocol._Parser()
        data = packet(protocol.HELLO, protocol.HELLO_TEXT) + packet(protocol.STATE, board_state())
        frames = []
        for byte in data:
            frames.extend(parser.feed(bytes((byte,))))
        self.assertEqual([kind for kind, _ in frames], [protocol.HELLO, protocol.STATE])
        for raw in (b'bad\x20\x00\x01x', packet(protocol.STATE, b'x'),
                    packet(protocol.PANEL_QUERY, b'x' * 126), b'x' * 8193):
            with self.assertRaises(OSError):
                list(protocol._Parser().feed(raw))

    def test_state_rejects_impossible_sheet_and_selectors(self):
        source = list(protocol.STATE_STRUCT.unpack(board_state()))
        for index, value in ((1, 6), (2, 0), (7, 999), (12, 99)):
            changed = source.copy()
            changed[index] = value
            with self.assertRaises(OSError):
                protocol.NativeState.decode(protocol.STATE_STRUCT.pack(*changed), time.monotonic())

        source[0] |= 128
        board = protocol.NativeState.decode(protocol.STATE_STRUCT.pack(*source), time.monotonic())
        self.assertEqual(board.axes, Axes(right_sign=-1, down_sign=-1))

    def test_boot_change_closes_channel_without_reconnect(self):
        client, wire = self.client()
        wire.incoming.put(packet(protocol.STATE, board_state(boot=99)))
        client.thread.join(1)
        self.assertFalse(client.connected.is_set())
        self.assertTrue(wire.closed)
        self.assertIsNone(client.snapshot())
        self.assertIn('перезапущена', client.error)

    def test_wrong_numeric_peer_never_sends_packet(self):
        wire = FakeWire()
        client = protocol.NativeDwinClient('tcp://192.168.1.8:8888', expected_peer='192.168.1.9',
                                          wire_factory=lambda *a, **k: wire)
        client.start()
        client.thread.join(1)
        self.assertFalse(client.connected.is_set())
        self.assertTrue(wire.closed)
        self.assertEqual(wire.outgoing, [])

    def test_claim_timeout_closes_instead_of_accepting_late_grant(self):
        client, wire = self.client()
        def no_ack(data):
            wire.outgoing.append((data[3], data[6:]))
            return len(data)
        wire.write = no_ack
        self.assertFalse(client.claim(timeout=.04))
        self.assertFalse(client.lease_ready())
        wire.incoming.put(packet(protocol.STATE, board_state(owner=True)) + packet(protocol.ACK, b'\x01\0'))
        client.thread.join(1)
        self.assertTrue(wire.closed)
        self.assertFalse(client.connected.is_set())

    def test_duplicate_panel_query_is_not_replayed(self):
        client, wire = self.client(interactive=True)
        query = packet(protocol.PANEL_QUERY, b'\0\0\0\x09\x01' + 'Сколько времени?'.encode('cp1251'))
        wire.incoming.put(query + query)
        for _ in range(100):
            if len(client._queries_seen):
                break
            time.sleep(.005)
        self.assertEqual(len(client.panel_requests()), 1)
        self.assertEqual(client.panel_requests(), [])

    def test_stop_is_explicit_even_when_observer_close_is_passive(self):
        wire = Mock()
        controller = Controller(start_worker=False)
        controller.session = GrblSession(wire, controller.cancelled, Mock(), DLC32)
        controller.session.trusted = controller.session.native_panel = True
        controller.stop()
        self.assertTrue(controller.cancelled.is_set())
        wire.write.assert_called_once_with(b'\x18')

    def test_cancelled_or_noncanonical_native_job_never_claims_board(self):
        controller = Controller(start_worker=False)
        controller.session = SimpleNamespace(native_panel=True,
            wire=SimpleNamespace(port='tcp://192.168.1.8:23', peer_address='192.168.1.8'),
            settings=verified_settings())
        panel = Mock(peer_address='192.168.1.8')
        controller.cancelled.set()
        with self.assertRaises(Cancelled):
            controller.execute_job('jog', {'panel_client': panel, 'axes': Axes()})
        panel.claim.assert_not_called()
        controller.cancelled.clear()
        controller.session.settings['_fluidnc_wiring'] = 'legacy_xy'
        with self.assertRaisesRegex(ValueError, 'конфигурация'):
            controller.execute_job('jog', {'panel_client': panel, 'axes': Axes()})
        panel.claim.assert_not_called()

    def test_native_identity_is_not_inferred_from_firmware_name(self):
        self.assertEqual(native_panel_capabilities(['[VER:4.1 FluidNC v4.1.1 JD-DGUS2.1]']), (False, False))
        self.assertEqual(native_panel_capabilities(['[JD-DGUS:2,LOCAL=1,UI=1,UPTIME_MS=4,RESET_REASON=1]']), (True, True))
        for markers in (['[JD-DGUS:1,UI=1]'], ['[JD-DGUS:2,UI=1,UI=0]'],
                        ['[JD-DGUS:2,UI=1]', '[JD-DGUS:2,UI=1]']):
            self.assertEqual(native_panel_capabilities(markers), (False, False))

    def test_session_lease_checks_generation_before_any_ordinary_write(self):
        wire = Mock(peer_address='192.168.1.8', port='tcp://192.168.1.8:23')
        session = GrblSession(wire, threading.Event(), Mock(), DLC32)
        session.native_panel = session.native_owned = True
        session.native_context = (4, 3)
        panel = Mock(peer_address='192.168.1.8')
        panel.snapshot.return_value = protocol.NativeState.decode(board_state(owner=True), time.monotonic())
        session.native_client = panel
        with self.assertRaisesRegex(RuntimeError, 'изменилось'):
            session.command('M8')
        wire.write.assert_not_called()

    def test_leaving_completed_pc_operation_does_not_reset_later_local_jog(self):
        wire = Mock()
        session = GrblSession(wire, threading.Event(), Mock(), DLC32)
        session.trusted = session.native_panel = session.has_actuated = True
        session.native_owned = False
        session.close()
        wire.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
