"""Connection history tests use temporary files and never touch a board."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connection_preferences import load_verified_endpoints, remember_verified_endpoint


class ConnectionPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.target = self.folder / "connection-settings.json"

    def test_no_data_directory_does_not_save_or_connect(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("No network access")):
            self.assertEqual(load_verified_endpoints(None), [])
            self.assertIsNone(remember_verified_endpoint(None, "tcp://192.168.0.1:23"))

    def test_remembers_only_endpoints_with_most_recent_first_and_no_duplicates(self):
        for index in range(1, 11):
            remember_verified_endpoint(self.folder, f"tcp://192.168.0.{index}:23")
        remember_verified_endpoint(self.folder, "tcp://192.168.0.5:23")
        expected = ["tcp://192.168.0.5:23"] + [f"tcp://192.168.0.{i}:23" for i in (10, 9, 8, 7, 6, 4, 3)]
        self.assertEqual(load_verified_endpoints(self.folder), expected)
        payload = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"version": 1, "wifi_endpoints": expected})
        self.assertFalse(self.target.with_suffix(".json.tmp").exists())

    def test_load_sanitizes_stale_file_without_resolving_names_or_connecting(self):
        self.target.write_text(json.dumps({"version": 1, "wifi_endpoints": [
            "tcp://192.168.0.8:23", "tcp://192.168.0.8:23", "tcp://8.8.8.8:23",
            "tcp://user:secret@192.168.0.1:23", "tcp://fluidnc.local:23",
            "https://192.168.0.1", "tcp://192.168.0.1:23/path", None, {},
            "tcp://[fd00:0:0:0:0:0:0:1]:23"]}), encoding="utf-8")
        with patch("socket.getaddrinfo", side_effect=AssertionError("No network access")):
            self.assertEqual(load_verified_endpoints(self.folder),
                             ["tcp://192.168.0.8:23", "tcp://[fd00::1]:23"])

    def test_invalid_endpoint_does_not_overwrite_existing_preferences(self):
        remember_verified_endpoint(self.folder, "tcp://192.168.0.8:23")
        original = self.target.read_bytes()
        for endpoint in ("tcp://8.8.8.8:23", "tcp://fluidnc.local:23",
                         "tcp://user:password@192.168.0.1:23", "COM5"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                remember_verified_endpoint(self.folder, endpoint)
            self.assertEqual(self.target.read_bytes(), original)

    def test_missing_malformed_or_unknown_version_is_ignored(self):
        self.assertEqual(load_verified_endpoints(self.folder), [])
        for text in ("{", "[]", '{"version": 2, "wifi_endpoints": []}',
                     '{"version": true, "wifi_endpoints": []}',
                     '{"version": 1, "wifi_endpoints": "tcp://192.168.0.1:23"}',
                     " " * 16385):
            with self.subTest(text=text[:80]):
                self.target.write_text(text, encoding="utf-8")
                self.assertEqual(load_verified_endpoints(self.folder), [])


if __name__ == "__main__":
    unittest.main()
