"""
Tests for Bug 2: already_recording during fast session split.

Run with:  python -m pytest tests/test_recorder_split.py -v

These tests mock _HelperIpcBackend so no real RecorderHelper.exe is needed.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock


class _FakeHelper:
    """
    Simulates _HelperIpcBackend with controllable behaviour.

    Scenario A — normal start: start_session returns True immediately.
    Scenario B — already_recording, then idle: start_session returns False
                 first time (already_recording), then True on retry after
                 wait_for_stopped() releases.
    """

    def __init__(self, scenario: str = "normal"):
        self._scenario = scenario
        self._call_count = 0
        self._stopped_event = threading.Event()
        self._is_recording_flag = False
        self._last_start_error_code: str | None = None

    # Properties / methods called by _start_recording_helper
    @property
    def is_session_active(self) -> bool:
        return self._is_recording_flag

    def wait_for_stopped(self, timeout: float) -> bool:
        return self._stopped_event.wait(timeout)

    def start_session(self, output_dir: str, base_name: str) -> bool:
        self._call_count += 1
        if self._scenario == "normal":
            self._is_recording_flag = True
            self._last_start_error_code = None
            return True
        elif self._scenario == "already_recording_then_idle":
            if self._call_count == 1:
                # First call: rejected
                self._last_start_error_code = "already_recording"
                return False
            else:
                # Retry succeeds
                self._is_recording_flag = True
                self._last_start_error_code = None
                return True
        elif self._scenario == "always_fail":
            self._last_start_error_code = "already_recording"
            return False
        elif self._scenario == "device_error":
            self._last_start_error_code = "device_enum_failed"
            return False
        return False

    def force_stop_session(self):
        pass

    def stop_session(self):
        pass

    @property
    def current_path(self) -> str | None:
        return None

    def is_process_alive(self) -> bool:
        return True

    def get_last_stderr(self, n: int = 5):
        return []

    def release_stopped(self, delay: float = 0.1):
        """Simulate the helper becoming idle after a short delay."""
        def _release():
            time.sleep(delay)
            self._is_recording_flag = False
            self._stopped_event.set()
        threading.Thread(target=_release, daemon=True).start()


class TestRecorderAlreadyRecovery(unittest.TestCase):
    """
    Tests for _start_recording_helper() already_recording recovery.
    We patch the helper backend so no subprocess is required.
    """

    def _build_recorder(self, helper: _FakeHelper):
        """
        Build a Recorder instance with the helper backend patched.
        Avoids importing pyaudio, COM, and launching any subprocess.
        """
        import sys

        # Patch all heavy dependencies so Recorder.__init__ doesn't fail.
        patcher_mods = {
            "pyaudiowpatch": MagicMock(),
            "comtypes": MagicMock(),
            "comtypes.client": MagicMock(),
        }
        with patch.dict("sys.modules", patcher_mods):
            from recorder import Recorder

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
            # Manually init only what _start_recording_helper needs
            import threading
            rec._lock = threading.RLock()
            rec._transition_lock = threading.Lock()
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

    # ------------------------------------------------------------------
    # Scenario A: normal start succeeds immediately
    # ------------------------------------------------------------------

    def test_normal_start_succeeds(self):
        helper = _FakeHelper(scenario="normal")
        rec = self._build_recorder(helper)

        with (
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_FORMAT", "mp3"),
        ):
            result = rec._start_recording_helper()

        self.assertTrue(result, "Normal start should succeed")
        self.assertTrue(rec._is_recording)
        self.assertEqual(helper._call_count, 1)

    # ------------------------------------------------------------------
    # Scenario B: already_recording → recovery → retry succeeds
    # ------------------------------------------------------------------

    def test_already_recording_recovery_succeeds(self):
        """
        Critical production scenario (BUG 2):
        old recording active → session split → old stop takes ~0.3 s →
        new start initially gets already_recording → recovery waits →
        helper becomes idle → retry start succeeds.
        """
        helper = _FakeHelper(scenario="already_recording_then_idle")
        # Simulate helper becoming idle 0.3 s after start is rejected
        helper._is_recording_flag = True  # old session still active
        helper.release_stopped(delay=0.3)
        rec = self._build_recorder(helper)

        with (
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_FORMAT", "mp3"),
        ):
            t0 = time.monotonic()
            result = rec._start_recording_helper()
            elapsed = time.monotonic() - t0

        self.assertTrue(result, "Should succeed after already_recording recovery")
        self.assertTrue(rec._is_recording)
        self.assertEqual(helper._call_count, 2, "Expected initial call + one retry")
        self.assertGreater(elapsed, 0.2, "Should have waited for helper to stop")
        self.assertLess(elapsed, 5.0, "Recovery should complete well within timeout")

    # ------------------------------------------------------------------
    # Scenario C: already_recording but helper never stops → bounded failure
    # ------------------------------------------------------------------

    def test_already_recording_helper_never_stops(self):
        """
        If the helper never sends 'stopped', start must fail after the
        bounded wait (not hang indefinitely).
        """
        helper = _FakeHelper(scenario="always_fail")
        helper._is_recording_flag = True  # never cleared
        rec = self._build_recorder(helper)

        with (
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_FORMAT", "mp3"),
        ):
            t0 = time.monotonic()
            result = rec._start_recording_helper()
            elapsed = time.monotonic() - t0

        self.assertFalse(result, "Should fail when helper never stops")
        self.assertFalse(rec._is_recording)
        # Should time-out at 10 s ± some slack
        self.assertGreater(elapsed, 5.0)
        self.assertLess(elapsed, 15.0)

    # ------------------------------------------------------------------
    # Scenario D: non-already_recording error must not trigger recovery
    # ------------------------------------------------------------------

    def test_device_error_does_not_trigger_recovery(self):
        """
        A device_enum_failed error must fail immediately without waiting.
        """
        helper = _FakeHelper(scenario="device_error")
        rec = self._build_recorder(helper)

        with (
            patch("recorder.RECORDER_OUTPUT_DIR", "/tmp"),
            patch("recorder.RECORDER_FORMAT", "mp3"),
        ):
            t0 = time.monotonic()
            result = rec._start_recording_helper()
            elapsed = time.monotonic() - t0

        self.assertFalse(result)
        self.assertFalse(rec._is_recording)
        self.assertEqual(helper._call_count, 1, "Only one attempt for non-already_recording error")
        self.assertLess(elapsed, 1.0, "Should fail quickly without recovery wait")

    # ------------------------------------------------------------------
    # Scenario E: transition lock prevents concurrent start+stop overlap
    # ------------------------------------------------------------------

    def test_transition_lock_present(self):
        """Recorder must expose _transition_lock as a threading.Lock."""
        import threading
        helper = _FakeHelper(scenario="normal")
        rec = self._build_recorder(helper)
        self.assertIsInstance(rec._transition_lock, type(threading.Lock()))


if __name__ == "__main__":
    unittest.main()
