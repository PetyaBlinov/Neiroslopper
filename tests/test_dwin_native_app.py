"""Native panel UI handoff, using hidden windows and an in-memory board."""
from pathlib import Path
import queue
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from dwin_native import NativeState, STATE_STRUCT, ASK, MIC_START, MIC_STOP, VIEW_SHEET
from dwin_ui import NativeDwinWindow
from tk_test_support import close_tk_app


def board_state(*, request=22, generation=5):
    return NativeState.decode(STATE_STRUCT.pack(1 | 2 | 4 | 32, 1, 50,
        2000, 3000, 1000, 1000, 210000, 297000, generation, 4,
        request, 13 if request else 0, 10 if request else 0), time.monotonic())


class NativePanelAppTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, data_dir=None)
        self.root.update()
        self.app.native_panel = True

    def tearDown(self):
        close_tk_app(self)

    def window(self):
        window = self.app.dwin_window = NativeDwinWindow(self.app, show=False)
        return window

    def test_opening_window_does_not_connect_record_or_download(self):
        with patch('dwin_native.NativeDwinClient') as client, \
                patch('assistant.voice.VoiceService.start') as record:
            window = self.window()
            self.assertIsNone(window.bridge)
            self.assertIsNone(window.panel_assistant)
            self.assertFalse(window.helper_enabled.get())
            self.assertFalse(window.mic_enabled.get())
            client.assert_not_called()
            record.assert_not_called()

    def test_state_updates_calibration_without_running_pending_draw(self):
        self.app.paths = [[(1, 1), (20, 20)]]
        before = self.app.paths
        with patch.object(self.app, 'draw') as draw, patch.object(self.app, 'submit') as submit:
            self.app.apply_panel_state(board_state())
            self.assertTrue(self.app.zero)
            self.assertEqual((self.app.sheet.width, self.app.sheet.height), (210, 297))
            self.assertEqual(self.app.position, (1.0, 2.0))
            self.assertIs(self.app.paths, before)
            self.assertTrue(self.app.panel_request_button.winfo_manager())
            draw.assert_not_called()
            submit.assert_not_called()
        self.app.apply_panel_state(None)
        self.assertFalse(self.app.zero)
        self.assertIsNone(self.app.sheet)
        self.assertIs(self.app.paths, before)

    def test_explicit_request_click_keeps_identity_for_worker_recheck(self):
        window = self.window()
        panel = window.bridge = Mock()
        panel.snapshot.return_value = board_state()
        self.app.paths = [[(1, 1), (20, 20)]]
        with patch.object(self.app, 'draw') as draw:
            self.app.handle_panel_request()
            draw.assert_called_once_with(panel_request=(4, 5, 22, 13))
        panel.claim.assert_not_called()

    def test_calibration_routes_to_board_not_pc_coordinate_writes(self):
        window = self.window()
        panel = window.bridge = Mock()
        panel.snapshot.return_value = board_state(request=0)
        with patch.object(self.app.controller, 'submit') as submit:
            self.assertTrue(self.app.submit('zero'))
            self.assertTrue(self.app.submit('corner'))
            self.assertTrue(self.app.submit('clear'))
        self.assertEqual([call.args for call in panel.calibrate.call_args_list], [(1,), (2,), (0,)])
        submit.assert_not_called()

    def test_voice_draft_opens_assistant_without_sending_or_drawing(self):
        window = self.window()
        window.mic_enabled.set(True)
        assistant = SimpleNamespace(insert_voice_text=Mock(return_value=True), send=Mock())
        with patch.object(self.app, 'open_assistant') as open_assistant, \
                patch.object(self.app, 'draw') as draw:
            self.app.assistant_window = assistant
            self.assertTrue(window.review_voice('Напиши привет'))
            self.app.assistant_window = None
        open_assistant.assert_called_once()
        assistant.insert_voice_text.assert_called_once_with('Напиши привет')
        assistant.send.assert_not_called()
        draw.assert_not_called()

    def test_disabled_permissions_reject_questions_and_mic_but_allow_sheet(self):
        window = self.window()
        assistant = window.panel_assistant = Mock(voice_pending=False)
        bridge = Mock()
        bridge.panel_requests.return_value = [(11, ASK, 'Вопрос'), (12, MIC_START, ''), (13, VIEW_SHEET, '')]
        window.service_assistant(bridge)
        assistant.start.assert_not_called()
        assistant.start_voice.assert_not_called()
        self.assertEqual(bridge.panel_answer.call_count, 2)
        bridge.panel_sheet.assert_called_once()
        args = bridge.panel_sheet.call_args.args
        self.assertEqual(args[0], 13)
        self.assertEqual(args[2], ())
        self.assertLess(args[1][3], 278)


if __name__ == '__main__':
    unittest.main()
