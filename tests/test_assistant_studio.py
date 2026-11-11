"""Complete assistant workflows with local geometry and fake streamed replies."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from assistant.runtime import Reply
from assistant_studio import SandboxDocument, scene_for, selected_request
from controller import Controller
from editor_tools.planner import Plan
from editor_tools.scene import scene_plan, render_scene
from editor_tools.studio_store import Templates
from tk_test_support import close_tk_app


def text_plan():
    return Plan("print_text", {"text": "Анна", "height_mm": 5, "spacing_mm": 1}, "Надпись")


class SandboxDocumentTests(unittest.TestCase):
    def test_pronoun_requires_existing_explicit_selection_and_preserves_rest_of_command(self):
        scene = {"objects": [{"id": "o1"}]}
        prompt = "Сдвинь его вправо на 2 мм и поверни на 15 градусов"
        self.assertEqual(selected_request(prompt, {"studio_scene": scene}), prompt)
        self.assertEqual(selected_request(prompt, {"studio_scene": scene, "studio_selected": "missing"}), prompt)
        self.assertEqual(selected_request(prompt, {"studio_scene": scene, "studio_selected": "o1"}),
                         "Сдвинь вправо на 2 мм и поверни на 15 градусов")

    def test_metadata_history_and_invalid_commit_are_atomic(self):
        document = SandboxDocument()
        empty = document.snapshot()
        written = document.commit(empty, text_plan())
        self.assertEqual(written.state["preview_task"].parameters["text"], "Анна")
        before = deepcopy(document.content())
        history = deepcopy(document.history.past)
        with self.assertRaises(ValueError):
            document.commit(written, Plan("move_preview", {"dx_mm": 900, "dy_mm": 0}))
        self.assertEqual(document.content(), before)
        self.assertEqual(document.history.past, history)
        with self.assertRaises(ValueError):
            document.commit(empty, text_plan())
        self.assertEqual(document.undo().paths, ())
        restored = document.redo()
        self.assertEqual(restored.state["preview_task"].parameters["text"], "Анна")
        self.assertEqual(scene_for(restored)["objects"][0]["source"]["kind"], "print_text")


class AssistantStudioTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        with patch("app.Controller", side_effect=lambda **kw: Controller(start_worker=False, **kw)), \
                patch("serial.tools.list_ports.comports", return_value=[]):
            self.app = App(self.root, data_dir=None)
        self.app.open_assistant()
        self.window = self.app.assistant_window
        self.window.root.withdraw()
        self.root.update()

    def tearDown(self):
        self.app.controller.busy = self.app.svg_loading = False
        close_tk_app(self)

    def wait_for(self, predicate):
        limit = time.monotonic() + 5
        while time.monotonic() < limit:
            self.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail("Local assistant operation did not finish")

    def request(self, prompt):
        self.window.entry.delete("1.0", "end")
        self.window.entry.insert("1.0", prompt)
        self.window.send()
        self.wait_for(lambda: not self.window.busy)

    def card(self):
        from assistant.studio_commands import default_card
        self.app.apply_editor_preview(self.app.editor_snapshot(), scene_plan(default_card(210, 297)))

    def test_selection_by_list_prompt_and_click_edits_only_chosen_object(self):
        self.card()
        window = self.window
        original = deepcopy(window.current_scene())
        window.object_picker.current(2)
        window.select_object()
        self.assertEqual(window.selected_object(), original["objects"][1]["id"])
        self.request("Сдвинь вправо на 2 мм")
        scene = window.pending.reply.plan.parameters["scene"]
        self.assertEqual(scene["objects"][0], original["objects"][0])
        self.assertNotEqual(scene["objects"][1], original["objects"][1])
        self.assertEqual(scene["objects"][2:], original["objects"][2:])
        self.assertEqual(window.app.editor_snapshot().state["preview_task"].parameters["scene"], original)
        self.request("Выбери объект 3")
        self.assertEqual(window.selected_object(), original["objects"][2]["id"])
        self.request("Выбери всё")
        self.assertIsNone(window.selected_object())
        window.cancel_request()
        obj = original["objects"][0]
        point = render_scene({"version": 1, "objects": [obj], "route": None}, 210, 297)[0][0]
        ox, oy, scale = window.canvas_transform
        window.select_at(SimpleNamespace(x=ox + point[0] * scale, y=oy + point[1] * scale))
        self.assertEqual(window.selected_object(), obj["id"])
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_three_variants_require_choice_then_explicit_apply_and_share_history(self):
        self.card()
        before = deepcopy(self.app.paths)
        self.request("Сделай три оформления")
        window = self.window
        self.assertEqual(len(window.variants), 3)
        self.assertIsNone(window.pending)
        self.assertEqual(self.app.paths, before)
        window.variant_picker.current(1)
        window.choose_variant()
        self.assertIsNotNone(window.pending)
        self.assertEqual(self.app.paths, before)
        window.apply_proposal()
        self.assertNotEqual(self.app.paths, before)
        self.app.undo_editor_preview()
        self.assertEqual(self.app.paths, before)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_selected_pronoun_survives_apply_undo_and_workspace_return(self):
        self.card()
        window = self.window
        window.object_picker.current(2)
        window.select_object()
        chosen = window.selected_object()
        original = deepcopy(window.current_scene())
        self.request("Сдвинь его вправо на 2 мм")
        self.assertIsNotNone(window.pending)
        window.apply_proposal()
        self.assertEqual(window.selected_object(), chosen)
        self.assertEqual(window.current_scene()["objects"][0], original["objects"][0])
        window.undo()
        self.assertEqual(window.current_scene(), original)
        self.assertEqual(window.selected_object(), chosen)
        window.mode.set("Пробный лист")
        window.switch_mode()
        self.assertIsNone(window.selected_object())
        window.mode.set("Эскиз плоттера")
        window.switch_mode()
        self.assertEqual(window.selected_object(), chosen)
        self.request("Сдвинь его")
        self.assertIsNone(window.pending)
        self.assertEqual(window.current_scene(), original)
        replacement = text_plan()
        self.app.apply_editor_preview(self.app.editor_snapshot(), replacement)
        self.assertIsNone(window.selected_object())

    def test_shared_template_store_literal_recipient_and_same_template_followup(self):
        self.card()
        window = self.window
        with tempfile.TemporaryDirectory() as directory:
            window.templates = Templates(directory)
            window.object_picker.current(1)
            window.select_object()
            self.assertTrue(window.save_template("Праздник", "Для {name}"))
            manual_store = Templates(directory)
            self.assertEqual(manual_store.names(), ["Праздник"])
            stored = manual_store.load("Праздник")
            self.assertEqual(stored["recipient_object"], "o1")
            paths = deepcopy(self.app.paths)
            self.request("Праздник для Бориса")
            scene = window.pending.reply.plan.parameters["scene"]
            self.assertEqual(scene["objects"][0]["source"]["parameters"]["text"], "Для Бориса")
            self.assertEqual(scene["objects"][1:], stored["scene"]["objects"][1:])
            self.assertEqual(self.app.paths, paths)
            self.request("Сделай такую же открытку для Иры")
            self.assertEqual(window.pending.reply.plan.parameters["scene"]["objects"][0]["source"]["parameters"]["text"], "Для Иры")
            window.cancel_request()
            self.assertTrue(window.load_template("Праздник"))
            self.assertEqual(self.app.paths, paths)

    def test_template_dialog_records_name_field_and_saved_preview_stays_unapplied(self):
        self.card()
        window = self.window
        with tempfile.TemporaryDirectory() as directory:
            window.templates = Templates(directory)
            window.object_picker.current(1)
            window.select_object()
            self.request("Замени текст на «Для Ольги»")
            paths = deepcopy(self.app.paths)
            with patch("assistant_ui.simpledialog.askstring", side_effect=["Поздравление", "Для {name}"]) as ask:
                self.assertTrue(window.save_template())
                self.assertEqual(ask.call_count, 2)
            data = window.templates.load("Поздравление")
            self.assertEqual(data["recipient_object"], "o1")
            self.assertEqual(data["recipient_format"], "Для {name}")
            self.assertEqual(self.app.paths, paths)
            self.request("шаблон «Поздравление» для Маши")
            scene = window.pending.reply.plan.parameters["scene"]
            from assistant.studio_commands import caption
            self.assertEqual(caption(scene["objects"][0]), "Для Маши")

    def test_sandbox_has_independent_undo_and_transfer_is_staged_and_stale_checked(self):
        window = self.window
        window.mode.set("Пробный лист")
        window.switch_mode()
        self.request("Напиши печатными «Анна» высотой 5 мм")
        window.apply_proposal()
        written = window.current_snapshot()
        self.assertTrue(written.paths)
        self.assertFalse(self.app.paths)
        self.assertEqual(written.state["preview_task"].parameters["text"], "Анна")
        window.undo()
        self.assertFalse(window.current_snapshot().paths)
        window.redo()
        self.assertEqual(window.current_snapshot().paths, written.paths)
        window.transfer_sandbox()
        self.assertEqual(window.mode.get(), "Эскиз плоттера")
        self.assertIsNotNone(window.pending)
        self.assertFalse(self.app.paths)
        window.apply_proposal()
        self.assertEqual(self.app.editor_snapshot().paths, written.paths)
        self.app.undo_editor_preview()
        self.assertFalse(self.app.paths)
        window.mode.set("Пробный лист")
        window.switch_mode()
        window.transfer_sandbox()
        self.assertIsNotNone(window.pending)
        self.app.apply_editor_preview(self.app.editor_snapshot(), text_plan())
        before = deepcopy(self.app.paths)
        window.apply_proposal()
        self.assertEqual(self.app.paths, before)
        self.assertIsNone(window.pending)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_main_editor_changes_leave_local_proposal_intact_and_mode_switch_cancels_it(self):
        window = self.window
        window.mode.set("Пробный лист")
        window.switch_mode()
        self.request("Напиши печатными «Анна» высотой 5 мм")
        pending = window.pending
        self.app.apply_editor_preview(self.app.editor_snapshot(), text_plan())
        self.assertIs(window.pending, pending)
        window.mode.set("Эскиз плоттера")
        window.switch_mode()
        self.assertIsNone(window.pending)
        self.assertFalse(window.sandbox.paths)

    def test_stream_chunks_render_before_completion_without_duplicate_final_text(self):
        window = self.window
        release = threading.Event()
        worker_thread = []
        main_thread = threading.get_ident()
        def answer(prompt, state, *, on_chunk, **kwargs):
            worker_thread.append(threading.get_ident())
            on_chunk("Первый ")
            on_chunk("ответ")
            release.wait(3)
            return Reply("Первый ответ", plan=text_plan())
        with patch.object(window.engine, "answer", side_effect=answer):
            window.fill_example("Подготовь надпись")
            window.send()
            try:
                self.wait_for(lambda: "Первый ответ" in window.transcript.get("1.0", "end"))
                self.assertTrue(window.busy)
                self.assertIsNone(window.pending)
                self.assertFalse(self.app.paths)
                self.assertNotEqual(worker_thread[0], main_thread)
            finally:
                release.set()
            self.wait_for(lambda: not window.busy)
        self.assertIsNotNone(window.pending)
        self.assertEqual(window.transcript.get("1.0", "end").count("Первый ответ"), 1)
        self.assertFalse(self.app.paths)

    def test_cancel_discards_late_stream_and_plan_and_does_not_touch_history(self):
        window = self.window
        release = threading.Event()
        entered = threading.Event()
        def answer(prompt, state, *, on_chunk, **kwargs):
            entered.set()
            release.wait(3)
            on_chunk("LATE CHUNK")
            return Reply("LATE RESULT", plan=text_plan())
        with patch.object(window.engine, "answer", side_effect=answer):
            window.fill_example("Подготовь надпись")
            window.send()
            self.assertTrue(entered.wait(1))
            window.cancel_request()
            release.set()
            self.wait_for(lambda: not window.worker.is_alive())
            window.pump_results()
        transcript = window.transcript.get("1.0", "end")
        self.assertNotIn("LATE", transcript)
        self.assertIsNone(window.pending)
        self.assertEqual(self.app.editor_history.past, [])
        self.assertFalse(self.app.paths)

    def test_external_proposal_rejects_stale_or_invalid_without_partial_edit(self):
        window = self.window
        before = window.current_snapshot()
        self.assertTrue(window.offer_external_plan(before, text_plan(), "Образец"))
        valid = window.pending
        self.assertFalse(window.offer_external_plan(before, Plan("jog", {"distance": 1}), "Неверно"))
        self.assertIs(window.pending, valid)
        self.assertFalse(self.app.paths)
        self.app.apply_editor_preview(before, text_plan())
        self.assertFalse(window.offer_external_plan(before, text_plan(), "Устарело"))
        self.assertIsNone(window.pending)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
