"""Voice stays a reviewed text draft; fake devices never open a microphone."""
from pathlib import Path
from types import SimpleNamespace
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from assistant.voice import AudioClip, Transcript
from controller import Controller
from tk_test_support import close_tk_app


class FakeVoiceService:
    instances = []

    def __init__(self, events):
        self.events = events
        self.busy = self.recording = self.closed = False
        self.token = 0
        self.started = []
        self.recognized = []
        self.stop_count = self.cancel_count = self.device_count = 0
        self.instances.append(self)

    def devices(self):
        self.device_count += 1
        return [SimpleNamespace(index=4, name="Fake microphone", is_default=True)]

    def start(self, device=None):
        self.token += 1
        self.busy = self.recording = True
        self.started.append(device)
        return self.token

    def stop(self):
        self.stop_count += 1
        self.recording = False

    def recognize(self, clip, profile="fast", allow_download=False):
        self.token += 1
        self.busy = True
        self.recognized.append((clip, profile, allow_download))
        return self.token

    def cancel(self):
        self.cancel_count += 1
        self.busy = self.recording = False

    def close(self):
        self.closed = True
        self.cancel()

    def emit(self, kind, value=None, *, token=None):
        if kind == "finished":
            self.busy = self.recording = False
        self.events.put(SimpleNamespace(token=self.token if token is None else token,
                                        kind=kind, value=value))


class VoiceUiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        with patch("app.Controller", side_effect=lambda **kw: Controller(start_worker=False, **kw)), \
                patch("serial.tools.list_ports.comports", return_value=[]):
            self.app = App(self.root, data_dir=None)
        self.root.update()
        FakeVoiceService.instances = []
        self.patches = [patch("voice_ui.VoiceService", FakeVoiceService),
                        patch("voice_ui.dependency_status", return_value=()),
                        patch("voice_ui.model_status", return_value=SimpleNamespace(
                            model="tiny", ready=True, download_mb=75))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        self.app.controller.busy = False
        self.app.svg_loading = False
        close_tk_app(self)

    def wait_for(self, predicate, timeout=3):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail("Voice dialog did not reach its expected state")

    def assistant(self):
        self.app.open_assistant()
        owner = self.app.assistant_window
        owner.root.withdraw()
        self.root.update()
        return owner

    def dialog(self, owner=None):
        owner = owner or self.assistant()
        owner.open_voice()
        dialog = owner.voice_window
        dialog.root.withdraw()
        self.wait_for(lambda: dialog.phase != "loading")
        return owner, dialog, dialog.service

    def recorded(self, dialog, service):
        dialog.start_recording()
        dialog.stop_recording()
        clip = AudioClip(b"\x01\x00" * 16000, 16000)
        service.emit("recorded", clip)
        service.emit("finished")
        dialog.pump_results()
        self.assertEqual(dialog.phase, "recorded")
        self.assertIs(dialog.clip, clip)
        return clip

    def reviewed(self, dialog, service, text="Напиши «Привет» высотой 5 мм"):
        self.recorded(dialog, service)
        dialog.recognize()
        service.emit("result", Transcript(text, elapsed_s=.4))
        service.emit("finished")
        dialog.pump_results()
        self.assertEqual(dialog.phase, "review")

    def test_assistant_open_does_not_create_voice_service(self):
        owner = self.assistant()
        self.assertIsNone(owner.voice_window)
        self.assertEqual(FakeVoiceService.instances, [])
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_dialog_only_discovers_until_explicit_record_and_recognize(self):
        owner, dialog, service = self.dialog()
        self.assertEqual(service.device_count, 1)
        self.assertEqual(service.started, [])
        self.assertEqual(service.recognized, [])
        owner.open_voice()
        self.assertIs(owner.voice_window, dialog)
        self.assertEqual(len(FakeVoiceService.instances), 1)
        self.assertEqual(str(owner.send_button["state"]), "disabled")
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_recording_stop_does_not_automatically_recognize(self):
        _, dialog, service = self.dialog()
        clip = self.recorded(dialog, service)
        self.assertEqual(service.started, [4])
        self.assertEqual(service.stop_count, 1)
        self.assertEqual(service.recognized, [])
        self.assertEqual(str(dialog.recognize_button["state"]), "normal")
        dialog.recognize()
        self.assertEqual(service.recognized, [(clip, "fast", False)])
        self.assertEqual(dialog.phase, "transcribing")

    def test_result_is_reviewed_and_inserted_without_request_or_motion(self):
        owner, dialog, service = self.dialog()
        owner.entry.insert("1.0", "Начало конец")
        owner.entry.mark_set("insert", "1.6")
        before = self.app.editor_snapshot()
        with patch.object(owner, "send") as send, \
                patch.object(self.app, "apply_assistant_preview") as apply, \
                patch.object(self.app.controller, "submit") as motion:
            self.reviewed(dialog, service, "По центру")
            self.assertEqual(owner.entry.get("1.0", "end-1c"), "Начало конец")
            self.assertEqual(str(dialog.insert_button["state"]), "normal")
            dialog.text.delete("1.0", "end")
            dialog.text.insert("1.0", "Поверни на 90 градусов")
            dialog.insert_text()
            send.assert_not_called()
            apply.assert_not_called()
            motion.assert_not_called()
        self.assertEqual(owner.entry.get("1.0", "end-1c"), "Начало Поверни на 90 градусов конец")
        self.assertIsNone(owner.voice_window)
        self.assertTrue(service.closed)
        self.assertEqual(self.app.editor_snapshot().paths, before.paths)
        self.assertEqual(self.app.editor_history.past, [])

    def test_send_and_apply_are_blocked_while_voice_dialog_is_open(self):
        owner, dialog, _ = self.dialog()
        owner.entry.insert("1.0", "Напиши печатными «Привет» высотой 5 мм")
        with patch.object(owner.engine, "answer") as answer, \
                patch.object(self.app, "apply_assistant_preview") as apply:
            self.assertEqual(owner.send(), "break")
            owner.apply_proposal()
            answer.assert_not_called()
            apply.assert_not_called()
        self.assertEqual(owner.entry.get("1.0", "end-1c"), "Напиши печатными «Привет» высотой 5 мм")
        self.assertFalse(dialog.closed)

    def test_cancel_preserves_typed_query_and_unapplied_proposal(self):
        owner = self.assistant()
        owner.entry.insert("1.0", "Напиши печатными «Привет» высотой 5 мм")
        owner.send()
        self.wait_for(lambda: not owner.busy)
        proposal = owner.pending
        self.assertIsNotNone(proposal)
        owner.entry.insert("1.0", "Я ещё дописываю")
        _, dialog, service = self.dialog(owner)
        owner.restore_pending()
        self.assertIs(owner.pending, proposal)
        dialog.start_recording()
        token = service.token
        dialog.cancel()
        service.emit("result", Transcript("Старый ответ"), token=token)
        service.emit("finished", token=token)
        dialog.pump_results()
        self.assertEqual(owner.entry.get("1.0", "end-1c"), "Я ещё дописываю")
        self.assertIs(owner.pending, proposal)
        self.assertEqual(str(owner.apply_button["state"]), "normal")
        self.assertEqual(self.app.paths, [])
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_old_operation_result_is_ignored_and_equal_integer_token_is_accepted(self):
        _, dialog, service = self.dialog()
        service.token = 1000
        self.recorded(dialog, service)
        old_token = service.token
        dialog.recognize()
        self.assertNotEqual(service.token, old_token)
        service.emit("result", Transcript("Устаревшее"), token=old_token)
        # Token values may be reconstructed through a queue; object identity
        # is not the identity of a voice operation.
        current_token = int(str(service.token))
        self.assertIsNot(current_token, service.token)
        service.emit("result", Transcript("Актуальное"), token=current_token)
        service.emit("finished", token=current_token)
        dialog.pump_results()
        self.assertEqual(dialog.text.get("1.0", "end-1c"), "Актуальное")
        self.assertEqual(dialog.phase, "review")

    def test_cancel_while_recognizing_closes_service_and_discards_result(self):
        owner, dialog, service = self.dialog()
        owner.entry.insert("1.0", "Сохранить это")
        self.recorded(dialog, service)
        dialog.recognize()
        dialog.cancel()
        service.emit("result", Transcript("Поздний ответ"))
        dialog.pump_results()
        self.assertTrue(service.closed)
        self.assertEqual(service.cancel_count, 1)
        self.assertIsNone(owner.voice_window)
        self.assertEqual(owner.entry.get("1.0", "end-1c"), "Сохранить это")
        self.assertEqual(str(owner.send_button["state"]), "normal")

    def test_assistant_close_cancels_voice_without_waiting_for_worker(self):
        owner, dialog, service = self.dialog()
        dialog.start_recording()
        owner.close()
        self.assertTrue(owner.closed)
        self.assertTrue(dialog.closed)
        self.assertTrue(service.closed)
        self.assertIsNone(dialog.timer)
        self.assertIsNone(dialog.token)
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_missing_optional_dependencies_leave_text_assistant_usable(self):
        owner = self.assistant()
        owner.entry.insert("1.0", "Сохранить")
        with patch("voice_ui.dependency_status", return_value=("sounddevice", "faster-whisper")):
            owner.open_voice()
        dialog = owner.voice_window
        self.assertEqual(dialog.phase, "unavailable")
        self.assertIn("INSTALL_VOICE.cmd", dialog.status.get())
        self.assertEqual(dialog.service.device_count, 0)
        self.assertEqual(dialog.service.started, [])
        self.assertEqual(dialog.service.recognized, [])
        dialog.close()
        self.assertEqual(owner.entry.get("1.0", "end-1c"), "Сохранить")
        self.assertEqual(str(owner.send_button["state"]), "normal")

    def test_missing_device_permission_is_reported_without_starting_recording(self):
        owner = self.assistant()
        with patch.object(FakeVoiceService, "devices", side_effect=ValueError("Микрофон недоступен")):
            _, dialog, service = self.dialog(owner)
        self.assertEqual(dialog.phase, "idle")
        self.assertIn("Микрофон недоступен", dialog.status.get())
        self.assertEqual(service.started, [])
        self.assertEqual(str(dialog.record_button["state"]), "disabled")

    def test_start_failure_preserves_owner_text_and_allows_retry(self):
        owner, dialog, service = self.dialog()
        owner.entry.insert("1.0", "Ручной запрос")
        with patch.object(service, "start", side_effect=ValueError("Доступ к микрофону закрыт")):
            dialog.start_recording()
        self.assertEqual(dialog.phase, "idle")
        self.assertIn("Доступ к микрофону закрыт", dialog.status.get())
        self.assertEqual(owner.entry.get("1.0", "end-1c"), "Ручной запрос")
        self.assertEqual(str(dialog.record_button["state"]), "normal")
        self.assertEqual(service.recognized, [])

    def test_model_download_requires_explicit_recognition_action(self):
        owner = self.assistant()
        with patch("voice_ui.model_status", return_value=SimpleNamespace(
                model="tiny", ready=False, download_mb=75)):
            _, dialog, service = self.dialog(owner)
            self.assertIn("75", dialog.model_info.get())
            self.assertEqual(dialog.recognize_button["text"], "Скачать и распознать")
            clip = self.recorded(dialog, service)
            self.assertEqual(service.recognized, [])
            dialog.recognize()
        self.assertEqual(service.recognized, [(clip, "fast", True)])

    def test_refreshing_devices_preserves_reviewed_text_and_insertion(self):
        owner, dialog, service = self.dialog()
        self.reviewed(dialog, service)
        dialog.text.delete("1.0", "end")
        dialog.text.insert("1.0", "Исправленная фраза")
        dialog.discover()
        self.wait_for(lambda: dialog.phase != "loading")
        self.assertEqual(dialog.phase, "review")
        self.assertEqual(dialog.text.get("1.0", "end-1c"), "Исправленная фраза")
        self.assertEqual(str(dialog.insert_button["state"]), "normal")
        with patch.object(service, "devices", side_effect=ValueError("Микрофон отключён")):
            dialog.discover()
            self.wait_for(lambda: dialog.phase != "loading")
        self.assertEqual(dialog.phase, "review")
        self.assertEqual(dialog.text.get("1.0", "end-1c"), "Исправленная фраза")
        self.assertEqual(str(dialog.insert_button["state"]), "normal")
        self.assertEqual(owner.entry.get("1.0", "end-1c"), "")
        self.assertEqual(self.app.controller.jobs.qsize(), 0)

    def test_too_long_combined_query_is_not_partially_inserted(self):
        owner, dialog, service = self.dialog()
        existing = "А" * 1795
        owner.entry.insert("1.0", existing)
        self.reviewed(dialog, service, "Б" * 10)
        dialog.insert_text()
        self.assertFalse(dialog.closed)
        self.assertEqual(owner.entry.get("1.0", "end-1c"), existing)
        self.assertEqual(dialog.text.get("1.0", "end-1c"), "Б" * 10)
        self.assertIn("1800", dialog.status.get())

    def test_direct_voice_insert_rejects_empty_control_or_oversize_text(self):
        owner = self.assistant()
        owner.entry.insert("1.0", "До после")
        owner.entry.mark_set("insert", "1.2")
        for invalid in ("", "  ", "Текст\x00", "Текст\rвнутри", "А" * 1801, None):
            with self.subTest(invalid=repr(invalid)[:30]):
                self.assertFalse(owner.insert_voice_text(invalid))
                self.assertEqual(owner.entry.get("1.0", "end-1c"), "До после")
        self.assertTrue(owner.insert_voice_text("В середине"))
        self.assertEqual(owner.entry.get("1.0", "end-1c"), "До В середине после")


if __name__ == "__main__":
    unittest.main()
