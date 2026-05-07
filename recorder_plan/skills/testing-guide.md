# Skill: Testing guide

## Key principle: never require real audio hardware
All tests mock the pyaudiowpatch stream and the Windows COM API.
Tests must pass on any machine including CI with no audio devices.

## Test file structure

```
tests/
  conftest.py                  # shared fixtures and mocks
  test_device_manager.py       # DeviceManager unit tests
  test_capture_engine.py       # CaptureEngine unit tests  
  test_recorder.py             # Recorder public interface tests
  test_audio_output.py         # WAV writing + MP3 conversion
  test_config.py               # config.py parsing tests
```

## conftest.py — key fixtures

```python
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import wave, struct, io, threading, tempfile
from pathlib import Path

@pytest.fixture
def tmp_output_dir(tmp_path):
    d = tmp_path / "recordings"
    d.mkdir()
    return d

@pytest.fixture
def mock_pyaudio():
    """Mock the entire pyaudiowpatch module."""
    with patch("recorder.pyaudio") as mock_pa:
        mock_instance = MagicMock()
        mock_pa.PyAudio.return_value = mock_instance

        # Default: 2 devices — 1 USB headset, 1 built-in
        mock_instance.get_device_count.return_value = 2
        mock_instance.get_device_info_by_index.side_effect = [
            {
                "name": "Plantronics USB Headset",
                "maxInputChannels": 1,
                "defaultSampleRate": 44100.0,
                "hostApi": 0,
                "index": 0,
            },
            {
                "name": "Realtek HD Audio (Built-in)",
                "maxInputChannels": 1,
                "defaultSampleRate": 44100.0,
                "hostApi": 0,
                "index": 1,
            },
        ]

        # Fake stream that yields silence
        mock_stream = MagicMock()
        mock_stream.read.return_value = b'\x00\x00' * 1024  # silent PCM
        mock_instance.open.return_value = mock_stream

        # WASAPI info
        mock_instance.get_host_api_info_by_type.return_value = {
            "defaultInputDevice": 0  # Plantronics is comm default
        }
        mock_pa.paWASAPI = 1
        mock_pa.paInt16 = 8

        yield mock_pa, mock_instance, mock_stream

@pytest.fixture
def mock_imm_notification():
    """Mock IMMNotificationClient registration — always succeeds."""
    with patch("recorder.comtypes") as mock_comtypes:
        mock_comtypes.CoCreateInstance.return_value = MagicMock()
        mock_comtypes.S_OK = 0
        yield mock_comtypes

@pytest.fixture
def mock_imm_notification_fail():
    """Mock IMMNotificationClient registration — always fails."""
    with patch("recorder.comtypes") as mock_comtypes:
        mock_comtypes.CoCreateInstance.side_effect = Exception("COM init failed")
        yield mock_comtypes
```

## test_device_manager.py — required tests

```python
class TestDeviceManager:

    def test_selects_usb_headset_as_first_choice(mock_pyaudio, ...):
        """USB headset should always win over built-in mic."""

    def test_falls_back_to_scoring_when_comm_query_fails(mock_pyaudio, ...):
        """When comm device query fails, score list picks headset."""

    def test_highest_scored_device_wins(mock_pyaudio, ...):
        """Score algo: USB+headset name beats built-in."""

    def test_device_change_triggers_reselection(mock_pyaudio, ...):
        """Simulate IMMNotificationClient callback → device changes."""

    def test_polling_fallback_activates_when_com_fails(mock_imm_notification_fail, ...):
        """DEV-004: polling thread starts when COM registration fails."""

    def test_no_devices_logs_dev003(mock_pyaudio, ...):
        """When device count = 0, logs [DEV-003]."""

    def test_score_usb_headset(self):
        """Score function: USB+headset name = high score."""
        assert score_device("Plantronics USB Headset", "WASAPI", True) >= 7

    def test_score_builtin_mic(self):
        """Score function: built-in mic = low/negative score."""
        assert score_device("Realtek HD Audio (Built-in)", "MME", False) < 0
```

## test_capture_engine.py — required tests

```python
class TestCaptureEngine:

    def test_start_creates_wav_file(mock_pyaudio, tmp_output_dir):
        """start() should create a valid WAV file at the given path."""

    def test_stop_closes_wav_file(mock_pyaudio, tmp_output_dir):
        """stop() should close WAV; file should be readable by wave.open."""

    def test_wav_file_contains_audio_data(mock_pyaudio, tmp_output_dir):
        """After recording N chunks, WAV file size > header-only size."""

    def test_watchdog_detects_dead_thread(mock_pyaudio, tmp_output_dir):
        """Kill record thread manually → watchdog triggers HLT-001."""

    def test_watchdog_starts_new_segment_on_recovery(mock_pyaudio, tmp_output_dir):
        """After thread death, watchdog opens new WAV file (seg2)."""

    def test_device_change_during_recording_switches_device(mock_pyaudio, ...):
        """Device change event → old WAV closed, new WAV opened on new device."""

    def test_stream_open_retries_three_times(mock_pyaudio, ...):
        """If stream.open fails, retry exactly 3 times before giving up."""

    def test_rec003_when_all_devices_fail(mock_pyaudio, ...):
        """All stream opens fail → logs [REC-003], returns False from start()."""
```

## test_recorder.py — required tests

```python
class TestRecorder:

    def test_start_recording_returns_true_on_success(mock_pyaudio, tmp_output_dir):
        ...

    def test_start_recording_returns_false_when_no_device(mock_pyaudio, ...):
        ...

    def test_stop_recording_sets_is_recording_false(mock_pyaudio, ...):
        ...

    def test_stop_when_not_recording_returns_false(...):
        ...

    def test_detach_contexts_resets_state(mock_pyaudio, ...):
        """After detach_contexts(), internal context list is empty."""

    def test_resolve_final_files_returns_existing_wav(tmp_output_dir):
        """Given a context pointing to a real WAV file, resolve returns it."""

    def test_resolve_final_files_waits_for_non_empty_file(tmp_output_dir):
        """If file is 0 bytes, resolve retries until non-zero (max 3s)."""

    def test_ensure_recording_alive_restarts_dead_engine(...):
        ...

    def test_refresh_bandicam_paths_alias_exists():
        """refresh_bandicam_paths must exist as an alias."""
        r = Recorder()
        assert hasattr(r, "refresh_bandicam_paths")
        assert r.refresh_bandicam_paths == r.refresh_recorder_paths or callable(r.refresh_bandicam_paths)

    def test_double_start_returns_false(mock_pyaudio, ...):
        """Calling start_recording() twice without stop returns False."""

    def test_force_stop_sets_not_recording(mock_pyaudio, ...):
        ...
```

## test_audio_output.py — required tests

```python
class TestAudioOutput:

    def test_wav_file_is_valid(tmp_output_dir):
        """Written WAV is readable by wave.open and has correct params."""
        # Write 1 second of silence manually, then check
        wav_path = tmp_output_dir / "test.wav"
        with wave.open(str(wav_path), 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b'\x00\x00' * 44100)
        with wave.open(str(wav_path), 'rb') as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 44100
            assert wf.getnframes() == 44100

    def test_mp3_conversion_produces_non_empty_file(tmp_output_dir):
        """MP3 conversion with lameenc produces a non-empty .mp3 file."""
        # Create real WAV, convert, check mp3 exists and size > 0

    def test_mp3_conversion_fallback_to_wav_on_failure(tmp_output_dir):
        """If lameenc fails, WAV path is returned instead."""

    def test_format_wav_returns_wav_path(tmp_output_dir):
        ...

    def test_format_mp3_returns_mp3_path_deletes_wav(tmp_output_dir):
        ...

    def test_format_both_returns_both_paths(tmp_output_dir):
        ...
```

## Running tests

```bash
# All tests
pytest tests/ -v

# Single file
pytest tests/test_device_manager.py -v

# With coverage
pytest tests/ -v --cov=recorder --cov-report=term-missing

# Stop on first failure
pytest tests/ -x -v
```

## Acceptance criteria
- All tests pass on a machine with AND without audio devices
- Coverage on recorder.py: minimum 80%
- No test takes > 2 seconds (all IO is mocked)
- No test creates real audio streams
