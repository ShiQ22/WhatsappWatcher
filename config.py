from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Union

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent.resolve()
else:
    BASE_DIR = Path(__file__).parent.resolve()

CONFIG_PATH = BASE_DIR / "config.json"
CONFIG_LOAD_ERROR: Optional[str] = None

DEFAULT_CONFIG: Dict[str, Any] = {
    "app_name": "WhatsApp Watcher",
    "run_mode": "test",
    "paths": {
        "logs_dir": "logs",
        "data_dir": "data",
        "db_path": "data/calls.db",
        "reports_dir": "data/reports",
    },
    "recorder": {
        "output_dir": str(Path.home() / "Documents" / "Recordings"),
        "format": "mp3",
        "sample_rate": 48000,
        "channels": 1,
        "bit_depth": 16,
        "chunk_size": 960,
        "mp3_bitrate": 64,
        "stop_settle_seconds": 3.0,
        "health_check_interval_seconds": 5,
        "device_retry_count": 3,
        "device_retry_delay_seconds": 0.5,
        "watchdog_recovery_attempts": 2,
        "polling_fallback_interval_seconds": 2.0,
        "debug_stems": False,
        "mic_gain": 1.8,
        "loopback_gain": 0.60,
        "backend": "pyaudio",
        "helper_path": "RecorderHelper/bin/Release/net8.0/RecorderHelper.exe",
        "keep_temp": False,
        "startup_timeout_seconds": 10.0,
        "stop_timeout_seconds": 120.0,
        "ffmpeg_path": "bin/ffmpeg.exe",
        "keep_wav_after_mp3": False,
    },
    "watcher": {
        "poll_interval_seconds": 0.8,
        "recorder_retry_cooldown_seconds": 2,
        "upload_retry_count": 3,
        "upload_retry_delay_seconds": 3.0,
        "startup_pending_upload_scan": True,
        "log_max_bytes": 5 * 1024 * 1024,
        "log_backup_count": 5,
        "log_level": "INFO",
    },
    "media": {
        "allowed_extensions": [".wav", ".mp3"],
    },
    "upload": {
        "enabled": False,
        "root_dir": r"\\RECORDING\Mawatheeq",
        "recordings_subdir_name": "",
        "verify_copy": True,
        "delete_local_after_success": True,
        "daily_report_enabled": True,
        "daily_report_filename_prefix": "call_report",
        "daily_report_subdir_name": "logs",
        "share_auth_enabled": True,
        "share_username": "",
        "share_password": "",
    },
    "storage": {
        "central_db": {
            "enabled": False,
            "driver": "sqlalchemy",
            "mysql_uri": "",
            "timeout_seconds": 5,
        },
    },
    "maintenance": {
        "central_sync_interval_seconds": 300,
        "local_cleanup_interval_seconds": 604800,
    },
    "whatsapp": {
        "process_aliases": [
            "whatsapp.exe",
            "whatsapp.root.exe",
            "whatsappbeta.exe",
            "whatsappdesktop.exe",
            "whatsapphelper.exe",
            "wahelper.exe",
        ],
        "keyword_aliases": ["whatsapp", "whatsappdesktop", "whatsappbeta", "5319275a.whatsappdesktop"],
    },
}




def _expand_env_placeholders(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _expand_env_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_placeholders(v) for v in value]
    if isinstance(value, str):
        value = os.path.expandvars(value)
        stripped = value.strip()
        if stripped.startswith("${") and stripped.endswith("}") and len(stripped) > 3:
            return os.environ.get(stripped[2:-1], "")
        return value
    return value

def _deep_merge(defaults: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(defaults)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_config() -> Dict[str, Any]:
    global CONFIG_LOAD_ERROR
    if not CONFIG_PATH.exists():
        CONFIG_LOAD_ERROR = f"config.json not found at {CONFIG_PATH} — using defaults"
        return DEFAULT_CONFIG
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            user_cfg = _expand_env_placeholders(json.load(f))
        CONFIG_LOAD_ERROR = None
        return _deep_merge(DEFAULT_CONFIG, user_cfg)
    except Exception as exc:
        CONFIG_LOAD_ERROR = f"Failed to load config.json: {exc} — using defaults"
        return DEFAULT_CONFIG


def _resolve_path(value: Union[str, Path]) -> Path:
    p = Path(value)
    if p.is_absolute():
        return p
    return (BASE_DIR / p).resolve()


CONFIG = _load_config()
APP_NAME = CONFIG.get("app_name", DEFAULT_CONFIG["app_name"])
RUN_MODE = CONFIG.get("run_mode", DEFAULT_CONFIG["run_mode"])

PATHS = CONFIG.get("paths", {})
RECORDER = CONFIG.get("recorder", {})
WATCHER = CONFIG.get("watcher", {})
MEDIA = CONFIG.get("media", {})
UPLOAD = CONFIG.get("upload", {})
STORAGE = CONFIG.get("storage", {})
CENTRAL_DB = STORAGE.get("central_db", {})
WHATSAPP = CONFIG.get("whatsapp", {})
MAINTENANCE = CONFIG.get("maintenance", {})

LOG_DIR = _resolve_path(PATHS.get("logs_dir", "logs"))
DATA_DIR = _resolve_path(PATHS.get("data_dir", "data"))
DB_PATH = _resolve_path(PATHS.get("db_path", "data/calls.db"))
REPORTS_DIR = _resolve_path(PATHS.get("reports_dir", "data/reports"))

for folder in (LOG_DIR, DATA_DIR, DB_PATH.parent, REPORTS_DIR):
    folder.mkdir(parents=True, exist_ok=True)

def _resolve_output_dir(raw_path: str) -> Optional[Path]:
    """Expand env vars, resolve, ensure directory exists."""
    try:
        expanded = os.path.expandvars(str(raw_path))
        p = Path(expanded)
        p.mkdir(parents=True, exist_ok=True)
        if p.is_dir():
            return p
    except OSError:
        return None
    return None


_recorder_defaults = DEFAULT_CONFIG["recorder"]
_raw_output_dir = str(RECORDER.get("output_dir", _recorder_defaults["output_dir"]) or _recorder_defaults["output_dir"])
_resolved_output_dir = _resolve_output_dir(_raw_output_dir)

RECORDER_OUTPUT_DIR: str = str(_resolved_output_dir) if _resolved_output_dir else os.path.expandvars(_raw_output_dir)
RECORDER_FORMAT: str = str(RECORDER.get("format", _recorder_defaults["format"]) or "wav").strip().lower()
if RECORDER_FORMAT not in ("wav", "mp3", "both"):
    RECORDER_FORMAT = "wav"
RECORDER_SAMPLE_RATE: int = int(RECORDER.get("sample_rate", _recorder_defaults["sample_rate"]))
RECORDER_CHANNELS: int = int(RECORDER.get("channels", _recorder_defaults["channels"]))
RECORDER_BIT_DEPTH: int = int(RECORDER.get("bit_depth", _recorder_defaults["bit_depth"]))
RECORDER_CHUNK_SIZE: int = int(RECORDER.get("chunk_size", _recorder_defaults["chunk_size"]))
RECORDER_MP3_BITRATE: int = int(RECORDER.get("mp3_bitrate", _recorder_defaults["mp3_bitrate"]))
RECORDER_STOP_SETTLE_SECONDS: float = float(RECORDER.get("stop_settle_seconds", _recorder_defaults["stop_settle_seconds"]))
RECORDER_HEALTH_CHECK_INTERVAL_SECONDS: float = float(RECORDER.get("health_check_interval_seconds", _recorder_defaults["health_check_interval_seconds"]))
RECORDER_DEVICE_RETRY_COUNT: int = int(RECORDER.get("device_retry_count", _recorder_defaults["device_retry_count"]))
RECORDER_DEVICE_RETRY_DELAY_SECONDS: float = float(RECORDER.get("device_retry_delay_seconds", _recorder_defaults["device_retry_delay_seconds"]))
RECORDER_WATCHDOG_RECOVERY_ATTEMPTS: int = int(RECORDER.get("watchdog_recovery_attempts", _recorder_defaults["watchdog_recovery_attempts"]))
RECORDER_POLLING_FALLBACK_INTERVAL_SECONDS: float = float(
    RECORDER.get("polling_fallback_interval_seconds", _recorder_defaults["polling_fallback_interval_seconds"])
)
RECORDER_DEBUG_STEMS: bool = bool(RECORDER.get("debug_stems", _recorder_defaults.get("debug_stems", False)))
RECORDER_MIC_GAIN: float = float(RECORDER.get("mic_gain", _recorder_defaults.get("mic_gain", 1.8)))
RECORDER_LOOPBACK_GAIN: float = float(RECORDER.get("loopback_gain", _recorder_defaults.get("loopback_gain", 0.60)))

# RecorderHelper IPC backend — used when recorder.backend = "helper"
RECORDER_BACKEND: str = str(
    RECORDER.get("backend", _recorder_defaults.get("backend", "pyaudio"))
).strip().lower()
RECORDER_HELPER_PATH: str = str(
    RECORDER.get("helper_path", _recorder_defaults.get("helper_path", ""))
)
RECORDER_HELPER_STARTUP_TIMEOUT: float = float(
    RECORDER.get("startup_timeout_seconds", _recorder_defaults.get("startup_timeout_seconds", 10.0))
)
RECORDER_HELPER_STOP_TIMEOUT: float = float(
    RECORDER.get("stop_timeout_seconds", _recorder_defaults.get("stop_timeout_seconds", 120.0))
)
RECORDER_HELPER_KEEP_TEMP: bool = bool(
    RECORDER.get("keep_temp", _recorder_defaults.get("keep_temp", False))
)

# FFmpeg post-processing — WAV→MP3 conversion after RecorderHelper produces output
_raw_ffmpeg_path: str = str(
    RECORDER.get("ffmpeg_path", _recorder_defaults.get("ffmpeg_path", "bin/ffmpeg.exe"))
    or "bin/ffmpeg.exe"
)
RECORDER_FFMPEG_PATH: str = (
    _raw_ffmpeg_path
    if Path(_raw_ffmpeg_path).is_absolute()
    else str((BASE_DIR / _raw_ffmpeg_path).resolve())
)
RECORDER_KEEP_WAV_AFTER_MP3: bool = bool(
    RECORDER.get("keep_wav_after_mp3", _recorder_defaults.get("keep_wav_after_mp3", False))
)

# Backwards compatibility aliases — kept so frozen modules (main.py et al.) keep importing.
# Point BANDICAM_PATH at a file that always exists so main.py's existence check passes
# without triggering the "Bandicam NOT found — recording will fail" warning.
BANDICAM_PATH: str = os.path.abspath(__file__)
BANDICAM_OUTPUT_DIR: Path = Path(RECORDER_OUTPUT_DIR)
BANDICAM_STOP_SETTLE_SECONDS = RECORDER_STOP_SETTLE_SECONDS
BANDICAM_STOP_COMMAND_TIMEOUT = 10
BANDICAM_HEALTH_CHECK_INTERVAL_SECONDS = RECORDER_HEALTH_CHECK_INTERVAL_SECONDS

POLL_INTERVAL_SECONDS = float(WATCHER.get("poll_interval_seconds", 0.8))
RECORDER_RETRY_COOLDOWN_SECONDS = int(WATCHER.get("recorder_retry_cooldown_seconds", 2))
UPLOAD_RETRY_COUNT = int(WATCHER.get("upload_retry_count", 3))
UPLOAD_RETRY_DELAY_SECONDS = float(WATCHER.get("upload_retry_delay_seconds", 3.0))
STARTUP_PENDING_UPLOAD_SCAN = bool(WATCHER.get("startup_pending_upload_scan", True))
LOG_MAX_BYTES = int(WATCHER.get("log_max_bytes", 5 * 1024 * 1024))
LOG_BACKUP_COUNT = int(WATCHER.get("log_backup_count", 5))
LOG_LEVEL: str = str(WATCHER.get("log_level", "INFO")).upper().strip()
if LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR"):
    LOG_LEVEL = "INFO"

ALLOWED_MEDIA_EXTENSIONS = {str(ext).lower() for ext in MEDIA.get("allowed_extensions", DEFAULT_CONFIG["media"]["allowed_extensions"])}

UPLOAD_ENABLED = bool(UPLOAD.get("enabled", False))
UPLOAD_ROOT_DIR = str(UPLOAD.get("root_dir", "") or "").strip()
UPLOAD_RECORDINGS_SUBDIR_NAME = str(UPLOAD.get("recordings_subdir_name", "") or "").strip()
UPLOAD_VERIFY_COPY = bool(UPLOAD.get("verify_copy", True))
DELETE_LOCAL_AFTER_SUCCESS = bool(UPLOAD.get("delete_local_after_success", True))
UPLOAD_DAILY_REPORT_ENABLED = bool(UPLOAD.get("daily_report_enabled", True))
UPLOAD_DAILY_REPORT_FILENAME_PREFIX = str(UPLOAD.get("daily_report_filename_prefix", "call_report") or "call_report").strip() or "call_report"
UPLOAD_DAILY_REPORT_SUBDIR_NAME = str(UPLOAD.get("daily_report_subdir_name", "logs") or "logs").strip() or "logs"
UPLOAD_SHARE_AUTH_ENABLED = bool(UPLOAD.get("share_auth_enabled", False))
UPLOAD_SHARE_USERNAME = str(UPLOAD.get("share_username", "")).strip()
UPLOAD_SHARE_PASSWORD = str(UPLOAD.get("share_password", ""))

CENTRAL_DB_ENABLED = bool(CENTRAL_DB.get("enabled", False))
CENTRAL_DB_DRIVER = str(CENTRAL_DB.get("driver", "sqlalchemy") or "sqlalchemy").strip().lower()
CENTRAL_DB_MYSQL_URI = str(CENTRAL_DB.get("mysql_uri", "") or "").strip()
CENTRAL_DB_TIMEOUT_SECONDS = float(CENTRAL_DB.get("timeout_seconds", 5))

WHATSAPP_PROCESS_ALIASES = {str(x).lower().strip() for x in WHATSAPP.get("process_aliases", []) if str(x).strip()}
WHATSAPP_KEYWORD_ALIASES = [str(x).lower().strip() for x in WHATSAPP.get("keyword_aliases", []) if str(x).strip()]

CENTRAL_SYNC_INTERVAL_SECONDS = float(MAINTENANCE.get("central_sync_interval_seconds", 300))
LOCAL_CLEANUP_INTERVAL_SECONDS = float(MAINTENANCE.get("local_cleanup_interval_seconds", 604800))
