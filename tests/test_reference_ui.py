"""Reference workspace lifecycle and proposal boundary, with no real cloud."""
import gc
import importlib.util
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HAS_IMAGE = all(importlib.util.find_spec(name) for name in ('cv2', 'numpy', 'PIL'))


@unittest.skipUnless(HAS_IMAGE, 'Optional image tracing dependencies are not installed')
class ReferenceWindowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from assistant.reference_ui import ReferenceWindow
        from editor_tools.preview import snapshot
        from ui_theme import apply_theme
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        apply_theme(self.root)
        class Owner:
            reference_window = None
            proposals = []
            version = 0
            def current_snapshot(self):
                return snapshot({'sheet_width': 210, 'sheet_height': 297, 'busy': False}, [], (self.version,))
            def offer_external_plan(self, before, plan, label):
                if before.revision != self.current_snapshot().revision:
                    return False
                from editor_tools.planner import transform_paths
                transform_paths([], plan, 210, 297)
                self.proposals.append((before, plan, label))
                return True
        self.owner = Owner()
        self.owner.root = self.root
        self.owner.proposals = []
        self.cloud = patch('assistant.cloud_backend.generate', side_effect=AssertionError('unexpected live cloud'))
        self.cloud_mock = self.cloud.start()
        self.window = ReferenceWindow(self.owner, 'логотип')
        self.owner.reference_window = self.window
        self.root.update()

    def tearDown(self):
        if getattr(self, 'window', None):
            self.window.close()
            if self.window.worker:
                self.window.worker.join(timeout=5)
            self.window = None
        self.cloud.stop()
        self.root.destroy()
        self.root = self.owner = None
        gc.collect()

    def wait_job(self):
        deadline = time.monotonic() + 15
        while self.window.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.02)
        self.assertFalse(self.window.busy, self.window.status.get())

    def create_asset(self):
        from PIL import Image, ImageDraw
        from assistant.image_trace import asset_from_bytes
        image = Image.new('RGB', (120, 120), 'white')
        ImageDraw.Draw(image).ellipse((15, 15, 105, 105), outline='black', width=3)
        raw = io.BytesIO()
        image.save(raw, 'PNG')
        return asset_from_bytes(raw.getvalue(), 'test.png')

    def test_open_is_idle_without_upload_or_search(self):
        self.assertFalse(self.window.busy)
        self.assertIsNone(self.window.worker)
        self.assertEqual(self.window.query.get(), 'логотип')
        self.assertEqual(self.owner.proposals, [])
        self.cloud_mock.assert_not_called()

    def test_routed_request_keeps_width_and_anchor_in_build_options(self):
        from assistant.reference_ui import ReferenceWindow
        self.window.close()
        self.window = ReferenceWindow(self.owner,
            'Нарисуй Porsche 911 шириной 80 мм в левом верхнем углу')
        self.owner.reference_window = self.window
        self.window.asset = self.create_asset()
        self.assertEqual(self.window.width.get(), '80')
        self.assertEqual(self.window.anchor.get(), 'Сверху слева')
        self.assertIn('Porsche', self.window.query.get())
        with patch.object(self.window, 'start_job') as job:
            self.window.build_variants()
        options = job.call_args.args[1][2]
        self.assertEqual(options.width_mm, 80)
        self.assertEqual(options.anchor, 'top_left')
        self.cloud_mock.assert_not_called()

    def test_local_build_proposes_only_after_explicit_click(self):
        self.window.asset = self.create_asset()
        self.window.build_variants()
        self.assertIsNone(getattr(self.window.worker._target, '__self__', None))
        self.wait_job()
        self.assertEqual(len(self.window.variants), 3)
        self.assertIsNotNone(self.window.result, self.window.status.get())
        self.assertEqual(self.owner.proposals, [])
        self.window.propose()
        self.assertEqual(len(self.owner.proposals), 1)
        self.assertEqual(self.owner.current_snapshot().paths, ())
        self.cloud_mock.assert_not_called()

    def test_stale_snapshot_never_proposes(self):
        self.window.asset = self.create_asset()
        self.window.build_variants()
        self.wait_job()
        self.owner.version += 1
        self.window.propose()
        self.assertEqual(self.owner.proposals, [])
        self.assertIn('изменился', self.window.status.get())

    def test_cancel_ignores_late_result_and_close_clears_owner(self):
        token = self.window.token
        self.window.cancel_job()
        self.window.events.put((token, 'file', self.create_asset()))
        self.window.pump()
        self.assertIsNone(self.window.asset)
        self.window.close()
        self.assertTrue(self.window.closed)
        self.assertIsNone(self.owner.reference_window)

    def test_denied_upload_does_not_start_cloud_worker(self):
        self.window.asset = self.create_asset()
        self.window.build_variants()
        self.wait_job()
        with patch('assistant.reference_ui.messagebox.askyesno', return_value=False) as consent:
            self.window.choose()
            self.window.review()
        self.assertEqual(consent.call_count, 2)
        self.assertFalse(self.window.busy)
        self.cloud_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
