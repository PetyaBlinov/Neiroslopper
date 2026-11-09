"""Sketch assistant policy, dialogue and transport parsing; no external requests."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assistant import cloud_backend, creative_planner
from assistant.draft_context import applied_task
from assistant.planner import Plan, transform_paths
from assistant.runtime import Assistant
from assistant.task_memory import TaskMemory
from editor_tools import planner as manual_planner
from editor_tools.preview import snapshot
from geometry import Sheet, validate_paths
from machine_profiles import DLC32


def editor_state(paths=()):
    before = snapshot({"sheet_width": 210., "sheet_height": 297.,
                       "machine_profile": DLC32, "busy": False,
                       "connected": False, "simulation": False,
                       "pen_power_raises": True, "pen_continuous": False,
                       "pen_lift_enabled": True, "pen_lift_ready": False},
                      paths, ("offline", 1))
    return {**before.state, "editor_paths": before.paths}


class AssistantBackendTests(unittest.TestCase):
    def setUp(self):
        self.engine = Assistant()
        self.cloud = patch("assistant.cloud_backend.generate", side_effect=AssertionError("Unexpected model request"))
        self.generate = self.cloud.start()
        self.addCleanup(self.cloud.stop)

    def answer(self, prompt, state=None):
        return self.engine.answer(prompt, editor_state() if state is None else state, use_llm=False)

    def text(self):
        return self.answer("Напиши печатными «Привет Hello» высотой 5 мм").plan

    def test_shared_geometry_uses_canonical_plan_type(self):
        self.assertIs(Plan, manual_planner.Plan)
        plan = self.text()
        self.assertIsInstance(plan, manual_planner.Plan)
        paths = transform_paths((), plan, 210, 297)
        from editor_tools.scene import from_plan
        scene = from_plan(plan, paths, 210, 297)
        self.assertEqual(scene["objects"][0]["source"]["kind"], "print_text")

    def test_local_unicode_text_produces_bounded_geometry_without_network(self):
        state = editor_state()
        original = deepcopy(state)
        reply = self.answer("Напиши печатными «Привет Hello» высотой 5 мм", state)
        self.assertEqual(reply.plan.kind, "print_text")
        self.assertEqual(reply.plan.parameters["text"], "Привет Hello")
        paths = transform_paths((), reply.plan, 210, 297)
        self.assertGreater(len(paths), 10)
        validate_paths(paths, Sheet(210, 297))
        self.assertEqual(state, original)
        self.generate.assert_not_called()

    def test_pending_followup_is_calculated_from_same_base_once(self):
        state = editor_state([[(10., 10.), (20., 10.), (20., 20.)]])
        first = self.answer("Сдвинь рисунок вправо на 5 мм", state)
        second = self.answer("Сдвинь рисунок вправо на 3 мм",
                             {**state, "preview_draft": first.plan})
        paths = transform_paths(state["editor_paths"], second.plan, 210, 297)
        self.assertAlmostEqual(min(x for path in paths for x, _ in path), 18.)
        self.assertEqual(state["editor_paths"][0][0], (10., 10.))
        self.generate.assert_not_called()

    def test_text_replacement_reuses_editable_metadata(self):
        plan = self.text()
        paths = transform_paths((), plan, 210, 297)
        state = {**editor_state(paths), "preview_task": applied_task(plan, None)}
        reply = self.answer("Замени текст на «Пока»", state)
        self.assertIsNotNone(reply.plan)
        changed = transform_paths(paths, reply.plan, 210, 297)
        task = applied_task(reply.plan, state["preview_task"])
        from editor_tools.scene import from_plan
        scene = from_plan(task, changed, 210, 297)
        self.assertEqual(scene["objects"][0]["source"]["parameters"]["text"], "Пока")
        self.assertEqual(state["preview_task"].parameters["text"], "Привет Hello")

    def test_out_of_bounds_local_plan_returns_no_proposal(self):
        state = editor_state([[(10., 10.), (20., 10.)]])
        reply = self.answer("Сдвинь рисунок вправо на 1000 мм", state)
        self.assertIsNone(reply.plan)
        self.assertTrue(reply.validation_error)
        self.assertEqual(state["editor_paths"][0][0], (10., 10.))

    def test_hardware_code_and_motion_requests_never_create_plans_or_cloud_calls(self):
        for prompt in ("Подними ручку", "Запусти рисование", "Подключи COM5", "G1 X10", "M8", "eval('move')"):
            with self.subTest(prompt=prompt):
                reply = self.engine.answer(prompt, editor_state(), use_llm=True)
                self.assertIsNone(reply.plan)
        self.generate.assert_not_called()

    def test_cancellation_prevents_cloud_and_geometry(self):
        cancel = threading.Event()
        cancel.set()
        reply = self.engine.answer("Нарисуй гору и солнце", editor_state(), use_llm=True, cancel=cancel)
        self.assertIsNone(reply.plan)
        self.assertIn("отмен", reply.text.casefold())
        self.generate.assert_not_called()

    def test_free_chat_is_opt_in(self):
        reply = self.answer("Расскажи короткую шутку")
        self.assertIsNone(reply.plan)
        self.generate.assert_not_called()
        self.generate.side_effect = None
        self.generate.return_value = "Ответ тестового сервиса"
        reply = self.engine.answer("Расскажи короткую шутку", editor_state(), use_llm=True)
        self.assertEqual(reply.text, "Ответ тестового сервиса")
        self.assertIsNone(reply.plan)
        self.assertEqual(self.generate.call_count, 1)

    def test_task_memory_tracks_applied_editor_revision_only(self):
        memory = TaskMemory()
        memory.note(("editor", 1), "proposed", "Надпись Привет")
        memory.applied(("editor", 1), ("editor", 2))
        saved = memory.snapshot(("editor", 2))
        self.assertEqual(saved["outcome"], "applied")
        self.assertTrue(saved["not_motion_state"])
        self.assertEqual(memory.snapshot(("editor", 3)), {})

    def test_help_describes_voice_review_and_does_not_claim_smart_image_ui(self):
        voice = self.answer("Как включить голосовой ввод?")
        self.assertIn("«Голос»", voice.text)
        self.assertIn("«Вставить в запрос»", voice.text)
        self.assertIn("локальное распознавание", voice.text)
        self.assertIsNone(voice.plan)
        image = self.answer("Как проверить обводку Codex?")
        self.assertIn("пока не интегрированы", image.text)
        display = self.answer("Как работает DWIN?")
        self.assertIn("ещё не перенесены", display.text)
        self.assertNotIn("S24 Ultra", display.text)

    def test_mks_pen_description_keeps_ten_second_whole_transfer(self):
        reply = self.answer("Как работает соленоид?")
        self.assertIn("10 с", reply.text)
        self.assertIn("целиком", reply.text)
        self.assertNotIn("нужны остановки для охлаждения", reply.text)
        self.assertIsNone(reply.plan)
        speed = self.answer("Какая скорость у моторов на 100%?")
        self.assertIn("загруженных настройках", speed.text)
        self.assertNotIn("3000", speed.text)

    def test_cloud_snapshot_does_not_invent_verified_board_or_display_settings(self):
        from assistant.chat_context import chat_snapshot
        state = chat_snapshot(editor_state())
        self.assertNotIn("board_revision", state)
        self.assertIn("Planned", state["motor_socket_roles"])
        self.assertNotIn("9600", state["dwin_connection"])
        self.assertFalse(state["connected"])


class CreativeResponseTests(unittest.TestCase):
    def selection(self, operation):
        return {"kind": "preview", "explanation": "Тестовый контур",
                "assumptions": [], "operations": [operation]}

    def test_model_vector_plan_is_data_with_checked_bounds(self):
        request = self.selection({"kind": "polyline", "parameters": {
            "points": [[20, 30], [40, 10], [60, 30]], "mode": "replace"}})
        reply = creative_planner.validate("Нарисуй гору", request, editor_state())
        self.assertIsInstance(reply.plan, Plan)
        paths = transform_paths((), reply.plan, 210, 297)
        self.assertEqual(paths[0][1], (40., 10.))
        validate_paths(paths, Sheet(210, 297))

    def test_unregistered_out_of_bounds_and_duplicate_data_are_rejected(self):
        for operation in ({"kind": "jog", "parameters": {"distance": 10}},
                          {"kind": "polyline", "parameters": {
                              "points": [[20, 30], [1000, 10]], "mode": "replace"}}):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                creative_planner.validate("Нарисуй гору", self.selection(operation), editor_state())
        for raw in ('{"kind":"preview","kind":"clarify"}', '{"value":NaN}'):
            with self.assertRaises(ValueError):
                creative_planner._load(raw)


class CloudTransportPolicyTests(unittest.TestCase):
    def test_commands_and_environment_never_choose_api_billing_or_execution(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "CODEX_API_KEY": "fake", "PATH": "keep"}):
            environment = cloud_backend.child_environment()
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("CODEX_API_KEY", environment)
        self.assertEqual(environment["PATH"], "keep")
        args = cloud_backend.command("codex.exe", "isolated", None)
        self.assertEqual(args[:3], ["codex.exe", "--no-daemon", "exec"])
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--json"):
            self.assertIn(flag, args)
        for feature in ("shell_tool", "unified_exec", "plugins", "hooks", "computer_use"):
            self.assertIn(feature, args)

    def test_schema_parser_rejects_duplicates_and_nonfinite_numbers(self):
        source = {"type": "object", "properties": {"value": {"type": "number"}}, "required": ["value"]}
        value = cloud_backend.unwrap_response('{"result":{"value":3}}', source)
        self.assertEqual(json.loads(value), {"value": 3})
        for raw in ('{"result":{"value":3},"result":{}}', '{"result":{"value":NaN}}'):
            with self.assertRaises(ValueError):
                cloud_backend.unwrap_response(raw, source)

    def test_cancel_before_transport_does_not_check_login_or_spawn_process(self):
        cancel = threading.Event()
        cancel.set()
        with patch("assistant.cloud_backend.login_status") as login, \
                patch("assistant.cloud_backend.subprocess.Popen") as process:
            with self.assertRaises(cloud_backend.CloudError):
                cloud_backend.generate([{"role": "user", "content": "test"}], channel="chat", cancel=cancel)
        login.assert_not_called()
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
