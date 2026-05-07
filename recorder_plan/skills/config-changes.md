# Skill: Config changes

## config.json — replace `bandicam` block

Remove the entire `bandicam` block. Add this `recorder` block in its place:

```json
"recorder": {
  "output_dir": "%USERPROFILE%/Documents/Recordings",
  "format": "wav",
  "sample_rate": 44100,
  "channels": 1,
  "bit_depth": 16,
  "chunk_size": 1024,
  "mp3_bitrate": 128,
  "stop_settle_seconds": 3.0,
  "health_check_interval_seconds": 5,
  "device_retry_count": 3,
  "device_retry_delay_seconds": 0.5,
  "watchdog_recovery_attempts": 2,
  "polling_fallback_interval_seconds": 2.0
}
```

`format` options:
- `"wav"` — WAV only (safest, largest files)
- `"mp3"` — convert to MP3 after recording, delete WAV (smallest files)
- `"both"` — keep both WAV and MP3

## config.py — what to add/change

Add these constants, reading from the `recorder` config block:

```python
# Recorder
RECORDER_OUTPUT_DIR: str          # expands %USERPROFILE% etc.
RECORDER_FORMAT: str              # "wav" | "mp3" | "both"
RECORDER_SAMPLE_RATE: int         # default 44100
RECORDER_CHANNELS: int            # default 1
RECORDER_BIT_DEPTH: int           # default 16
RECORDER_CHUNK_SIZE: int          # default 1024
RECORDER_MP3_BITRATE: int         # default 128
RECORDER_STOP_SETTLE_SECONDS: float
RECORDER_HEALTH_CHECK_INTERVAL_SECONDS: float
RECORDER_DEVICE_RETRY_COUNT: int
RECORDER_DEVICE_RETRY_DELAY_SECONDS: float
RECORDER_WATCHDOG_RECOVERY_ATTEMPTS: int
RECORDER_POLLING_FALLBACK_INTERVAL_SECONDS: float
```

Remove these constants (Bandicam):
```python
# DELETE these:
BANDICAM_PATH
BANDICAM_OUTPUT_DIR
BANDICAM_STOP_SETTLE_SECONDS
BANDICAM_STOP_COMMAND_TIMEOUT
BANDICAM_HEALTH_CHECK_INTERVAL_SECONDS
```

## ALLOWED_MEDIA_EXTENSIONS — update

Current value includes video formats (`.mp4`, `.avi`, etc.) for Bandicam.
Update to audio only:

```python
ALLOWED_MEDIA_EXTENSIONS: Set[str] = {".wav", ".mp3"}
```

This set is used by `main.py` and `storage.py` for file matching — keep it in config.py.

## Output directory resolution

```python
import os
from pathlib import Path

def _resolve_output_dir(raw_path: str) -> Optional[Path]:
    """Expand env vars, resolve, check exists."""
    try:
        expanded = os.path.expandvars(raw_path)
        p = Path(expanded)
        p.mkdir(parents=True, exist_ok=True)
        if p.is_dir():
            return p
    except Exception:
        pass
    return None
```

## main.py compatibility

`main.py` imports from `config.py`:
```python
from config import (
    BANDICAM_HEALTH_CHECK_INTERVAL_SECONDS,  # used in ensure_recording_alive check
    ...
)
```

DO NOT change these imports in main.py. Instead, add aliases in config.py:
```python
# Backwards compatibility aliases for main.py
BANDICAM_HEALTH_CHECK_INTERVAL_SECONDS = RECORDER_HEALTH_CHECK_INTERVAL_SECONDS
BANDICAM_STOP_SETTLE_SECONDS = RECORDER_STOP_SETTLE_SECONDS
```

Check `config.py` for ALL `BANDICAM_*` usages and add an alias for each one that
`main.py` or any other frozen file imports. Do not miss any — it will cause an ImportError.
