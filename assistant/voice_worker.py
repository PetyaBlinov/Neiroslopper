"""Cancellable local ASR process: bounded memory-WAV input, JSON output.

Adapted from the team's DWIN assistant. No microphone, Tk, machine transport or
audio files. Model downloads require the explicit --allow-download action.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
import wave

from .voice import MAX_AUDIO_BYTES, MAX_SECONDS, MAX_TEXT, PROFILES, VoiceError, model_status, prepare_wav


def summarize_segments(segments, *, elapsed_s, extra_warnings=()):
    parts, warnings, total_speech = [], list(extra_warnings), 0.0
    for index, segment in enumerate(segments):
        if index >= 100:
            raise VoiceError("Слишком длинный результат. Произнесите более короткую фразу.")
        text = segment.text
        if not isinstance(text, str) or not text.strip():
            continue
        metrics = (segment.avg_logprob, segment.no_speech_prob, segment.compression_ratio, segment.start, segment.end)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in metrics):
            raise VoiceError("Не получена проверяемая расшифровка.")
        if not (segment.avg_logprob <= 0 and 0 <= segment.no_speech_prob <= 1 and segment.compression_ratio >= 0
                and 0 <= segment.start <= segment.end <= MAX_SECONDS + 0.1):
            raise VoiceError("Показатели расшифровки вне допустимого диапазона.")
        if segment.no_speech_prob > 0.8 or segment.avg_logprob < -1.5 or segment.compression_ratio > 3.0:
            raise VoiceError("Речь распознана ненадёжно. Повторите фразу ближе к микрофону.")
        if segment.avg_logprob < -0.7 or segment.no_speech_prob > 0.35 or segment.compression_ratio > 2.4:
            warnings.append("Есть неуверенно распознанные слова: проверьте текст.")
        total_speech += max(0, segment.end - segment.start)
        parts.append(text)
    text = "".join(parts).strip()
    if total_speech < 0.2 or not text:
        raise VoiceError("Речь не обнаружена. Проверьте выбранный микрофон.")
    if len(text) > MAX_TEXT:
        raise VoiceError("Слишком длинный текст: запишите более короткую фразу.")
    return {"text": text, "warnings": list(dict.fromkeys(warnings)), "elapsed_s": elapsed_s}


def transcribe_wav(raw: bytes, profile: str, *, allow_download=False):
    # Only decode bytes; do not accept a path or URL from the UI.
    if not isinstance(raw, bytes) or len(raw) > MAX_AUDIO_BYTES:
        raise VoiceError("Слишком большая запись.")
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                raise VoiceError("Ожидается моно PCM16 WAV.")
            rate, frames = wav.getframerate(), wav.getnframes()
            if frames > rate * MAX_SECONDS:
                raise VoiceError("Запись длиннее 30 секунд.")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * 2:
                raise VoiceError("Запись оборвана.")
    except (wave.Error, EOFError) as exc:
        raise VoiceError("Некорректная запись WAV.") from exc
    checked_wav, warnings = prepare_wav(pcm, rate)
    status = model_status(profile)
    if not status.ready and not allow_download:
        raise VoiceError("Модель ещё не скачана. Разрешите загрузку перед распознаванием.")
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    if not allow_download:
        os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise VoiceError("Компоненты голоса не установлены. Запустите INSTALL_VOICE.cmd в папке приложения.") from exc
    if not status.ready:
        from huggingface_hub import snapshot_download
        # This is the only network-capable operation and only the explicit
        # recognition action can reach it. No audio is sent to the model host.
        # Use Hub's current public API directly: faster-whisper 1.2.1's
        # download helper still passes an argument removed in Hub 1.x.
        snapshot_download("Systran/faster-whisper-" + PROFILES[profile], local_dir=str(status.path),
                          token=False, allow_patterns=["config.json", "preprocessor_config.json",
                          "model.bin", "tokenizer.json", "vocabulary.*"])
        status = model_status(profile)
        if not status.ready:
            raise VoiceError("Загрузка модели не завершена. Проверьте интернет и повторите распознавание.")
    started = time.perf_counter()
    model = WhisperModel(str(status.path), device="cpu", compute_type="int8", cpu_threads=4,
                         num_workers=1, local_files_only=True)
    segments, info = model.transcribe(io.BytesIO(checked_wav), language="ru", task="transcribe",
        beam_size=5, temperature=0.0, condition_on_previous_text=False, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
        no_speech_threshold=0.6, log_prob_threshold=-1.0, compression_ratio_threshold=2.4,
        without_timestamps=True)
    if info.duration_after_vad < 0.2:
        raise VoiceError("Речь не обнаружена. Запишите фразу в более тихом месте.")
    # Consume lazily under the parent's cancellable process, with segment cap.
    result = summarize_segments(segments, elapsed_s=0.0, extra_warnings=warnings)
    result["elapsed_s"] = time.perf_counter() - started
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=tuple(PROFILES), required=True)
    parser.add_argument("--allow-download", action="store_true")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_AUDIO_BYTES + 1)
        result = transcribe_wav(raw, args.profile, allow_download=args.allow_download)
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        return 0
    except VoiceError as exc:
        message = str(exc)[:500]
    except Exception:
        message = "Не удалось запустить локальную модель речи. Проверьте компоненты и загрузку модели."
    sys.stdout.buffer.write(json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
