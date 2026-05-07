"""
Shared test fixtures.
ALL audio hardware is mocked — tests must pass on any machine.
"""
import sys
from pathlib import Path

import pytest
import wave
from unittest.mock import MagicMock, patch

# Ensure the project root is on sys.path so `import recorder` works when
# pytest is invoked from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


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


def _build_pa_mock(devices, comm_default_index=0, open_side_effect=None):
    instance = MagicMock()
    instance.get_device_count.return_value = len(devices)
    instance.get_device_info_by_index.side_effect = lambda i: devices[i]

    def host_api_info_by_index(idx):
        return {"name": "Windows WASAPI", "index": idx}

    instance.get_host_api_info_by_index.side_effect = host_api_info_by_index

    if comm_default_index is None:
        instance.get_host_api_info_by_type.side_effect = OSError("no comm device")
    else:
        instance.get_host_api_info_by_type.return_value = {
            "defaultInputDevice": comm_default_index,
        }

    stream = MagicMock()
    stream.read.return_value = make_silent_pcm(1024)
    stream.get_read_available.return_value = 4096  # always ready for non-blocking mic path
    stream.is_active.return_value = True
    if open_side_effect is None:
        instance.open.return_value = stream
    else:
        instance.open.side_effect = open_side_effect
    return instance, stream


@pytest.fixture
def mock_pyaudio():
    """
    Mock pyaudiowpatch with 2 devices (USB headset + built-in).
    USB headset is the default communications device.
    """
    with patch("recorder.pyaudio") as mock_pa:
        instance, stream = _build_pa_mock(FAKE_DEVICES, comm_default_index=0)
        mock_pa.PyAudio.return_value = instance
        mock_pa.paWASAPI = 1
        mock_pa.paInt16 = 8
        yield mock_pa, instance, stream


@pytest.fixture
def mock_pyaudio_no_comm():
    """USB headset present but comm-default query fails — score list must pick it."""
    with patch("recorder.pyaudio") as mock_pa:
        instance, stream = _build_pa_mock(FAKE_DEVICES, comm_default_index=None)
        mock_pa.PyAudio.return_value = instance
        mock_pa.paWASAPI = 1
        mock_pa.paInt16 = 8
        yield mock_pa, instance, stream


@pytest.fixture
def mock_pyaudio_no_devices():
    """Mock pyaudiowpatch with zero input devices."""
    with patch("recorder.pyaudio") as mock_pa:
        instance, _ = _build_pa_mock([], comm_default_index=None)
        mock_pa.PyAudio.return_value = instance
        mock_pa.paWASAPI = 1
        mock_pa.paInt16 = 8
        yield mock_pa, instance, None


@pytest.fixture
def mock_pyaudio_stream_always_fails():
    """Mock where stream.open() always raises OSError."""
    with patch("recorder.pyaudio") as mock_pa:
        instance, _ = _build_pa_mock(
            FAKE_DEVICES,
            comm_default_index=0,
            open_side_effect=OSError(-9999, "Unanticipated host error"),
        )
        mock_pa.PyAudio.return_value = instance
        mock_pa.paWASAPI = 1
        mock_pa.paInt16 = 8
        yield mock_pa, instance, None


# ── Windows COM / IMMNotificationClient mock ──────────────────────────────────

@pytest.fixture
def mock_com_ok():
    """COM registration succeeds — CoCreateInstance returns a usable MagicMock."""
    with patch("recorder.comtypes") as mock_ct:
        mock_ct.CoCreateInstance.return_value = MagicMock()
        mock_ct.GUID = MagicMock(side_effect=lambda s: s)
        mock_ct.S_OK = 0
        yield mock_ct


@pytest.fixture
def mock_com_fail():
    """COM registration fails → polling fallback should activate."""
    with patch("recorder.comtypes") as mock_ct:
        mock_ct.CoCreateInstance.side_effect = Exception("COM init failed [DEV-001]")
        mock_ct.GUID = MagicMock(side_effect=lambda s: s)
        mock_ct.S_OK = 0
        yield mock_ct


# ── Config mock ───────────────────────────────────────────────────────────────

@pytest.fixture
def mock_config(tmp_path):
    """
    Patch recorder-level config constants to safe test values.
    Uses tmp_path so any recording goes to an isolated temp directory.
    Also patches shutil.disk_usage to report ample free space.
    """
    output_dir = tmp_path / "recordings"
    output_dir.mkdir(exist_ok=True)

    disk_mock = MagicMock()
    disk_mock.free = 500 * 1024 * 1024  # 500 MB — always passes the 100 MB check

    with patch("recorder.RECORDER_OUTPUT_DIR", str(output_dir)), \
         patch("recorder.RECORDER_FORMAT", "wav"), \
         patch("recorder.RECORDER_STOP_SETTLE_SECONDS", 1.0), \
         patch("recorder.RECORDER_DEVICE_RETRY_COUNT", 1), \
         patch("recorder.RECORDER_DEVICE_RETRY_DELAY_SECONDS", 0.0), \
         patch("recorder.RECORDER_WATCHDOG_RECOVERY_ATTEMPTS", 2), \
         patch("recorder.shutil") as mock_shutil, \
         patch("recorder._check_os_mute", return_value=None), \
         patch("recorder._check_whatsapp_mute", return_value=None):
        mock_shutil.disk_usage.return_value = disk_mock
        yield output_dir
