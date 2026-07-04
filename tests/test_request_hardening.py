import asyncio
import unittest
from unittest import mock

from PyQt6.QtWidgets import QApplication

import companion_manager as cm


app = QApplication.instance() or QApplication([])


class _DummyThread:
    def __init__(self, *args, **kwargs):
        self._alive = False

    def start(self):
        self._alive = False

    def is_alive(self):
        return self._alive


class _DummyListener:
    def __init__(self, on_level, on_wake):
        self.audio_available = True
        self._on_level = on_level
        self._on_wake = on_wake
        self.stop_recording_called = False

    def start(self):
        return None

    def stop(self):
        return None

    def start_recording(self):
        return None

    def stop_recording(self):
        self.stop_recording_called = True
        return b""

    def snapshot_recording(self):
        return b""

    def set_wake_word_enabled(self, enabled: bool):
        return None


class _DummyFuture:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


class _DummyTask:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


class RequestHardeningTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(cm, "AmbientListener", _DummyListener),
            mock.patch.object(cm.threading, "Thread", _DummyThread),
            mock.patch.object(cm.skills_pkg, "load_all", lambda: None),
        ]
        for patcher in patches:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(patches)])
        self.manager = cm.CompanionManager()
        self.manager._tts = mock.Mock()
        self.manager._tts.stop = mock.Mock()

    def test_parse_points_deduplicates_tags_for_active_session(self):
        session = cm.RequestSession(request_id=1, origin="test", started_at=0.0)
        self.manager._active_session = session
        seen = []
        self.manager._show_detected_point = lambda _session, x, y, label: seen.append((x, y, label))

        self.manager._parse_points(session, "[POINT:10,20:button:screen1]")
        self.manager._parse_points(session, "[POINT:10,20:button:screen1]")

        self.assertEqual(seen, [(10.0, 20.0, "button")])

    def test_parse_points_rejects_stale_session(self):
        session = cm.RequestSession(request_id=2, origin="test", started_at=0.0)
        self.manager._active_session = None

        with self.assertRaises(cm.SessionCancelled):
            self.manager._parse_points(session, "[POINT:10,20:button:screen1]")

    def test_finish_request_session_cancels_owned_tasks_and_clears_state(self):
        session = cm.RequestSession(request_id=3, origin="test", started_at=0.0)
        session.search_task = _DummyTask()
        session.locate_task = _DummyTask()
        session.tts_task = _DummyTask()
        self.manager._active_session = session
        self.manager._current_task = _DummyFuture()

        self.manager._finish_request_session(session, final_phase="completed")

        self.assertTrue(session.cleanup_done)
        self.assertIsNone(self.manager._active_session)
        self.assertIsNone(self.manager._current_task)
        self.assertTrue(session.search_task.cancelled)
        self.assertTrue(session.locate_task.cancelled)
        self.assertTrue(session.tts_task.cancelled)

    def test_stop_cancels_active_request_session(self):
        session = cm.RequestSession(request_id=4, origin="test", started_at=0.0)
        self.manager._active_session = session
        self.manager._state = cm.AppState.LISTENING
        self.manager._current_task = _DummyFuture()
        self.manager._stop_live_transcription = mock.Mock()

        self.manager.stop()

        self.assertTrue(session.cancelled)
        self.assertTrue(session.cleanup_done)
        self.assertTrue(self.manager._current_task is None or self.manager._current_task.cancelled)
        self.assertTrue(self.manager._listener.stop_recording_called)
        self.manager._stop_live_transcription.assert_called_once()

    def test_history_for_request_drops_app_memory_for_screenshot_turns(self):
        self.manager._app_memory["demo"] = [cm.Message(role="user", content="old context")]

        history = self.manager._history_for_request("demo", ["fresh-image"])

        self.assertEqual(history, [])
        self.assertEqual(len(self.manager._app_memory["demo"]), 1)

    def test_capture_request_screenshot_clears_stale_session_image(self):
        session = cm.RequestSession(request_id=5, origin="test", started_at=0.0, screenshot="stale")
        self.manager._active_session = session
        fresh = mock.Mock(base64_jpeg="new-b64")

        with mock.patch.object(cm, "capture_primary", return_value=fresh):
            screenshots, images_b64 = self.manager._capture_request_screenshot(session)

        self.assertEqual(screenshots, [fresh])
        self.assertEqual(images_b64, ["new-b64"])
        self.assertIs(session.screenshot, fresh)


if __name__ == "__main__":
    unittest.main()
