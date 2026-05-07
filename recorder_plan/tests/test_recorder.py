"""
test_recorder.py — Public Recorder interface tests.
All tests mock audio hardware. No real device required.
"""
import pytest
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

# Will be importable after recorder.py is written
# from recorder import Recorder, RecordingContext


class TestRecorderStartStop:

    def test_start_recording_returns_true_on_success(self, mock_pyaudio, mock_com_ok, mock_config, tmp_output_dir):
        """start_recording() returns True when device found and stream opened."""
        from recorder import Recorder
        r = Recorder()
        result = r.start_recording()
        assert result is True
        assert r.is_recording is True
        assert r.started_at is not None

    def test_start_recording_returns_false_when_no_device(self, mock_pyaudio_no_devices, mock_com_ok, mock_config):
        """start_recording() returns False when no input devices exist."""
        from recorder import Recorder
        r = Recorder()
        result = r.start_recording()
        assert result is False
        assert r.is_recording is False

    def test_start_recording_returns_false_when_all_streams_fail(self, mock_pyaudio_stream_always_fails, mock_com_ok, mock_config):
        """start_recording() returns False when all stream.open() calls fail."""
        from recorder import Recorder
        r = Recorder()
        result = r.start_recording()
        assert result is False
        assert r.is_recording is False

    def test_start_when_already_recording_returns_false(self, mock_pyaudio, mock_com_ok, mock_config):
        """Calling start_recording() twice without stop returns False."""
        from recorder import Recorder
        r = Recorder()
        r.start_recording()
        result = r.start_recording()
        assert result is False

    def test_stop_recording_sets_not_recording(self, mock_pyaudio, mock_com_ok, mock_config):
        """stop_recording() sets is_recording to False."""
        from recorder import Recorder
        r = Recorder()
        r.start_recording()
        result = r.stop_recording()
        assert result is True
        assert r.is_recording is False

    def test_stop_when_not_recording_returns_false(self, mock_config):
        """stop_recording() when not recording returns False."""
        from recorder import Recorder
        r = Recorder()
        result = r.stop_recording()
        assert result is False

    def test_force_stop_always_sets_not_recording(self, mock_pyaudio, mock_com_ok, mock_config):
        """force_stop_recording() must set is_recording=False even if stop fails."""
        from recorder import Recorder
        r = Recorder()
        r.start_recording()
        result = r.force_stop_recording()
        assert r.is_recording is False


class TestRecorderContexts:

    def test_detach_contexts_returns_context_list(self, mock_pyaudio, mock_com_ok, mock_config):
        """detach_contexts() returns list with at least one context after recording."""
        from recorder import Recorder
        r = Recorder()
        r.start_recording()
        r.stop_recording()
        contexts = r.detach_contexts()
        assert isinstance(contexts, list)
        assert len(contexts) >= 1

    def test_detach_contexts_resets_internal_state(self, mock_pyaudio, mock_com_ok, mock_config):
        """Calling detach_contexts() twice returns empty list on second call."""
        from recorder import Recorder
        r = Recorder()
        r.start_recording()
        r.stop_recording()
        r.detach_contexts()
        second = r.detach_contexts()
        assert second == []

    def test_context_has_start_marker(self, mock_pyaudio, mock_com_ok, mock_config):
        """RecordingContext has non-None start_marker after recording."""
        from recorder import Recorder
        r = Recorder()
        r.start_recording()
        r.stop_recording()
        ctx = r.detach_context()
        assert ctx.start_marker is not None
        assert ctx.started_at is not None


class TestRecorderResolve:

    def test_resolve_final_files_returns_existing_wav(self, mock_pyaudio, mock_com_ok, mock_config, tmp_output_dir):
        """resolve_final_files() returns path of WAV file created during recording."""
        from recorder import Recorder
        r = Recorder()
        r.start_recording()
        time.sleep(0.1)  # let a few chunks be written
        r.stop_recording()
        contexts = r.detach_contexts()
        files = r.resolve_final_files(contexts)
        assert len(files) >= 1
        for f in files:
            assert Path(f).exists()
            assert Path(f).stat().st_size > 0

    def test_resolve_empty_contexts_returns_empty(self, mock_config):
        """resolve_final_files([]) returns []."""
        from recorder import Recorder
        r = Recorder()
        result = r.resolve_final_files([])
        assert result == []


class TestRecorderCompatibility:

    def test_refresh_bandicam_paths_alias_exists(self):
        """refresh_bandicam_paths must exist as callable alias."""
        from recorder import Recorder
        r = Recorder()
        assert hasattr(r, "refresh_bandicam_paths")
        assert callable(r.refresh_bandicam_paths)

    def test_refresh_bandicam_paths_returns_bool(self, mock_pyaudio, mock_com_ok, mock_config):
        """refresh_bandicam_paths() returns True when output dir exists."""
        from recorder import Recorder
        r = Recorder()
        result = r.refresh_bandicam_paths()
        assert isinstance(result, bool)


class TestRecorderHealth:

    def test_ensure_recording_alive_returns_true_when_not_recording(self, mock_config):
        """ensure_recording_alive() returns True when is_recording=False (no-op)."""
        from recorder import Recorder
        r = Recorder()
        assert r.ensure_recording_alive() is True

    def test_ensure_recording_alive_returns_true_when_healthy(self, mock_pyaudio, mock_com_ok, mock_config):
        """ensure_recording_alive() returns True when recording thread is alive."""
        from recorder import Recorder
        r = Recorder()
        r.start_recording()
        result = r.ensure_recording_alive()
        assert result is True
        r.stop_recording()
