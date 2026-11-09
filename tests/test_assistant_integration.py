"""Assistant proposals edit the sketch only; no real ports or model requests."""
from copy import deepcopy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from controller import Controller
from editor_tools.planner import Plan
from tk_test_support import close_tk_app


class AssistantIntegrationTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        with patch("app.Controller", side_effect=lambda **kw: Controller(start_worker=False, **kw)), \
                patch("serial.tools.list_ports.comports", return_value=[]):
            self.app = App(self.root, data_dir=None)
        self.root.update()

    def tearDown(self):
        self.app.controller.busy = False
        self.app.svg_loading = False
        close_tk_app(self)

    def wait_for(self, predicate, timeout=4):
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            self.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail("Assistant's local request did not finish")

    def text_plan(self):
        return Plan("print_text", {"text": "Привет", "height_mm": 5,
                                   "spacing_mm": 1}, "Надпись Привет")

    def seed_paths(self):
        self.app.paths = [[(5., 5.), (15., 5.), (15., 15.)]]
        self.app.loaded()

    def window(self):
        self.app.open_assistant()
        window = self.app.assistant_window
        window.root.withdraw()
        self.root.update()
        return window

    def request(self, window, prompt):
        window.entry.delete("1.0", "end")
        window.entry.insert("1.0", prompt)
        window.send()
        self.wait_for(lambda: not window.busy)

    @staticmethod
    def min_x(paths):
        return min(x for path in paths for x, _ in path)

    def test_applying_proposal_and_manual_edit_share_undo_redo(self):
        self.app.apply_editor_preview(self.app.editor_snapshot(), self.text_plan())
        text_paths = deepcopy(self.app.paths)
        self.app.apply_assistant_preview(self.app.editor_snapshot(),
                                        Plan("move_preview", {"dx_mm": 5, "dy_mm": 0}, "Сдвиг на 5 мм"))
        moved_paths = deepcopy(self.app.paths)
        self.assertNotEqual(moved_paths, text_paths)
        self.assertIsInstance(self.app.editor_task, Plan)
        self.app.undo_editor_preview()
        self.assertEqual(self.app.paths, text_paths)
        self.app.redo_editor_preview()
        self.assertEqual(self.app.paths, moved_paths)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)
        self.assertIsNone(self.app.controller.session)
        self.assertFalse(self.app.zero)
        self.assertIsNone(self.app.sheet)

    def test_app_journal_records_action_codes_without_user_content(self):
        from eventlog import EventLog
        with tempfile.TemporaryDirectory() as directory:
            self.app.eventlog = EventLog(directory)
            self.assertTrue(self.app.submit("check_endstops"))
            self.app.controller.busy = False
            self.root.after_cancel(self.app.pump_timer)
            self.app.pump_timer = None
            self.app.controller.events.put(("connected", "DEMO"))
            self.app.controller.events.put(("error", {"error_type": "ValueError",
                "message": "private text password=secret", "log_path": "C:/private/plotter.json"}))
            with patch("app.messagebox.showerror"):
                self.app.pump()
            self.app.stop()
            content = self.app.eventlog.path.read_text(encoding="ascii")
            records = [json.loads(line) for line in content.splitlines()]
            names = {item["event"] for item in records}
            self.assertTrue({"action.accepted", "connection.opened", "action.error", "app.stop"} <= names)
            for text in ("private", "password", "secret", "plotter.json"):
                self.assertNotIn(text, content)
            self.app.eventlog = EventLog(None)

    def test_invalid_stale_busy_and_motor_plans_leave_entire_sketch_unchanged(self):
        self.seed_paths()
        for reason in ("stale", "busy", "svg", "bounds", "motor"):
            with self.subTest(reason=reason):
                before = self.app.editor_snapshot()
                paths = self.app.paths
                history = deepcopy(self.app.editor_history.past)
                metadata = (self.app.file_name, self.app.editor_task, self.app.editor_anchor)
                plan = Plan("move_preview", {"dx_mm": 2, "dy_mm": 0}, "Сдвиг")
                if reason == "stale":
                    self.app.preview_revision += 1
                elif reason == "busy":
                    self.app.controller.busy = True
                elif reason == "svg":
                    self.app.svg_loading = True
                elif reason == "bounds":
                    plan = Plan("move_preview", {"dx_mm": 1000, "dy_mm": 0}, "За лист")
                else:
                    plan = Plan("jog", {"distance": 10}, "Движение запрещено")
                with self.assertRaises(ValueError):
                    self.app.apply_assistant_preview(before, plan)
                self.assertIs(self.app.paths, paths)
                self.assertEqual(self.app.editor_history.past, history)
                self.assertEqual((self.app.file_name, self.app.editor_task, self.app.editor_anchor), metadata)
                self.assertEqual(self.app.controller.jobs.qsize(), 0)
                self.app.controller.busy = self.app.svg_loading = False

    def test_assistant_text_keeps_editable_metadata_through_saved_project(self):
        from editor_project import save_project, load_project
        from editor_tools.scene import from_plan
        self.app.apply_assistant_preview(self.app.editor_snapshot(), self.text_plan())
        scene = from_plan(self.app.editor_task, self.app.paths, 210, 297)
        self.assertEqual(scene["objects"][0]["source"]["kind"], "print_text")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.plotter.json"
            save_project(path, self.app.drawing_sheet(), self.app.paths,
                         self.app.file_name, self.app.editor_task, self.app.editor_anchor)
            restored = load_project(path)
        self.assertIsInstance(restored.task, Plan)
        self.assertEqual(restored.task.parameters["text"], "Привет")
        self.assertEqual(restored.paths, self.app.paths)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_assistant_window_is_singleton_and_codex_is_opt_in(self):
        window = self.window()
        self.app.open_assistant()
        self.assertIs(self.app.assistant_window, window)
        self.assertFalse(window.use_codex.get())
        self.assertFalse(self.app.connected)
        self.assertIsNone(self.app.controller.session)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_local_text_request_is_preview_only_until_explicit_apply(self):
        window = self.window()
        window.entry.insert("1.0", "Напиши печатными «Привет» высотой 5 мм")
        with patch.object(self.app.controller, "submit", wraps=self.app.controller.submit) as submit:
            window.send()
            self.wait_for(lambda: window.pending is not None)
            self.assertEqual(self.app.paths, [])
            self.assertEqual(str(window.apply_button["state"]), "normal")
            self.assertEqual(self.app.editor_history.past, [])
            window.apply_proposal()
            self.assertTrue(self.app.paths)
            self.assertIsNone(window.pending)
            submit.assert_not_called()
        self.assertEqual(self.app.controller.jobs.qsize(), 0)
        self.assertFalse(self.app.zero)
        self.assertIsNone(self.app.sheet)
        self.app.undo_editor_preview()
        self.assertEqual(self.app.paths, [])
        self.app.redo_editor_preview()
        self.assertTrue(self.app.paths)

    def test_editor_change_invalidates_pending_proposal(self):
        window = self.window()
        window.entry.insert("1.0", "Напиши печатными «Привет» высотой 5 мм")
        window.send()
        self.wait_for(lambda: window.pending is not None)
        self.app.preview_revision += 1
        self.app.invalidate_editor()
        self.assertIsNone(window.pending)
        self.assertEqual(str(window.apply_button["state"]), "disabled")
        self.assertEqual(self.app.paths, [])
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_cancelling_drops_a_late_local_result_without_editing(self):
        window = self.window()
        window.entry.insert("1.0", "Напиши печатными «Привет» высотой 5 мм")
        window.send()
        window.cancel_request()
        limit = time.monotonic() + .15
        while time.monotonic() < limit:
            self.root.update()
            time.sleep(.01)
        window.pump_results()
        self.assertIsNone(window.pending)
        self.assertEqual(self.app.paths, [])
        self.assertEqual(self.app.editor_history.past, [])
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_followup_changes_unapplied_text_proposal_then_applies_whole_result(self):
        window = self.window()
        self.request(window, "Напиши печатными «Привет» высотой 5 мм")
        self.assertIsNotNone(window.pending)
        first = deepcopy(window.pending.paths)
        self.request(window, "Сдвинь рисунок вправо на 5 мм")
        self.assertIsNotNone(window.pending)
        self.assertAlmostEqual(self.min_x(window.pending.paths) - self.min_x(first), 5)
        proposed = deepcopy(window.pending.paths)
        self.assertEqual(self.app.paths, [])
        window.apply_proposal()
        self.assertEqual(self.app.paths, proposed)
        self.assertEqual(len(self.app.editor_history.past), 1)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_readonly_question_preserves_valid_proposal_for_continued_editing(self):
        window = self.window()
        self.request(window, "Напиши печатными «Привет» высотой 5 мм")
        proposed = deepcopy(window.pending.paths)
        self.request(window, "Что я просил?")
        self.assertIsNotNone(window.pending)
        self.assertEqual(window.pending.paths, proposed)
        self.assertEqual(str(window.apply_button["state"]), "normal")
        self.request(window, "Сдвинь рисунок вправо на 5 мм")
        self.assertIsNotNone(window.pending)
        self.assertAlmostEqual(self.min_x(window.pending.paths) - self.min_x(proposed), 5)
        self.assertEqual(self.app.paths, [])
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_rejected_followup_keeps_last_valid_proposal_and_sketch_unchanged(self):
        window = self.window()
        self.request(window, "Напиши печатными «Привет» высотой 5 мм")
        proposed = deepcopy(window.pending.paths)
        self.request(window, "Увеличь рисунок до 10000%")
        self.assertIsNotNone(window.pending)
        self.assertEqual(window.pending.paths, proposed)
        memory = window.memory.snapshot(self.app.editor_revision())
        self.assertEqual(memory["outcome"], "rejected")
        self.assertTrue(memory["validation_error"])
        self.assertIn("10000%", memory["request"])
        self.assertEqual(self.app.paths, [])
        self.assertEqual(self.app.editor_history.past, [])
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_ui_apply_rejection_does_not_change_paths_or_history(self):
        self.seed_paths()
        window = self.window()
        self.request(window, "Сдвинь рисунок вправо на 5 мм")
        self.assertIsNotNone(window.pending)
        paths = self.app.paths
        history = deepcopy(self.app.editor_history.past)
        metadata = (self.app.file_name, self.app.editor_task, self.app.editor_anchor)
        self.app.preview_revision += 1
        window.apply_proposal()
        self.assertIs(self.app.paths, paths)
        self.assertEqual(self.app.editor_history.past, history)
        self.assertEqual((self.app.file_name, self.app.editor_task, self.app.editor_anchor), metadata)
        self.assertIsNone(window.pending)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
