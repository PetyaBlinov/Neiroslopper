"""Reference features use generated pixels and fake cloud only; no hardware."""
import importlib.util
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from assistant.reference_workflow import reference_request, ReviewBudget, run_job

HAS_IMAGE = all(importlib.util.find_spec(name) for name in ('cv2', 'numpy', 'PIL'))


class ReferenceRequests(unittest.TestCase):
    def test_selected_trace_keeps_rendered_route_and_neighbor_directions(self):
        from copy import deepcopy
        from types import SimpleNamespace
        from editor_tools.scene import scene_plan, render_scene
        from assistant.reference_workflow import selected_plan
        paths = [[(10, 10), (20, 10)], [(30, 10), (40, 10)]]
        obj = {'id': 'o1', 'name': 'Образец', 'source': {'kind': 'paths', 'parameters': {'paths': paths}},
               'matrix': [1, 0, 0, 1, 0, 0]}
        trace_scene = {'version': 1, 'objects': [obj], 'route': [
            {'object': 'o1', 'path': 1, 'reverse': True},
            {'object': 'o1', 'path': 0, 'reverse': False}]}
        rendered = render_scene(trace_scene, 210, 297)
        result = SimpleNamespace(plan=scene_plan(trace_scene), paths=tuple(tuple(path) for path in rendered),
                                 sheet=(210, 297))
        neighbor = deepcopy(obj)
        neighbor['id'], neighbor['name'] = 'o2', 'Сосед'
        for target_route in (None, [
                {'object': 'o2', 'path': 1, 'reverse': True},
                {'object': 'o1', 'path': 0, 'reverse': False},
                {'object': 'o2', 'path': 0, 'reverse': True},
                {'object': 'o1', 'path': 1, 'reverse': False}]):
            target = {'version': 1, 'objects': [deepcopy(obj), neighbor], 'route': target_route}
            original = deepcopy(target)
            proposal = selected_plan(result, target, 'o1')
            actual_scene = proposal.parameters['scene']
            self.assertEqual(actual_scene['objects'][1], neighbor)
            actual = render_scene(actual_scene, 210, 297)
            expected = rendered + paths if target_route is None else [list(reversed(paths[1])),
                                                                    *rendered, list(reversed(paths[0]))]
            self.assertEqual(actual, expected)
            self.assertEqual(target, original)
        invalid = SimpleNamespace(plan=result.plan, paths=tuple(reversed(result.paths)), sheet=(210, 297))
        with self.assertRaises(ValueError):
            selected_plan(invalid, original, 'o1')

    def test_ordinary_requests_enter_reference_flow(self):
        for prompt in ('Найди логотип МФТИ', 'Поищи автомобиль для рисунка', 'Нарисуй логотип университета',
                       'Нарисуй кота по фото', 'найди официальное изображение', 'нарисуй Porsche 911'):
            self.assertTrue(reference_request(prompt), prompt)
        for prompt in ('поверни рисунок на 20 градусов', 'сделай рамку', 'найди ошибку в коде',
                       'напечатай слово логотип', 'нарисуй рамку и напиши логотип', None, '', 'а'*601):
            self.assertIsNone(reference_request(prompt), prompt)

    def test_open_import_does_not_require_optional_images(self):
        script = '''
import sys
class NoImage:
    def find_spec(self, name, *args):
        if name.split('.')[0] in ('PIL', 'cv2', 'numpy'):
            raise ImportError('optional import attempted: ' + name)
sys.meta_path.insert(0, NoImage())
from assistant.reference_ui import ReferenceWindow, reference_request
assert reference_request('найди логотип')
'''
        completed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_cancelled_worker_emits_nothing(self):
        events, cancelled = queue.Queue(), threading.Event()
        cancelled.set()
        run_job(events, object(), cancelled, 'file', 'never-open-this.png')
        self.assertTrue(events.empty())

    def test_unknown_worker_job_is_plain_error(self):
        events, token = queue.Queue(), object()
        run_job(events, token, threading.Event(), 'unknown', None)
        actual_token, kind, value = events.get_nowait()
        self.assertIs(actual_token, token)
        self.assertEqual(kind, 'error')
        self.assertIsInstance(value, str)

    def test_sites_fail_to_commons_without_image_upload(self):
        from assistant.reference_images import Candidate
        from assistant.reference_workflow import search_references
        candidate = Candidate('Test', 'https://upload.wikimedia.org/a.png', '', 'https://commons.wikimedia.org/a', 'image/png')
        with patch('assistant.web_lookup.lookup', side_effect=ValueError('offline')) as lookup, \
             patch('assistant.reference_images.search', return_value=[candidate]) as search, \
             patch('assistant.reference_images.download', side_effect=ValueError('no thumbnail')):
            rows = search_references('логотип', True)
        self.assertEqual(rows[0].candidate, candidate)
        lookup.assert_called_once()
        search.assert_called_once()

    def test_private_or_executable_web_urls_are_rejected(self):
        from assistant.web_assets import public_url, global_address
        for url in ('http://example.com/a.png', 'https://127.0.0.1/a.png', 'https://device.local/a.png',
                    'file:///secret', 'https://user:password@example.com/a.png'):
            with self.assertRaises(ValueError, msg=url):
                public_url(url)
        for address in ('127.0.0.1', '192.168.1.5', '::1', '169.254.1.2', '224.0.0.1'):
            self.assertFalse(global_address(address))
        self.assertTrue(global_address('8.8.8.8'))

    def test_cancel_interrupts_slow_http_headers_without_network(self):
        from assistant.web_assets import _download
        class SlowHeaders:
            sock = None
            def __init__(self, *args, **kwargs):
                self.closed = threading.Event()
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                if not self.closed.wait(2):
                    raise AssertionError('Cancellation did not close slow HTTP headers')
                raise OSError('connection closed')
            def close(self):
                self.closed.set()
        cancelled = threading.Event()
        timer = threading.Timer(.08, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with patch('assistant.web_assets.PublicHTTPS', SlowHeaders):
                with self.assertRaises(ValueError):
                    _download('https://example.com/image.png', {'image/png'}, 10000, cancelled)
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            timer.cancel()


@unittest.skipUnless(HAS_IMAGE, 'Optional image tracing dependencies are not installed')
class ReferencePixels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PIL import Image, ImageDraw
        from assistant.image_trace import asset_from_bytes, Options
        from assistant.art_direction import build_variants
        from geometry import Sheet
        image = Image.new('RGB', (180, 150), 'white')
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 30, 160, 120), outline='black', width=3)
        draw.ellipse((55, 55, 125, 105), outline='black', width=2)
        stream = io.BytesIO()
        image.save(stream, 'PNG')
        cls.raw = stream.getvalue()
        cls.asset = asset_from_bytes(cls.raw, 'synthetic.png')
        cls.options = Options(width_mm=90, high_detail=True, kind='line')
        with patch('assistant.cloud_backend.generate', side_effect=AssertionError('local build uploaded pixels')):
            cls.variants = build_variants(cls.asset, Sheet(210, 297), cls.options)
        cls.result = cls.variants[0][1]

    def test_three_local_profiles_use_canonical_plan(self):
        from editor_tools.planner import Plan, transform_paths
        self.assertEqual([key for key, _ in self.variants], ['clean', 'balanced', 'detailed'])
        for key, result in self.variants:
            self.assertIsInstance(result.plan, Plan)
            self.assertEqual(tuple(tuple(path) for path in transform_paths([], result.plan, 210, 297)), result.paths)
            self.assertTrue(result.registration['full_frame'])
            self.assertFalse(result.image.flags.writeable)

    def test_explicit_choice_uploads_original_and_three_plain_previews(self):
        from PIL import Image
        from assistant.art_direction import choose_variants
        def fake(messages, **kwargs):
            self.assertEqual(kwargs['channel'], 'vision_review')
            self.assertEqual(len(kwargs['images']), 4)
            self.assertNotIn('synthetic.png', json.dumps(messages))
            for raw in kwargs['images']:
                with Image.open(io.BytesIO(raw)) as image:
                    self.assertLessEqual(max(image.size), 1536)
                    self.assertFalse(image.getexif())
            return json.dumps({'winner': 'balanced', 'reason': 'Читаемые линии'})
        with patch('assistant.cloud_backend.generate', side_effect=fake) as cloud:
            direction = choose_variants(self.variants, 'сохрани силуэт')
        self.assertEqual(direction.winner, 'balanced')
        cloud.assert_called_once()

    def test_review_cache_and_two_call_budget(self):
        from dataclasses import replace
        from assistant.trace_review import Review
        budget = ReviewBudget()
        report = Review('Читаемый силуэт', 'faithful', (), ())
        with patch('assistant.trace_review.review', return_value=report) as cloud:
            self.assertIs(budget.review(self.result), report)
            self.assertIs(budget.review(self.result), report)
            second = replace(self.result, paths=self.result.paths[:-1])
            budget.review(second)
            third = replace(self.result, paths=self.result.paths[1:])
            with self.assertRaises(ValueError):
                budget.review(third)
            self.assertEqual(cloud.call_count, 2)

    def test_review_cache_distinguishes_focus_crop(self):
        from dataclasses import replace
        from assistant.trace_review import fingerprint
        focused = replace(self.result, options=replace(self.result.options, subject_box=(10, 10, 100, 100)))
        self.assertNotEqual(fingerprint(self.result), fingerprint(focused))

    def test_reference_replaces_selected_object_preserving_other_objects(self):
        from copy import deepcopy
        from editor_tools.scene import checked_scene
        from assistant.reference_workflow import selected_plan
        scene = deepcopy(self.result.plan.parameters['scene'])
        other = deepcopy(scene['objects'][0])
        other['id'], other['name'] = 'o2', 'Сохранить этот объект'
        scene['objects'].append(other)
        scene['route'] = None
        original = deepcopy(scene)
        proposal = selected_plan(self.variants[-1][1], scene, scene['objects'][0]['id'])
        checked_scene(proposal.parameters['scene'], *self.result.sheet)
        self.assertEqual(proposal.parameters['scene']['objects'][1], other)
        self.assertEqual(scene, original)
        with self.assertRaises(ValueError):
            selected_plan(self.result, scene, 'o99')

    def test_visual_review_original_trace_and_focus_crop_pairs(self):
        from dataclasses import replace
        from assistant.trace_review import image_inputs, review
        result = replace(self.result, options=replace(self.result.options, subject_box=(20, 30, 120, 110)))
        images, crops = image_inputs(result)
        self.assertEqual(len(images), 4)
        self.assertEqual(len(crops), 1)
        response = json.dumps({'summary': 'Проверьте угол', 'assessment': 'lost_detail',
            'issues': [{'box': [100, 100, 500, 500], 'description': 'Пропала грань',
                        'action': 'recover_detail', 'confidence': 'medium'}], 'uncertainties': []})
        with patch('assistant.cloud_backend.generate', return_value=response) as cloud:
            report = review(result)
        self.assertEqual(report.issues[0]['action'], 'recover_detail')
        self.assertEqual(len(cloud.call_args.kwargs['images']), 4)

    def test_model_cannot_add_motion_or_unbounded_repairs(self):
        from assistant.trace_review import checked_review, Review, repair
        base = {'summary': 'Оценка', 'assessment': 'lost_detail', 'issues': [], 'uncertainties': []}
        for issue in ({'box': [0, 0, 100, 100], 'description': 'foo', 'action': 'G0 X100', 'confidence': 'high'},):
            with self.assertRaises(ValueError):
                checked_review(json.dumps({**base, 'issues': [issue]}))
        issue = {'box': [0, 0, 200, 200], 'description': 'foo', 'action': 'simplify', 'confidence': 'high'}
        with self.assertRaises(ValueError):
            checked_review(json.dumps({**base, 'issues': [issue] * 5}))
        with self.assertRaises(ValueError):
            checked_review('{"summary":"one","summary":"two"}')
        report = Review('Оценка', 'lost_detail', (issue,), ())
        with patch('assistant.trace_review.trace', return_value=self.result) as local, \
             patch('assistant.cloud_backend.generate', side_effect=AssertionError('local repair uploaded')):
            repaired = repair(self.asset, self.result, report)
        self.assertIs(repaired, self.result)
        self.assertEqual(len(local.call_args.args[2].repairs), 1)

    def test_svg_is_local_validated_vector(self):
        from dataclasses import replace
        from assistant.image_trace import asset_from_bytes
        from assistant.art_direction import build_variants
        from geometry import Sheet
        raw = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><path d="M10 10 L80 10 L80 80 Z"/></svg>'
        asset = asset_from_bytes(raw, 'shape.svg')
        with patch('assistant.cloud_backend.generate', side_effect=AssertionError('SVG uploaded')):
            variants = build_variants(asset, Sheet(210, 297), replace(self.options, kind='auto'))
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0][1].kind, 'vector')


if __name__ == '__main__':
    unittest.main()
