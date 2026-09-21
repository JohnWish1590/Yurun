import json
import queue
import socket
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import config
import dictionary
import hotkey
import privileged_ipc
import typer
from voice_session import VoiceSession


class ConfigContractTests(unittest.TestCase):
    def test_save_and_reload_preserve_supported_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            with patch.object(config, "config_path", return_value=path):
                saved = config.Config()
                saved.set("input_mode", "direct")
                saved.set("hotkey", "F8")

                loaded = config.Config()
                self.assertEqual(loaded.get("input_mode"), "direct")
                self.assertEqual(loaded.get("hotkey"), "F8")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["hotkey"], "F8")

    def test_failed_save_rolls_back_in_memory_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            with patch.object(config, "config_path", return_value=path):
                saved = config.Config()
                old_value = saved.get("hotkey")
                with patch.object(config, "_atomic_write", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        saved.set("hotkey", "F8")
                self.assertEqual(saved.get("hotkey"), old_value)


class DictionaryContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "user_dictionary.json"
        self.cache = dictionary._cache
        dictionary._cache = None
        self.path_patch = patch.object(dictionary, "dict_path", return_value=self.path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        dictionary._cache = self.cache
        self.temp_dir.cleanup()

    def test_learning_round_trip_and_local_replace(self):
        dictionary.add_entry("changelog", "天气log")
        self.assertEqual(dictionary.apply_local_replace("请查看天气log"), "请查看changelog")

        entries = dictionary.get_entries()
        self.assertEqual(entries[0]["text"], "changelog")
        self.assertEqual(entries[0]["aliases"][0]["text"], "天气log")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))[0]["text"], "changelog")

    def test_failed_save_does_not_publish_new_entry_to_cache(self):
        with patch.object(dictionary, "save", return_value=False):
            with self.assertRaises(OSError):
                dictionary.add_entry("not-persisted")
        self.assertEqual(dictionary.get_entries(), [])


class HotkeyContractTests(unittest.TestCase):
    def test_supported_key_names_and_formatting(self):
        self.assertEqual(hotkey._vk_for("F8"), 0x77)
        self.assertEqual(hotkey._vk_for("a"), ord("A"))
        self.assertEqual(hotkey.format_hotkey(hotkey.MOD_ALT | hotkey.MOD_CONTROL, "F8"),
                         "Ctrl + Alt + F8")
        self.assertEqual(hotkey.hotkey_id(hotkey.MOD_ALT, "F8"), (hotkey.MOD_ALT, 0x77))


class IpcContractTests(unittest.TestCase):
    def test_json_socket_preserves_utf8_messages(self):
        left, right = socket.socketpair()
        try:
            sender = privileged_ipc._JsonSocket(left)
            receiver = privileged_ipc._JsonSocket(right)
            sender.send({"kind": "type", "payload": {"text": "中文、emoji 🙂"}})
            self.assertEqual(receiver.recv()["payload"]["text"], "中文、emoji 🙂")
        finally:
            left.close()
            right.close()

    def test_proof_changes_when_nonce_changes(self):
        secret = b"s" * 32
        self.assertEqual(privileged_ipc._proof(secret, "nonce"),
                         privileged_ipc._proof(secret, "nonce"))
        self.assertNotEqual(privileged_ipc._proof(secret, "nonce"),
                            privileged_ipc._proof(secret, "other"))

    def test_type_text_uses_one_request_for_a_batch(self):
        bridge = privileged_ipc.PrivilegedBridge()
        with patch.object(bridge, "request", return_value={"ok": True, "sent": 6}) as request:
            self.assertEqual(bridge.type_text("session-1", "abc"), 6)
        request.assert_called_once_with(
            "type", {"session_id": "session-1", "text": "abc"}, timeout=0.8)

    def test_unexpected_disconnect_is_reported_once(self):
        class BrokenConnection:
            def recv(self):
                raise ConnectionError("closed")

            def close(self):
                pass

        events = []
        bridge = privileged_ipc.PrivilegedBridge(on_event=events.append)
        bridge._conn = BrokenConnection()
        bridge._running = True
        bridge._read_loop()
        self.assertFalse(bridge.connected)
        self.assertEqual(events[0]["event"], "connection_lost")
        self.assertIs(events[0]["_bridge"], bridge)

    def test_app_routes_connection_events_to_ui_queue(self):
        import main

        app = object.__new__(main.App)
        app.ui_q = queue.Queue()
        app._on_privileged_bridge_event({"event": "connection_lost"})
        self.assertEqual(app.ui_q.get_nowait(),
                         ("bridge_event", {"event": "connection_lost"}))

    def test_stale_bridge_disconnect_does_not_drop_new_bridge(self):
        import main

        app = object.__new__(main.App)
        current = MagicMock()
        stale = MagicMock()
        app.privileged_bridge = current
        app._handle_privileged_bridge_lost(stale)
        self.assertIs(app.privileged_bridge, current)
        stale.close.assert_called_once_with()


class InputContractTests(unittest.TestCase):
    def test_unicode_input_struct_uses_utf16_scan_field(self):
        event = typer._make_unicode_input(ord("中"), False)
        self.assertEqual(event.type, typer.INPUT_KEYBOARD)
        self.assertEqual(event.ki.wScan, ord("中"))
        self.assertEqual(event.ki.dwFlags, typer.KEYEVENTF_UNICODE)

    def test_non_bmp_characters_are_sent_as_utf16_surrogates(self):
        events = typer._character_inputs("🙂")
        self.assertEqual([event.ki.wScan for event in events],
                         [0xD83D, 0xD83D, 0xDE42, 0xDE42])
        self.assertEqual(typer.input_event_count("🙂"), 4)

    def test_main_input_step_sends_a_bounded_batch(self):
        import main

        app = object.__new__(main.App)
        app._type_job = {
            "round_id": None,
            "buffer": "a" * (main.TYPE_BATCH_CHARS + 3),
            "total_chars": main.TYPE_BATCH_CHARS + 3,
            "sent_chars": 0,
            "interval_ms": 8,
            "on_done": None,
        }
        app._type_jobs = deque()
        app._typing = True
        app._type_interval_ms = 8
        app._stream_complete_rounds = set()
        app._streaming_rounds = set()
        app._sessions = {}
        app.root = SimpleNamespace(after=lambda _delay, _callback: None)
        app._is_active_session = lambda _round_id: False
        app._record_first_insert = lambda _round_id: None
        app._abort_type_job = lambda _job, reason: self.fail(reason)

        with patch("typer.type_text", return_value=main.TYPE_BATCH_CHARS * 2) as sender:
            app._type_step()

        sender.assert_called_once_with("a" * main.TYPE_BATCH_CHARS)
        self.assertEqual(app._type_job["sent_chars"], main.TYPE_BATCH_CHARS)
        self.assertEqual(len(app._type_job["buffer"]), 3)


class ClipboardContractTests(unittest.TestCase):
    def test_clipboard_snapshot_limits_formats_to_safe_text_handles(self):
        import clipboard_transaction

        self.assertEqual(
            clipboard_transaction.SAFE_FORMATS,
            frozenset({clipboard_transaction.CF_TEXT,
                       clipboard_transaction.CF_DIB,
                       clipboard_transaction.CF_OEMTEXT,
                       clipboard_transaction.CF_UNICODETEXT,
                       clipboard_transaction.CF_LOCALE,
                       clipboard_transaction.CF_DIBV5}),
        )
        self.assertNotIn(2, clipboard_transaction.SAFE_FORMATS)  # CF_BITMAP

    def test_delayed_restore_skips_newer_user_clipboard(self):
        import main

        class FakeRoot:
            def __init__(self):
                self.calls = []

            def clipboard_clear(self):
                self.calls.append("clear")

            def clipboard_append(self, value):
                self.calls.append(("append", value))

            def update(self):
                self.calls.append("update")

        app = object.__new__(main.App)
        app.root = FakeRoot()
        with patch("main._clipboard_sequence", return_value=2):
            self.assertFalse(app._clipboard_restore("old", expected_sequence=1))
        self.assertEqual(app.root.calls, [])

    def test_restore_uses_owned_clipboard_sequence(self):
        import main

        class FakeRoot:
            def clipboard_clear(self):
                pass

            def clipboard_append(self, value):
                self.value = value

            def update(self):
                pass

        app = object.__new__(main.App)
        app.root = FakeRoot()
        app._correction_clip_armed = True
        app._correction_clip_backup = "old"
        app._correction_clip_sequence = 7
        with patch("main._clipboard_sequence", return_value=7):
            app._correction_clipboard_restore()
        self.assertEqual(app.root.value, "old")
        self.assertFalse(app._correction_clip_armed)

    def test_correction_does_not_arm_without_native_snapshot(self):
        import main

        app = object.__new__(main.App)
        app._clipboard_backup = lambda: None
        app._correction_clip_backup = "stale"
        app._correction_clip_sequence = 7
        app._correction_clip_armed = False
        self.assertFalse(app._correction_clipboard_arm())
        self.assertIsNone(app._correction_clip_backup)


class SessionContractTests(unittest.TestCase):
    def test_keyup_is_recorded_once(self):
        import threading

        session = VoiceSession(1, None, threading.Event(), {})
        first = session.mark_keyup()
        second = session.mark_keyup()
        self.assertEqual(first, second)


class SingleInstanceContractTests(unittest.TestCase):
    def test_pid_record_contains_identity_metadata(self):
        import json as json_module
        import singleinstance

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "yurun.pid"
            singleinstance._write_pid(path)
            record = json_module.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], __import__("os").getpid())
            self.assertTrue(record["executable"])
            self.assertTrue(record["script"])


if __name__ == "__main__":
    unittest.main()
