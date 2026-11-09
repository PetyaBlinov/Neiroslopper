"""Voice tests use synthetic audio and fake inputs only; never a live microphone."""
from array import array
import importlib
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from assistant import voice
from assistant import voice_worker


def pcm(seconds=0.5, rate=8000, amplitude=3000):
    samples = array("h", (amplitude if index % 2 else -amplitude for index in range(int(seconds * rate))))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def finish(service):
    service._thread.join(timeout=3)
    if service._thread.is_alive():
        service.cancel()
        raise AssertionError("Voice job did not finish")


def drain(events):
    result = []
    while not events.empty():
        result.append(events.get_nowait())
    return result


class CallbackStop(Exception):
    pass


class CallbackAbort(Exception):
    pass


def fake_sounddevice(*, seconds=0.5, end=True, overflow=False, on_open=None):
    """RawInputStream simulator; it creates no PortAudio stream."""
    opened = []
    class Stream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def __enter__(self):
            opened.append(self.kwargs)
            if on_open:
                on_open()
            try:
                self.kwargs["callback"](pcm(seconds), int(seconds * 8000), None, overflow)
            except (CallbackStop, CallbackAbort):
                self.kwargs["finished_callback"]()
            if end:
                self.kwargs["finished_callback"]()
            return self
        def __exit__(self, *args):
            self.kwargs["finished_callback"]()
    inputs = [{"name": "Synthetic microphone", "max_input_channels": 1, "hostapi": 0,
               "default_samplerate": 8000},
              {"name": "Output only", "max_input_channels": 0, "hostapi": 0,
               "default_samplerate": 8000}]
    return SimpleNamespace(default=SimpleNamespace(device=(0, -1)),
                           query_hostapis=lambda: [{"name": "Fake"}],
                           query_devices=lambda *args: inputs[0] if args else inputs,
                           RawInputStream=Stream, CallbackStop=CallbackStop, CallbackAbort=CallbackAbort,
                           opened=opened)


class VoiceImportTests(unittest.TestCase):
    def test_import_does_not_import_optional_modules_or_open_anything(self):
        # A clean process also protects this test from module reload/class identity.
        command = """import builtins
old = builtins.__import__
def checked(name, *args, **kwargs):
    if name.split('.')[0] in {'sounddevice','faster_whisper','numpy','huggingface_hub'}:
        raise AssertionError(name)
    return old(name, *args, **kwargs)
builtins.__import__ = checked
import assistant.voice
import assistant.voice_worker
"""
        result = subprocess.run([sys.executable, "-c", command], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_dependency_check_only_reads_specs(self):
        with patch.object(voice.importlib.util, "find_spec", side_effect=lambda name: None if name == "sounddevice" else object()) as spec:
            self.assertEqual(voice.dependency_status(), ("sounddevice",))
        self.assertEqual(spec.call_count, 2)

    def test_models_are_tiny_and_small_and_cache_inspection_makes_no_directory(self):
        self.assertEqual(voice.PROFILES, {"fast": "tiny", "quality": "small"})
        with tempfile.TemporaryDirectory() as folder, patch.object(voice, "MODEL_ROOT", Path(folder) / "absent"):
            status = voice.model_status("fast")
            self.assertEqual(status.model, "tiny")
            self.assertFalse(status.ready)
            self.assertFalse(status.path.exists())
            self.assertLess(status.download_mb, 100)
            status.path.mkdir(parents=True)
            for name in ("model.bin", "config.json", "tokenizer.json"):
                (status.path / name).write_bytes(b"mock model file")
            self.assertTrue(voice.model_status().ready)
        with self.assertRaises(voice.VoiceError):
            voice.model_status("medium")

    def test_devices_enumerate_without_capture(self):
        fake = fake_sounddevice()
        with patch.dict(sys.modules, {"sounddevice": fake}):
            devices = voice.VoiceService.devices()
        self.assertEqual(devices, [voice.InputDevice(0, "Synthetic microphone · Fake", True)])
        self.assertFalse(fake.opened)


class VoiceAudioTests(unittest.TestCase):
    def test_wav_is_mono_pcm16_in_memory(self):
        raw, warnings = voice.prepare_wav(pcm(), 8000)
        with wave.open(io.BytesIO(raw), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 8000))
            self.assertEqual(wav.readframes(wav.getnframes()), pcm())
        self.assertFalse(warnings)

    def test_silence_duration_bad_rate_and_unaligned_bytes_are_rejected(self):
        for raw, rate in ((b"\0" * 8000, 8000), (pcm(0.1), 8000), (pcm(30.01), 8000),
                          (pcm(), 7999), (pcm(), True), (pcm() + b"x", 8000)):
            with self.subTest(rate=rate, size=len(raw)), self.assertRaises(voice.VoiceError):
                voice.prepare_wav(raw, rate)

    def test_signal_warnings(self):
        self.assertTrue(voice.prepare_wav(pcm(amplitude=80), 8000)[1])
        self.assertTrue(voice.prepare_wav(pcm(amplitude=32767), 8000)[1])

    def test_protocol_preserves_words_and_rejects_invalid_result(self):
        text = "Ёлка 12,5 мм — нарисуй квадрат."
        raw = json.dumps({"text": text, "warnings": [], "elapsed_s": 0.4}, ensure_ascii=False).encode()
        self.assertEqual(voice.parse_worker_result(raw).text, text)
        for raw in (b'{"text":"x","text":"y","warnings":[],"elapsed_s":0}',
                    b'{"text":"x","warnings":[],"elapsed_s":NaN}',
                    b'{"text":"x","warnings":[],"elapsed_s":true}',
                    b'{"text":"","warnings":[],"elapsed_s":0}', b'[]'):
            with self.subTest(raw=raw), self.assertRaises(voice.VoiceError):
                voice.parse_worker_result(raw)


class VoiceServiceTests(unittest.TestCase):
    def test_recording_never_transcribes_and_has_one_finished(self):
        events = queue.Queue()
        service = voice.VoiceService(events)
        fake = fake_sounddevice()
        with patch.dict(sys.modules, {"sounddevice": fake}), patch.object(service, "_transcribe", side_effect=AssertionError("ASR started")):
            token = service.start(0)
            finish(service)
        items = drain(events)
        self.assertEqual([e.kind for e in items], ["state", "recorded", "finished"])
        self.assertTrue(all(e.token == token for e in items))
        self.assertIsInstance(items[1].value, voice.AudioClip)
        self.assertFalse(service.busy)
        self.assertFalse(service.recording)

    def test_capture_strictly_caps_thirty_seconds(self):
        service = voice.VoiceService(queue.Queue())
        fake = fake_sounddevice(seconds=30.01)
        with patch.dict(sys.modules, {"sounddevice": fake}):
            service.start()
            finish(service)
        clip = next(e.value for e in drain(service.events) if e.kind == "recorded")
        self.assertEqual(clip.duration_s, 30)
        self.assertTrue(clip.warnings)

    def test_stop_keeps_clip_cancel_discards_it_and_prevents_second_job(self):
        service = voice.VoiceService(queue.Queue())
        opened = threading.Event()
        fake = fake_sounddevice(end=False, on_open=opened.set)
        with patch.dict(sys.modules, {"sounddevice": fake}):
            first = service.start()
            self.assertTrue(opened.wait(1))
            with self.assertRaises(voice.VoiceError):
                service.start()
            service.stop()
            finish(service)
            self.assertIn("recorded", [e.kind for e in drain(service.events)])
            second = service.start()
            service.cancel()
            finish(service)
        self.assertGreater(second, first)
        self.assertNotIn("recorded", [e.kind for e in drain(service.events)])

    def test_capture_overflow_and_bad_device_report_errors(self):
        for device in (-1, True, "0"):
            with self.assertRaises(voice.VoiceError):
                voice.VoiceService(queue.Queue()).start(device)
        service = voice.VoiceService(queue.Queue())
        with patch.dict(sys.modules, {"sounddevice": fake_sounddevice(overflow=True)}):
            service.start()
            finish(service)
        self.assertEqual([e.kind for e in drain(service.events)], ["state", "error", "finished"])

    def test_recognize_requires_explicit_download_and_result_is_draft_data(self):
        service = voice.VoiceService(queue.Queue())
        clip = voice.AudioClip(pcm(), 8000)
        absent = voice.ModelStatus("tiny", Path("fake"), False, 75)
        with patch.object(voice, "model_status", return_value=absent), patch.object(service, "_transcribe", return_value=voice.Transcript("Квадрат 10 мм")) as transcribe:
            with self.assertRaises(voice.VoiceError):
                service.recognize(clip)
            token = service.recognize(clip, allow_download=True)
            finish(service)
        items = drain(service.events)
        self.assertEqual([e.kind for e in items], ["state", "result", "finished"])
        self.assertEqual(items[1].value.text, "Квадрат 10 мм")
        self.assertTrue(all(e.token == token for e in items))
        self.assertTrue(transcribe.call_args.args[2])

    def test_cancel_inference_kills_child_and_drops_late_output(self):
        service = voice.VoiceService(queue.Queue())
        class Process:
            returncode = None
            killed = False
            def communicate(self, input=None, timeout=None):
                if self.killed:
                    return b"", b""
                service.cancel()
                raise subprocess.TimeoutExpired("fake", timeout)
            def poll(self):
                return self.returncode
            def kill(self):
                self.killed = True
                self.returncode = -1
        child = Process()
        with patch.object(voice.subprocess, "Popen", return_value=child) as popen:
            self.assertIsNone(service._transcribe(b"fake WAV", "fast", False))
        self.assertTrue(child.killed)
        self.assertNotIn("--allow-download", popen.call_args.args[0])
        self.assertEqual(popen.call_args.kwargs["env"]["HF_HUB_OFFLINE"], "1")

    def test_close_suppresses_stale_results_and_new_jobs(self):
        service = voice.VoiceService(queue.Queue())
        service.close()
        service._publish(0, "result", voice.Transcript("late"))
        self.assertTrue(service.events.empty())
        with self.assertRaises(voice.VoiceError):
            service.start()


class VoiceWorkerTests(unittest.TestCase):
    def segment(self, **kwargs):
        return SimpleNamespace(**({"text": " Ёлка 10 мм.", "avg_logprob": -0.3, "no_speech_prob": 0.05,
                                  "compression_ratio": 1.1, "start": 0.0, "end": 0.5} | kwargs))

    def test_segment_confidence_is_checked(self):
        result = voice_worker.summarize_segments([self.segment()], elapsed_s=0.2)
        self.assertEqual(result["text"], "Ёлка 10 мм.")
        with self.assertRaises(voice.VoiceError):
            voice_worker.summarize_segments([self.segment(no_speech_prob=0.9)], elapsed_s=0.2)
        with self.assertRaises(voice.VoiceError):
            voice_worker.summarize_segments([self.segment()] * 101, elapsed_s=0.2)

    def test_worker_never_imports_model_for_invalid_audio_or_unapproved_download(self):
        wav, _ = voice.prepare_wav(pcm(), 8000)
        absent = voice.ModelStatus("tiny", Path("absent"), False, 75)
        with patch.object(voice_worker, "model_status", return_value=absent), patch.dict(sys.modules, {"faster_whisper": None}):
            with self.assertRaisesRegex(voice.VoiceError, "Модель ещё"):
                voice_worker.transcribe_wav(wav, "fast")
            with self.assertRaises(voice.VoiceError):
                voice_worker.transcribe_wav(b"not WAV", "fast", allow_download=True)

    def test_fake_model_uses_cpu_int8_local_cache_and_memory_wav(self):
        wav, _ = voice.prepare_wav(pcm(), 8000)
        captured = {}
        segment = self.segment()
        class WhisperModel:
            def __init__(self, path, **kwargs):
                captured.update(path=path, **kwargs)
            def transcribe(self, audio, **kwargs):
                captured.update(audio=audio, transcribe=kwargs)
                return iter([segment]), SimpleNamespace(duration_after_vad=0.5)
        ready = voice.ModelStatus("tiny", Path("fake-ready-model"), True, 75)
        with patch.object(voice_worker, "model_status", return_value=ready), patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=WhisperModel)}):
            result = voice_worker.transcribe_wav(wav, "fast")
        self.assertEqual(result["text"], "Ёлка 10 мм.")
        self.assertEqual((captured["device"], captured["compute_type"], captured["local_files_only"]), ("cpu", "int8", True))
        self.assertIsInstance(captured["audio"], io.BytesIO)

    def test_download_requires_explicit_flag_and_uses_current_hub_api_without_audio(self):
        wav, _ = voice.prepare_wav(pcm(), 8000)
        absent = voice.ModelStatus("tiny", Path("fake-model"), False, 75)
        ready = voice.ModelStatus("tiny", Path("fake-model"), True, 75)
        class WhisperModel:
            def __init__(self, *args, **kwargs):
                pass
            def transcribe(model, audio, **kwargs):
                return iter([self.segment()]), SimpleNamespace(duration_after_vad=0.5)
        with patch.object(voice_worker, "model_status", side_effect=[absent, ready]), \
                patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=WhisperModel),
                                        "huggingface_hub": SimpleNamespace(snapshot_download=lambda *a, **kw: None)}), \
                patch("huggingface_hub.snapshot_download") as download:
            voice_worker.transcribe_wav(wav, "fast", allow_download=True)
        self.assertEqual(download.call_args.args, ("Systran/faster-whisper-tiny",))
        self.assertIs(download.call_args.kwargs["token"], False)
        self.assertNotIn("audio", download.call_args.kwargs)
        self.assertNotIn("local_dir_use_symlinks", download.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
