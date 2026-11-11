"""UI6 application changes: fake devices, fake model, no real session or LAN."""
from pathlib import Path
import json
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from assistant import cloud_backend
from assistant.saved_chat import thread_id, save, load
from assistant.vector_drawing import answer
from assistant_ui import prepare_reply
from controller import GrblSession, board_runtime_info, native_panel_capabilities
from editor_tools.preview import snapshot
from editor_tools.planner import transform_paths
from machine_profiles import DLC32
from wifi_discovery import board_mac
from wifi_reconnect import Reconnect, _search

IDENTIFIER = '12345678-1234-1234-1234-123456789abc'
MAC = 'AA-BB-CC-DD-EE-FF'
NETWORK = '[MSG:Mode=STA:SSID=test:Status=Connected:IP=192.168.1.8:MAC=' + MAC + ']'


class NetworkAndChatTests(unittest.TestCase):
    def test_wrapped_dgus_marker_and_duplicate_are_checked(self):
        marker = '[JD-DGUS:2,UI=1,UPTIME_MS=30,RESET_REASON=1]'
        wrapped = '[MSG:' + marker + ']'
        self.assertEqual(native_panel_capabilities([wrapped]), (True, True))
        self.assertEqual(board_runtime_info([wrapped])['firmware_uptime_ms'], 30)
        self.assertEqual(native_panel_capabilities([marker, wrapped]), (False, False))

    def test_mac_is_single_and_rechecked_before_config(self):
        self.assertEqual(board_mac([NETWORK]), MAC)
        self.assertEqual(board_mac([NETWORK, NETWORK]), '')
        session = GrblSession(Mock(), threading.Event(), Mock(), DLC32)
        session.command = Mock(return_value=['[VER:4.1 FluidNC v4.1.1:]', NETWORK])
        session.refresh_settings = Mock()
        with self.assertRaisesRegex(RuntimeError, 'другая плата'):
            session.initialize(expected_mac='00-00-00-00-00-00')
        session.refresh_settings.assert_not_called()
        self.assertFalse(session.trusted)

    def test_reconnect_constructor_is_idle_and_stop_ignores_late_reply(self):
        with patch('wifi_reconnect.threading.Thread') as worker:
            service = Reconnect()
            self.assertIsNone(service.poll())
            worker.assert_not_called()
            service.arm('tcp://192.168.1.8:23', MAC)
            token = service.token
            service.disarm()
            service.events.put((token, 'tcp://192.168.1.9:23'))
            self.assertIsNone(service.poll())
            worker.assert_not_called()

    def test_search_uses_bound_mac_and_cancellation(self):
        events, stop, token = queue.Queue(), threading.Event(), object()
        with patch('board_discovery.discover_board', return_value='192.168.1.9') as search:
            _search(events, token, stop, 'tcp://192.168.1.8:23', MAC)
            search.assert_called_once_with('192.168.1.8', MAC)
        self.assertEqual(events.get_nowait(), (token, 'tcp://192.168.1.9:23'))
        stop.set()
        with patch('board_discovery.discover_board', return_value='192.168.1.9'):
            _search(events, token, stop, 'tcp://192.168.1.8:23', MAC)
        self.assertTrue(events.empty())

    def test_saved_chat_validates_identifier_and_removes_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(load(directory))
            save(directory, 'codex://threads/' + IDENTIFIER)
            self.assertEqual(load(directory), IDENTIFIER)
            save(directory, '')
            self.assertIsNone(load(directory))
        for value in ('--last', '../file', 'https://example.com/chat', IDENTIFIER + ';echo x'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                thread_id(value)

    def test_resume_keeps_isolation_and_only_explicit_session(self):
        args = cloud_backend.resume_command('codex.exe', Path('test-folder'), IDENTIFIER)
        self.assertEqual(args[2:4], ['exec', 'resume'])
        self.assertEqual(args[-2:], [IDENTIFIER, '-'])
        self.assertNotIn('--ephemeral', args)
        self.assertIn('sandbox_mode="read-only"', args)
        for feature in cloud_backend.DISABLED_FEATURES:
            self.assertIn(feature, args)
        with self.assertRaises(ValueError):
            cloud_backend.resume_command('codex.exe', Path('.'), '--last')

    def test_abort_then_close_sends_only_one_successful_reset(self):
        wire = Mock()
        wire.write.return_value = 1
        session = GrblSession(wire, threading.Event(), Mock(), DLC32)
        session.trusted = session.has_actuated = True
        session.abort()
        session.close()
        wire.write.assert_called_once_with(b'\x18')


class VectorWebTests(unittest.TestCase):
    def setUp(self):
        self.before = snapshot({'sheet_width': 210, 'sheet_height': 297, 'busy': False}, [], ('test', 1))
        self.state = dict(self.before.state)
        self.payload = {'kind': 'preview', 'explanation': 'Пробный контур',
                        'sources': [{'title': 'Образец', 'url': 'https://example.com/shape'}],
                        'paths': ['M20 30 L40 10 Q45 20 60 30']}

    def test_model_returns_only_checked_vector_proposal(self):
        with patch('assistant.cloud_backend.generate', return_value=json.dumps(self.payload)) as model:
            reply = answer('Нарисуй Porsche 911 по фото', self.state)
        self.assertEqual(model.call_args.kwargs['channel'], 'web_drawing')
        self.assertNotIn('images', model.call_args.kwargs)
        paths = transform_paths([], reply.plan, 210, 297)
        self.assertEqual(paths[0][0], (20, 30))
        self.assertIn('example.com', reply.text)
        self.assertEqual(self.before.paths, ())

    def test_missing_sources_code_and_out_of_sheet_are_rejected(self):
        for changed in ({'sources': []}, {'paths': ['M20 30 <script>']},
                        {'paths': ['M20 30 L400 10']}, {'paths': ['M20 30 LNaN 4']}):
            with self.subTest(changed=changed), patch('assistant.cloud_backend.generate',
                    return_value=json.dumps({**self.payload, **changed})), self.assertRaises(ValueError):
                answer('Нарисуй Porsche по фото', self.state)

    def test_cancelled_request_never_calls_cloud(self):
        stop = threading.Event()
        stop.set()
        with patch('assistant.cloud_backend.generate') as model:
            self.assertIsNone(answer('Нарисуй Porsche по фото', self.state, stop).plan)
        model.assert_not_called()

    def test_worker_stages_without_controller_or_main_edit(self):
        events, token = queue.Queue(), object()
        with patch('assistant.cloud_backend.generate', return_value=json.dumps(self.payload)):
            prepare_reply(Mock(), 'Нарисуй Porsche по фото', self.before,
                {**self.state, 'web_drawing': True}, True, threading.Event(), token, events)
        received, kind, proposal = events.get_nowait()
        self.assertIs(received, token)
        self.assertEqual(kind, 'reply')
        self.assertIsNotNone(proposal.reply.plan)
        self.assertEqual(self.before.paths, ())


if __name__ == '__main__':
    unittest.main()
