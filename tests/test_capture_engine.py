"""
Tests for CaptureEngine in recorder.py.
All audio hardware is mocked — these tests must pass without a real soundcard.
"""
import logging
import struct
import threading
import time
import wave
from unittest.mock import MagicMock, patch

import pytest


def _wait_for(condition, timeout=2.0, interval=0.02):
    """Spin-wait helper — returns True if `condition()` becomes truthy in time."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def _pick_device(dm):
    """Convenience — selects the best device (avoids re-implementing scoring in tests)."""
    return dm.select_best_device()


# ── Stream open / close ──────────────────────────────────────────────────────

class TestStreamLifecycle:
    def test_start_opens_stream_and_wav(self, mock_pyaudio, tmp_output_dir):
        from recorder import CaptureEngine, DeviceManager
        _, pa, stream = mock_pyaudio
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            device = _pick_device(dm)
            out = tmp_output_dir / "rec.wav"
            assert engine.start(str(out), device) is True
            try:
                assert engine.is_active is True
                assert engine.output_path == str(out)
                # PyAudio.open() must have been called with input=True.
                pa.open.assert_called()
                kwargs = pa.open.call_args.kwargs
                assert kwargs.get("input") is True
                assert kwargs.get("rate") == 44100
                assert kwargs.get("channels") == 1
                assert kwargs.get("input_device_index") == 0
            finally:
                engine.stop()
            # After stop: stream closed, thread dead, file finalized.
            assert engine.is_active is False
            stream.stop_stream.assert_called()
            stream.close.assert_called()
        finally:
            dm.stop()

    def test_stop_when_not_started_returns_false(self, mock_pyaudio):
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            assert engine.stop() is False
        finally:
            dm.stop()

    def test_double_start_is_ignored(self, mock_pyaudio, tmp_output_dir):
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            device = _pick_device(dm)
            out1 = tmp_output_dir / "a.wav"
            out2 = tmp_output_dir / "b.wav"
            assert engine.start(str(out1), device) is True
            try:
                assert engine.start(str(out2), device) is False
                assert engine.output_path == str(out1)
            finally:
                engine.stop()
        finally:
            dm.stop()

    def test_start_with_no_device_returns_false_logs_rec001(
        self, mock_pyaudio, tmp_output_dir, caplog
    ):
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            with caplog.at_level(logging.ERROR, logger="watcher.recorder"):
                ok = engine.start(str(tmp_output_dir / "x.wav"), None)
            assert ok is False
            assert any("[REC-001]" in r.message for r in caplog.records), (
                f"expected [REC-001]; got: {[r.message for r in caplog.records]}"
            )
        finally:
            dm.stop()

    def test_stream_open_failure_logs_rec002_rec003(
        self, mock_pyaudio_stream_always_fails, tmp_output_dir, caplog
    ):
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            device = _pick_device(dm)
            with caplog.at_level(logging.WARNING, logger="watcher.recorder"):
                ok = engine.start(str(tmp_output_dir / "fail.wav"), device)
            assert ok is False
            messages = [r.message for r in caplog.records]
            assert any("[REC-002]" in m for m in messages), messages
            assert any("[REC-003]" in m for m in messages), messages
        finally:
            dm.stop()


# ── WAV file creation ────────────────────────────────────────────────────────

class TestWavFileCreation:
    def test_wav_file_exists_after_stop(self, mock_pyaudio, tmp_output_dir):
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            out = tmp_output_dir / "session.wav"
            engine.start(str(out), _pick_device(dm))
            # Let the record thread write a few chunks.
            assert _wait_for(lambda: engine.bytes_written > 0, timeout=2.0), (
                "record thread should have written at least one chunk"
            )
            engine.stop()
            assert out.exists()
            assert out.stat().st_size > 44  # bigger than just a header
            # WAV must be parseable with the right parameters.
            with wave.open(str(out), "rb") as wf:
                assert wf.getnchannels() == 1
                assert wf.getsampwidth() == 2
                assert wf.getframerate() == 44100
                assert wf.getnframes() > 0
        finally:
            dm.stop()

    def test_wav_open_failure_logs_rec008(self, mock_pyaudio, tmp_output_dir, caplog):
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            # Pass an unwritable path: a directory rather than a file.
            bad_path = str(tmp_output_dir)  # this is a directory, can't be opened as WAV
            with caplog.at_level(logging.ERROR, logger="watcher.recorder"):
                ok = engine.start(bad_path, _pick_device(dm))
            assert ok is False
            assert any("[REC-008]" in r.message for r in caplog.records)
        finally:
            dm.stop()


# ── Watchdog thread lifecycle ────────────────────────────────────────────────

class TestWatchdog:
    def test_watchdog_thread_starts_and_stops(self, mock_pyaudio, tmp_output_dir):
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=0.05)
            engine.start(str(tmp_output_dir / "wd.wav"), _pick_device(dm))
            try:
                wd = engine._watchdog_thread
                assert wd is not None and wd.is_alive()
            finally:
                engine.stop()
            # After stop, watchdog must not still be alive.
            assert wd is None or not wd.is_alive()
        finally:
            dm.stop()

    def test_watchdog_recovers_dead_record_thread(
        self, mock_pyaudio, tmp_output_dir, caplog
    ):
        """Kill the record thread and confirm the watchdog logs HLT-001 + restarts."""
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=0.05)
            engine.start(str(tmp_output_dir / "rec.wav"), _pick_device(dm))
            try:
                # Force the record thread to exit by setting the stop_event...
                # but watchdog only triggers when thread is dead WITHOUT stop_event.
                # Simpler: directly mark the existing thread dead in engine state.
                with engine._lock:
                    fake_dead = threading.Thread(target=lambda: None, daemon=True)
                    fake_dead.start()
                    fake_dead.join()
                    engine._record_thread = fake_dead
                    engine._thread_started_at = time.time() - 10.0
                with caplog.at_level(logging.WARNING, logger="watcher.recorder"):
                    triggered = _wait_for(
                        lambda: any("[HLT-001]" in r.message for r in caplog.records),
                        timeout=2.0,
                    )
                assert triggered, (
                    f"watchdog should have logged HLT-001; got: {[r.message for r in caplog.records]}"
                )
            finally:
                engine.stop()
        finally:
            dm.stop()

    def test_watchdog_exhausts_after_max_attempts(
        self, mock_pyaudio_stream_always_fails, tmp_output_dir, caplog
    ):
        """If recovery keeps failing (stream open fails), HLT-002 fires + recovery_exhausted=True."""
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=0.05)
            # Bypass start() — stream open always fails. Manually set engine state
            # so the watchdog can drive recovery directly.
            with engine._lock:
                engine._device = {"name": "Fake", "index": 0}
                engine._device_name = "Fake"
                engine._output_path = str(tmp_output_dir / "x.wav")
                engine._thread_started_at = time.time() - 1.0
                # Pretend a record thread existed and died.
                dead = threading.Thread(target=lambda: None, daemon=True)
                dead.start()
                dead.join()
                engine._record_thread = dead

            with caplog.at_level(logging.ERROR, logger="watcher.recorder"):
                # Drive watchdog manually instead of starting threads — keeps the test
                # deterministic and avoids races.
                for _ in range(5):
                    engine._trigger_recovery()
                    if engine.recovery_exhausted:
                        break
            assert engine.recovery_exhausted is True
            assert any("[HLT-002]" in r.message for r in caplog.records), (
                f"expected [HLT-002]; got: {[r.message for r in caplog.records]}"
            )
        finally:
            dm.stop()


# ── Sample rate fallback ─────────────────────────────────────────────────────

class TestSampleRateFallback:

    def test_falls_back_to_native_rate_when_configured_rate_rejected(
        self, mock_com_ok, tmp_output_dir
    ):
        """When pa.open() fails with errno -9997, engine falls back to device native rate."""
        from recorder import CaptureEngine, DeviceManager

        with patch("recorder.pyaudio") as mock_pa:
            pa_inst = MagicMock()
            pa_inst.get_device_count.return_value = 1
            pa_inst.get_device_info_by_index.return_value = {
                "name": "USB Audio Device", "maxInputChannels": 1,
                "defaultSampleRate": 16000.0, "hostApi": 0, "index": 0,
            }
            pa_inst.get_host_api_info_by_index.return_value = {"name": "WASAPI"}
            pa_inst.get_host_api_info_by_type.side_effect = OSError("no comm")
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paInt16 = 8
            mock_pa.paWASAPI = 1

            good_stream = MagicMock()
            good_stream.read.return_value = b"\x00\x00" * 1024

            def open_side_effect(**kwargs):
                if kwargs.get("rate") == 44100:
                    raise OSError(-9997, "Invalid sample rate")
                return good_stream  # 16000 Hz succeeds

            pa_inst.open.side_effect = open_side_effect

            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, sample_rate=44100, watchdog_interval=10.0)
                    device = {
                        "index": 0, "name": "USB Audio Device",
                        "host_api_name": "WASAPI", "max_input_channels": 1,
                        "default_sample_rate": 16000.0,
                    }
                    wav_path = str(tmp_output_dir / "fallback.wav")
                    result = engine.start(wav_path, device)

                    assert result is True
                    assert engine._actual_sample_rate == 16000

                    engine.stop()

                    # WAV header must reflect the actual (16000 Hz) rate, not config (44100 Hz)
                    with wave.open(wav_path, "rb") as wf:
                        assert wf.getframerate() == 16000
                finally:
                    dm.stop()

    def test_all_rates_fail_returns_false(self, mock_pyaudio_stream_always_fails,
                                          mock_com_ok, tmp_output_dir):
        """When ALL rates fail with non-9997 errors, _open_stream returns False."""
        from recorder import CaptureEngine, DeviceManager

        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            device = dm.select_best_device()
            result = engine.start(str(tmp_output_dir / "fail.wav"), device)
            assert result is False
        finally:
            dm.stop()


# ── Device changes mid-recording ─────────────────────────────────────────────

class TestDeviceChangeMidRecording:
    def test_engine_survives_device_change_callback(self, mock_pyaudio, tmp_output_dir):
        """A DeviceManager device-change callback must not crash an active engine."""
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            engine.start(str(tmp_output_dir / "live.wav"), _pick_device(dm))
            try:
                # Engine is recording. Simulate the OS firing a device-change event.
                changed = []
                dm._change_callback = lambda: changed.append("ping")
                dm.notify_device_change("test-mid-rec")
                # No exception, callback fired, engine still active.
                assert changed == ["ping"]
                assert engine.is_active is True
            finally:
                engine.stop()
            assert engine.is_active is False
        finally:
            dm.stop()

    def test_engine_records_to_wav_under_simulated_device_churn(
        self, mock_pyaudio, tmp_output_dir
    ):
        """Fire several change notifications while recording; final WAV is still valid."""
        from recorder import CaptureEngine, DeviceManager
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            out = tmp_output_dir / "churn.wav"
            engine.start(str(out), _pick_device(dm))
            try:
                dm._change_callback = lambda: None
                for i in range(5):
                    dm.notify_device_change(f"churn-{i}")
                    time.sleep(0.01)
                assert _wait_for(lambda: engine.bytes_written > 0, timeout=2.0)
            finally:
                engine.stop()
            assert out.exists()
            with wave.open(str(out), "rb") as wf:
                assert wf.getnframes() > 0
        finally:
            dm.stop()


# ── Dual stream capture (loopback + mic) ─────────────────────────────────────

def _build_dual_pa_mock(
    loopback_succeeds=True,
    mic_succeeds=True,
    loopback_channels=1,
    loopback_data=None,
    mic_data=None,
):
    """
    Build a PyAudio mock for the new [Loopback]-device-scan approach.

    Device layout:
      index 0 — "USB Mic"                                  (normal input, no [Loopback])
      index 1 — "Speakers (USB Audio Device) [Loopback]"   (WASAPI loopback input)

    _open_loopback_stream() scans for "[Loopback]" in name + maxInputChannels > 0 +
    "WASAPI" in api_name, then opens via input_device_index=1 (no as_loopback kwarg).
    _open_stream() opens via input_device_index=0.
    open_side_effect dispatches on input_device_index.
    """
    if mic_data is None:
        mic_data = b"\x00\x00" * 1024
    if loopback_data is None:
        loopback_data = b"\x00\x00" * 1024

    FAKE_DUAL = [
        {
            "name": "USB Mic", "maxInputChannels": 1, "maxOutputChannels": 0,
            "defaultSampleRate": 48000.0, "hostApi": 0, "index": 0,
        },
        {
            # "[Loopback]" in name + maxInputChannels > 0 + WASAPI → picked by scanner
            "name": "Speakers (USB Audio Device) [Loopback]",
            "maxInputChannels": loopback_channels,
            "maxOutputChannels": 0,
            "defaultSampleRate": 48000.0, "hostApi": 0, "index": 1,
        },
    ]

    instance = MagicMock()
    instance.get_device_count.return_value = 2
    instance.get_device_info_by_index.side_effect = lambda i: FAKE_DUAL[i]
    instance.get_host_api_info_by_index.return_value = {"name": "Windows WASAPI", "index": 0}
    instance.get_host_api_info_by_type.return_value = {
        "defaultInputDevice": 0,
        "defaultOutputDevice": 1,
    }
    instance.paWASAPI = 1
    instance.paInt16 = 8

    lb_stream = MagicMock()
    lb_stream.read.return_value = loopback_data

    mic_stream = MagicMock()
    mic_stream.read.return_value = mic_data
    mic_stream.get_read_available.return_value = 4096  # always ready for non-blocking mic path
    mic_stream.is_active.return_value = True

    def open_side_effect(**kwargs):
        idx = kwargs.get("input_device_index")
        if idx == 1:                        # [Loopback] device
            if loopback_succeeds:
                return lb_stream
            raise Exception("Loopback not available")
        # Microphone device (index 0) or any other
        if mic_succeeds:
            return mic_stream
        raise OSError(-9999, "Mic device error")

    instance.open.side_effect = open_side_effect
    return instance, mic_stream, lb_stream


_DUAL_DEVICE = {
    "index": 0, "name": "USB Mic", "host_api_name": "WASAPI",
    "max_input_channels": 1, "default_sample_rate": 48000.0,
}


class TestDualStreamCapture:
    """Tests for [Loopback]-device-scan + microphone dual-stream capture."""

    def test_loopback_stream_opened_on_start(self, mock_com_ok, tmp_output_dir, caplog):
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, lb_stream = _build_dual_pa_mock()
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    with caplog.at_level(logging.INFO, logger="watcher.recorder"):
                        result = engine.start(str(tmp_output_dir / "test.wav"), _DUAL_DEVICE)
                    try:
                        assert result is True
                        assert engine._loopback_stream is not None
                        assert any(
                            "loopback stream opened" in r.message for r in caplog.records
                        )
                    finally:
                        engine.stop()
                finally:
                    dm.stop()

    def test_mic_stream_opened_alongside_loopback(
        self, mock_com_ok, tmp_output_dir, caplog
    ):
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, mic_stream, _ = _build_dual_pa_mock()
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    with caplog.at_level(logging.INFO, logger="watcher.recorder"):
                        result = engine.start(str(tmp_output_dir / "test.wav"), _DUAL_DEVICE)
                    try:
                        assert result is True
                        assert engine._loopback_stream is not None
                        assert engine._mic_stream is not None
                        assert any(
                            "dual capture active" in r.message
                            and "LOOPBACK(incoming)+MIC(outgoing)" in r.message
                            for r in caplog.records
                        )
                    finally:
                        engine.stop()
                finally:
                    dm.stop()

    def test_recording_continues_if_loopback_fails(
        self, mock_com_ok, tmp_output_dir
    ):
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, mic_stream, _ = _build_dual_pa_mock(loopback_succeeds=False)
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    result = engine.start(str(tmp_output_dir / "test.wav"), _DUAL_DEVICE)
                    try:
                        assert result is True
                        assert engine._loopback_stream is None
                        assert engine._mic_stream is not None
                    finally:
                        engine.stop()
                finally:
                    dm.stop()

    def test_recording_continues_if_mic_fails(self, mock_com_ok, tmp_output_dir):
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, lb_stream = _build_dual_pa_mock(mic_succeeds=False)
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)), \
                 patch("recorder.RECORDER_DEVICE_RETRY_COUNT", 1), \
                 patch("recorder.RECORDER_DEVICE_RETRY_DELAY_SECONDS", 0.0):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    result = engine.start(str(tmp_output_dir / "test.wav"), _DUAL_DEVICE)
                    try:
                        assert result is True
                        assert engine._loopback_stream is not None
                        assert engine._mic_stream is None
                    finally:
                        engine.stop()
                finally:
                    dm.stop()

    def test_both_fail_returns_false(self, mock_com_ok, tmp_output_dir):
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, _ = _build_dual_pa_mock(
                loopback_succeeds=False, mic_succeeds=False
            )
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)), \
                 patch("recorder.RECORDER_DEVICE_RETRY_COUNT", 1), \
                 patch("recorder.RECORDER_DEVICE_RETRY_DELAY_SECONDS", 0.0):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    result = engine.start(str(tmp_output_dir / "test.wav"), _DUAL_DEVICE)
                    assert result is False
                finally:
                    dm.stop()

    def test_mix_adds_loopback_and_mic_samples(self, mock_com_ok, tmp_output_dir):
        """Loopback value=16 + mic value=32 should produce WAV samples of value=48."""
        from recorder import CaptureEngine, DeviceManager
        lb_bytes = struct.pack("<h", 16) * 1024   # 1024 samples of 16
        mic_bytes = struct.pack("<h", 32) * 1024  # 1024 samples of 32
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, _ = _build_dual_pa_mock(
                loopback_data=lb_bytes, mic_data=mic_bytes
            )
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, sample_rate=48000, watchdog_interval=10.0)
                    out = tmp_output_dir / "mix.wav"
                    assert engine.start(str(out), _DUAL_DEVICE) is True
                    try:
                        assert _wait_for(lambda: engine.bytes_written > 0, timeout=2.0)
                    finally:
                        engine.stop()
                    with wave.open(str(out), "rb") as wf:
                        frames = wf.readframes(wf.getnframes())
                    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
                    assert len(samples) > 0
                    assert samples[0] == 48, (
                        f"expected mixed value 48 (16+32), got {samples[0]}"
                    )
                finally:
                    dm.stop()

    def test_mix_clamps_to_int16_range(self, mock_com_ok, tmp_output_dir):
        """30000 + 10000 = 40000 overflows int16; must clamp to 32767."""
        from recorder import CaptureEngine, DeviceManager
        lb_bytes = struct.pack("<h", 30000) * 1024
        mic_bytes = struct.pack("<h", 10000) * 1024
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, _ = _build_dual_pa_mock(
                loopback_data=lb_bytes, mic_data=mic_bytes
            )
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, sample_rate=48000, watchdog_interval=10.0)
                    out = tmp_output_dir / "clamp.wav"
                    assert engine.start(str(out), _DUAL_DEVICE) is True
                    try:
                        assert _wait_for(lambda: engine.bytes_written > 0, timeout=2.0)
                    finally:
                        engine.stop()
                    with wave.open(str(out), "rb") as wf:
                        frames = wf.readframes(wf.getnframes())
                    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
                    assert len(samples) > 0
                    assert samples[0] == 32767, (
                        f"expected clamped value 32767, got {samples[0]}"
                    )
                finally:
                    dm.stop()

    def test_loopback_opens_as_stereo_when_possible(self, mock_com_ok, tmp_output_dir):
        """When the loopback device reports maxInputChannels >= 2, it must open as channels=2
        and engine._loopback_channels must be 2. The record loop converts stereo→mono."""
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, _ = _build_dual_pa_mock(
                mic_succeeds=False,
                loopback_channels=2,
            )
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)), \
                 patch("recorder.RECORDER_DEVICE_RETRY_COUNT", 1), \
                 patch("recorder.RECORDER_DEVICE_RETRY_DELAY_SECONDS", 0.0):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, sample_rate=48000, watchdog_interval=10.0)
                    out = tmp_output_dir / "stereo_lb.wav"
                    assert engine.start(str(out), None) is True
                    try:
                        pass
                    finally:
                        engine.stop()
                    # pa.open() for the loopback device (index=1) must use channels=2
                    loopback_opens = [
                        c.kwargs for c in pa_inst.open.call_args_list
                        if c.kwargs.get("input_device_index") == 1
                    ]
                    assert loopback_opens, "loopback device (index=1) was never opened"
                    assert all(k.get("channels") == 2 for k in loopback_opens), (
                        f"loopback opened with channels != 2: {loopback_opens}"
                    )
                    assert engine._loopback_channels == 2, (
                        f"_loopback_channels should be 2, got {engine._loopback_channels}"
                    )
                finally:
                    dm.stop()

    def test_stop_closes_both_streams(self, mock_com_ok, tmp_output_dir):
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, mic_stream, lb_stream = _build_dual_pa_mock()
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    assert engine.start(str(tmp_output_dir / "stop.wav"), _DUAL_DEVICE) is True
                    engine.stop()
                    assert engine._loopback_stream is None
                    assert engine._mic_stream is None
                    lb_stream.stop_stream.assert_called()
                    lb_stream.close.assert_called()
                    mic_stream.stop_stream.assert_called()
                    mic_stream.close.assert_called()
                finally:
                    dm.stop()

    def test_silence_detection_logs_rec009_after_6s(
        self, mock_pyaudio, tmp_output_dir, caplog
    ):
        """6+ seconds of RMS < 10 must trigger [REC-009]."""
        from recorder import CaptureEngine, DeviceManager
        _, pa, stream = mock_pyaudio
        stream.read.return_value = b"\x00\x00" * 1024  # silence
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            with caplog.at_level(logging.WARNING, logger="watcher.recorder"):
                engine.start(str(tmp_output_dir / "silent.wav"), _pick_device(dm))
                try:
                    triggered = _wait_for(
                        lambda: any("[REC-009]" in r.message for r in caplog.records),
                        timeout=10.0,
                    )
                    assert triggered, (
                        "expected [REC-009] after 6s of silence; "
                        f"got: {[r.message for r in caplog.records]}"
                    )
                finally:
                    engine.stop()
        finally:
            dm.stop()

    def test_loopback_prefers_usb_device_over_others(
        self, mock_com_ok, tmp_output_dir, caplog
    ):
        """When multiple [Loopback] devices exist, USB one is preferred."""
        from recorder import CaptureEngine, DeviceManager
        DEVICES = [
            {
                "name": "Speakers (Synaptics HD Audio) [Loopback]",
                "maxInputChannels": 2, "maxOutputChannels": 0,
                "defaultSampleRate": 48000.0, "hostApi": 0, "index": 0,
            },
            {
                "name": "Speakers (USB Audio Device) [Loopback]",
                "maxInputChannels": 2, "maxOutputChannels": 0,
                "defaultSampleRate": 48000.0, "hostApi": 0, "index": 1,
            },
        ]
        with patch("recorder.pyaudio") as mock_pa:
            instance = MagicMock()
            instance.get_device_count.return_value = 2
            instance.get_device_info_by_index.side_effect = lambda i: DEVICES[i]
            instance.get_host_api_info_by_index.return_value = {"name": "Windows WASAPI"}
            usb_stream = MagicMock()
            usb_stream.read.return_value = b"\x00\x00" * 1024
            instance.open.return_value = usb_stream
            mock_pa.PyAudio.return_value = instance
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8

            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    with caplog.at_level(logging.INFO, logger="watcher.recorder"):
                        stream = engine._open_loopback_stream()
                    assert stream is not None
                    # USB device should have been selected
                    call_kwargs = instance.open.call_args.kwargs
                    assert call_kwargs.get("input_device_index") == 1, (
                        "should prefer index=1 (USB device)"
                    )
                    assert any("USB" in r.message for r in caplog.records
                               if "loopback" in r.message.lower())
                finally:
                    dm.stop()

    def test_loopback_falls_back_to_first_when_no_usb(
        self, mock_com_ok, tmp_output_dir
    ):
        """When no USB loopback device, first [Loopback] device is used."""
        from recorder import CaptureEngine, DeviceManager
        DEVICES = [
            {
                "name": "Speakers (Synaptics HD Audio) [Loopback]",
                "maxInputChannels": 2, "maxOutputChannels": 0,
                "defaultSampleRate": 48000.0, "hostApi": 0, "index": 0,
            },
        ]
        with patch("recorder.pyaudio") as mock_pa:
            instance = MagicMock()
            instance.get_device_count.return_value = 1
            instance.get_device_info_by_index.side_effect = lambda i: DEVICES[i]
            instance.get_host_api_info_by_index.return_value = {"name": "Windows WASAPI"}
            fallback_stream = MagicMock()
            fallback_stream.read.return_value = b"\x00\x00" * 1024
            instance.open.return_value = fallback_stream
            mock_pa.PyAudio.return_value = instance
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8

            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    stream = engine._open_loopback_stream()
                    assert stream is not None
                    call_kwargs = instance.open.call_args.kwargs
                    assert call_kwargs.get("input_device_index") == 0
                finally:
                    dm.stop()

    def test_no_loopback_devices_returns_none(
        self, mock_com_ok, tmp_output_dir, caplog
    ):
        """If no device has [Loopback] in name, _open_loopback_stream returns None."""
        from recorder import CaptureEngine, DeviceManager
        DEVICES = [
            {
                "name": "Microphone (USB Audio Device)",
                "maxInputChannels": 1, "maxOutputChannels": 0,
                "defaultSampleRate": 48000.0, "hostApi": 0, "index": 0,
            },
            {
                "name": "Speakers (USB Audio Device)",  # no [Loopback]
                "maxInputChannels": 0, "maxOutputChannels": 2,
                "defaultSampleRate": 48000.0, "hostApi": 0, "index": 1,
            },
        ]
        with patch("recorder.pyaudio") as mock_pa:
            instance = MagicMock()
            instance.get_device_count.return_value = 2
            instance.get_device_info_by_index.side_effect = lambda i: DEVICES[i]
            instance.get_host_api_info_by_index.return_value = {"name": "Windows WASAPI"}
            mock_pa.PyAudio.return_value = instance
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8

            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    with caplog.at_level(logging.WARNING, logger="watcher.recorder"):
                        result = engine._open_loopback_stream()
                    assert result is None
                    assert any(
                        "no [Loopback] devices found" in r.message for r in caplog.records
                    )
                finally:
                    dm.stop()

    def test_mic_probe_logs_warning_when_silent(
        self, mock_pyaudio, tmp_output_dir, caplog
    ):
        """Mic probe returning zeros must log 'mic probe silent'."""
        from recorder import CaptureEngine, DeviceManager
        _, pa, stream = mock_pyaudio
        stream.read.return_value = b"\x00\x00" * 1024   # zero data → rms=0
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            device = _pick_device(dm)
            with caplog.at_level(logging.WARNING, logger="watcher.recorder"):
                ok = engine._open_stream(device)
            try:
                assert ok is True
                assert any("mic probe silent" in r.message for r in caplog.records), (
                    f"expected 'mic probe silent'; got: {[r.message for r in caplog.records]}"
                )
            finally:
                engine.stop()
        finally:
            dm.stop()

    def test_mic_probe_logs_ok_when_audio(
        self, mock_pyaudio, tmp_output_dir, caplog
    ):
        """Mic probe returning non-zero data must log 'mic probe OK'."""
        from recorder import CaptureEngine, DeviceManager
        _, pa, stream = mock_pyaudio
        # 1024 samples of value 1000 → rms=1000 > 1
        stream.read.return_value = struct.pack("<h", 1000) * 1024
        dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, watchdog_interval=10.0)
            device = _pick_device(dm)
            with caplog.at_level(logging.INFO, logger="watcher.recorder"):
                ok = engine._open_stream(device)
            try:
                assert ok is True
                assert any("mic probe OK" in r.message for r in caplog.records), (
                    f"expected 'mic probe OK'; got: {[r.message for r in caplog.records]}"
                )
            finally:
                engine.stop()
        finally:
            dm.stop()

    # ── Blocking read + stream-close crash-fix tests ─────────────────────────

    def test_stop_closes_streams_before_joining(self, mock_com_ok, tmp_output_dir):
        """
        stop() order: stop_stream() BEFORE join (unblocks read()), close() AFTER join.
        stop_stream() signals PortAudio to unblock a pending read(). The record
        thread exits, join() completes quickly, then close() is safe to call.
        NEVER call close() before join — concurrent read()+close() = access violation.
        """
        from recorder import CaptureEngine, DeviceManager
        import time as _time
        # stop_stream() unblocks blocking read() (real PortAudio behaviour).
        # Simulate this: stop_stream() sets an event; read() blocks on that event.
        stop_event = threading.Event()

        def blocking_read(*args, **kwargs):
            stop_event.wait(timeout=10.0)
            return b"\x00\x00" * 1024   # returns normally after stop_stream()

        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, mic_stream, lb_stream = _build_dual_pa_mock()
            lb_stream.read.side_effect = blocking_read
            lb_stream.stop_stream.side_effect = lambda: stop_event.set()
            # Mic streams normally
            mic_stream.read.return_value = b"\x00\x00" * 1024
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    assert engine.start(str(tmp_output_dir / "close.wav"), _DUAL_DEVICE) is True
                    time.sleep(0.05)  # let thread enter blocking read()
                    t0 = _time.time()
                    engine.stop()
                    elapsed = _time.time() - t0
                    # stop_stream() unblocked read() → thread exited → join was fast
                    assert elapsed < 1.5, (
                        f"stop() took {elapsed:.2f}s — stop_stream() may not have "
                        "unblocked read() before join"
                    )
                    assert lb_stream.stop_stream.called, "stop_stream() never called"
                    assert lb_stream.close.called, "close() never called"
                finally:
                    dm.stop()

    def test_record_thread_handles_loopback_oserror_without_crash(
        self, mock_com_ok, tmp_output_dir, caplog
    ):
        """OSError on loopback read must log [REC-005], null loopback, continue with mic."""
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, mic_stream, lb_stream = _build_dual_pa_mock()
            lb_stream.read.side_effect = OSError(-9999, "device lost")
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    assert engine.start(str(tmp_output_dir / "err.wav"), _DUAL_DEVICE) is True
                    try:
                        assert _wait_for(
                            lambda: any("[REC-005]" in r.message for r in caplog.records),
                            timeout=2.0,
                        ), "expected [REC-005] log from loopback OSError"
                    finally:
                        engine.stop()
                    assert any("[REC-005]" in r.message for r in caplog.records)
                finally:
                    dm.stop()

    def test_record_thread_exits_on_oserror_when_stop_set(
        self, mock_com_ok, tmp_output_dir, caplog
    ):
        """
        When stop_event is set BEFORE OSError is raised, [REC-005] must NOT be logged.
        stop() sets stop_event first, then closes streams; close() triggers OSError in
        the blocking read(). At that point stop_event is already True → thread breaks
        silently without logging the expected error.
        """
        from recorder import CaptureEngine, DeviceManager
        # read() blocks until close() is called (simulates real blocking stream).
        # close() sets an event that makes read() raise OSError.
        # stop() order: set stop_event → stop_stream() → close() → OSError in read().
        close_event = threading.Event()

        def blocking_read(*args, **kwargs):
            close_event.wait(timeout=10.0)
            raise OSError(-9999, "stream closed by stop()")

        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, mic_stream, lb_stream = _build_dual_pa_mock()
            lb_stream.read.side_effect = blocking_read
            lb_stream.stop_stream.side_effect = lambda: None
            lb_stream.close.side_effect = lambda: close_event.set()
            # Mic returns data normally
            mic_stream.read.return_value = b"\x00\x00" * 1024
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, watchdog_interval=10.0)
                    assert engine.start(str(tmp_output_dir / "oserr.wav"), _DUAL_DEVICE) is True
                    time.sleep(0.05)   # let thread enter blocking read()
                    # stop() sets stop_event, then calls close() → OSError in read()
                    # At that point stop_event IS set → thread breaks without [REC-005]
                    engine.stop()
                    rec005_msgs = [
                        r.message for r in caplog.records if "[REC-005]" in r.message
                    ]
                    assert not rec005_msgs, (
                        f"[REC-005] logged for expected stop-OSError: {rec005_msgs}"
                    )
                finally:
                    dm.stop()

    def test_no_zeros_from_timing_mismatch(self, mock_com_ok, tmp_output_dir):
        """
        With blocking reads both streams drain in lockstep — no silence gaps.
        Loopback=1000, mic=0: mixed output = 1000+0 = 1000 (non-zero).
        Zero percentage must be < 5%.
        """
        from recorder import CaptureEngine, DeviceManager
        lb_bytes = struct.pack("<h", 1000) * 1024   # non-zero
        mic_bytes = b"\x00\x00" * 1024              # zeros

        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, _ = _build_dual_pa_mock(
                loopback_data=lb_bytes,
                mic_data=mic_bytes,
            )
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, sample_rate=48000, watchdog_interval=10.0)
                    out = tmp_output_dir / "zeros.wav"
                    assert engine.start(str(out), _DUAL_DEVICE) is True
                    try:
                        assert _wait_for(
                            lambda: engine.bytes_written >= 1024 * 2 * 4,
                            timeout=3.0,
                        ), "not enough data written"
                    finally:
                        engine.stop()
                    with wave.open(str(out), "rb") as wf:
                        frames = wf.readframes(wf.getnframes())
                    if not frames:
                        pytest.skip("no frames written")
                    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
                    zero_pct = sum(1 for s in samples if s == 0) / len(samples)
                    assert zero_pct < 0.05, (
                        f"Zero samples {zero_pct*100:.1f}% — timing mismatch still present"
                    )
                finally:
                    dm.stop()

    def test_loopback_stereo_converts_to_mono(self, mock_com_ok, tmp_output_dir):
        """Stereo loopback (L=100, R=300) must produce mono output of 200 (average)."""
        from recorder import CaptureEngine, DeviceManager
        # 512 stereo pairs: L=100, R=300 → each mono sample should be (100+300)//2 = 200
        lb_bytes = struct.pack("<hh", 100, 300) * 512  # 1024 total samples = 2048 bytes
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, _ = _build_dual_pa_mock(
                loopback_channels=2,
                loopback_data=lb_bytes,
                mic_succeeds=False,
            )
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)), \
                 patch("recorder.RECORDER_DEVICE_RETRY_COUNT", 1), \
                 patch("recorder.RECORDER_DEVICE_RETRY_DELAY_SECONDS", 0.0):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, sample_rate=48000, watchdog_interval=10.0)
                    out = tmp_output_dir / "stereo_mono.wav"
                    assert engine.start(str(out), None) is True
                    try:
                        assert _wait_for(lambda: engine.bytes_written > 0, timeout=2.0), (
                            "no bytes written — loopback data not reaching WAV"
                        )
                    finally:
                        engine.stop()
                    with wave.open(str(out), "rb") as wf:
                        frames = wf.readframes(wf.getnframes())
                    assert frames, "WAV is empty"
                    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
                    assert samples[0] == 200, (
                        f"stereo (L=100, R=300) should average to 200, got {samples[0]}"
                    )
                finally:
                    dm.stop()

    def test_loopback_does_not_use_get_read_available(
        self, mock_com_ok, tmp_output_dir
    ):
        """Loopback stream must never call get_read_available() — it drives timing via
        blocking read(). get_read_available() is reserved for the non-blocking mic path."""
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, lb_stream = _build_dual_pa_mock(mic_succeeds=False)
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)), \
                 patch("recorder.RECORDER_DEVICE_RETRY_COUNT", 1), \
                 patch("recorder.RECORDER_DEVICE_RETRY_DELAY_SECONDS", 0.0):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, sample_rate=48000, watchdog_interval=10.0)
                    out = tmp_output_dir / "lb_nopoll.wav"
                    assert engine.start(str(out), None) is True
                    try:
                        assert _wait_for(lambda: engine.bytes_written > 0, timeout=2.0)
                    finally:
                        engine.stop()
                    assert lb_stream.get_read_available.call_count == 0, (
                        "loopback must not call get_read_available(); "
                        f"it was called {lb_stream.get_read_available.call_count} time(s)"
                    )
                finally:
                    dm.stop()

    def test_mic_uses_get_read_available_before_read(
        self, mock_com_ok, tmp_output_dir
    ):
        """Mic stream must call get_read_available() before each read() call."""
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, mic_stream, _ = _build_dual_pa_mock()
            # Return chunk_size so the mic read proceeds
            mic_stream.get_read_available.return_value = 1024
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, sample_rate=48000, watchdog_interval=10.0)
                    out = tmp_output_dir / "mic_poll.wav"
                    assert engine.start(str(out), _DUAL_DEVICE) is True
                    try:
                        assert _wait_for(lambda: engine.bytes_written > 0, timeout=2.0)
                    finally:
                        engine.stop()
                    assert mic_stream.get_read_available.call_count >= 1, (
                        "mic must call get_read_available() at least once; "
                        f"call count: {mic_stream.get_read_available.call_count}"
                    )
                finally:
                    dm.stop()

    def test_mic_not_ready_does_not_delay_or_block_loopback(
        self, mock_com_ok, tmp_output_dir
    ):
        """When mic get_read_available() returns 0 (not ready), loopback data still
        gets written to the WAV without any blocking on the mic path."""
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, mic_stream, _ = _build_dual_pa_mock()
            mic_stream.get_read_available.return_value = 0  # mic never ready
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            with patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_output_dir)):
                dm = DeviceManager()
                try:
                    engine = CaptureEngine(dm, sample_rate=48000, watchdog_interval=10.0)
                    out = tmp_output_dir / "mic_skip.wav"
                    assert engine.start(str(out), _DUAL_DEVICE) is True
                    try:
                        written = _wait_for(lambda: engine.bytes_written > 0, timeout=2.0)
                    finally:
                        engine.stop()
                    assert written, (
                        "loopback data should have been written even when mic is not ready"
                    )
                    # mic.read() is called once during the probe in _open_stream(),
                    # but the record loop must not call it when available == 0.
                    assert mic_stream.read.call_count <= 1, (
                        f"mic.read() called {mic_stream.read.call_count} times; "
                        "only the probe read is allowed — record loop must skip "
                        "mic reads when get_read_available() returns 0"
                    )
                finally:
                    dm.stop()


# ── Resample duration-ratio tests ────────────────────────────────────────────

class TestResample:

    def test_resample_preserves_duration_ratio(self):
        """_resample() must preserve the duration proportional to the rate ratio."""
        import struct as _struct
        from recorder import CaptureEngine

        n = 441
        data = _struct.pack(f"<{n}h", *([1000] * n))

        # Same rate: output must equal input (short-circuit path)
        result = CaptureEngine._resample(data, 44100, 44100)
        assert len(result) // 2 == n, (
            f"same-rate: expected {n} samples, got {len(result)//2}"
        )

        # 44100→48000: ratio = 48000/44100 ≈ 1.0884
        expected_up = int(n * 48000 / 44100)
        result_up = CaptureEngine._resample(data, 44100, 48000)
        assert abs(len(result_up) // 2 - expected_up) <= 1, (
            f"44100→48000: expected ~{expected_up} samples, got {len(result_up)//2}"
        )

        # 48000→44100: ratio = 44100/48000 ≈ 0.9188
        expected_down = int(n * 44100 / 48000)
        result_down = CaptureEngine._resample(data, 48000, 44100)
        assert abs(len(result_down) // 2 - expected_down) <= 1, (
            f"48000→44100: expected ~{expected_down} samples, got {len(result_down)//2}"
        )


# ── Duration check [REC-010] tests ───────────────────────────────────────────

class TestDurationCheck:

    def _make_engine(self, mock_com_ok):
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, _ = _build_dual_pa_mock()
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            dm = DeviceManager()
        engine = CaptureEngine(dm, sample_rate=44100, watchdog_interval=10.0)
        return engine, dm

    def test_duration_check_logs_rec010_on_bad_ratio(
        self, mock_com_ok, caplog
    ):
        """[REC-010] must be logged when WAV duration is much longer than wall time."""
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, _ = _build_dual_pa_mock()
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, sample_rate=44100, watchdog_interval=10.0)
            # Simulate: 30 seconds wall time but 60 seconds of WAV audio (ratio=2.0)
            engine._mix_rate = 44100
            engine._sample_width = 2
            engine._channels = 1
            engine._record_started_monotonic = time.monotonic() - 30.0
            engine._bytes_written = int(60 * 44100 * 2 * 1)  # 60s of audio

            with caplog.at_level(logging.WARNING, logger="watcher.recorder"):
                engine._log_duration_check()

            msgs = [r.message for r in caplog.records]
            assert any("[REC-010]" in m for m in msgs), (
                f"[REC-010] not logged for ratio≈2.0; got: {msgs}"
            )
        finally:
            dm.stop()

    def test_duration_check_no_warning_on_good_ratio(
        self, mock_com_ok, caplog
    ):
        """No [REC-010] when WAV duration is within 10% of wall time."""
        from recorder import CaptureEngine, DeviceManager
        with patch("recorder.pyaudio") as mock_pa:
            pa_inst, _, _ = _build_dual_pa_mock()
            mock_pa.PyAudio.return_value = pa_inst
            mock_pa.paWASAPI = 1
            mock_pa.paInt16 = 8
            dm = DeviceManager()
        try:
            engine = CaptureEngine(dm, sample_rate=44100, watchdog_interval=10.0)
            # Simulate: 30 seconds wall time, 30 seconds of WAV audio (ratio=1.0)
            engine._mix_rate = 44100
            engine._sample_width = 2
            engine._channels = 1
            engine._record_started_monotonic = time.monotonic() - 30.0
            engine._bytes_written = int(30 * 44100 * 2 * 1)  # 30s of audio

            with caplog.at_level(logging.WARNING, logger="watcher.recorder"):
                engine._log_duration_check()

            msgs = [r.message for r in caplog.records if "[REC-010]" in r.message]
            assert not msgs, (
                f"[REC-010] should not be logged for ratio≈1.0; got: {msgs}"
            )
        finally:
            dm.stop()
