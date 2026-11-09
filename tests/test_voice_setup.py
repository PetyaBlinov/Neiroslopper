"""Optional vendor API checks: synthetic audio, no mic/model/network access."""
import importlib.util
import inspect
import io
import math
import struct
import unittest
import wave


VOICE_INSTALLED = all(importlib.util.find_spec(name) is not None for name in (
    "av", "numpy", "ctranslate2", "faster_whisper", "huggingface_hub",
))


@unittest.skipUnless(VOICE_INSTALLED, "Optional voice libraries are not installed")
class InstalledVoiceApiTests(unittest.TestCase):
    def test_decoder_accepts_memory_wave_with_pinned_pyav(self):
        # Exercises the av.open(metadata_errors=...) call in faster-whisper,
        # which newer PyAV versions removed. There is no speech model involved.
        from faster_whisper.audio import decode_audio

        sample_rate = 16000
        samples = [int(2000 * math.sin(2 * math.pi * 440 * i / sample_rate))
                   for i in range(1600)]
        audio = io.BytesIO()
        with wave.open(audio, "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(sample_rate)
            stream.writeframes(struct.pack("<1600h", *samples))
        audio.seek(0)
        decoded = decode_audio(audio, sampling_rate=sample_rate)
        self.assertEqual(len(decoded), len(samples))
        self.assertEqual(str(decoded.dtype), "float32")

    def test_hub_accepts_explicit_download_parameters_without_calling_it(self):
        # Signature binding checks a real library API without accessing the Hub.
        # faster-whisper's older download wrapper passes a removed parameter.
        from huggingface_hub import snapshot_download

        inspect.signature(snapshot_download).bind(
            "Systran/faster-whisper-tiny", local_dir="unused-test-path", token=False,
            allow_patterns=["config.json", "preprocessor_config.json", "model.bin",
                            "tokenizer.json", "vocabulary.*"],
        )

    def test_cpu_int8_runtime_is_available(self):
        import ctranslate2

        self.assertIn("int8", ctranslate2.get_supported_compute_types("cpu"))


if __name__ == "__main__":
    unittest.main()
