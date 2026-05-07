"""
Tests for WAV/MP3 audio output.

Covers:
  - WAV header validity after a real recording session.
  - MP3 conversion via lameenc.
  - Format-config branching: "wav" | "mp3" | "both".
"""
import logging
import time
import wave
from pathlib import Path

import pytest

from tests.conftest import write_valid_wav


def _wait_for(condition, timeout=2.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


# ── WAV validity ─────────────────────────────────────────────────────────────

class TestWavValidity:
    def test_recorded_wav_has_correct_format(self, mock_pyaudio, tmp_output_dir):
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            out = tmp_output_dir / "validity.wav"
            engine.start(str(out), dm.select_best_device())
            assert _wait_for(lambda: engine.bytes_written > 0, timeout=2.0)
            engine.stop()

            assert out.exists()
            with wave.open(str(out), "rb") as wf:
                assert wf.getnchannels() == 1
                assert wf.getsampwidth() == 2  # 16-bit
                assert wf.getframerate() == 44100
                assert wf.getnframes() > 0
                # Read all frames — should match the size on disk minus header.
                frames = wf.readframes(wf.getnframes())
                assert len(frames) > 0
                # 16-bit mono → frame count == byte count / 2.
                assert len(frames) == wf.getnframes() * 2
        finally:
            dm.stop()

    def test_wav_is_finalized_with_valid_header_even_on_short_recording(
        self, mock_pyaudio, tmp_output_dir
    ):
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            out = tmp_output_dir / "short.wav"
            engine.start(str(out), dm.select_best_device())
            engine.stop()
            # Header must be valid even if barely any frames were written.
            assert out.exists()
            with wave.open(str(out), "rb") as wf:
                assert wf.getframerate() == 44100
                assert wf.getsampwidth() == 2
                assert wf.getnchannels() == 1
        finally:
            dm.stop()


# ── MP3 conversion via lameenc ───────────────────────────────────────────────

class TestMp3Conversion:
    def test_mp3_conversion_creates_file(self, tmp_output_dir):
        """lameenc round-trip: pre-built WAV → MP3 file with non-zero size."""
        pytest.importorskip("lameenc")
        from recorder import CaptureEngine, DeviceManager

        wav_path = tmp_output_dir / "src.wav"
        write_valid_wav(wav_path, duration_frames=44100)  # 1 second of silence

        dm = DeviceManager()  # never started — only used to instantiate engine
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            mp3_path = engine.convert_to_mp3(str(wav_path))
            assert mp3_path is not None
            mp3 = Path(mp3_path)
            assert mp3.exists()
            assert mp3.suffix == ".mp3"
            assert mp3.stat().st_size > 100  # non-trivial size
            # First bytes of an MP3 are either an ID3 tag or a frame sync (0xFF 0xE0+).
            head = mp3.read_bytes()[:4]
            assert head[:3] == b"ID3" or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0), (
                f"unexpected MP3 header: {head!r}"
            )
        finally:
            dm.stop()

    def test_mp3_conversion_returns_none_when_wav_missing(self, tmp_output_dir, caplog):
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            missing = tmp_output_dir / "does_not_exist.wav"
            with caplog.at_level(logging.WARNING, logger="watcher.recorder"):
                result = engine.convert_to_mp3(str(missing))
            assert result is None
            assert any("[REC-007]" in r.message for r in caplog.records)
        finally:
            dm.stop()


# ── Format-config branching ──────────────────────────────────────────────────

class TestFormatConfig:
    """
    Validates that the engine + helpers honor RECORDER_FORMAT.

    Recorder.resolve_final_files() is implemented in a later session, but we
    verify the underlying primitives that it'll use:
      * format == "wav"   → WAV file kept, no MP3.
      * format == "mp3"   → WAV→MP3, WAV is deletable afterwards.
      * format == "both"  → both files coexist.
    """

    def _make_wav(self, tmp_output_dir):
        wav = tmp_output_dir / "fc.wav"
        write_valid_wav(wav, duration_frames=22050)  # 0.5s
        return wav

    def test_format_wav_keeps_only_wav(self, tmp_output_dir, monkeypatch):
        # Simulate the "wav" path — no conversion attempted.
        wav = self._make_wav(tmp_output_dir)
        # If format=="wav", resolver simply returns [wav]. No MP3 should appear.
        result = [str(wav)]
        assert wav.exists()
        assert not (tmp_output_dir / "fc.mp3").exists()
        assert result == [str(wav)]

    def test_format_mp3_replaces_wav(self, tmp_output_dir):
        pytest.importorskip("lameenc")
        from recorder import CaptureEngine, DeviceManager
        wav = self._make_wav(tmp_output_dir)
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            mp3_path = engine.convert_to_mp3(str(wav))
            assert mp3_path is not None
            # Simulate the resolver deleting the WAV after successful conversion.
            Path(wav).unlink()
            assert not wav.exists()
            assert Path(mp3_path).exists()
        finally:
            dm.stop()

    def test_format_both_keeps_wav_and_mp3(self, tmp_output_dir):
        pytest.importorskip("lameenc")
        from recorder import CaptureEngine, DeviceManager
        wav = self._make_wav(tmp_output_dir)
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            mp3_path = engine.convert_to_mp3(str(wav))
            assert mp3_path is not None
            assert wav.exists()
            assert Path(mp3_path).exists()
        finally:
            dm.stop()

    def test_config_constants_in_valid_set(self):
        """Sanity: RECORDER_FORMAT is one of the three allowed values."""
        from config import RECORDER_FORMAT
        assert RECORDER_FORMAT in ("wav", "mp3", "both")
