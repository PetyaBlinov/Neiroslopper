"""Panel data only: no hardware, microphone, model download or cloud request."""
from pathlib import Path
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assistant.runtime import Reply
from assistant.voice import VoiceEvent, Transcript
from dwin_assistant import PanelAssistant, DONE, ERROR, panel_text, sheet_segments
from editor_tools.preview import snapshot


class FakeVoice:
    def __init__(self, events):
        self.events = events
        self.token = 0
        self.starts = self.stops = self.cancels = self.closes = 0
        self.recognitions = []

    def start(self, device=None):
        self.starts += 1
        self.token += 1
        return self.token

    def stop(self):
        self.stops += 1

    def recognize(self, clip, *, allow_download):
        self.recognitions.append((clip, allow_download))
        self.token += 1
        return self.token

    def cancel(self):
        self.cancels += 1

    def close(self):
        self.closes += 1


class PanelAssistantTests(unittest.TestCase):
    def setUp(self):
        self.output = []
        self.backend = Mock()
        self.backend.answer.return_value = Reply('Use the two corners. Отметьте два угла.')
        self.review = Mock(return_value=True)
        self.assistant = PanelAssistant(lambda *args: self.output.append(args), assistant=self.backend,
                                        voice_factory=FakeVoice, review_voice=self.review)
        self.addCleanup(self.assistant.close)
        self.snapshot = snapshot({'sheet_width': 210, 'sheet_height': 297,
                                  'file_name': 'Тест', 'pen_lift_enabled': True},
                                 [[(1, 1), (20, 20)]], ('draft', 1))

    def finish(self):
        for _ in range(100):
            self.assistant.pump()
            if not self.assistant.busy:
                return
            time.sleep(.005)
        self.fail('Background fixture did not finish')

    def test_ru_en_read_only_answer_never_receives_editor_geometry(self):
        self.assertTrue(self.assistant.start(9, 'Как задать лист? How to set paper?', self.snapshot))
        self.finish()
        args, kwargs = self.backend.answer.call_args
        self.assertEqual(args[1]['file_name'], 'Тест')
        self.assertNotIn('paths', args[1])
        self.assertFalse(kwargs['allow_creative'])
        self.assertEqual(self.output[-1], (9, DONE, 'Use the two corners. Отметьте два угла.'))
        self.assertEqual(self.snapshot.paths, (((1.0, 1.0), (20.0, 20.0)),))

    def test_proposed_plan_or_variants_are_not_applied_or_sent_to_panel(self):
        self.backend.answer.return_value = Reply('draw', plan=object())
        self.assistant.start(9, 'Нарисуй', self.snapshot)
        self.finish()
        self.assertEqual(self.output[-1][1], ERROR)
        self.assertIn('ПК', self.output[-1][2])
        self.assertEqual(self.snapshot.revision, ('draft', 1))

    def test_voice_is_local_then_reviewed_on_pc_never_automatically_sent(self):
        self.assertTrue(self.assistant.start_voice(19, self.snapshot))
        voice = self.assistant._voice
        self.assertEqual(voice.starts, 1)
        self.assertTrue(self.assistant.stop_voice(19))
        self.assertEqual(voice.stops, 1)
        clip = object()
        voice.events.put(VoiceEvent(1, 'recorded', clip))
        voice.events.put(VoiceEvent(1, 'finished'))
        self.assistant.pump()
        self.assertEqual(voice.recognitions, [(clip, False)])
        voice.events.put(VoiceEvent(2, 'result', Transcript('Напиши привет', ('Проверьте число.',))))
        voice.events.put(VoiceEvent(2, 'finished'))
        self.assistant.pump()
        self.review.assert_called_once_with('Напиши привет')
        self.backend.answer.assert_not_called()
        self.assertEqual(self.output[-1][1], DONE)
        self.assertIn('Проверьте', self.output[-1][2])
        self.assertFalse(self.assistant.busy)

    def test_late_cancelled_voice_or_answer_cannot_reach_a_new_request(self):
        self.assistant.start_voice(19, self.snapshot)
        self.assertFalse(self.assistant.cancel(20))
        self.assertTrue(self.assistant.busy)
        self.assertTrue(self.assistant.cancel(19))
        self.assertFalse(self.assistant.busy)
        self.assistant.start_voice(20, self.snapshot)
        voice = self.assistant._voice
        voice.events.put(VoiceEvent(1, 'result', Transcript('Старый запрос')))
        voice.events.put(VoiceEvent(1, 'finished'))
        self.assistant.pump()
        self.review.assert_not_called()
        self.backend.answer.assert_not_called()
        self.assertTrue(self.assistant.busy)

    def test_capture_error_does_not_download_model_or_send_request(self):
        self.assistant.start_voice(19, self.snapshot)
        voice = self.assistant._voice
        voice.events.put(VoiceEvent(1, 'error', 'Нет микрофона'))
        voice.events.put(VoiceEvent(1, 'finished'))
        self.assistant.pump()
        self.assertEqual(self.output[-1], (19, ERROR, 'Нет микрофона'))
        self.assertEqual(voice.recognitions, [])
        self.backend.answer.assert_not_called()

    def test_preview_is_a_bounded_fresh_sheet_snapshot(self):
        paths = [[(x, y), (x + .4, y + .4)] for x in range(0, 200, 4) for y in range(0, 280, 4)]
        snap = snapshot(self.snapshot.state, paths, ('fresh', 2))
        bbox, lines = sheet_segments(snap)
        self.assertEqual(len(lines), 160)
        for row in (bbox, *lines):
            self.assertTrue(all(0 <= row[i] < (420 if i % 2 == 0 else 278) for i in range(4)))
        self.assertEqual(sheet_segments(self.snapshot)[1], ((bbox[0] + 1, bbox[1] + 1,
                                                           bbox[0] + round(20 * (bbox[2] - bbox[0]) / 210),
                                                           bbox[1] + round(20 * (bbox[3] - bbox[1]) / 297)),))
        with self.assertRaises(ValueError):
            sheet_segments(snap, max_segments=161)

    def test_panel_text_preserves_inner_content_and_bounds_length(self):
        value = panel_text('## Ответ\n**Белая** бумага, [пример](https://example.com), ' + 'длинный ' * 100)
        self.assertLessEqual(len(value), 112)
        self.assertIn('Белая бумага, пример', value)
        self.assertNotIn('**', value)


if __name__ == '__main__':
    unittest.main()
