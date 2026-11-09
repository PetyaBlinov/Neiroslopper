"""Offline diagnostics: no windows, serial ports, sockets or motor actions."""
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eventlog import EventLog, record_event, age


def rows(log):
    return [json.loads(line) for line in log.path.read_text(encoding="ascii").splitlines()]


class EventLogTests(unittest.TestCase):
    def test_parallel_events_keep_complete_records_and_private_payloads_are_dropped(self):
        with TemporaryDirectory() as directory:
            log = EventLog(directory)

            def write():
                for i in range(40):
                    log.record("action_submitted", action="jog", count=i, mode="Wi-Fi",
                               prompt="draw my private text", gcode="G0 X999", port="COM5",
                               endpoint="tcp://192.168.1.7:23", password="secret", reason="secret",
                               caller="192.168.1.7", error_type="private-failure-message")

            threads = [threading.Thread(target=write) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            saved = rows(log)
            self.assertEqual(len(saved), 160)
            self.assertTrue(all(row["action"] == "jog" and row["mode"] == "Wi-Fi" for row in saved))
            self.assertTrue(all("utc" in row and "monotonic_s" in row for row in saved))
            text = log.path.read_text(encoding="ascii")
            for private in ("private", "G0 X999", "COM5", "192.168", "secret", "password", "endpoint"):
                self.assertNotIn(private, text)

    def test_rotation_keeps_bounded_complete_json_history(self):
        with TemporaryDirectory() as directory:
            log = EventLog(directory, max_bytes=8192, backups=2)
            for i in range(1000):
                log.record("action_finished", action="jog", count=i, success=True)
            files = list(log.path.parent.iterdir())
            self.assertEqual(len(files), 3)
            self.assertLessEqual(sum(path.stat().st_size for path in files), 3 * 8192)
            for path in files:
                for line in path.read_text(encoding="ascii").splitlines():
                    self.assertEqual(json.loads(line)["event"], "action_finished")
            self.assertEqual(rows(log)[-1]["count"], 999)

    def test_no_data_directory_is_inert_and_creates_nothing(self):
        log = EventLog(None)
        self.assertIsNone(log.path)
        with patch.object(Path, "mkdir") as mkdir:
            self.assertFalse(log.record("app_started", connected=False))
        mkdir.assert_not_called()

    def test_read_only_disk_and_broken_optional_logger_do_not_escape(self):
        with TemporaryDirectory() as directory:
            log = EventLog(directory)
            with patch.object(Path, "mkdir", side_effect=PermissionError("disk unavailable")):
                self.assertFalse(log.record("action_submitted", action="draw"))
        logger = Mock()
        logger.record.side_effect = OSError("disk unavailable")
        record_event(SimpleNamespace(eventlog=logger), "stop_requested", reason="user_stop")
        record_event(SimpleNamespace(), "optional_no_logger")
        logger.record.assert_called_once_with("stop_requested", reason="user_stop")

    def test_nonfinite_or_mistyped_values_and_invalid_event_names_are_rejected(self):
        with TemporaryDirectory() as directory:
            log = EventLog(directory)
            self.assertFalse(log.record("private message with spaces", action="jog"))
            self.assertTrue(log.record("controller_error", error_type="OSError", errno=5,
                                       duration_ms=math.inf, count=2 ** 128, ready="secret", reason="worker_error"))
            record = rows(log)[0]
            self.assertEqual(record["error_type"], "OSError")
            self.assertEqual(record["errno"], 5)
            self.assertNotIn("duration_ms", record)
            self.assertNotIn("count", record)
            self.assertNotIn("ready", record)

    def test_rotation_without_backups_keeps_only_current_file(self):
        with TemporaryDirectory() as directory:
            log = EventLog(directory, max_bytes=8192, backups=0)
            for i in range(300):
                log.record("action_finished", action="jog", count=i)
            self.assertEqual(list(log.path.parent.iterdir()), [log.path])
            self.assertLessEqual(log.path.stat().st_size, 8192)
            self.assertEqual(rows(log)[-1]["count"], 299)

    def test_unknown_age_is_distinct_from_zero(self):
        self.assertIsNone(age(1.0, None))
        self.assertIsNone(age(math.inf, 0))
        self.assertEqual(age(1.0, 2.0), 0)
        self.assertEqual(age(3.2359, 1.0), 2.236)

    def test_dwin_stop_is_logged_without_changing_its_dispatch(self):
        from dwin_bridge import CommandRouter, Upload, TOUCH_VP
        with TemporaryDirectory() as directory:
            app = SimpleNamespace(closed=False, stop=Mock(), eventlog=EventLog(directory))
            code, _ = CommandRouter(app).dispatch(Upload(TOUCH_VP, (14,), 0), armed=False, linked=False)
            self.assertEqual(code, 0)
            app.stop.assert_called_once_with()
            events = rows(app.eventlog)
            self.assertEqual([item["event"] for item in events], ["dwin.input", "dwin.result"])
            self.assertEqual(events[0]["key"], 14)
            self.assertEqual(events[1]["result_code"], 0)


if __name__ == "__main__":
    unittest.main()
