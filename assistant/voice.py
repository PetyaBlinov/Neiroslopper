"""Explicit, local dictation, adapted from the team's DWIN assistant.

Importing this module opens no device, loads no model and makes no network call.
Recording and recognition are separate actions. Audio stays in memory, and the
result is a draft, never an assistant command or a machine instruction.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Literal
import wave

ROOT = Path(__file__).resolve().parent.parent
MODEL_ROOT = Path(os.environ.get("PLOTTER_VOICE_MODEL_ROOT") or ROOT / "data" / "voice-models").expanduser()
PROFILES = {"fast": "tiny", "quality": "small"}  # Multilingual, including Russian.
MAX_SECONDS = 30
MAX_AUDIO_BYTES = 6_000_128
MAX_RESPONSE_BYTES = 65_536
MAX_TEXT = 1800
WORKER_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 600
_MICROPHONE_LOCK = threading.Lock()


class VoiceError(ValueError):
    """A short, actionable message suitable for the dictation dialog."""


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    is_default: bool = False


@dataclass(frozen=True)
class AudioClip:
    pcm: bytes
    sample_rate: int
    warnings: tuple[str, ...] = ()

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / (self.sample_rate * 2)


@dataclass(frozen=True)
class Transcript:
    text: str
    warnings: tuple[str, ...] = ()
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class ModelStatus:
    model: str
    path: Path
    ready: bool
    download_mb: int


@dataclass(frozen=True)
class VoiceEvent:
    token: int
    kind: Literal["state", "level", "recorded", "result", "error", "finished"]
    value: object = None


def dependency_status() -> tuple[str, ...]:
    """Check installation metadata without importing audio/ML libraries."""
    missing = []
    for module, distribution in (("sounddevice", "sounddevice"), ("faster_whisper", "faster-whisper")):
        try:
            present = importlib.util.find_spec(module) is not None
        except (ValueError, ImportError):
            present = module in sys.modules
        if not present:
            missing.append(distribution)
    return tuple(missing)


missing_dependencies = dependency_status


def model_status(profile: str = "fast") -> ModelStatus:
    """Inspect only our local cache; no model imports, downloads or validation inference."""
    if profile not in PROFILES:
        raise VoiceError("Выберите режим распознавания.")
    path = MODEL_ROOT / PROFILES[profile]
    required = ("model.bin", "config.json", "tokenizer.json")
    try:
        ready = all((path / name).is_file() and (path / name).stat().st_size > 0 for name in required)
    except OSError:
        ready = False
    return ModelStatus(PROFILES[profile], path, ready, 75 if profile == "fast" else 485)


def pcm_stats(data: bytes) -> dict[str, float]:
    if not isinstance(data, bytes) or not data or len(data) % 2:
        raise VoiceError("Нет целой записи PCM16.")
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    count = len(samples)
    return {"rms": math.sqrt(sum(x * x for x in samples) / count) / 32768,
            "peak": max(abs(x) for x in samples) / 32768,
            "clipping_ratio": sum(abs(x) >= 32760 for x in samples) / count}


def prepare_wav(pcm: bytes, sample_rate: int) -> tuple[bytes, tuple[str, ...]]:
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 96000:
        raise VoiceError("Неподдерживаемая частота микрофона.")
    if not isinstance(pcm, bytes) or len(pcm) > MAX_AUDIO_BYTES:
        raise VoiceError("Слишком большая запись.")
    duration = len(pcm) / (sample_rate * 2)
    if not 0.25 <= duration <= MAX_SECONDS:
        raise VoiceError("Запись должна длиться от 0,25 до 30 секунд.")
    stats = pcm_stats(pcm)
    if stats["rms"] < 0.0008:
        raise VoiceError("Не слышно речи. Проверьте микрофон и повторите запись.")
    warnings = []
    if stats["rms"] < 0.008:
        warnings.append("Тихий сигнал: проверьте слова и числа.")
    if stats["clipping_ratio"] > 0.01:
        warnings.append("Микрофон перегружен: уменьшите усиление или отодвиньтесь.")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue(), tuple(warnings)


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VoiceError("Повтор поля ответа распознавания.")
        result[key] = value
    return result


def parse_worker_result(raw: bytes) -> Transcript:
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise VoiceError("Слишком большой ответ распознавания.")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs)
    except (ValueError, UnicodeError) as exc:
        raise VoiceError("Некорректный ответ распознавания.") from exc
    if not isinstance(result, dict) or set(result) != {"text", "warnings", "elapsed_s"}:
        raise VoiceError("Неверная форма ответа распознавания.")
    text, warnings, elapsed = result["text"], result["warnings"], result["elapsed_s"]
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise VoiceError("Нет короткого распознанного текста. Повторите фразу.")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise VoiceError("Недопустимые символы в распознанном тексте.")
    if not isinstance(warnings, list) or len(warnings) > 12 or any(
            not isinstance(item, str) or not item or len(item) > 500 for item in warnings):
        raise VoiceError("Неверные предупреждения распознавания.")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or not 0 <= elapsed <= DOWNLOAD_TIMEOUT:
        raise VoiceError("Неверное время распознавания.")
    return Transcript(text, tuple(warnings), float(elapsed))


class VoiceService:
    """One background job at a time; only plain data crosses the event queue.

    ``start`` records, ``stop`` produces ``recorded`` with an AudioClip.
    ``recognize`` explicitly starts local recognition, optionally downloading
    a missing model. Every accepted action returns a token and emits exactly
    one ``finished``. The UI must ignore events whose token is no longer its
    current token, and clear that token when cancelling or closing.
    """

    def __init__(self, events: queue.Queue):
        self.events = events
        self._lock = threading.Lock()
        self._busy = False
        self._recording = False
        self._closed = False
        self._token = 0
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._thread = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._recording

    @staticmethod
    def devices() -> list[InputDevice]:
        """Enumerate inputs without opening a capture stream."""
        try:
            import sounddevice as sd
            default = int(sd.default.device[0])
            hosts = sd.query_hostapis()
            return [InputDevice(index, f"{item['name']} · {hosts[item['hostapi']]['name']}", index == default)
                    for index, item in enumerate(sd.query_devices()) if item["max_input_channels"] > 0]
        except ImportError as exc:
            raise VoiceError("Для голосового ввода запустите INSTALL_VOICE.cmd в папке приложения.") from exc
        except Exception as exc:
            raise VoiceError("Не удалось получить микрофоны. Проверьте устройства и разрешения Windows.") from exc

    def _begin(self, target, *args) -> int:
        with self._lock:
            if self._closed:
                raise VoiceError("Окно голосового ввода закрыто.")
            if self._busy:
                raise VoiceError("Предыдущая операция ещё завершается.")
            self._token += 1
            token = self._token
            self._stop = threading.Event()
            self._cancel = threading.Event()
            self._busy = True
            self._thread = threading.Thread(target=self._run, args=(token, target, args), daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._busy = False
                raise
            return token

    def start(self, device: int | None = None) -> int:
        if device is not None and (isinstance(device, bool) or not isinstance(device, int) or device < 0):
            raise VoiceError("Выберите доступный микрофон.")
        return self._begin(self._capture, device)

    def stop(self) -> None:
        self._stop.set()

    def recognize(self, clip: AudioClip, profile: str = "fast", *, allow_download: bool = False) -> int:
        if not isinstance(clip, AudioClip):
            raise VoiceError("Сначала запишите фразу.")
        status = model_status(profile)
        if not status.ready and not allow_download:
            raise VoiceError("Модель ещё не скачана. Разрешите загрузку перед распознаванием.")
        return self._begin(self._recognize, clip, profile, bool(allow_download))

    def cancel(self) -> None:
        with self._lock:
            self._cancel.set()
            self._stop.set()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel.set()
            self._stop.set()

    def _publish(self, token: int, kind, value=None) -> None:
        with self._lock:
            if self._closed or self._cancel.is_set() or token != self._token:
                return
            self.events.put(VoiceEvent(token, kind, value))

    def _run(self, token, target, args):
        try:
            if not self._cancel.is_set():
                target(token, *args)
        except Exception as exc:
            self._publish(token, "error", str(exc)[:500] if isinstance(exc, VoiceError) else
                          "Не удалось выполнить голосовой ввод. Проверьте микрофон и установку компонентов.")
        finally:
            with self._lock:
                self._recording = False
                self._busy = False
                # Put finished before allowing the next job to enqueue events.
                self.events.put(VoiceEvent(token, "finished"))

    def _capture(self, token, device):
        if not _MICROPHONE_LOCK.acquire(blocking=False):
            raise VoiceError("Микрофон уже используется другим окном голосового ввода.")
        try:
            import sounddevice as sd
            selected = sd.query_devices(device, "input")
            rate = int(round(selected["default_samplerate"]))
            if not 8000 <= rate <= 96000:
                raise VoiceError("Неподдерживаемая частота микрофона; выберите другой вход.")
            chunks, errors = [], []
            count = 0
            limit = rate * MAX_SECONDS
            done = threading.Event()
            data_lock = threading.Lock()

            def capture(indata, frames, timing, status):
                nonlocal count
                if self._stop.is_set() or self._cancel.is_set():
                    raise sd.CallbackStop
                with data_lock:
                    if status:
                        errors.append("Пропущен фрагмент звука. Повторите запись; числа могли потеряться.")
                        raise sd.CallbackAbort
                    raw = bytes(indata)
                    if len(raw) != frames * 2:
                        errors.append("Микрофон вернул неполный фрагмент. Повторите запись.")
                        raise sd.CallbackAbort
                    raw = raw[:max(0, limit - count) * 2]
                    chunks.append(raw)
                    count += len(raw) // 2
                    if len(chunks) % 5 == 0 and raw:
                        self._publish(token, "level", min(1.0, pcm_stats(raw)["rms"] * 8))
                    if count >= limit:
                        raise sd.CallbackStop

            with sd.RawInputStream(device=device, samplerate=rate, channels=1, dtype="int16",
                                   blocksize=max(1, rate // 50), latency="high",
                                   callback=capture, finished_callback=done.set):
                with self._lock:
                    self._recording = True
                self._publish(token, "state", "Запись…")
                started = time.monotonic()
                while not done.wait(0.05):
                    if self._stop.is_set() or self._cancel.is_set() or time.monotonic() - started >= MAX_SECONDS:
                        break
            with self._lock:
                self._recording = False
            if self._cancel.is_set():
                return
            with data_lock:
                if errors:
                    raise VoiceError(errors[0])
                pcm = b"".join(chunks)
            _, warnings = prepare_wav(pcm, rate)
            if count >= limit:
                warnings += ("Запись закончена по лимиту 30 с: проверьте конец фразы.",)
            self._publish(token, "recorded", AudioClip(pcm, rate, warnings))
        except ImportError as exc:
            raise VoiceError("Для голосового ввода запустите INSTALL_VOICE.cmd в папке приложения.") from exc
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("Не удалось записать звук. Проверьте доступ к микрофону в параметрах Windows.") from exc
        finally:
            _MICROPHONE_LOCK.release()

    def _recognize(self, token, clip, profile, allow_download):
        wav, warnings = prepare_wav(clip.pcm, clip.sample_rate)
        self._publish(token, "state", "Загружаю модель и распознаю…" if not model_status(profile).ready else "Распознаю локально…")
        transcript = self._transcribe(wav, profile, allow_download)
        if transcript is not None:
            self._publish(token, "result", Transcript(transcript.text,
                tuple(dict.fromkeys((*clip.warnings, *warnings, *transcript.warnings))), transcript.elapsed_s))

    def _transcribe(self, wav: bytes, profile: str, allow_download: bool) -> Transcript | None:
        if self._cancel.is_set():
            return None
        environment = dict(os.environ, HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_PROGRESS_BARS="1",
                           PYTHONIOENCODING="utf-8", OMP_NUM_THREADS="4", PLOTTER_VOICE_MODEL_ROOT=str(MODEL_ROOT))
        if not allow_download:
            environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe":
            python = python.with_name("python.exe")
        command = [str(python), "-X", "utf8", "-m", "assistant.voice_worker", "--profile", profile]
        if allow_download:
            command.append("--allow-download")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creationflags)
        started = time.monotonic()
        first = True
        try:
            while True:
                if self._cancel.is_set():
                    return None
                if time.monotonic() - started > (DOWNLOAD_TIMEOUT if allow_download else WORKER_TIMEOUT):
                    raise VoiceError("Распознавание заняло слишком долго. Повторите короче или выберите быстрый режим.")
                try:
                    stdout, _stderr = process.communicate(input=wav if first else None, timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if self._cancel.is_set():
                return None
            if process.returncode:
                try:
                    error = json.loads(stdout.decode("utf-8")) if len(stdout) <= MAX_RESPONSE_BYTES else {}
                    message = error.get("error") if isinstance(error, dict) and set(error) == {"error"} else None
                except (ValueError, UnicodeError):
                    message = None
                raise VoiceError(message[:500] if isinstance(message, str) else "Локальное распознавание не запустилось. Проверьте компоненты голоса.")
            return parse_worker_result(stdout)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
