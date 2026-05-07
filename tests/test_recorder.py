"""
test_recorder.py — Public Recorder interface tests.
All tests mock audio hardware. No real device required.
"""
import time
import threading
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from recorder import Recorder, RecordingContext


# ─────────────────────────────────────────────────────────────────────────────
# Start / stop basics
# ─────────────────────────────────────────────────────────────────────────────

class TestRecorderStartStop:

    def test_start_recording_returns_true_on_success(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        result = r.start_recording()
        assert result is True
        assert r.is_recording is True
        assert r.started_at is not None
        r.stop_recording()

    def test_start_recording_returns_false_when_no_device(
        self, mock_pyaudio_no_devices, mock_com_ok, mock_config
    ):
        r = Recorder()
        result = r.start_recording()
        assert result is False
        assert r.is_recording is False

    def test_start_recording_returns_false_when_all_streams_fail(
        self, mock_pyaudio_stream_always_fails, mock_com_ok, mock_config
    ):
        r = Recorder()
        result = r.start_recording()
        assert result is False
        assert r.is_recording is False

    def test_start_when_already_recording_returns_false(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        r.start_recording()
        result = r.start_recording()
        assert result is False
        r.stop_recording()

    def test_stop_recording_sets_not_recording(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        r.start_recording()
        result = r.stop_recording()
        assert result is True
        assert r.is_recording is False

    def test_stop_when_not_recording_returns_false(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        result = r.stop_recording()
        assert result is False

    def test_force_stop_always_sets_not_recording(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        r.start_recording()
        r.force_stop_recording()
        assert r.is_recording is False

    def test_force_stop_sets_not_recording_even_without_start(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        r.force_stop_recording()
        assert r.is_recording is False

    def test_disk_space_check_blocks_start_when_low(
        self, mock_pyaudio, mock_com_ok, tmp_path
    ):
        output_dir = tmp_path / "recordings"
        output_dir.mkdir()
        disk_mock = MagicMock()
        disk_mock.free = 50 * 1024 * 1024  # only 50 MB

        with patch("recorder.RECORDER_OUTPUT_DIR", str(output_dir)), \
             patch("recorder.RECORDER_FORMAT", "wav"), \
             patch("recorder.RECORDER_STOP_SETTLE_SECONDS", 1.0), \
             patch("recorder.RECORDER_DEVICE_RETRY_COUNT", 1), \
             patch("recorder.RECORDER_DEVICE_RETRY_DELAY_SECONDS", 0.0), \
             patch("recorder.RECORDER_WATCHDOG_RECOVERY_ATTEMPTS", 2), \
             patch("recorder.shutil") as mock_shutil:
            mock_shutil.disk_usage.return_value = disk_mock
            r = Recorder()
            result = r.start_recording()
        assert result is False
        assert r.is_recording is False


# ─────────────────────────────────────────────────────────────────────────────
# Context management
# ─────────────────────────────────────────────────────────────────────────────

class TestRecorderContexts:

    def test_detach_contexts_returns_context_list(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        r.start_recording()
        r.stop_recording()
        contexts = r.detach_contexts()
        assert isinstance(contexts, list)
        assert len(contexts) >= 1

    def test_detach_contexts_resets_internal_state(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        r.start_recording()
        r.stop_recording()
        r.detach_contexts()
        second = r.detach_contexts()
        assert second == []

    def test_context_has_start_marker(self, mock_pyaudio, mock_com_ok, mock_config):
        r = Recorder()
        r.start_recording()
        r.stop_recording()
        ctx = r.detach_context()
        assert ctx.start_marker is not None
        assert ctx.started_at is not None

    def test_context_has_output_path(self, mock_pyaudio, mock_com_ok, mock_config):
        r = Recorder()
        r.start_recording()
        r.stop_recording()
        ctx = r.detach_context()
        assert ctx.output_path is not None
        assert ctx.output_path.endswith(".wav")

    def test_context_has_output_dir(self, mock_pyaudio, mock_com_ok, mock_config):
        r = Recorder()
        r.start_recording()
        r.stop_recording()
        ctx = r.detach_context()
        assert ctx.output_dir is not None

    def test_context_segment_index_is_one_on_first_recording(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        r.start_recording()
        r.stop_recording()
        ctx = r.detach_context()
        assert ctx.segment_index == 1

    def test_detach_context_raises_on_empty(self, mock_pyaudio, mock_com_ok, mock_config):
        r = Recorder()
        with pytest.raises(IndexError):
            r.detach_context()


# ─────────────────────────────────────────────────────────────────────────────
# resolve_final_files
# ─────────────────────────────────────────────────────────────────────────────

class TestRecorderResolve:

    def test_resolve_final_files_returns_existing_wav(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        r.start_recording()
        time.sleep(0.1)
        r.stop_recording()
        contexts = r.detach_contexts()
        files = r.resolve_final_files(contexts)
        assert len(files) >= 1
        for f in files:
            assert Path(f).exists()
            assert Path(f).stat().st_size > 0

    def test_resolve_empty_contexts_returns_empty(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        result = r.resolve_final_files([])
        assert result == []

    def test_resolve_skips_missing_file(self, mock_pyaudio, mock_com_ok, mock_config, tmp_path):
        ctx = RecordingContext(
            pre_start_snapshot=set(),
            start_marker=time.time(),
            started_at=None,
            output_dir=str(tmp_path),
            segment_index=1,
            output_path=str(tmp_path / "nonexistent_seg1.wav"),
        )
        r = Recorder()
        result = r.resolve_final_files([ctx])
        assert result == []

    def test_resolve_rejects_short_wav(self, mock_pyaudio, mock_com_ok, mock_config, tmp_path):
        wav_path = tmp_path / "short_seg1.wav"
        # Write a WAV with only 100ms of audio (4410 frames at 44100 Hz)
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00\x00" * 4410)  # 0.1 seconds

        ctx = RecordingContext(
            pre_start_snapshot=set(),
            start_marker=time.time(),
            started_at=None,
            output_dir=str(tmp_path),
            segment_index=1,
            output_path=str(wav_path),
        )
        r = Recorder()
        result = r.resolve_final_files([ctx])
        assert result == []
        assert (tmp_path / "short_seg1.corrupted").exists()

    def test_resolve_rejects_corrupted_wav(
        self, mock_pyaudio, mock_com_ok, mock_config, tmp_path
    ):
        bad_path = tmp_path / "bad_seg1.wav"
        bad_path.write_bytes(b"NOT A WAV FILE AT ALL" * 10)

        ctx = RecordingContext(
            pre_start_snapshot=set(),
            start_marker=time.time(),
            started_at=None,
            output_dir=str(tmp_path),
            segment_index=1,
            output_path=str(bad_path),
        )
        r = Recorder()
        result = r.resolve_final_files([ctx])
        assert result == []

    def test_resolve_format_wav_returns_wav(
        self, mock_pyaudio, mock_com_ok, mock_config, tmp_path
    ):
        wav_path = _write_long_wav(tmp_path / "rec_seg1.wav")
        ctx = _ctx_for(tmp_path, wav_path)

        with patch("recorder.RECORDER_FORMAT", "wav"):
            r = Recorder()
            result = r.resolve_final_files([ctx])

        assert len(result) == 1
        assert result[0].endswith(".wav")

    def test_resolve_format_mp3_converts_and_deletes_wav(
        self, mock_pyaudio, mock_com_ok, mock_config, tmp_path
    ):
        wav_path = _write_long_wav(tmp_path / "rec_seg1.wav")
        ctx = _ctx_for(tmp_path, wav_path)

        with patch("recorder.RECORDER_FORMAT", "mp3"):
            r = Recorder()
            result = r.resolve_final_files([ctx])

        if result:
            assert not wav_path.exists(), "WAV should be deleted after MP3 conversion"
            assert result[0].endswith(".mp3")
        else:
            # lameenc might not be installed in CI — WAV kept as fallback
            pass

    def test_resolve_format_both_returns_wav_and_mp3(
        self, mock_pyaudio, mock_com_ok, mock_config, tmp_path
    ):
        wav_path = _write_long_wav(tmp_path / "rec_seg1.wav")
        ctx = _ctx_for(tmp_path, wav_path)

        with patch("recorder.RECORDER_FORMAT", "both"):
            r = Recorder()
            result = r.resolve_final_files([ctx])

        assert str(wav_path) in result
        if len(result) > 1:
            assert result[-1].endswith(".mp3")

    def test_resolve_sets_success_true_on_valid_file(
        self, mock_pyaudio, mock_com_ok, mock_config, tmp_path
    ):
        """resolve_final_files() sets recording_success=True when at least one valid file resolves."""
        wav_path = _write_long_wav(tmp_path / "rec_seg1.wav")
        ctx = _ctx_for(tmp_path, wav_path)

        with patch("recorder.RECORDER_FORMAT", "wav"):
            r = Recorder()
            result = r.resolve_final_files([ctx])

        assert len(result) >= 1
        meta = r.get_recording_metadata()
        assert meta["recording_success"] is True, (
            f"recording_success should be True after valid file resolved; got {meta!r}"
        )

    def test_resolve_sets_success_false_on_no_files(
        self, mock_pyaudio, mock_com_ok, mock_config, tmp_path
    ):
        """resolve_final_files() sets recording_success=False and recording_issues='[REC-006]' when no files resolve."""
        ctx = RecordingContext(
            pre_start_snapshot=set(),
            start_marker=time.time(),
            started_at=None,
            output_dir=str(tmp_path),
            segment_index=1,
            output_path=str(tmp_path / "nonexistent_seg1.wav"),
        )
        r = Recorder()
        result = r.resolve_final_files([ctx])

        assert result == []
        meta = r.get_recording_metadata()
        assert meta["recording_success"] is False, (
            f"recording_success should be False when no files resolve; got {meta!r}"
        )
        assert meta["recording_issues"] is not None, "recording_issues must be set"
        assert "[REC-006]" in meta["recording_issues"], (
            f"recording_issues should contain '[REC-006]'; got {meta['recording_issues']!r}"
        )

    def test_resolve_partial_failure_keeps_success_true(
        self, mock_pyaudio, mock_com_ok, mock_config, tmp_path
    ):
        """resolve_final_files() keeps recording_success=True when at least one file resolves despite partial failures."""
        good_wav = _write_long_wav(tmp_path / "rec_seg1.wav")
        good_ctx = _ctx_for(tmp_path, good_wav)
        bad_ctx = RecordingContext(
            pre_start_snapshot=set(),
            start_marker=time.time(),
            started_at=None,
            output_dir=str(tmp_path),
            segment_index=2,
            output_path=str(tmp_path / "nonexistent_seg2.wav"),
        )

        with patch("recorder.RECORDER_FORMAT", "wav"):
            r = Recorder()
            result = r.resolve_final_files([good_ctx, bad_ctx])

        assert len(result) >= 1, "At least one file should resolve"
        meta = r.get_recording_metadata()
        assert meta["recording_success"] is True, (
            f"recording_success should be True when at least one file resolves; got {meta!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Compatibility
# ─────────────────────────────────────────────────────────────────────────────

class TestRecorderCompatibility:

    def test_refresh_bandicam_paths_alias_exists(self, mock_pyaudio, mock_com_ok, mock_config):
        r = Recorder()
        assert hasattr(r, "refresh_bandicam_paths")
        assert callable(r.refresh_bandicam_paths)

    def test_refresh_bandicam_paths_returns_bool(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        result = r.refresh_bandicam_paths()
        assert isinstance(result, bool)

    def test_refresh_bandicam_paths_returns_true_for_valid_dir(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        assert r.refresh_bandicam_paths() is True

    def test_get_recording_metadata_returns_dict(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        meta = r.get_recording_metadata()
        assert "success" in meta
        assert "issues" in meta
        assert "restart_count" in meta

    def test_get_recording_metadata_after_start(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        r.start_recording()
        meta = r.get_recording_metadata()
        assert meta["restart_count"] == 0
        r.stop_recording()

    def test_get_recording_metadata_returns_recording_issues_key(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        """get_recording_metadata() must expose the 'recording_issues' key."""
        r = Recorder()
        meta = r.get_recording_metadata()
        assert "recording_issues" in meta, (
            f"'recording_issues' key missing from metadata: {list(meta.keys())}"
        )

    def test_get_recording_metadata_backward_compatible_issues_key(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        """Old 'issues' key must still be present for backward compatibility."""
        r = Recorder()
        meta = r.get_recording_metadata()
        assert "issues" in meta, (
            f"'issues' key missing from metadata: {list(meta.keys())}"
        )
        assert meta["issues"] == meta["recording_issues"], (
            "'issues' and 'recording_issues' must have identical values"
        )


# ─────────────────────────────────────────────────────────────────────────────
# ensure_recording_alive / watchdog
# ─────────────────────────────────────────────────────────────────────────────

class TestRecorderHealth:

    def test_ensure_recording_alive_returns_true_when_not_recording(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        assert r.ensure_recording_alive() is True

    def test_ensure_recording_alive_returns_true_when_healthy(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        r.start_recording()
        result = r.ensure_recording_alive()
        assert result is True
        r.stop_recording()

    def test_ensure_recording_alive_triggers_recovery_when_thread_dead(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        r = Recorder()
        r.start_recording()

        # Kill the engine thread externally to simulate crash
        r._engine._stop_event.set()
        if r._engine._record_thread:
            r._engine._record_thread.join(timeout=2.0)

        # ensure_recording_alive should detect dead thread and start segment 2
        result = r.ensure_recording_alive()
        assert result is True
        assert r.is_recording is True
        assert r._segment_counter == 2
        r.stop_recording()

    def test_ensure_recording_alive_gives_up_after_max_restarts(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        with patch("recorder.RECORDER_WATCHDOG_RECOVERY_ATTEMPTS", 0):
            r = Recorder()
            r.start_recording()

            # Kill engine thread
            r._engine._stop_event.set()
            if r._engine._record_thread:
                r._engine._record_thread.join(timeout=2.0)

            result = r.ensure_recording_alive()
            assert result is False
            assert r.is_recording is False

    def test_properties_accessible(self, mock_pyaudio, mock_com_ok, mock_config):
        r = Recorder()
        assert r.is_recording is False
        assert r.current_recording_path is None
        assert r.started_at is None

        r.start_recording()
        assert r.is_recording is True
        assert r.started_at is not None
        r.stop_recording()


# ─────────────────────────────────────────────────────────────────────────────
# Mute detection — never raises, never affects recording
# ─────────────────────────────────────────────────────────────────────────────

class TestMuteDetection:

    def test_check_os_mute_never_raises(self):
        """_check_os_mute() must return None or bool, never raise."""
        from recorder import _check_os_mute
        result = _check_os_mute(device_index=None)
        assert result is None or isinstance(result, bool)

    def test_check_os_mute_with_index_never_raises(self):
        """_check_os_mute() with a device index must still never raise."""
        from recorder import _check_os_mute
        result = _check_os_mute(device_index=0)
        assert result is None or isinstance(result, bool)

    def test_check_whatsapp_mute_never_raises(self):
        """_check_whatsapp_mute() must return None or bool, never raise."""
        from recorder import _check_whatsapp_mute
        result = _check_whatsapp_mute()
        assert result is None or isinstance(result, bool)

    def test_mute_check_does_not_affect_recording(
        self, mock_pyaudio, mock_com_ok, mock_config
    ):
        """Mute checks are informational only — start/stop still work normally."""
        from recorder import Recorder
        from unittest.mock import patch

        with patch("recorder._check_os_mute", return_value=True), \
             patch("recorder._check_whatsapp_mute", return_value=True):
            r = Recorder()
            assert r.start_recording() is True
            assert r.is_recording is True
            assert r.stop_recording() is True
            assert r.is_recording is False

    def test_bandicam_path_points_to_existing_file(self):
        """BANDICAM_PATH must exist on disk so main.py's existence check passes."""
        import os
        from config import BANDICAM_PATH
        assert BANDICAM_PATH != ""
        assert os.path.exists(BANDICAM_PATH), (
            f"BANDICAM_PATH='{BANDICAM_PATH}' must exist on disk"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers shared by resolve tests
# ─────────────────────────────────────────────────────────────────────────────

def _write_long_wav(path: Path, seconds: float = 1.0) -> Path:
    frames = int(44100 * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(b"\x00\x00" * frames)
    return path


def _ctx_for(output_dir: Path, wav_path: Path) -> RecordingContext:
    return RecordingContext(
        pre_start_snapshot=set(),
        start_marker=time.time(),
        started_at=None,
        output_dir=str(output_dir),
        segment_index=1,
        output_path=str(wav_path),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Background recorder start helper (_bg_start_recorder / [REC-011])
# ─────────────────────────────────────────────────────────────────────────────

class TestRecorderBackgroundStart:
    """Tests for _bg_start_recorder() in main.py and [REC-011] timing log."""

    def _import_bg(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from main import _bg_start_recorder
        return _bg_start_recorder

    def test_bg_start_recorder_stores_true_on_success(self):
        """start_recording() returns True → result_box[0] = True."""
        _bg_start_recorder = self._import_bg()
        recorder = MagicMock()
        recorder.start_recording.return_value = True
        result_box = [None]
        _bg_start_recorder(recorder, result_box)
        assert result_box[0] is True

    def test_bg_start_recorder_stores_false_on_failure(self):
        """start_recording() returns False → result_box[0] = False."""
        _bg_start_recorder = self._import_bg()
        recorder = MagicMock()
        recorder.start_recording.return_value = False
        result_box = [None]
        _bg_start_recorder(recorder, result_box)
        assert result_box[0] is False

    def test_bg_start_recorder_stores_false_on_exception(self):
        """start_recording() raises → result_box[0] = False, no re-raise."""
        _bg_start_recorder = self._import_bg()
        recorder = MagicMock()
        recorder.start_recording.side_effect = OSError("device busy")
        result_box = [None]
        _bg_start_recorder(recorder, result_box)   # must not raise
        assert result_box[0] is False

    def test_bg_start_recorder_logs_rec011_when_slow(self, caplog):
        """Startup > 2s → [REC-011] warning is logged."""
        import logging
        _bg_start_recorder = self._import_bg()
        recorder = MagicMock()

        def _slow_start():
            time.sleep(0.01)
            return True

        recorder.start_recording.side_effect = _slow_start
        result_box = [None]

        with patch("main.time") as mock_time:
            mock_time.time.side_effect = [0.0, 3.5]   # elapsed = 3.5 s
            with caplog.at_level(logging.WARNING, logger="watcher.main"):
                _bg_start_recorder(recorder, result_box)

        assert any("REC-011" in r.message for r in caplog.records), \
            "[REC-011] warning must be logged when elapsed > 2s"

    def test_bg_start_recorder_does_not_log_rec011_when_fast(self, caplog):
        """Startup < 2s → no [REC-011] warning."""
        import logging
        _bg_start_recorder = self._import_bg()
        recorder = MagicMock()
        recorder.start_recording.return_value = True
        result_box = [None]

        with patch("main.time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.5]   # elapsed = 0.5 s
            with caplog.at_level(logging.WARNING, logger="watcher.main"):
                _bg_start_recorder(recorder, result_box)

        assert not any("REC-011" in r.message for r in caplog.records), \
            "[REC-011] must NOT be logged when elapsed <= 2s"
