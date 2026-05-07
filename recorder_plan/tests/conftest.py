"""
Shared test fixtures.
ALL audio hardware is mocked — tests must pass on any machine.
"""
import pytest
import wave
import threading
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_silent_pcm(frames: int = 44100, channels: int = 1, width: int = 2) -> bytes:
    """Return silent raw PCM bytes (16-bit zeros)."""
    return b'\x00\x00' * frames * channels


def write_valid_wav(path: Path, duration_frames: int = 44100) -> Path:
    """Write a valid WAV file to path. Returns path."""
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(make_silent_pcm(duration_frames))
    return path


# ── Directories ───────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_output_dir(tmp_path):
    d = tmp_path / "recordings"
    d.mkdir()
    return d


# ── pyaudiowpatch mock ────────────────────────────────────────────────────────

FAKE_DEVICES = [
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

FAKE_NO_DEVICES = []


@pytest.fixture
def mock_pyaudio():
    """
    Mock pyaudiowpatch with 2 devices (USB headset + built-in).
    Stream.read() returns silent PCM chunks.
    USB headset is the default communications device.
    """
    with patch("recorder.pyaudio") as mock_pa:
        instance = MagicMock()
        mock_pa.PyAudio.return_value = instance

        instance.get_device_count.return_value = len(FAKE_DEVICES)
        instance.get_device_info_by_index.side_effect = lambda i: FAKE_DEVICES[i]

        stream = MagicMock()
        stream.read.return_value = make_silent_pcm(1024)
        stream.is_active.return_value = True
        instance.open.return_value = stream

        instance.get_host_api_info_by_type.return_value = {
            "defaultInputDevice": 0  # Plantronics is comm default
        }
        mock_pa.paWASAPI = 1
        mock_pa.paInt16 = 8

        yield mock_pa, instance, stream


@pytest.fixture
def mock_pyaudio_no_devices():
    """Mock pyaudiowpatch with zero input devices."""
    with patch("recorder.pyaudio") as mock_pa:
        instance = MagicMock()
        mock_pa.PyAudio.return_value = instance
        instance.get_device_count.return_value = 0
        instance.get_device_info_by_index.side_effect = IndexError
        mock_pa.paWASAPI = 1
        mock_pa.paInt16 = 8
        yield mock_pa, instance, None


@pytest.fixture
def mock_pyaudio_stream_always_fails():
    """Mock where stream.open() always raises OSError."""
    with patch("recorder.pyaudio") as mock_pa:
        instance = MagicMock()
        mock_pa.PyAudio.return_value = instance
        instance.get_device_count.return_value = len(FAKE_DEVICES)
        instance.get_device_info_by_index.side_effect = lambda i: FAKE_DEVICES[i]
        instance.open.side_effect = OSError(-9999, "Unanticipated host error")
        instance.get_host_api_info_by_type.return_value = {"defaultInputDevice": 0}
        mock_pa.paWASAPI = 1
        mock_pa.paInt16 = 8
        yield mock_pa, instance, None


# ── Windows COM / IMMNotificationClient mock ──────────────────────────────────

@pytest.fixture
def mock_com_ok():
    """COM registration succeeds."""
    with patch("recorder.comtypes") as mock_ct:
        mock_ct.CoCreateInstance.return_value = MagicMock()
        mock_ct.S_OK = 0
        yield mock_ct


@pytest.fixture
def mock_com_fail():
    """COM registration fails → polling fallback should activate."""
    with patch("recorder.comtypes") as mock_ct:
        mock_ct.CoCreateInstance.side_effect = Exception("COM init failed [DEV-001]")
        mock_ct.S_OK = 0
        yield mock_ct


# ── Config mock ───────────────────────────────────────────────────────────────

@pytest.fixture
def mock_config(tmp_output_dir):
    """Override all RECORDER_* config constants."""
    config_overrides = {
        "recorder.RECORDER_OUTPUT_DIR": str(tmp_output_dir),
        "recorder.RECORDER_FORMAT": "wav",
        "recorder.RECORDER_SAMPLE_RATE": 44100,
        "recorder.RECORDER_CHANNELS": 1,
        "recorder.RECORDER_BIT_DEPTH": 16,
        "recorder.RECORDER_CHUNK_SIZE": 1024,
        "recorder.RECORDER_MP3_BITRATE": 128,
        "recorder.RECORDER_STOP_SETTLE_SECONDS": 0.1,   # short for tests
        "recorder.RECORDER_HEALTH_CHECK_INTERVAL_SECONDS": 0.1,
        "recorder.RECORDER_DEVICE_RETRY_COUNT": 3,
        "recorder.RECORDER_DEVICE_RETRY_DELAY_SECONDS": 0.01,
        "recorder.RECORDER_WATCHDOG_RECOVERY_ATTEMPTS": 2,
    }
    with patch.multiple("recorder", **{k.split(".")[-1]: v for k, v in config_overrides.items()}):
        yield config_overrides
