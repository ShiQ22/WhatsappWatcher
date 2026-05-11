"""
Tests for Issue 2: RecorderHelper restart + retry on capture_exception / start_timeout.

Run with:  python -m pytest tests/test_recorder_restart.py -v

These tests mock _HelperIpcBackend so no real RecorderHelper.exe is needed.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Fake helper — extended for restart/retry scenarios
# ---------------------------------------------------------------------------

class _FakeHelper:
    """
    Simulates _HelperIpcBackend with controllable per-scenario behaviour.

    Scenarios
    ---------
    normal                      start_session → True immediately
    already_recording_then_idle first call → False/already_recording;
                                after release_stopped() → True
    always_fail_already_rec     always False/already_recording (never stops)
    device_error                always False/device_enum_failed
    capture_exception           always False/capture_exception
    start_timeout               always False/start_timeout
    """

    def __init__(self, scenario: str = "normal"):
        self._scenario = scenario
        self._call_count = 0
        self._stopped_event = threading.Event()
        self._is_recording_flag = False
        self._last_start_error_code: str | None = None

    # ── interface expected by _start_recording_helper ────────────────────

    def start_session(self, output_dir: str, base_name: str) -> bool:
        self._call_count += 1
        if self._scenario == "normal":
            self._is_recording_flag = True
            self._last_start_error_code = None
            return True
        elif self._scenario == "already_recording_then_idle":
            if self._call_count == 1:
                self._last_start_error_code = "already_recording"
                return False
            else:
                self._is_recording_flag = True
                self._last_start_error_code = None
                return True
        elif self._scenario == "always_fail_already_rec":
            self._last_start_error_code = "already_recording"
            return False
        elif self._scenario == "device_error":
            self._last_start_error_code = "device_enum_failed"
            return False
        elif self._scenario == "capture_exception":
            self._last_start_error_code = "capture_exception"
            return False
        elif self._scenario == "start_timeout":
            self._last_start_error_code = "start_timeout"
            return False
        return False

    def wait_for_stopped(self, timeout: float) -> bool:
        return self._stopped_event.wait(timeout)

    def shutdown(self) -> None:
        pass

    def force_stop_session(self) -> None:
        pass

    def stop_session(self) -> None:
        pass

    @property
    def current_path(self) -> str | None:
        return None

    def is_process_alive(self) -> bool:
        return True

    def get_last_stderr(self, n: int = 5):
        return []

    def release_stopped(self, delay: float = 0.1) -> None:
        """Simulate the helper becoming idle after a short delay."""
        def _release():
            time.sleep(delay)
            self._is_recording_flag = False
            self._stopped_event.set()
        threading.Thread(target=_release, daemon=True).start()


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestRecorderHelperRestart(unittest.TestCase):
    """Tests for _start_recording_helper() restart+retry on capture_exception/start_timeout."""

    def _build_recorder(self, helper: _FakeHelper):
        """
        Build a Recorder instance with the helper backend patched.
        Avoids importing pyaudio, COM, and launching any subprocess.
        """
        patcher_mods = {
            "pyaudiowpatch": MagicMock(),
            "comtypes": MagicMock(),
            "comtypes.client": MagicMock(),
        }
        with patch.dict("sys.modules", patcher_mods):
            from recorder import Recorder  # noqa: F401 (import side-effect only)

        with (
            patch("recorder.RECORDER_BACKEND", "helper"),
            patch("recorder.RECORDER_HELPER_PATH", "fake.exe"),
            patch("recorder.RECORDER_HELPER_STARTUP_TIMEOUT", 1.0),
            patch("recorder.RECORDER_HELPER_STOP_TIMEOUT", 5.0),
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_MIC_GAIN", 1.0),
            patch("recorder.RECORDER_LOOPBACK_GAIN", 1.0),
            patch("recorder.RECORDER_HELPER_KEEP_TEMP", False),
            patch("recorder._HelperIpcBackend", return_value=helper),
            patch("recorder.faulthandler"),
        ):
            rec = Recorder.__new__(Recorder)
            import threading as _t
            rec._lock = _t.RLock()
            rec._transition_lock = _t.Lock()
            rec._helper = helper
            rec._engine = None
            rec._device_manager = None
            rec._is_recording = False
            rec._started_at = None
            rec._segment_counter = 1
            rec._active_context = None
            rec._completed_contexts = []
            rec._recording_success = False
            rec._recording_issues = None
            rec._restart_count = 0
            rec._last_error_code = None
            rec._last_mute_check = 0.0
            rec._device_index = None
            rec._device_name_for_mute = ""
            rec._ar_fast_suppressed = 0
            rec._ar_fast_last_log_t = 0.0
            from pathlib import Path
            rec.bandicam_path = Path("/fake/config.py")
            rec.bandicam_output_dir = Path("/tmp")
            return rec

    # ── Test 1: capture_exception → restart → retry succeeds ────────────

    def test_capture_exception_triggers_restart_and_retry_succeeds(self):
        """
        Production scenario 458005:
        capture_exception during start → helper restart → retry start → success.
        """
        helper1 = _FakeHelper(scenario="capture_exception")
        helper2 = _FakeHelper(scenario="normal")
        rec = self._build_recorder(helper1)

        with (
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_FORMAT", "mp3"),
            patch("recorder._HelperIpcBackend", return_value=helper2) as mock_backend,
        ):
            result = rec._start_recording_helper()

        self.assertTrue(result, "Should succeed after restart + retry")
        self.assertTrue(rec._is_recording)
        self.assertIs(rec._helper, helper2, "Helper should be the restarted instance")
        mock_backend.assert_called_once()
        self.assertEqual(helper1._call_count, 1, "Original helper called once (failed)")
        self.assertEqual(helper2._call_count, 1, "Retry helper called once (succeeded)")

    # ── Test 2: start_timeout → restart → retry succeeds ────────────────

    def test_start_timeout_triggers_restart_and_retry_succeeds(self):
        """
        start_session waits 10s with no response → start_timeout tagged →
        helper restart → retry → success.
        """
        helper1 = _FakeHelper(scenario="start_timeout")
        helper2 = _FakeHelper(scenario="normal")
        rec = self._build_recorder(helper1)

        with (
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_FORMAT", "mp3"),
            patch("recorder._HelperIpcBackend", return_value=helper2) as mock_backend,
        ):
            result = rec._start_recording_helper()

        self.assertTrue(result, "Should succeed after restart + retry")
        self.assertTrue(rec._is_recording)
        self.assertIs(rec._helper, helper2)
        mock_backend.assert_called_once()

    # ── Test 3: capture_exception → restart OK → retry also fails ───────

    def test_capture_exception_retry_also_fails(self):
        helper1 = _FakeHelper(scenario="capture_exception")
        helper2 = _FakeHelper(scenario="capture_exception")  # retry fails too
        rec = self._build_recorder(helper1)

        with (
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_FORMAT", "mp3"),
            patch("recorder._HelperIpcBackend", return_value=helper2),
        ):
            result = rec._start_recording_helper()

        self.assertFalse(result, "Should return False when retry also fails")
        self.assertFalse(rec._is_recording)
        self.assertIs(rec._helper, helper2, "Helper was restarted even though retry failed")

    # ── Test 4: helper restart itself fails → returns False, no crash ────

    def test_helper_restart_fails_returns_false_no_crash(self):
        helper1 = _FakeHelper(scenario="capture_exception")
        rec = self._build_recorder(helper1)

        with (
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_FORMAT", "mp3"),
            patch("recorder._HelperIpcBackend",
                  side_effect=TimeoutError("[RH-001] RecorderHelper did not emit 'ready'")),
        ):
            result = rec._start_recording_helper()

        self.assertFalse(result, "Should return False cleanly when restart fails")
        self.assertFalse(rec._is_recording)
        # No unhandled exception — test reaching here proves it

    # ── Test 5: normal start still passes (regression) ───────────────────

    def test_normal_start_still_passes(self):
        helper = _FakeHelper(scenario="normal")
        rec = self._build_recorder(helper)

        with (
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_FORMAT", "mp3"),
        ):
            result = rec._start_recording_helper()

        self.assertTrue(result, "Normal start should succeed")
        self.assertTrue(rec._is_recording)
        self.assertEqual(helper._call_count, 1, "Exactly one start_session call")

    # ── Test 6: already_recording recovery still passes (regression) ─────

    def test_already_recording_recovery_still_passes(self):
        """
        already_recording path must be unaffected by the restructuring.
        Old recording active → stop → retry succeeds.
        """
        helper = _FakeHelper(scenario="already_recording_then_idle")
        helper._is_recording_flag = True
        helper.release_stopped(delay=0.2)
        rec = self._build_recorder(helper)

        with (
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_FORMAT", "mp3"),
        ):
            t0 = time.monotonic()
            result = rec._start_recording_helper()
            elapsed = time.monotonic() - t0

        self.assertTrue(result, "already_recording recovery should succeed")
        self.assertTrue(rec._is_recording)
        self.assertEqual(helper._call_count, 2, "Initial call + one retry")
        self.assertGreater(elapsed, 0.1, "Should have waited for helper to stop")

    # ── Test 7: non-restart error (device_enum_failed) not retried ────────

    def test_device_error_does_not_trigger_restart(self):
        """
        device_enum_failed must still fail immediately — no restart, no retry.
        """
        helper = _FakeHelper(scenario="device_error")
        rec = self._build_recorder(helper)

        with (
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_FORMAT", "mp3"),
            patch("recorder._HelperIpcBackend") as mock_backend,
        ):
            t0 = time.monotonic()
            result = rec._start_recording_helper()
            elapsed = time.monotonic() - t0

        self.assertFalse(result)
        self.assertFalse(rec._is_recording)
        self.assertEqual(helper._call_count, 1, "Only one attempt for non-recoverable error")
        self.assertLess(elapsed, 1.0, "Should fail quickly without restart/wait")
        mock_backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
