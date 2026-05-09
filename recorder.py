from __future__ import annotations

import collections
import copy
import faulthandler
import json
import logging
import math
import os
import shutil
import struct
import subprocess
import threading
import time
import wave
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import pyaudiowpatch as pyaudio
import comtypes
import comtypes.client

from config import (
    BASE_DIR,
    BANDICAM_PATH,
    RECORDER_OUTPUT_DIR,
    RECORDER_FORMAT,
    RECORDER_SAMPLE_RATE,
    RECORDER_CHANNELS,
    RECORDER_BIT_DEPTH,
    RECORDER_CHUNK_SIZE,
    RECORDER_MP3_BITRATE,
    RECORDER_STOP_SETTLE_SECONDS,
    RECORDER_HEALTH_CHECK_INTERVAL_SECONDS,
    RECORDER_DEVICE_RETRY_COUNT,
    RECORDER_DEVICE_RETRY_DELAY_SECONDS,
    RECORDER_WATCHDOG_RECOVERY_ATTEMPTS,
    RECORDER_POLLING_FALLBACK_INTERVAL_SECONDS,
    RECORDER_DEBUG_STEMS,
    RECORDER_MIC_GAIN,
    RECORDER_LOOPBACK_GAIN,
    RECORDER_BACKEND,
    RECORDER_HELPER_PATH,
    RECORDER_HELPER_STARTUP_TIMEOUT,
    RECORDER_HELPER_STOP_TIMEOUT,
    RECORDER_HELPER_KEEP_TEMP,
    RECORDER_FFMPEG_PATH,
    RECORDER_KEEP_WAV_AFTER_MP3,
)

log = logging.getLogger("watcher.recorder")


@dataclass
class RecordingContext:
    pre_start_snapshot: Set[str]
    start_marker: Optional[float]
    started_at: Optional[datetime]
    output_dir: Optional[str] = None
    segment_index: int = 1
    output_path: Optional[str] = None


# CLSID of MMDeviceEnumerator (Windows Core Audio)
_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"


class DeviceManager:
    """
    Selects the best Windows audio input device and watches for plug/unplug
    events. Uses pyaudiowpatch (PortAudio) for enumeration and the Windows
    MMAPI IMMNotificationClient for live device-change events. If COM
    registration fails it falls back to polling.

    Failure codes:
      DEV-001  PyAudio / IMMDeviceEnumerator init failed
      DEV-002  Default-comm-device query returned nothing (warning)
      DEV-003  Zero input devices found on this machine
      DEV-004  IMMNotificationClient registration failed (polling fallback)
      DEV-CHG  Device-change event observed (informational)
    """

    BUILTIN_KEYWORDS = ("built-in", "internal", "realtek", "conexant", "idt", "laptop")
    HEADSET_KEYWORDS = ("headset", "headphone", "earphone")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pa: Optional[Any] = None
        self._enumerator: Optional[Any] = None
        self._notification_client: Optional[Any] = None
        self._use_polling_fallback: bool = False
        self._polling_thread: Optional[threading.Thread] = None
        self._polling_stop = threading.Event()
        self._change_callback: Optional[Callable[[], None]] = None
        self._last_comm_index: int = -1
        self._init_pyaudio()

    # ── public API ───────────────────────────────────────────────────────────

    @staticmethod
    def score_device(name: str, host_api_name: str, is_comm_default: bool) -> int:
        """Pure-function scoring for an input device. Higher = preferred."""
        score = 0
        name_lower = (name or "").lower()
        host_lower = (host_api_name or "").lower()
        if is_comm_default:
            score += 4
        if "usb" in name_lower or "wasapi" in host_lower:
            score += 3
        if any(word in name_lower for word in DeviceManager.HEADSET_KEYWORDS):
            score += 2
        if "bluetooth" in name_lower or " bt " in name_lower:
            score += 1
        if any(word in name_lower for word in DeviceManager.BUILTIN_KEYWORDS):
            score -= 3
        return score

    def list_input_devices(self) -> List[Dict[str, Any]]:
        with self._lock:
            pa = self._pa
        if pa is None:
            return []
        try:
            count = int(pa.get_device_count())
        except Exception:
            log.exception("[DEV-003] device count query failed")
            return []

        devices: List[Dict[str, Any]] = []
        for i in range(count):
            try:
                info = pa.get_device_info_by_index(i)
            except Exception:
                log.exception("[DEV-003] device info query failed | index=%s", i)
                continue
            try:
                if int(info.get("maxInputChannels", 0)) <= 0:
                    continue
                devices.append({
                    "index": int(info.get("index", i)),
                    "name": str(info.get("name", "")),
                    "host_api_name": self._safe_host_api_name(info.get("hostApi"), pa),
                    "max_input_channels": int(info.get("maxInputChannels", 0)),
                    "default_sample_rate": float(info.get("defaultSampleRate", 0) or 0),
                })
            except Exception:
                log.exception("[DEV-003] device info parse failed | index=%s", i)
                continue
        return devices

    def get_default_comm_device_index(self) -> Optional[int]:
        with self._lock:
            pa = self._pa
        if pa is None:
            return None
        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            idx = int(wasapi_info.get("defaultInputDevice", -1))
            if idx >= 0:
                return idx
        except Exception:
            log.exception("[DEV-002] WASAPI comm-device query failed — falling back to score list")
            return None
        log.warning("[DEV-002] No comm device found — falling back to score list")
        return None

    def select_best_device(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            devices = self.list_input_devices()
            if not devices:
                log.error("[DEV-003] No audio input devices found")
                return None

            comm_idx = self.get_default_comm_device_index()
            for dev in devices:
                is_comm = (comm_idx is not None and dev["index"] == comm_idx)
                dev["is_comm_default"] = is_comm
                dev["score"] = self.score_device(
                    dev["name"], dev["host_api_name"], is_comm
                )

            devices.sort(key=lambda d: (d["score"], 1 if d.get("is_comm_default") else 0), reverse=True)
            best = devices[0]
            self._last_comm_index = comm_idx if comm_idx is not None else -1
            log.info(
                "REC → selected device: %s | score=%s | api=%s | index=%s",
                best["name"], best["score"], best["host_api_name"] or "?", best["index"],
            )
            return best

    def list_real_mic_devices(self) -> List[Dict[str, Any]]:
        """
        Like list_input_devices() but excludes WASAPI [Loopback] entries.
        Use this for mic selection. Loopback discovery stays in _open_loopback_stream().
        """
        devices = self.list_input_devices()
        real: List[Dict[str, Any]] = []
        for dev in devices:
            name = dev.get("name", "")
            if _is_loopback_device_name(name):
                log.info(
                    "REC → excluding loopback device from mic candidates | name=%s", name
                )
                continue
            real.append(dev)
        return real

    def select_best_mic_device(self) -> Optional[Dict[str, Any]]:
        """
        Select the highest-scoring *real* microphone.
        Returns None when no real mic exists (caller handles loopback-only mode).
        Never returns a [Loopback] device.

        Priority (descending):
          USB/headset comm-default > USB headset > headset > built-in (valid fallback)
        """
        with self._lock:
            devices = self.list_real_mic_devices()
            if not devices:
                return None

            comm_idx = self.get_default_comm_device_index()
            for dev in devices:
                is_comm = (comm_idx is not None and dev["index"] == comm_idx)
                dev["is_comm_default"] = is_comm
                dev["score"] = self.score_device(
                    dev["name"], dev["host_api_name"], is_comm
                )

            devices.sort(
                key=lambda d: (d["score"], 1 if d.get("is_comm_default") else 0),
                reverse=True,
            )
            best = devices[0]
            self._last_comm_index = comm_idx if comm_idx is not None else -1
            log.info(
                "REC → real mic selected | device=%s | score=%s | api=%s | index=%s",
                best["name"], best["score"], best["host_api_name"] or "?", best["index"],
            )
            return best

    def get_fresh_usb_mic_name_if_missing(self, live_names: Set[str]) -> Optional[str]:
        """
        Spin up a temporary fresh PyAudio instance and enumerate input devices.
        Return the name of the first USB/headset real mic that is visible in the
        fresh instance but absent from live_names, or None.
        Also returns the first non-loopback real mic if no USB/headset is found
        but any real mic is missing from the live list (built-in fallback case).
        Logs the mismatch if found.
        """
        try:
            pa_fresh = pyaudio.PyAudio()
            count = pa_fresh.get_device_count()
            usb_result: Optional[str] = None
            any_result: Optional[str] = None
            for i in range(count):
                try:
                    d = pa_fresh.get_device_info_by_index(i)
                    if int(d.get("maxInputChannels", 0)) <= 0:
                        continue
                    name = str(d.get("name", ""))
                    if _is_loopback_device_name(name):
                        continue
                    if name in live_names:
                        continue
                    # Found a real mic not visible in live DeviceManager
                    name_lower = name.lower()
                    if usb_result is None and (
                        "usb" in name_lower
                        or any(kw in name_lower for kw in self.HEADSET_KEYWORDS)
                    ):
                        usb_result = name
                    if any_result is None:
                        any_result = name
                except Exception:
                    continue
            pa_fresh.terminate()
            result = usb_result or any_result
            if result:
                log.info(
                    "REC → fresh-vs-live PA mismatch | fresh sees mic not in live list"
                    " | device=%s", result,
                )
            return result
        except Exception:
            log.exception("REC → fresh PA snapshot failed")
            return None

    def register_change_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback fired whenever the device topology changes."""
        with self._lock:
            self._change_callback = callback
        self._register_notification_client(callback)

    def notify_device_change(self, reason: str = "") -> None:
        """Public entry-point used by the IMM callback or by tests/polling."""
        log.info("DEV-CHG → device topology change | reason=%s", reason or "external")
        cb: Optional[Callable[[], None]]
        with self._lock:
            cb = self._change_callback
        if cb is None:
            return
        try:
            cb()
        except Exception:
            log.exception("[DEV-CHG] change callback raised")

    def stop(self) -> None:
        """Tear down COM registration, polling thread, PyAudio."""
        self._polling_stop.set()
        thread = self._polling_thread
        if thread is not None:
            thread.join(timeout=1.0)
            self._polling_thread = None

        enumerator = self._enumerator
        client = self._notification_client
        if enumerator is not None and client is not None:
            try:
                enumerator.UnregisterEndpointNotificationCallback(client)
            except Exception:
                log.exception("[DEV-001] Unregister notification callback failed")
        self._enumerator = None
        self._notification_client = None

        pa = self._pa
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                log.exception("[DEV-001] PyAudio terminate failed")
        self._pa = None

    # ── internals ────────────────────────────────────────────────────────────

    def _init_pyaudio(self) -> None:
        try:
            self._pa = pyaudio.PyAudio()
        except Exception:
            log.exception("[DEV-001] PyAudio init failed")
            self._pa = None

    def reinit_pyaudio(self) -> None:
        """
        Terminate and reinitialize PyAudio. Resets PortAudio internal state.
        Call at the start of each recording to clear stale stream state.
        """
        with self._lock:
            pa = self._pa
            self._pa = None
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass
        self._init_pyaudio()
        log.info("DEV → PyAudio reinitialized for new call")

    def _safe_host_api_name(self, host_api_index: Any, pa: Any = None) -> str:
        if pa is None:
            with self._lock:
                pa = self._pa
        if pa is None or host_api_index is None:
            return ""
        try:
            api_info = pa.get_host_api_info_by_index(int(host_api_index))
            return str(api_info.get("name", ""))
        except Exception:
            return ""

    def _register_notification_client(self, callback: Callable[[], None]) -> None:
        try:
            clsid = comtypes.GUID(_CLSID_MMDeviceEnumerator)
            self._enumerator = comtypes.CoCreateInstance(clsid)
            self._notification_client = self._build_notification_client(callback)
            self._enumerator.RegisterEndpointNotificationCallback(self._notification_client)
            log.info("DEV → IMMNotificationClient registered")
        except Exception:
            log.exception(
                "[DEV-004] Notification client failed — polling fallback active (%.1fs interval)",
                RECORDER_POLLING_FALLBACK_INTERVAL_SECONDS,
            )
            self._use_polling_fallback = True
            self._start_polling_thread(callback)

    def _build_notification_client(self, callback: Callable[[], None]) -> Any:
        """
        Build a thin object holding the change callback. The production COM
        subclass of IMMNotificationClient is wired in via comtypes when the
        MMDevAPILib type-library has been generated; here we keep a plain
        Python proxy so registration succeeds in tests, and the real COM
        callback path is added when the audio capture engine lands.
        """
        outer = self

        class _NotificationProxy:
            def OnDefaultDeviceChanged(self, flow, role, pwstrDefaultDeviceId):
                if role == 1:  # eCommunications
                    outer.notify_device_change("default-comm-changed")
                return getattr(comtypes, "S_OK", 0)

            def OnDeviceAdded(self, pwstrDeviceId):
                outer.notify_device_change("device-added")
                return getattr(comtypes, "S_OK", 0)

            def OnDeviceRemoved(self, pwstrDeviceId):
                outer.notify_device_change("device-removed")
                return getattr(comtypes, "S_OK", 0)

            def OnDeviceStateChanged(self, pwstrDeviceId, dwNewState):
                outer.notify_device_change("device-state-changed")
                return getattr(comtypes, "S_OK", 0)

            def OnPropertyValueChanged(self, pwstrDeviceId, key):
                return getattr(comtypes, "S_OK", 0)

        return _NotificationProxy()

    def _start_polling_thread(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._polling_thread is not None:
                return
            self._polling_stop.clear()
            self._polling_thread = threading.Thread(
                target=self._polling_loop,
                args=(callback,),
                daemon=True,
                name="DeviceManagerPolling",
            )
            self._polling_thread.start()

    def _polling_loop(self, callback: Callable[[], None]) -> None:
        interval = max(0.1, float(RECORDER_POLLING_FALLBACK_INTERVAL_SECONDS))
        while not self._polling_stop.wait(interval):
            try:
                current = self.get_default_comm_device_index()
                with self._lock:
                    last = self._last_comm_index
                if current is not None and current != last:
                    with self._lock:
                        self._last_comm_index = current
                    log.info("DEV-CHG → polling detected comm-index change | %s → %s", last, current)
                    try:
                        callback()
                    except Exception:
                        log.exception("[DEV-CHG] change callback raised in polling loop")
            except Exception:
                log.exception("[DEV-004] polling loop iteration failed")


# ─────────────────────────────────────────────────────────────────────────────
# Module-level audio helper — used by both _SourceReader and CaptureEngine.
# ─────────────────────────────────────────────────────────────────────────────

def _resample_audio(data: bytes, from_rate: int, to_rate: int) -> bytes:
    """Nearest-neighbour resample of mono 16-bit PCM. Returns bytes."""
    if from_rate == to_rate or not data:
        return data
    samples = struct.unpack(f"<{len(data) // 2}h", data)
    ratio = to_rate / from_rate
    new_len = int(len(samples) * ratio)
    if new_len == 0:
        return b""
    resampled = [
        samples[min(int(i / ratio), len(samples) - 1)]
        for i in range(new_len)
    ]
    return struct.pack(f"<{new_len}h", *resampled)


def _is_loopback_device_name(name: str) -> bool:
    """True when this device is a WASAPI loopback capture — never a real mic."""
    return "[loopback]" in (name or "").lower()


# ─────────────────────────────────────────────────────────────────────────────
# _JitterBuffer — small FIFO audio frame queue for one source.
#
# push():  if full, drop OLDEST and log throttled (REC-014).  The oldest frame
#          is staler than the newest so it is the right one to discard.
#
# pop_or_hold():  returns (frame, status) where status is one of:
#   'ok'      — real frame dequeued FIFO (oldest first)
#   'hold'    — buffer empty but source is online; caller gets last_frame back
#   'offline' — source is offline; caller gets silence
#
# Buffer depth of 4 frames = 80 ms at 20 ms/chunk.  This absorbs GIL jitter
# without adding perceptible latency on a phone call.
# ─────────────────────────────────────────────────────────────────────────────

class _JitterBuffer:

    _DROP_LOG_THROTTLE = 1.0

    def __init__(self, maxlen: int, source_name: str) -> None:
        self._lock = threading.Lock()
        self._buf: collections.deque = collections.deque()
        self._maxlen = maxlen
        self._name = source_name
        self._last_drop_log: float = 0.0

    def push(self, frame: bytes) -> None:
        with self._lock:
            if len(self._buf) >= self._maxlen:
                self._buf.popleft()  # drop oldest to make room
                now = time.monotonic()
                if now - self._last_drop_log >= self._DROP_LOG_THROTTLE:
                    log.warning(
                        "[REC-014] %s queue overflow — dropping oldest frame",
                        self._name,
                    )
                    self._last_drop_log = now
            self._buf.append(frame)

    def pop_or_hold(
        self,
        source_online: bool,
        last_frame: bytes,
        silence: bytes,
    ) -> "tuple[bytes, str]":
        """FIFO pop with hold-last and offline-silence fallbacks."""
        with self._lock:
            if self._buf:
                return self._buf.popleft(), "ok"
        if source_online:
            return last_frame, "hold"
        return silence, "offline"

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)


# ─────────────────────────────────────────────────────────────────────────────
# _SourceReader — owns the timed non-blocking read for ONE audio source.
#
# Runs in its own daemon thread.  Each source tick (= one chunk period):
#   1. Poll stream.get_read_available() every 2 ms up to the full chunk budget.
#   2. If frames available: read chunk_size frames immediately (no blocking).
#   3. If still unavailable after budget: push explicit silence for that tick
#      and increment underflow counter (logged throttled).
#   4. Downmix stereo→mono (MicReader uses channel-mode selection; LoopbackReader
#      uses simple average).  Resample if source rate ≠ mix rate.
#   5. Push one normalised mono frame to _JitterBuffer every tick.
#
# If get_read_available() is unsupported, falls back to a single blocking read.
# Blocking reads that exceed 100 ms repeatedly mark the source offline so the
# writer fills silence until the reconnect path restores the stream.
#
# Never writes WAV files.  Only the _AudioWriter does that.
# ─────────────────────────────────────────────────────────────────────────────

class _SourceReader(threading.Thread):

    _UNDERFLOW_LOG_THROTTLE = 4.0   # seconds between underflow log lines
    _POLL_INTERVAL_S        = 0.002  # 2 ms per get_read_available() poll
    _MAX_BLOCKING_LAG_MS    = 100.0  # ms; blocking reads slower than this count as lag
    _MAX_CONSECUTIVE_LAG    = 5      # consecutive slow reads before marking offline

    def __init__(
        self,
        name: str,
        stop_event: threading.Event,
        chunk_size: int,
        sample_width: int,
        source_channels: int,
        source_rate: int,
        mix_rate: int,
        frame_buf: "_JitterBuffer",
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._stop_event = stop_event
        self._chunk_size = chunk_size
        self._sample_width = sample_width
        self._source_channels = source_channels
        self._source_rate = source_rate
        self._mix_rate = mix_rate
        self._frame_buf = frame_buf
        self._lock = threading.Lock()
        self._stream: Optional[Any] = None
        self._online: bool = False
        self._chunk_bytes: int = chunk_size * sample_width
        self._block_seconds: float = chunk_size / max(1, mix_rate)
        self._source_block_seconds: float = chunk_size / max(1, source_rate)
        # max polls before declaring underflow for this tick
        self._max_polls: int = max(1, int(self._source_block_seconds / self._POLL_INTERVAL_S))

        # Non-blocking read state
        self._get_avail_unsupported: bool = False
        self._avail_unsupported_logged: bool = False
        self._consecutive_lag: int = 0   # consecutive slow blocking reads
        self._underflow_ticks: int = 0
        self._underflow_log_ts: float = 0.0

        # Whether this reader is the mic (True) vs loopback (False)
        self._is_mic: bool = (name == "MicReader")

        # Mic channel-mode state (MicReader only)
        self._mic_ch_mode: str = "average"
        self._mic_ch_mode_logged: str = ""
        self._mic_ch_L2: float = 0.0   # running sum of L^2
        self._mic_ch_R2: float = 0.0
        self._mic_ch_LR: float = 0.0   # running sum of L*R
        self._mic_ch_n:  int   = 0     # sample count in window
        self._mic_ch_window_start: float = 0.0

    # ── stream lifecycle ──────────────────────────────────────────────────────

    def set_stream(self, stream: Any, source_channels: int, source_rate: int) -> None:
        """Inject a new live stream. May be called from any thread."""
        with self._lock:
            self._stream = stream
            self._source_channels = source_channels
            self._source_rate = source_rate
            self._online = True

    def go_offline(self) -> None:
        """
        Mark offline and unblock any pending stream.read() via stop_stream().
        Safe to call from any thread, including while run() is blocked.
        """
        with self._lock:
            old_stream = self._stream
            self._stream = None
            self._online = False
        if old_stream is not None:
            try:
                old_stream.stop_stream()
            except Exception:
                pass

    @property
    def is_online(self) -> bool:
        with self._lock:
            return self._online and self._stream is not None

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("REC → %s started", self.name)
        silence_raw = b"\x00" * self._chunk_bytes
        self._mic_ch_window_start = time.monotonic()

        while not self._stop_event.is_set():
            with self._lock:
                stream = self._stream
                online = self._online
                src_ch = self._source_channels
                src_rate = self._source_rate

            if not online or stream is None:
                self._stop_event.wait(self._block_seconds)
                continue

            iter_start = time.monotonic()

            # ── non-blocking poll for available frames ────────────────────────
            raw: Optional[bytes] = None

            if not self._get_avail_unsupported:
                for _poll in range(self._max_polls):
                    try:
                        avail = stream.get_read_available()
                    except Exception:
                        if not self._avail_unsupported_logged:
                            log.info(
                                "REC → get_read_available unsupported;"
                                " using blocking read fallback | source=%s",
                                self.name,
                            )
                            self._avail_unsupported_logged = True
                        self._get_avail_unsupported = True
                        break
                    if avail >= self._chunk_size:
                        break  # enough frames — proceed to read below
                    if self._stop_event.wait(self._POLL_INTERVAL_S):
                        return  # stop requested during poll
                else:
                    # Poll budget exhausted — push explicit silence for this tick
                    self._underflow_ticks += 1
                    now_uf = time.monotonic()
                    if now_uf - self._underflow_log_ts >= self._UNDERFLOW_LOG_THROTTLE:
                        log.info(
                            "REC → source underflow | source=%s | unavailable_ticks=%d",
                            self.name, self._underflow_ticks,
                        )
                        self._underflow_log_ts = now_uf
                    self._frame_buf.push(silence_raw)
                    elapsed = time.monotonic() - iter_start
                    sleep_for = self._source_block_seconds - elapsed
                    if sleep_for > 0.001:
                        self._stop_event.wait(sleep_for)
                    continue  # skip the read entirely

            # ── actual stream read (should return immediately if avail checked) ─
            read_start = time.monotonic()
            try:
                raw = stream.read(self._chunk_size, exception_on_overflow=False)
            except OSError:
                if self._stop_event.is_set():
                    break
                log.warning("REC → %s read error — source offline", self.name)
                with self._lock:
                    self._online = False
                    self._stream = None
                continue
            except Exception:
                if self._stop_event.is_set():
                    break
                log.exception("REC → %s read failed — source offline", self.name)
                with self._lock:
                    self._online = False
                    self._stream = None
                continue

            # ── blocking-read lag guard (fallback path only) ─────────────────
            if self._get_avail_unsupported:
                read_ms = (time.monotonic() - read_start) * 1000
                if read_ms > self._MAX_BLOCKING_LAG_MS:
                    self._consecutive_lag += 1
                    if self._consecutive_lag >= self._MAX_CONSECUTIVE_LAG:
                        log.warning(
                            "REC → %s blocking read repeatedly slow"
                            " (%.0f ms) — marking offline",
                            self.name, read_ms,
                        )
                        with self._lock:
                            self._online = False
                            self._stream = None
                        self._consecutive_lag = 0
                        continue
                else:
                    self._consecutive_lag = 0

            # ── validate byte count ───────────────────────────────────────────
            expected = self._chunk_size * src_ch * self._sample_width
            if len(raw) < expected:
                raw = raw + b"\x00" * (expected - len(raw))
            elif len(raw) > expected:
                raw = raw[:expected]

            # ── stereo→mono downmix ───────────────────────────────────────────
            if src_ch == 2:
                samples = struct.unpack(f"<{len(raw) // 2}h", raw)
                if len(samples) % 2 != 0:
                    samples = samples[:-1]

                if self._is_mic:
                    # ── mic channel-mode selection ────────────────────────────
                    L_raw = samples[0::2]
                    R_raw = samples[1::2]
                    n_smp = len(L_raw)
                    self._mic_ch_L2 += sum(int(v) * int(v) for v in L_raw)
                    self._mic_ch_R2 += sum(int(v) * int(v) for v in R_raw)
                    self._mic_ch_LR += sum(int(L_raw[i]) * int(R_raw[i]) for i in range(n_smp))
                    self._mic_ch_n  += n_smp

                    now_mc = time.monotonic()
                    if now_mc - self._mic_ch_window_start >= 1.0 and self._mic_ch_n > 0:
                        l_rms = math.sqrt(self._mic_ch_L2 / self._mic_ch_n)
                        r_rms = math.sqrt(self._mic_ch_R2 / self._mic_ch_n)
                        cov   = self._mic_ch_LR / self._mic_ch_n
                        corr  = cov / (l_rms * r_rms) if (l_rms > 0 and r_rms > 0) else 0.0
                        mx, mn = max(l_rms, r_rms), min(l_rms, r_rms)
                        if mx > 0 and mn / mx < 0.25:
                            new_mode = "left" if l_rms >= r_rms else "right"
                        elif corr >= 0.5:
                            new_mode = "average"
                        else:
                            new_mode = "left" if l_rms >= r_rms else "right"
                        if new_mode != self._mic_ch_mode_logged:
                            log.info(
                                "REC → mic channel mode | mode=%s"
                                " | L_rms=%.1f | R_rms=%.1f | corr=%.3f",
                                new_mode, l_rms, r_rms, corr,
                            )
                            self._mic_ch_mode_logged = new_mode
                        self._mic_ch_mode = new_mode
                        self._mic_ch_L2 = self._mic_ch_R2 = self._mic_ch_LR = 0.0
                        self._mic_ch_n  = 0
                        self._mic_ch_window_start = now_mc

                    mode = self._mic_ch_mode
                    if mode == "left":
                        mono = [int(samples[i]) for i in range(0, len(samples), 2)]
                    elif mode == "right":
                        mono = [int(samples[i + 1]) for i in range(0, len(samples), 2)]
                    else:
                        mono = [
                            max(-32768, min(32767, (int(samples[i]) + int(samples[i + 1])) // 2))
                            for i in range(0, len(samples), 2)
                        ]
                else:
                    # loopback — simple average
                    mono = [
                        max(-32768, min(32767, (int(samples[i]) + int(samples[i + 1])) // 2))
                        for i in range(0, len(samples), 2)
                    ]

                raw = struct.pack(f"<{len(mono)}h", *mono)

            # ── resample ──────────────────────────────────────────────────────
            if src_rate != self._mix_rate:
                raw = _resample_audio(raw, src_rate, self._mix_rate)

            # ── normalise to exact chunk_bytes ────────────────────────────────
            if len(raw) < self._chunk_bytes:
                raw = raw + b"\x00" * (self._chunk_bytes - len(raw))
            elif len(raw) > self._chunk_bytes:
                raw = raw[:self._chunk_bytes]

            # ── push to jitter buffer ─────────────────────────────────────────
            self._frame_buf.push(raw)

            # ── pace: sleep any remaining time in this source tick ────────────
            elapsed = time.monotonic() - iter_start
            sleep_for = self._source_block_seconds - elapsed
            if sleep_for > 0.001:
                self._stop_event.wait(sleep_for)

        log.info("REC → %s exiting", self.name)


# ─────────────────────────────────────────────────────────────────────────────
# _AudioWriter — consumes frames from two bounded queues, applies gain, mixes,
# and writes one chunk per wall-clock slot to the WAV file.
#
# Uses absolute time.monotonic() scheduling so the WAV grows at wall-clock
# rate regardless of source timing.  Never calls stream.read().
#
# Also owns debug stem WAV writes and silence/level logging.
# ─────────────────────────────────────────────────────────────────────────────

class _AudioWriter(threading.Thread):

    _LEVEL_LOG_INTERVAL = 4.0    # seconds between RMS level log lines
    _SILENCE_RMS_THRESHOLD = 10  # RMS below this counts as silence

    _MAX_HOLD = 1  # frames before hold-last gives up and writes silence

    def __init__(
        self,
        stop_event: threading.Event,
        lb_buf: "_JitterBuffer",
        mic_buf: "_JitterBuffer",
        wav_file: wave.Wave_write,
        chunk_size: int,
        sample_width: int,
        mix_rate: int,
        mic_gain: float,
        loopback_gain: float,
        debug_stems: bool,
        mic_debug_wav: Optional[wave.Wave_write],
        loopback_debug_wav: Optional[wave.Wave_write],
        lb_reader: "_SourceReader",
        mic_reader: "_SourceReader",
        on_bytes_written: Callable[[int], None],
        reconnect_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(name="CaptureEngineWriter", daemon=True)
        self._stop_event = stop_event
        self._lb_buf = lb_buf
        self._mic_buf = mic_buf
        self._wav_file = wav_file
        self._chunk_size = chunk_size
        self._sample_width = sample_width
        self._mix_rate = mix_rate
        self._mic_gain = float(mic_gain)
        self._loopback_gain = float(loopback_gain)
        self._debug_stems = debug_stems
        self._mic_debug_wav = mic_debug_wav
        self._loopback_debug_wav = loopback_debug_wav
        self._lb_reader = lb_reader
        self._mic_reader = mic_reader
        self._on_bytes_written = on_bytes_written
        self._reconnect_fn = reconnect_fn
        self._chunk_bytes: int = chunk_size * sample_width
        self._block_seconds: float = chunk_size / max(1, mix_rate)

        # Level logging accumulators
        self._level_timer: float = 0.0
        self._mic_sq: float = 0.0
        self._lb_sq: float = 0.0
        self._mix_sq: float = 0.0
        self._level_frames: int = 0
        self._clipped: int = 0

        # Silence detection state
        self._silence_buf: bytearray = bytearray()
        self._silent_secs: int = 0

        # Hold-last underrun tracking (one per source)
        self._lb_hold: int = 0
        self._mic_hold: int = 0
        self._lb_underrun_log: float = 0.0
        self._mic_underrun_log: float = 0.0

    def run(self) -> None:
        log.info("REC → writer started | mix_rate=%sHz | mic_gain=%.2f | loopback_gain=%.2f",
                 self._mix_rate, self._mic_gain, self._loopback_gain)
        silence = b"\x00" * self._chunk_bytes
        block_seconds = self._block_seconds
        count = self._chunk_bytes // 2  # int16 samples per chunk

        # Last valid frames (used for hold-last on brief underrun)
        lb_last  = silence
        mic_last = silence

        next_tick = time.monotonic() + block_seconds  # give readers one tick head-start
        self._level_timer = time.monotonic()

        try:
            while not self._stop_event.is_set():
                # ── absolute scheduling ───────────────────────────────────────
                now = time.monotonic()
                wait = next_tick - now
                if wait > 0:
                    if self._stop_event.wait(wait):
                        break
                elif (now - next_tick) * 1000 > 10:
                    log.warning(
                        "[REC-013] writer lag | behind_ms=%.1f",
                        (now - next_tick) * 1000,
                    )
                    next_tick = time.monotonic() + block_seconds

                if self._stop_event.is_set():
                    break

                lb_online  = self._lb_reader.is_online
                mic_online = self._mic_reader.is_online

                # ── FIFO pop with hold-last on brief underrun ─────────────────
                lb_data,  lb_status  = self._lb_buf.pop_or_hold(lb_online,  lb_last,  silence)
                mic_data, mic_status = self._mic_buf.pop_or_hold(mic_online, mic_last, silence)

                if lb_status == "ok":
                    lb_last = lb_data
                    self._lb_hold = 0
                elif lb_status == "hold":
                    self._lb_hold += 1
                    if self._lb_hold > self._MAX_HOLD:
                        lb_data = silence  # give up holding — silence is safer
                        now2 = time.monotonic()
                        if now2 - self._lb_underrun_log >= 2.0:
                            log.info(
                                "REC → source underrun | source=loopback"
                                " | hold_ticks=%d", self._lb_hold,
                            )
                            self._lb_underrun_log = now2

                if mic_status == "ok":
                    mic_last = mic_data
                    self._mic_hold = 0
                elif mic_status == "hold":
                    self._mic_hold += 1
                    if self._mic_hold > self._MAX_HOLD:
                        mic_data = silence
                        now2 = time.monotonic()
                        if now2 - self._mic_underrun_log >= 2.0:
                            log.info(
                                "REC → source underrun | source=mic"
                                " | hold_ticks=%d", self._mic_hold,
                            )
                            self._mic_underrun_log = now2

                # ── reconnect trigger when either source is offline ───────────
                if (
                    not self._stop_event.is_set()
                    and (not lb_online or not mic_online)
                    and self._reconnect_fn is not None
                ):
                    self._reconnect_fn()  # internally throttled to 2s

                # ── debug stems (exact frames used for mixing) ────────────────
                if self._debug_stems:
                    if self._loopback_debug_wav is not None:
                        try:
                            self._loopback_debug_wav.writeframesraw(lb_data)
                        except Exception:
                            pass
                    if self._mic_debug_wav is not None:
                        try:
                            self._mic_debug_wav.writeframesraw(mic_data)
                        except Exception:
                            pass

                # ── gain-mix: float/int32 intermediate, clamp to int16 ────────
                lb_s = struct.unpack(f"<{count}h", lb_data[:count * 2])
                mic_s = struct.unpack(f"<{count}h", mic_data[:count * 2])
                mixed: List[int] = []
                clipped_chunk = 0
                for i in range(count):
                    val = int(lb_s[i] * self._loopback_gain + mic_s[i] * self._mic_gain)
                    if val > 32767:
                        val = 32767
                        clipped_chunk += 1
                    elif val < -32768:
                        val = -32768
                        clipped_chunk += 1
                    mixed.append(val)
                self._clipped += clipped_chunk

                write_data = struct.pack(f"<{count}h", *mixed)

                # ── accumulate RMS for level log ──────────────────────────────
                self._lb_sq += sum(s * s for s in lb_s)
                self._mic_sq += sum(s * s for s in mic_s)
                self._mix_sq += sum(s * s for s in mixed)
                self._level_frames += count

                if time.monotonic() - self._level_timer >= self._LEVEL_LOG_INTERVAL:
                    self._do_level_log()

                # ── WAV write (writeframesraw: close() finalises header) ──────
                try:
                    self._wav_file.writeframesraw(write_data)
                    self._on_bytes_written(len(write_data))
                except Exception:
                    log.exception("[REC-005] WAV write error — writer exiting")
                    break

                # ── silence detection ─────────────────────────────────────────
                self._silence_buf.extend(write_data)
                check_size = self._mix_rate * 3 * 2
                if len(self._silence_buf) >= check_size:
                    smp = struct.unpack(
                        f"<{check_size // 2}h",
                        bytes(self._silence_buf[:check_size]),
                    )
                    rms = math.sqrt(sum(s * s for s in smp) / len(smp)) if smp else 0.0
                    if rms < self._SILENCE_RMS_THRESHOLD:
                        self._silent_secs += 3
                        if self._silent_secs >= 6:
                            log.warning(
                                "[REC-009] Recording silent | rms=%.1f | silent=%ds"
                                " | lb=%s mic=%s",
                                rms, self._silent_secs,
                                "ON" if self._lb_reader.is_online else "OFF",
                                "ON" if self._mic_reader.is_online else "OFF",
                            )
                    else:
                        if self._silent_secs > 0:
                            log.info("REC → audio detected | rms=%.1f", rms)
                        self._silent_secs = 0
                    self._silence_buf = bytearray()

                # ── advance absolute tick ─────────────────────────────────────
                next_tick += block_seconds

        finally:
            log.info("REC → writer exiting")

    def _do_level_log(self) -> None:
        frames = self._level_frames
        if frames > 0:
            mic_rms = math.sqrt(self._mic_sq / frames)
            lb_rms = math.sqrt(self._lb_sq / frames)
            mix_rms = math.sqrt(self._mix_sq / frames)
        else:
            mic_rms = lb_rms = mix_rms = 0.0
        log.info(
            "REC → levels | mic_rms=%.1f | loopback_rms=%.1f | mixed_rms=%.1f"
            " | clipped=%d | mic_online=%s | loopback_online=%s",
            mic_rms, lb_rms, mix_rms, self._clipped,
            "Y" if self._mic_reader.is_online else "N",
            "Y" if self._lb_reader.is_online else "N",
        )
        self._mic_sq = 0.0
        self._lb_sq = 0.0
        self._mix_sq = 0.0
        self._level_frames = 0
        self._clipped = 0
        self._level_timer = time.monotonic()


# ─────────────────────────────────────────────────────────────────────────────
# CaptureEngine — owns PyAudio streams, the WAV file, and three capture
# threads: LoopbackReader, MicReader, and Writer/Mixer.
#
# Threading model:
#   _SourceReader "LoopbackReader" — blocking loopback stream.read() only
#   _SourceReader "MicReader"      — blocking mic stream.read() only
#   _AudioWriter  "CaptureEngineWriter" — wall-clock-scheduled WAV writes;
#                                          never calls stream.read()
#   _watchdog_thread               — monitors writer, triggers recovery
#
# Failure codes used here:
#   REC-002  stream open failed on a specific device (warning, retried)
#   REC-003  all devices in fallback chain exhausted (error)
#   REC-004  record thread / stream tear-down failure (error)
#   REC-005  WAV write error mid-recording (error)
#   REC-006  WAV finalization failed (error)
#   REC-007  MP3 conversion failed (warning, WAV is kept)
#   REC-008  failed to open WAV file for writing (error)
#   REC-014  source reader queue overflow — oldest frame dropped (warning)
#   HLT-001  watchdog detected dead writer thread, stopping readers (warning)
#   HLT-002  recovery attempts exhausted (error)
# ─────────────────────────────────────────────────────────────────────────────


class CaptureEngine:
    """
    Live audio capture: PyAudio stream → WAV file.

    Threading model:
      * `_record_thread`   — drains the stream and writes WAV frames.
      * `_watchdog_thread` — every `_watchdog_interval` seconds, checks the
        record thread and triggers recovery if it died (HLT-001/002).
      * Both threads are daemon. `stop()` joins them with a bounded timeout.

    Locking:
      `_lock` (RLock) guards every mutable attribute. The record loop holds the
      lock only briefly to keep stream reads
      from being blocked by `stop()` or recovery.
    """

    def __init__(
        self,
        device_manager: DeviceManager,
        sample_rate: Optional[int] = None,
        channels: Optional[int] = None,
        chunk_size: Optional[int] = None,
        bit_depth: Optional[int] = None,
        watchdog_interval: Optional[float] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._device_manager = device_manager
        self._sample_rate = int(sample_rate or RECORDER_SAMPLE_RATE)
        self._channels = int(channels or RECORDER_CHANNELS)
        self._chunk_size = int(chunk_size or RECORDER_CHUNK_SIZE)
        self._bit_depth = int(bit_depth or RECORDER_BIT_DEPTH)
        self._sample_width = max(1, self._bit_depth // 8)
        self._watchdog_interval = float(
            watchdog_interval if watchdog_interval is not None
            else RECORDER_HEALTH_CHECK_INTERVAL_SECONDS
        )

        self._stream: Optional[Any] = None       # mic input stream
        self._loopback_stream: Optional[Any] = None  # WASAPI loopback stream
        self._mic_stream: Optional[Any] = None       # alias set after mic opens
        self._loopback_rate: int = self._sample_rate
        self._loopback_channels: int = 1
        self._mix_rate: int = self._sample_rate  # final WAV framerate
        self._wav_file: Optional[wave.Wave_write] = None
        self._output_path: Optional[str] = None
        self._device: Optional[Dict[str, Any]] = None
        self._device_name: str = ""
        self._bytes_written: int = 0

        self._stop_event = threading.Event()
        self._watchdog_stop = threading.Event()
        self._record_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._thread_started_at: float = 0.0

        self._actual_sample_rate: int = self._sample_rate  # set when stream opens

        self._tried_devices: List[str] = []
        self._recovery_attempts: int = 0
        self._recovery_exhausted: bool = False

        self._silence_buffer: bytearray = bytearray()
        self._silent_secs: int = 0
        self._record_started_monotonic: float = 0.0

        self._reconnect_lock = threading.Lock()
        self._reconnect_thread: Optional[threading.Thread] = None
        self._last_reconnect_attempt_ts: float = 0.0
        self._reconnect_disabled: bool = False  # set True by stop() before stop_event

        # Actual channel count the mic stream was opened with (1 or 2).
        # Set by _open_stream(); independent of the output WAV channel count.
        self._mic_channels: int = 1

        # Debug stem WAV files — written only when config debug_stems=true.
        # Never uploaded, never indexed in DB, not auto-deleted.
        self._debug_stems: bool = RECORDER_DEBUG_STEMS
        self._mic_debug_wav: Optional[wave.Wave_write] = None
        self._loopback_debug_wav: Optional[wave.Wave_write] = None
        self._mic_debug_path: Optional[str] = None
        self._loopback_debug_path: Optional[str] = None

        # Three-thread capture: LoopbackReader + MicReader + Writer
        self._lb_buf: _JitterBuffer = _JitterBuffer(maxlen=4, source_name="LoopbackReader")
        self._mic_buf: _JitterBuffer = _JitterBuffer(maxlen=4, source_name="MicReader")
        self._lb_reader: Optional[_SourceReader] = None
        self._mic_reader: Optional[_SourceReader] = None

        # USB reconnect fail counter — reset each call start
        self._mic_reconnect_fail_count: int = 0

    # ── public API ───────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        with self._lock:
            t = self._record_thread
            return t is not None and t.is_alive()

    @property
    def output_path(self) -> Optional[str]:
        with self._lock:
            return self._output_path

    @property
    def device_name(self) -> str:
        with self._lock:
            return self._device_name

    @property
    def bytes_written(self) -> int:
        with self._lock:
            return self._bytes_written

    @property
    def recovery_exhausted(self) -> bool:
        with self._lock:
            return self._recovery_exhausted

    def start(self, output_path: str, device: Optional[Dict[str, Any]]) -> bool:
        """Open WAV + dual streams (loopback + mic), launch record + watchdog threads."""
        with self._lock:
            if self._record_thread is not None and self._record_thread.is_alive():
                log.warning("CaptureEngine.start() ignored — already recording: %s",
                            self._output_path)
                return False

            self._stop_event.clear()
            self._reconnect_disabled = False
            self._device_manager.reinit_pyaudio()
            self._watchdog_stop.clear()
            self._tried_devices = []
            self._recovery_attempts = 0
            self._recovery_exhausted = False
            self._bytes_written = 0
            self._device = device
            self._device_name = str(device.get("name", "?")) if device else ""
            self._mic_channels = 1
            self._mic_debug_wav = None
            self._loopback_debug_wav = None
            self._mic_debug_path = None
            self._loopback_debug_path = None
            self._lb_reader = None
            self._mic_reader = None
            self._lb_buf = _JitterBuffer(maxlen=4, source_name="LoopbackReader")
            self._mic_buf = _JitterBuffer(maxlen=4, source_name="MicReader")
            self._mic_reconnect_fail_count = 0

            # Open WASAPI loopback (captures incoming voice / system audio)
            self._loopback_stream = self._open_loopback_stream()

            # Open microphone (outgoing voice) using existing rate-fallback logic
            if device:
                if self._open_stream(device):
                    self._mic_stream = self._stream
                else:
                    self._mic_stream = None
            else:
                self._mic_stream = None

            # Cannot record without at least one active stream
            if self._loopback_stream is None and self._mic_stream is None:
                if not device:
                    log.error("[REC-001] No audio input device available — recording skipped")
                else:
                    log.error("[REC-003] Both loopback and microphone streams failed")
                return False

            # Prefer loopback rate for the mix; fall back to actual mic rate
            if self._loopback_stream is not None:
                self._mix_rate = self._loopback_rate
            else:
                self._mix_rate = self._actual_sample_rate

            if not self._open_wav(output_path):
                for s in (self._loopback_stream, self._stream):
                    if s is not None:
                        try:
                            s.stop_stream()
                            s.close()
                        except Exception:
                            pass
                self._loopback_stream = None
                self._mic_stream = None
                self._stream = None
                return False

            # Correct WAV header to reflect the actual mix rate
            if self._mix_rate != self._sample_rate and self._wav_file is not None:
                self._wav_file.setframerate(self._mix_rate)
                log.info(
                    "REC → WAV framerate corrected | actual=%sHz | config=%sHz",
                    self._mix_rate, self._sample_rate,
                )

            # Open per-stem debug WAV files (mix_rate now final).
            # These are diagnostic only: not uploaded, no DB rows, not auto-deleted.
            if self._debug_stems:
                stem = str(Path(output_path).with_suffix(""))
                self._mic_debug_path = stem + "_mic_debug.wav"
                self._loopback_debug_path = stem + "_loopback_debug.wav"
                self._mic_debug_wav = self._open_debug_wav(self._mic_debug_path)
                self._loopback_debug_wav = self._open_debug_wav(self._loopback_debug_path)

            streams_active = []
            if self._loopback_stream:
                streams_active.append("LOOPBACK(incoming)")
            if self._mic_stream:
                streams_active.append("MIC(outgoing)")
            log.info(
                "REC → dual capture active | streams=%s | mix_rate=%sHz",
                "+".join(streams_active), self._mix_rate,
            )

            # ── Create source reader threads ──────────────────────────────────
            lb_reader = _SourceReader(
                name="LoopbackReader",
                stop_event=self._stop_event,
                chunk_size=self._chunk_size,
                sample_width=self._sample_width,
                source_channels=self._loopback_channels,
                source_rate=self._loopback_rate,
                mix_rate=self._mix_rate,
                frame_buf=self._lb_buf,
            )
            if self._loopback_stream is not None:
                lb_reader.set_stream(
                    self._loopback_stream,
                    self._loopback_channels,
                    self._loopback_rate,
                )
            self._lb_reader = lb_reader

            mic_reader = _SourceReader(
                name="MicReader",
                stop_event=self._stop_event,
                chunk_size=self._chunk_size,
                sample_width=self._sample_width,
                source_channels=self._mic_channels,
                source_rate=self._actual_sample_rate,
                mix_rate=self._mix_rate,
                frame_buf=self._mic_buf,
            )
            if self._mic_stream is not None:
                mic_reader.set_stream(
                    self._mic_stream,
                    self._mic_channels,
                    self._actual_sample_rate,
                )
            self._mic_reader = mic_reader

            # ── Create writer thread ──────────────────────────────────────────
            writer = _AudioWriter(
                stop_event=self._stop_event,
                lb_buf=self._lb_buf,
                mic_buf=self._mic_buf,
                wav_file=self._wav_file,
                chunk_size=self._chunk_size,
                sample_width=self._sample_width,
                mix_rate=self._mix_rate,
                mic_gain=RECORDER_MIC_GAIN,
                loopback_gain=RECORDER_LOOPBACK_GAIN,
                debug_stems=self._debug_stems,
                mic_debug_wav=self._mic_debug_wav,
                loopback_debug_wav=self._loopback_debug_wav,
                lb_reader=lb_reader,
                mic_reader=mic_reader,
                on_bytes_written=self._add_bytes_written,
                reconnect_fn=self._try_reconnect_streams_async,
            )

            # ── Start all three threads ───────────────────────────────────────
            self._record_started_monotonic = time.monotonic()
            self._thread_started_at = time.time()
            lb_reader.start()
            mic_reader.start()
            writer.start()
            self._record_thread = writer  # watchdog monitors the writer

            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                daemon=True,
                name="CaptureEngineWatchdog",
            )
            self._watchdog_thread.start()

            log.info("REC → recording started | file=%s | device=%s",
                     output_path, self._device_name)
            return True

    def stop(self) -> bool:
        """Signal stop, then: go_offline → stop_stream → join readers → join writer → close."""
        with self._lock:
            record_thread = self._record_thread   # writer thread
            watchdog_thread = self._watchdog_thread
            lb_reader = self._lb_reader
            mic_reader = self._mic_reader
            stream = self._stream
            loopback_stream = self._loopback_stream
            output_path = self._output_path
            if (record_thread is None and watchdog_thread is None
                    and stream is None and loopback_stream is None
                    and self._wav_file is None and lb_reader is None):
                return False
            # Disable reconnect before signalling stop so any in-flight
            # reconnect thread bails out before opening new streams.
            self._reconnect_disabled = True
            self._record_thread = None
            self._watchdog_thread = None
            self._lb_reader = None
            self._mic_reader = None
            self._stream = None
            self._mic_stream = None
            self._loopback_stream = None

        # 1. Signal stop to all threads
        self._stop_event.set()
        self._watchdog_stop.set()

        # 2. Unblock any pending blocking stream.read() inside reader threads.
        #    go_offline() calls stop_stream() and nulls the reader's stream ref.
        for reader in (lb_reader, mic_reader):
            if reader is not None:
                reader.go_offline()

        # Belt-and-suspenders: also stop_stream() via our own refs.
        for s in (loopback_stream, stream):
            if s is not None:
                try:
                    s.stop_stream()
                except Exception:
                    pass

        # 3. Join readers first (they unblock quickly after go_offline)
        for reader in (lb_reader, mic_reader):
            if reader is not None:
                reader.join(timeout=2.0)
                if reader.is_alive():
                    log.warning("[REC-004] %s still alive after 2.0s", reader.name)

        # 4. Join writer — it may still flush the last partial second of queued frames
        if record_thread is not None:
            record_thread.join(timeout=5.0)
            if record_thread.is_alive():
                log.warning(
                    "[REC-004] Writer thread still alive after 5.0s | path=%s",
                    output_path,
                )

        # 5. Join watchdog
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=2.0)

        # 6. close() streams AFTER all threads confirmed dead
        for s in (loopback_stream, stream):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

        # 7. Finalize WAV (writer's finally block does this too; this is the safety net)
        with self._lock:
            self._finalize_wav()

        log.info("REC → stop dispatched | path=%s | bytes=%s",
                 output_path, self._bytes_written)
        return True

    def request_device_switch(self) -> None:
        """
        Request a switch to the best available device mid-recording.
        Closes mic stream → record thread gets OSError → exits.
        Watchdog detects dead thread → selects best device → restarts.
        Resets recovery counters — device switch is not a fault.
        Safe to call from any thread.
        """
        with self._lock:
            if not self.is_active:
                return
            self._recovery_attempts = 0
            self._recovery_exhausted = False
            stream = self._stream
            self._stream = None
            self._mic_stream = None

        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                log.exception("[REC-005] Device switch: stream close failed")

        log.info(
            "REC → device switch requested | current=%s"
            " | watchdog will restart on best device",
            self._device_name,
        )

    def on_usb_disconnect(self) -> None:
        """
        Called immediately when the USB audio device is removed.
        Marks both source readers offline so their blocking stream.read() is
        unblocked via stop_stream().  Writer continues filling silence.
        Does NOT call close() — the OS WASAPI topology has already changed;
        close() can access-violate on a removed device.
        """
        with self._lock:
            lb = self._loopback_stream
            mic = self._stream
            lb_reader = self._lb_reader
            mic_reader = self._mic_reader
            self._loopback_stream = None
            self._stream = None
            self._mic_stream = None

        # go_offline() calls stop_stream() inside — unblocks blocking reads
        if lb_reader is not None:
            lb_reader.go_offline()
        if mic_reader is not None:
            mic_reader.go_offline()

        # Belt-and-suspenders: also attempt stop_stream() via our captured refs
        for s in (lb, mic):
            if s is not None:
                try:
                    s.stop_stream()
                except Exception:
                    pass

        log.info(
            "REC → USB disconnect handled | streams detached"
            " | writer fills silence until reconnect"
        )

    def _try_reconnect_streams_async(self) -> None:
        """
        Starts a non-blocking daemon thread to reopen streams after USB
        device return.  If a reconnect thread is already running, does nothing.
        Returns immediately if stop/finalization is in progress.
        """
        if self._reconnect_disabled or self._stop_event.is_set():
            return
        now = time.monotonic()
        with self._reconnect_lock:
            if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
                return
            # Throttle: don't start a new attempt within 2 s of the last one
            if now - self._last_reconnect_attempt_ts < 2.0:
                return
            self._last_reconnect_attempt_ts = now
            t = threading.Thread(
                target=self._try_reconnect_streams,
                daemon=True,
                name="CaptureEngineReconnect",
            )
            self._reconnect_thread = t
        t.start()

    def _try_reconnect_streams(self) -> None:
        """
        Runs in a daemon thread.  Reinitializes PyAudio and reopens loopback
        and/or mic streams that were nulled by on_usb_disconnect().
        """
        try:
            # 1. Early bail if recorder is stopping
            if self._stop_event.is_set():
                return

            # 2. Under lock: check stop and determine what is actually missing.
            #    Prefer reader.is_online because a reader can mark itself offline
            #    on a read failure while CaptureEngine may still hold the old
            #    stream reference.  Fall back to stream refs when no reader exists.
            with self._lock:
                if self._stop_event.is_set():
                    return
                lb_reader_ref = self._lb_reader
                mic_reader_ref = self._mic_reader
                need_lb = (
                    not lb_reader_ref.is_online
                    if lb_reader_ref is not None
                    else self._loopback_stream is None
                )
                need_mic = (
                    not mic_reader_ref.is_online
                    if mic_reader_ref is not None
                    else self._stream is None
                )

            if not need_lb and not need_mic:
                log.info("REC → reconnect skipped; streams already active")
                return

            # 3. Only reinit when BOTH streams are missing.
            #    Reinitializing with one stream still alive can invalidate it,
            #    causing the record loop to read from a broken stream handle.
            if self._stop_event.is_set():
                return
            if need_lb and need_mic:
                log.info("REC → reconnect thread: both streams missing — reinitializing PyAudio")
                self._device_manager.reinit_pyaudio()
                # 4. Stop check after reinit
                if self._stop_event.is_set():
                    return
            else:
                log.info(
                    "REC → reconnect thread: one stream alive — skipping reinit"
                    " | need_lb=%s | need_mic=%s", need_lb, need_mic,
                )

            if need_lb:
                new_lb = self._open_loopback_stream()
                if new_lb:
                    assigned = False
                    with self._lock:
                        if self._stop_event.is_set():
                            pass
                        elif self._loopback_stream is None:
                            self._loopback_stream = new_lb
                            self._mix_rate = self._loopback_rate
                            assigned = True
                        else:
                            pass  # Another path already assigned a stream
                    if not assigned:
                        try:
                            new_lb.stop_stream()
                            new_lb.close()
                        except Exception:
                            pass
                        if self._stop_event.is_set():
                            return
                    else:
                        # Inject new stream into the loopback reader thread
                        with self._lock:
                            lb_reader = self._lb_reader
                        if lb_reader is not None:
                            lb_reader.set_stream(new_lb, self._loopback_channels, self._loopback_rate)
                        log.info("REC → loopback stream reopened after reconnect")
                else:
                    log.warning(
                        "REC → loopback reopen failed — writer continues with silence"
                    )

            if need_mic:
                if self._stop_event.is_set() or self._reconnect_disabled:
                    return
                device = self._device_manager.select_best_mic_device()
                if device is None:
                    self._mic_reconnect_fail_count += 1
                    log.info(
                        "REC → no real mic in live PA | fail_count=%d"
                        " — checking fresh PA snapshot",
                        self._mic_reconnect_fail_count,
                    )
                    # Evidence-based reinit: only reinit if fresh PA sees a mic
                    # that the live instance misses (proves USB was replugged).
                    live_names = {
                        d["name"]
                        for d in self._device_manager.list_real_mic_devices()
                    }
                    fresh_name = self._device_manager.get_fresh_usb_mic_name_if_missing(
                        live_names
                    )
                    if fresh_name is not None and not self._stop_event.is_set():
                        log.info(
                            "REC → USB replug confirmed via PA mismatch | device=%s"
                            " | forcing PyAudio reinit", fresh_name,
                        )
                        # Signal lb_reader offline to unblock any pending read
                        with self._lock:
                            lb_reader_ref2 = self._lb_reader
                            self._loopback_stream = None
                        if lb_reader_ref2 is not None:
                            lb_reader_ref2.go_offline()

                        self._device_manager.reinit_pyaudio()

                        if not self._stop_event.is_set():
                            new_lb2 = self._open_loopback_stream()
                            if new_lb2 is not None:
                                with self._lock:
                                    if not self._stop_event.is_set():
                                        self._loopback_stream = new_lb2
                                if not self._stop_event.is_set() and lb_reader_ref2 is not None:
                                    lb_reader_ref2.set_stream(
                                        new_lb2,
                                        self._loopback_channels,
                                        self._loopback_rate,
                                    )
                                    log.info("REC → loopback reinjected after USB-replug reinit")
                                else:
                                    try:
                                        new_lb2.stop_stream()
                                        new_lb2.close()
                                    except Exception:
                                        pass

                            if not self._stop_event.is_set():
                                device = self._device_manager.select_best_mic_device()
                                if device is None:
                                    log.warning(
                                        "REC → reconnect skipped; no real mic even after"
                                        " reinit — mic stays offline"
                                    )
                    else:
                        log.info(
                            "REC → fresh PA also sees no new mic | mic stays offline"
                        )
                if device is None:
                    pass  # mic stays offline — fall through
                else:
                    dev_name = str(device.get("name", "?"))
                    with self._lock:
                        self._device = device
                        self._device_name = dev_name
                    opened = self._open_stream(device)  # sets self._stream on success
                    if opened:
                        discard_stream = None
                        new_mic_stream = None
                        mic_reader_local = None
                        with self._lock:
                            if self._stop_event.is_set() or self._reconnect_disabled:
                                # Stop raced us — close the freshly opened stream
                                discard_stream = self._stream
                                self._stream = None
                                self._mic_stream = None
                            else:
                                self._mic_stream = self._stream
                                new_mic_stream = self._stream
                                mic_reader_local = self._mic_reader
                        if discard_stream is not None:
                            try:
                                discard_stream.stop_stream()
                                discard_stream.close()
                            except Exception:
                                pass
                            return
                        # Inject new stream into the running mic reader thread
                        if mic_reader_local is not None and new_mic_stream is not None:
                            mic_reader_local.set_stream(
                                new_mic_stream, self._mic_channels, self._actual_sample_rate
                            )
                        # Classify restored device for logging
                        dev_lower = dev_name.lower()
                        if "usb" in dev_lower or any(
                            kw in dev_lower for kw in DeviceManager.HEADSET_KEYWORDS
                        ):
                            log.info(
                                "REC → USB/headset mic restored | device=%s", dev_name
                            )
                        else:
                            log.info(
                                "REC → using built-in microphone fallback | device=%s",
                                dev_name,
                            )
                    else:
                        log.warning(
                            "REC → mic reopen failed — writer continues with silence"
                        )

        except Exception:
            log.exception("REC → reconnect thread failed")

    def convert_to_mp3(self, wav_path: str) -> Optional[str]:
        """Public wrapper — used by Recorder.resolve_final_files()."""
        return self._convert_to_mp3(wav_path)

    # ── internal: stream / WAV lifecycle ─────────────────────────────────────

    def _pa_format(self) -> int:
        width = self._sample_width
        if width == 1:
            return getattr(pyaudio, "paInt8", 16)
        if width == 3:
            return getattr(pyaudio, "paInt24", 4)
        if width == 4:
            return getattr(pyaudio, "paInt32", 2)
        return getattr(pyaudio, "paInt16", 8)

    def _open_wav(self, path: str) -> bool:
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.isdir(path):
                # Reject early — wave.open() on a directory leaks a half-built
                # Wave_write whose __del__ raises AttributeError later.
                raise OSError(21, f"Is a directory: {path}")
            wav_file = wave.open(path, 'wb')
            wav_file.setnchannels(self._channels)
            wav_file.setsampwidth(self._sample_width)
            wav_file.setframerate(self._sample_rate)
            self._wav_file = wav_file
            self._output_path = path
            self._bytes_written = 0
            log.info("REC → WAV opened | path=%s | %sHz %sch %s-bit",
                     path, self._sample_rate, self._channels, self._bit_depth)
            return True
        except OSError:
            log.exception("[REC-008] Failed to open WAV file for writing | path=%s", path)
            self._wav_file = None
            return False

    def _open_stream(self, device: Dict[str, Any]) -> bool:
        pa = self._device_manager._pa
        if pa is None:
            log.error("[REC-001] PyAudio not initialized — cannot open stream")
            return False

        device_name = str(device.get("name", "?"))
        device_index = device.get("index")
        api_name = device.get("host_api_name", "?")
        retry_count = max(1, RECORDER_DEVICE_RETRY_COUNT)
        delay = max(0.0, RECORDER_DEVICE_RETRY_DELAY_SECONDS)

        # Query the device's native sample rate and max input channels BEFORE
        # opening, so we can try the hardware's preferred channel count first.
        native_rate = self._sample_rate
        max_input_channels = 1
        if device_index is not None:
            try:
                info = pa.get_device_info_by_index(device_index)
                queried = int(info.get("defaultSampleRate", 0) or 0)
                if queried > 0:
                    native_rate = queried
                max_input_channels = int(info.get("maxInputChannels", 1) or 1)
            except Exception:
                pass

        # Prefer opening the mic as stereo (2ch) when the device supports it.
        # We downmix to mono in the record loop, matching the loopback path.
        # If 2ch open fails, fall back to mono (1ch).
        channels_to_try = [2, 1] if max_input_channels >= 2 else [1]

        # Build a prioritised, deduped list of sample rates to try.
        rates_to_try = list(dict.fromkeys([
            self._sample_rate,
            native_rate,
            48000,
            44100,
            22050,
            16000,
            8000,
        ]))

        for rate in rates_to_try:
            if self._stop_event.is_set():
                break

            for ch in channels_to_try:
                attempt = 0
                while attempt < retry_count:
                    attempt += 1
                    try:
                        stream = pa.open(
                            format=self._pa_format(),
                            channels=ch,
                            rate=rate,
                            input=True,
                            input_device_index=device_index,
                            frames_per_buffer=self._chunk_size,
                        )
                        self._stream = stream
                        self._mic_channels = ch
                        self._actual_sample_rate = rate
                        if device_name not in self._tried_devices:
                            self._tried_devices.append(device_name)
                        log.info(
                            "REC → stream opened | rate=%sHz | channels=%s | device=%s | api=%s",
                            rate, ch, device_name, api_name,
                        )
                        if ch == 1 and max_input_channels >= 2:
                            log.info(
                                "REC → mic 2-ch open failed for rate=%sHz"
                                " — opened 1-ch fallback | device=%s",
                                rate, device_name,
                            )
                        if rate != self._sample_rate:
                            log.info(
                                "REC → config rate=%sHz not supported, using %sHz (device native)",
                                self._sample_rate, rate,
                            )

                        return True

                    except OSError as exc:
                        errno_val = exc.args[0] if exc.args else 0
                        if errno_val == -9997:
                            log.warning(
                                "REC → rate %sHz ch=%s rejected (invalid sample rate)"
                                " | device=%s — trying next",
                                rate, ch, device_name,
                            )
                            break  # skip to next ch (or next rate when ch list exhausted)
                        if errno_val == -9992:
                            log.warning(
                                "[REC-002] PortAudio insufficient memory (errno -9992)"
                                " on %s — reinitializing PyAudio and retrying",
                                device_name,
                            )
                            self._device_manager.reinit_pyaudio()
                            pa = self._device_manager._pa
                            if pa is None:
                                break
                            continue
                        log.exception(
                            "[REC-002] [attempt %s/%s] Stream open failed: %s",
                            attempt, retry_count, device_name,
                        )
                        if attempt < retry_count and delay > 0:
                            if self._stop_event.wait(delay):
                                break
                    except Exception:
                        log.exception(
                            "[REC-002] [attempt %s/%s] Stream open failed: %s",
                            attempt, retry_count, device_name,
                        )
                        if attempt < retry_count and delay > 0:
                            if self._stop_event.wait(delay):
                                break

        if device_name not in self._tried_devices:
            self._tried_devices.append(device_name)
        log.error(
            "[REC-003] All devices and rates exhausted — tried=%s — call saved without audio",
            self._tried_devices,
        )
        self._stream = None
        return False

    def _open_loopback_stream(self) -> Optional[Any]:
        """
        Find and open the WASAPI loopback device for the USB audio output.
        pyaudiowpatch exposes these as input devices named '[Loopback]'.
        Captures everything playing through speakers = remote party voice.
        """
        pa = self._device_manager._pa
        if pa is None:
            return None
        try:
            count = pa.get_device_count()
            loopback_devices = []

            for i in range(count):
                try:
                    info = pa.get_device_info_by_index(i)
                    name = info.get("name", "")
                    max_in = int(info.get("maxInputChannels", 0))
                    api_idx = int(info.get("hostApi", 0))
                    api_name = pa.get_host_api_info_by_index(api_idx).get("name", "")

                    if "[Loopback]" in name and max_in > 0 and "WASAPI" in api_name:
                        loopback_devices.append({
                            "index": i,
                            "name": name,
                            "rate": int(info.get("defaultSampleRate", 48000)),
                            "channels": 2 if max_in >= 2 else 1,
                        })
                except Exception:
                    continue

            if not loopback_devices:
                log.warning(
                    "REC → no [Loopback] devices found — "
                    "pyaudiowpatch may not be installed correctly"
                )
                return None

            # Prefer the USB Audio Device loopback; fall back to first found
            preferred = next(
                (d for d in loopback_devices if "USB" in d["name"]),
                loopback_devices[0],
            )

            log.info(
                "REC → loopback candidates: %s | selected: %s (index=%d)",
                [d["name"] for d in loopback_devices],
                preferred["name"], preferred["index"],
            )

            stream = pa.open(
                format=pyaudio.paInt16,
                channels=preferred["channels"],
                rate=preferred["rate"],
                input=True,
                input_device_index=preferred["index"],
                frames_per_buffer=self._chunk_size,
            )

            self._loopback_rate = preferred["rate"]
            self._loopback_channels = preferred["channels"]

            log.info(
                "REC → loopback stream opened | device=%s | index=%d"
                " | rate=%sHz | channels=%s",
                preferred["name"], preferred["index"],
                preferred["rate"], preferred["channels"],
            )
            return stream

        except Exception:
            log.exception("[REC-002] Loopback stream failed — will use microphone only")
            return None

    def _open_debug_wav(self, path: str) -> Optional[wave.Wave_write]:
        """Open a mono 16-bit WAV for per-stem debug output. Errors are non-fatal."""
        try:
            w = wave.open(path, 'wb')
            w.setnchannels(1)
            w.setsampwidth(self._sample_width)
            w.setframerate(self._mix_rate)
            log.info("REC → debug stem opened | path=%s", path)
            return w
        except Exception:
            log.exception("REC → debug stem open failed | path=%s", path)
            return None

    @staticmethod
    def _resample(data: bytes, from_rate: int, to_rate: int) -> bytes:
        """Nearest-neighbour resample — delegates to module-level helper."""
        return _resample_audio(data, from_rate, to_rate)

    def _finalize_wav(self) -> None:
        wav = self._wav_file
        if wav is None:
            return
        try:
            wav.close()
            log.info("REC → WAV finalized | path=%s | bytes=%s",
                     self._output_path, self._bytes_written)
        except Exception:
            log.exception("[REC-006] WAV finalize failed | path=%s | bytes=%s",
                          self._output_path, self._bytes_written)
        finally:
            self._wav_file = None

        # Close debug stems alongside the main WAV
        for dbg_wav in (self._mic_debug_wav, self._loopback_debug_wav):
            if dbg_wav is not None:
                try:
                    dbg_wav.close()
                except Exception:
                    pass
        self._mic_debug_wav = None
        self._loopback_debug_wav = None

    def _log_duration_check(self) -> None:
        """Log a ratio of WAV duration vs wall-clock duration. Warns on mismatch."""
        try:
            if self._record_started_monotonic <= 0:
                return
            wall_duration = time.monotonic() - self._record_started_monotonic
            if wall_duration <= 0:
                return
            sample_bytes = max(1, self._sample_width * self._channels)
            wav_frames = self._bytes_written // sample_bytes
            wav_duration = wav_frames / self._mix_rate if self._mix_rate > 0 else 0.0
            ratio = wav_duration / wall_duration
            log.info(
                "REC → duration check | wall=%.1fs | wav=%.1fs | ratio=%.2f",
                wall_duration, wav_duration, ratio,
            )
            if not (0.90 <= ratio <= 1.10):
                log.warning(
                    "[REC-010] WAV duration mismatch | wall=%.1fs | wav=%.1fs | ratio=%.2f",
                    wall_duration, wav_duration, ratio,
                )
        except Exception:
            log.exception("[REC-010] Duration check failed")

    # ── internal: watchdog thread ─────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self._watchdog_interval):
            try:
                with self._lock:
                    thread = self._record_thread
                    started_at = self._thread_started_at
                    exhausted = self._recovery_exhausted
                if exhausted:
                    continue
                if thread is None:
                    continue
                if not thread.is_alive():
                    uptime = int(time.time() - started_at) if started_at else 0
                    log.warning(
                        "[HLT-001] Watchdog: record thread dead | uptime=%ss",
                        uptime,
                    )
                    self._trigger_recovery()
            except Exception:
                log.exception("[HLT-001] Watchdog iteration failed")

    def _trigger_recovery(self) -> None:
        """
        Called by watchdog when the writer thread dies.  In the three-thread
        model the WAV is already finalized by the writer's finally block, so we
        cannot resume writing to it.  Stop the reader threads and mark recovery
        exhausted.  Recorder.ensure_recording_alive() will detect is_active==False
        and create a fresh segment with a new WAV file.
        """
        with self._lock:
            if self._recovery_exhausted:
                return
            max_attempts = max(1, RECORDER_WATCHDOG_RECOVERY_ATTEMPTS)
            self._recovery_attempts += 1
            attempt = self._recovery_attempts
            uptime = int(time.time() - self._thread_started_at) if self._thread_started_at else 0
            if attempt > max_attempts:
                self._recovery_exhausted = True
                log.error(
                    "[HLT-002] Recovery failed — call continues without further recording"
                )
                return

        log.warning(
            "[HLT-001] Writer thread dead | uptime=%ss | attempt %s/%s"
            " | stopping readers — Recorder will open new segment",
            uptime, attempt, max_attempts,
        )

        # Stop reader threads (they are still running, draining to a dead queue)
        self._stop_readers(timeout=2.0)

        # WAV is gone; flag exhausted so is_active returns False and Recorder
        # creates a new segment via ensure_recording_alive() → engine.start()
        with self._lock:
            self._recovery_exhausted = True
        log.warning("[HLT-001] Recovery: readers stopped — waiting for Recorder new segment")

    def _stop_readers(self, timeout: float = 2.0) -> None:
        """Mark both source readers offline and join with a bounded timeout."""
        with self._lock:
            lb_reader = self._lb_reader
            mic_reader = self._mic_reader
            self._lb_reader = None
            self._mic_reader = None
        for reader in (lb_reader, mic_reader):
            if reader is not None:
                reader.go_offline()
        for reader in (lb_reader, mic_reader):
            if reader is not None:
                reader.join(timeout=timeout)
                if reader.is_alive():
                    log.warning(
                        "REC → %s still alive after %.1fs — continuing",
                        reader.name, timeout,
                    )

    def _add_bytes_written(self, n: int) -> None:
        """Thread-safe bytes_written increment — called by _AudioWriter per chunk."""
        with self._lock:
            self._bytes_written += n

    # ── internal: MP3 conversion ─────────────────────────────────────────────

    def _convert_to_mp3(self, wav_path: str) -> Optional[str]:
        mp3_path = str(Path(wav_path).with_suffix(".mp3"))
        try:
            import lameenc
        except ImportError:
            log.exception("[REC-007] MP3 conversion failed — lameenc missing | wav=%s",
                          wav_path)
            return None

        try:
            with wave.open(wav_path, 'rb') as wf:
                rate = wf.getframerate()
                channels = wf.getnchannels()
                frames = wf.readframes(wf.getnframes())

            encoder = lameenc.Encoder()
            encoder.set_bit_rate(int(RECORDER_MP3_BITRATE))
            encoder.set_in_sample_rate(rate)
            encoder.set_channels(channels)
            encoder.set_quality(2)
            mp3_data = encoder.encode(frames)
            mp3_data += encoder.flush()

            with open(mp3_path, "wb") as f:
                f.write(mp3_data)

            log.info("REC → MP3 converted | wav=%s | mp3=%s | size=%s bytes",
                     wav_path, mp3_path, len(mp3_data))
            return mp3_path
        except Exception:
            log.exception("[REC-007] MP3 conversion failed | wav=%s — keeping WAV",
                          wav_path)
            return None


class _HelperIpcBackend:
    """
    Manages a RecorderHelper.exe --ipc subprocess and the JSON IPC protocol.
    One instance per Recorder when backend="helper".
    stdout = JSON events only; stderr = diagnostics drained to log.debug.
    """

    _PING_TIMEOUT  = 2.0
    _START_TIMEOUT = 10.0

    def __init__(
        self,
        exe_path: str,
        startup_timeout: float,
        stop_timeout: float,
        mic_gain: float,
        loopback_gain: float,
        keep_temp: bool,
    ) -> None:
        self._exe_path        = exe_path
        self._startup_timeout = startup_timeout
        self._stop_timeout    = stop_timeout
        self._mic_gain        = mic_gain
        self._loopback_gain   = loopback_gain
        self._keep_temp       = keep_temp

        self._lock  = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None

        self._is_recording: bool         = False
        self._current_path: Optional[str] = None
        self._merged_path:  Optional[str] = None
        self._merge_error:  Optional[str] = None

        self._ready_evt   = threading.Event()
        self._started_evt = threading.Event()
        self._merged_evt  = threading.Event()
        self._stopped_evt = threading.Event()
        self._pong_evt    = threading.Event()

        self._stderr_lines: collections.deque = collections.deque(maxlen=20)

        self._launch()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _launch(self) -> None:
        exe = Path(self._exe_path)
        if not exe.is_absolute():
            exe = (BASE_DIR / exe).resolve()
        if not exe.exists():
            raise FileNotFoundError(f"[RH-001] RecorderHelper not found: {exe}")

        log.info("[RH] APP_DIR=%s", BASE_DIR)
        log.info("[RH] Launching IPC subprocess | exe=%s", exe)

        # Hide the console window on Windows — IPC pipes are unaffected.
        _startupinfo = None
        _creationflags = 0
        if sys.platform == "win32":
            _startupinfo = subprocess.STARTUPINFO()
            _startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            _startupinfo.wShowWindow = 0  # SW_HIDE
            _creationflags = subprocess.CREATE_NO_WINDOW

        self._proc = subprocess.Popen(
            [str(exe), "--ipc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
            startupinfo=_startupinfo,
            creationflags=_creationflags,
        )
        threading.Thread(
            target=self._stdout_reader, daemon=True, name="rh-stdout"
        ).start()
        threading.Thread(
            target=self._stderr_reader, daemon=True, name="rh-stderr"
        ).start()
        if not self._ready_evt.wait(self._startup_timeout):
            self._kill()
            raise TimeoutError(
                f"[RH-001] RecorderHelper did not emit 'ready' within "
                f"{self._startup_timeout}s"
            )
        log.info("[RH] IPC subprocess ready")

    def _kill(self) -> None:
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            log.exception("[RH-005] Terminate failed")

    def shutdown(self) -> None:
        try:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self._kill()
        except Exception:
            log.exception("[RH-005] Shutdown failed")

    # ── commands ──────────────────────────────────────────────────────────────

    def _send(self, cmd: dict) -> None:
        try:
            self._proc.stdin.write(json.dumps(cmd) + "\n")
            self._proc.stdin.flush()
        except Exception:
            log.exception("[RH-002] IPC send failed | cmd=%s", cmd.get("cmd"))

    def start_session(self, output_dir: str, base_name: str) -> bool:
        self._started_evt.clear()
        self._merged_evt.clear()
        self._stopped_evt.clear()
        with self._lock:
            self._merged_path = None
            self._merge_error = None
        self._send({
            "cmd":           "start",
            "output_dir":    output_dir,
            "base_name":     base_name,
            "mic_gain":      self._mic_gain,
            "loopback_gain": self._loopback_gain,
            "keep_temp":     self._keep_temp,
        })
        if not self._started_evt.wait(self._START_TIMEOUT):
            log.error("[RH-002] 'started' event not received within %.0fs",
                      self._START_TIMEOUT)
            return False
        return True

    def stop_session(self) -> None:
        self._send({"cmd": "stop"})

    def force_stop_session(self) -> None:
        self._send({"cmd": "force_stop"})

    def ping(self) -> bool:
        self._pong_evt.clear()
        self._send({"cmd": "ping"})
        return self._pong_evt.wait(self._PING_TIMEOUT)

    def wait_for_merged(self, timeout: float) -> Optional[str]:
        self._merged_evt.wait(timeout)
        with self._lock:
            return self._merged_path

    def wait_for_stopped(self, timeout: float) -> bool:
        return self._stopped_evt.wait(timeout)

    def is_process_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def get_last_stderr(self, n: int = 5) -> List[str]:
        lines = list(self._stderr_lines)
        return lines[-n:] if n < len(lines) else lines

    @property
    def current_path(self) -> Optional[str]:
        with self._lock:
            return self._current_path

    # ── reader threads ────────────────────────────────────────────────────────

    def _stdout_reader(self) -> None:
        try:
            for raw in self._proc.stdout:
                line = raw.rstrip("\n")
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("[RH] Non-JSON on stdout: %.120s", line)
                    continue
                self._dispatch(evt)
        except Exception:
            log.exception("[RH] stdout reader crashed")
        finally:
            # Unblock any waiters if the process dies unexpectedly.
            self._ready_evt.set()
            self._started_evt.set()
            self._merged_evt.set()
            self._stopped_evt.set()
            self._pong_evt.set()

    def _stderr_reader(self) -> None:
        try:
            for raw in self._proc.stderr:
                line = raw.rstrip("\n")
                if line:
                    self._stderr_lines.append(line)
                    log.debug("[RH] %s", line)
        except Exception:
            pass

    def _dispatch(self, evt: dict) -> None:
        name = evt.get("event", "")

        if name == "ready":
            self._ready_evt.set()

        elif name == "started":
            path = evt.get("path")
            with self._lock:
                self._is_recording = True
                self._current_path = path
            self._started_evt.set()
            log.info("[RH] Recording started | segment=%s | path=%s",
                     evt.get("segment"), path)

        elif name == "seg_saved":
            log.info("[RH] Segment saved | segment=%s | path=%s",
                     evt.get("segment"), evt.get("path"))

        elif name == "device_lost":
            log.warning("[RH] Device lost | segment=%s", evt.get("segment"))

        elif name == "device_restored":
            log.info("[RH] Device restored | segment=%s | gap=%.1fs",
                     evt.get("segment"), evt.get("gap_seconds", 0.0))

        elif name == "merged":
            path = evt.get("path")
            with self._lock:
                self._merged_path = path
                self._merge_error = None
            self._merged_evt.set()
            log.info("[RH] Merge complete | path=%s | segments=%s",
                     path, evt.get("segments"))

        elif name == "merge_failed":
            with self._lock:
                self._merged_path = None
                self._merge_error = evt.get("error", "unknown")
            self._merged_evt.set()
            log.error("[RH-004] Merge failed | error=%s | segments=%s",
                      evt.get("error"), evt.get("segments"))

        elif name == "stopped":
            with self._lock:
                self._is_recording = False
                self._current_path = None
            self._stopped_evt.set()
            log.info("[RH] Session stopped")

        elif name == "pong":
            self._pong_evt.set()

        elif name == "error":
            code = evt.get("code", "?")
            msg  = evt.get("message", "")
            log.error("[RH-003] IPC error | code=%s | message=%s", code, msg)
            # Unblock relevant waiters so callers don't hang on error.
            if code in ("already_recording", "bad_params",
                        "device_enum_failed", "no_render_device",
                        "output_dir_error"):
                self._started_evt.set()
            elif code == "capture_exception":
                self._merged_evt.set()
                self._stopped_evt.set()

        else:
            log.debug("[RH] Unknown event: %s", name)


class Recorder:
    """
    Public recording interface. Delegates to CaptureEngine for stream/WAV work.
    Manages call lifecycle: segment numbering, context tracking, format resolution.
    """

    def __init__(self) -> None:
        _crash_log = BASE_DIR / "logs" / "crash.log"
        _crash_log.parent.mkdir(exist_ok=True)
        try:
            faulthandler.enable(
                file=open(str(_crash_log), "a"),
                all_threads=True,
            )
            log.info("STARTUP → faulthandler enabled | crash_log=%s", _crash_log)
        except Exception:
            log.warning("STARTUP → faulthandler could not be enabled")

        log.info(
            "STARTUP → paths | app_dir=%s | helper=%s | ffmpeg=%s",
            BASE_DIR, RECORDER_HELPER_PATH, RECORDER_FFMPEG_PATH,
        )

        self._lock = threading.RLock()

        if RECORDER_BACKEND == "helper":
            self._helper: Optional[_HelperIpcBackend] = _HelperIpcBackend(
                exe_path=RECORDER_HELPER_PATH,
                startup_timeout=RECORDER_HELPER_STARTUP_TIMEOUT,
                stop_timeout=RECORDER_HELPER_STOP_TIMEOUT,
                mic_gain=RECORDER_MIC_GAIN,
                loopback_gain=RECORDER_LOOPBACK_GAIN,
                keep_temp=RECORDER_HELPER_KEEP_TEMP,
            )
            self._device_manager = None
            self._engine = None
            self._usb_watcher_stop = None
            self._usb_watcher_thread = None
        else:
            self._helper = None
            self._device_manager = DeviceManager()
            self._engine = CaptureEngine(self._device_manager)

            # USB hot-swap: monitor for device removal and reconnection
            self._usb_watcher_stop = threading.Event()
            self._usb_watcher_thread = threading.Thread(
                target=self._usb_watcher_loop,
                daemon=True,
                name="USBDeviceWatcher",
            )
            self._usb_watcher_thread.start()

            # Wire IMMNotificationClient callback (previously never called)
            self._device_manager.register_change_callback(
                self._on_device_change
            )

            self._log_all_devices()

        self._is_recording: bool = False
        self._started_at: Optional[datetime] = None
        self._segment_counter: int = 1

        self._active_context: Optional[RecordingContext] = None
        self._completed_contexts: List[RecordingContext] = []

        self._recording_success: bool = False
        self._recording_issues: Optional[str] = None
        self._restart_count: int = 0
        self._last_error_code: Optional[str] = None

        # Mute detection state (informational — never affects recording)
        self._last_mute_check: float = 0.0
        self._device_index: Optional[int] = None
        self._device_name_for_mute: str = ""

        # Compatibility attributes — main.py reads these directly.
        # BANDICAM_PATH points to config.py (always exists) so the truthiness
        # check in main.py passes without triggering the "NOT found" warning.
        self.bandicam_path: Optional[Path] = Path(BANDICAM_PATH)
        self.bandicam_output_dir: Optional[Path] = self._resolve_output_dir()

    # ── properties ───────────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._is_recording

    @property
    def current_recording_path(self) -> Optional[str]:
        if self._helper is not None:
            return self._helper.current_path
        return self._engine.output_path

    @property
    def started_at(self) -> Optional[datetime]:
        with self._lock:
            return self._started_at

    # ── public API ───────────────────────────────────────────────────────────

    def refresh_recorder_paths(self) -> bool:
        try:
            p = Path(RECORDER_OUTPUT_DIR)
            p.mkdir(parents=True, exist_ok=True)
            valid = p.is_dir()
            with self._lock:
                self.bandicam_output_dir = p if valid else None
            return valid
        except OSError:
            log.exception("[REC-008] Output dir not accessible | path=%s", RECORDER_OUTPUT_DIR)
            return False

    refresh_bandicam_paths = refresh_recorder_paths

    def start_recording(self) -> bool:
        with self._lock:
            if self._is_recording:
                return False

        if self._helper is not None:
            return self._start_recording_helper()

        _t0 = time.monotonic()
        output_dir = RECORDER_OUTPUT_DIR

        # Pre-recording disk space check (REC-008)
        try:
            free_mb = shutil.disk_usage(output_dir).free / (1024 * 1024)
            if free_mb < 100:
                log.error("[REC-008] Disk low | free=%sMB", int(free_mb))
                return False
        except Exception:
            log.exception("[REC-008] Disk space check failed | dir=%s", output_dir)

        pre_start_snapshot = self._snapshot_output_dir(output_dir)
        output_path = self._generate_output_path(output_dir)

        # select_best_mic_device() never returns a [Loopback] device.
        # Returns None when no real mic exists — recording still proceeds
        # in loopback-only mode (mic channel writes silence until reconnect).
        device = self._device_manager.select_best_mic_device()

        dev_name_for_log = device.get("name", "?") if device else ""
        had_usb = self._has_usb_wasapi_input_device()

        if device is None:
            log.warning(
                "REC → no real microphone available"
                " — mic source will be silence until reconnect"
            )
        else:
            dev_lower = dev_name_for_log.lower()
            if "usb" in dev_lower or any(
                kw in dev_lower for kw in DeviceManager.HEADSET_KEYWORDS
            ):
                log.info(
                    "[DEV-USB] USB/headset mic selected for recording | device=%s",
                    dev_name_for_log,
                )
            elif any(kw in dev_lower for kw in DeviceManager.BUILTIN_KEYWORDS):
                log.info(
                    "REC → using built-in microphone fallback | device=%s",
                    dev_name_for_log,
                )
            else:
                log.info(
                    "REC → real mic selected | device=%s | api=%s",
                    dev_name_for_log,
                    device.get("host_api_name", "?"),
                )

        _t_engine = time.monotonic()
        if not self._engine.start(output_path, device):
            return False
        _t_engine_elapsed = time.monotonic() - _t_engine

        dev_idx = device.get("index") if device else None
        dev_name = device.get("name", "?") if device else ""

        # Mute check only meaningful when a real mic is selected.
        # UIA traversal can take 20-30s — always in a daemon thread.
        if device is not None:
            threading.Thread(
                target=self._do_mute_check,
                args=(dev_idx, dev_name),
                daemon=True,
                name="mute-check",
            ).start()

        _t_ctx = time.monotonic()
        now = datetime.now()
        with self._lock:
            self._is_recording = True
            self._started_at = now
            self._recording_success = False
            self._recording_issues = None
            self._restart_count = 0
            self._device_index = dev_idx
            self._device_name_for_mute = dev_name
            self._last_mute_check = time.time()
            self._active_context = RecordingContext(
                pre_start_snapshot=pre_start_snapshot,
                start_marker=time.time(),
                started_at=now,
                output_dir=output_dir,
                segment_index=self._segment_counter,
                output_path=output_path,
            )
        _t_ctx_elapsed = time.monotonic() - _t_ctx
        _t_total = time.monotonic() - _t0

        log.info(
            "REC → start timing | total=%.2fs | engine=%.2fs | context=%.2fs",
            _t_total, _t_engine_elapsed, _t_ctx_elapsed,
        )
        for _phase, _phase_elapsed in (("engine", _t_engine_elapsed), ("context", _t_ctx_elapsed)):
            if _phase_elapsed > 2.0:
                log.warning(
                    "[REC-011] Recording start phase slow | phase=%s | elapsed=%.2fs",
                    _phase, _phase_elapsed,
                )
        log.info("REC → recording started | segment=%s | path=%s | device=%s",
                 self._segment_counter, output_path, device.get("name", "?"))
        return True

    def stop_recording(self) -> bool:
        with self._lock:
            if not self._is_recording:
                return False

        if self._helper is not None:
            return self._stop_recording_helper()

        self._engine.stop()

        with self._lock:
            self._is_recording = False
            ctx = self._active_context
            self._active_context = None
            if ctx is not None:
                self._completed_contexts.append(ctx)

        log.info("REC → stop dispatched | segment=%s", self._segment_counter)
        return True

    def force_stop_recording(self) -> bool:
        if self._helper is not None:
            return self._force_stop_recording_helper()

        try:
            self._engine.stop()
        except Exception:
            log.exception("[REC-004] force_stop: engine stop raised")

        with self._lock:
            self._is_recording = False
            ctx = self._active_context
            self._active_context = None
            if ctx is not None:
                self._completed_contexts.append(ctx)

        log.info("REC → force stop | segment=%s", self._segment_counter)
        return True

    def detach_contexts(self) -> List[RecordingContext]:
        with self._lock:
            all_ctx = list(self._completed_contexts)
            if self._active_context is not None:
                all_ctx.append(self._active_context)
            result = copy.deepcopy(all_ctx)
            self._completed_contexts = []
            self._active_context = None
            self._started_at = None
            self._segment_counter = 1
            return result

    def detach_context(self) -> RecordingContext:
        with self._lock:
            if self._completed_contexts:
                return self._completed_contexts.pop(0)
        raise IndexError("No completed recording contexts available")

    def resolve_final_files(self, contexts: List[RecordingContext]) -> List[str]:
        if self._helper is not None:
            return self._resolve_final_files_helper(contexts)

        result: List[str] = []
        any_failed = False
        for ctx in contexts:
            path = getattr(ctx, "output_path", None)
            if not path:
                any_failed = True
                continue

            # Wait up to 3 seconds for file to appear and reach ≥ 100 bytes
            deadline = time.time() + 3.0
            found = False
            while time.time() < deadline:
                p = Path(path)
                if p.exists() and p.stat().st_size >= 100:
                    found = True
                    break
                time.sleep(0.1)

            if not found:
                log.error("[REC-006] WAV file never appeared | path=%s | waited=3s", path)
                any_failed = True
                continue

            # Validate WAV readability and minimum duration
            try:
                with wave.open(path, "rb") as wf:
                    frames = wf.getnframes()
                    duration = frames / wf.getframerate()
                if duration < 0.5:
                    log.warning("[REC-006] WAV too short | duration=%.2fs", duration)
                    _safe_rename_corrupted(path)
                    any_failed = True
                    continue
            except Exception:
                log.exception("[REC-006] WAV corrupted | path=%s", path)
                _safe_rename_corrupted(path)
                any_failed = True
                continue

            # Format branching
            fmt = RECORDER_FORMAT
            if fmt == "mp3":
                mp3_path = self._engine.convert_to_mp3(path)
                if mp3_path:
                    try:
                        Path(path).unlink()
                    except Exception:
                        log.exception("[REC-007] WAV delete failed after MP3 | path=%s", path)
                    result.append(mp3_path)
                else:
                    log.warning("[REC-007] MP3 conversion failed — keeping WAV | path=%s", path)
                    result.append(path)
            elif fmt == "both":
                result.append(path)
                mp3_path = self._engine.convert_to_mp3(path)
                if mp3_path:
                    result.append(mp3_path)
            else:
                result.append(path)

        # Update metadata — only after all existing validation logic above has run
        with self._lock:
            if result:
                self._recording_success = True
                if any_failed and self._recording_issues is None:
                    self._recording_issues = "[REC-006] partial"
            else:
                self._recording_success = False
                self._recording_issues = self._recording_issues or "[REC-006]"

        return result

    def resolve_final_file(self, ctx: RecordingContext) -> Optional[str]:
        files = self.resolve_final_files([ctx])
        return files[0] if files else None

    def ensure_recording_alive(self) -> bool:
        if self._helper is not None:
            return self._ensure_recording_alive_helper()

        with self._lock:
            if not self._is_recording:
                return True
            last_check = self._last_mute_check
            dev_idx = self._device_index
            dev_name = self._device_name_for_mute

        # Periodic mute-state probe — max once every 10 seconds, never blocks recording.
        # Must run in a daemon thread: _check_whatsapp_mute does a full UIA traversal
        # (20-30 s) — calling it synchronously here would block the main poll loop.
        now = time.time()
        if now - last_check >= 10.0:
            with self._lock:
                self._last_mute_check = now
            threading.Thread(
                target=self._do_mute_check,
                args=(dev_idx, dev_name),
                daemon=True,
                name="mute-check-health",
            ).start()

        if self._engine.is_active:
            return True

        # Engine thread is dead while we think we're recording — try recovery
        with self._lock:
            if self._restart_count >= RECORDER_WATCHDOG_RECOVERY_ATTEMPTS:
                log.error("[HLT-002] Recovery attempts exhausted")
                self._is_recording = False
                self._recording_issues = "[HLT-002]"
                return False

            self._restart_count += 1
            seg = self._segment_counter + 1
            self._segment_counter = seg
            ctx = self._active_context
            self._active_context = None
            if ctx is not None:
                self._completed_contexts.append(ctx)

        output_dir = RECORDER_OUTPUT_DIR
        device = self._device_manager.select_best_mic_device()
        output_path = self._generate_output_path(output_dir)
        pre_snap = self._snapshot_output_dir(output_dir)

        new_ctx = RecordingContext(
            pre_start_snapshot=pre_snap,
            start_marker=time.time(),
            started_at=datetime.now(),
            output_dir=output_dir,
            segment_index=seg,
            output_path=output_path,
        )

        if self._engine.start(output_path, device):
            with self._lock:
                self._active_context = new_ctx
            log.info("REC → watchdog recovery: new segment %s | path=%s", seg, output_path)
            return True

        with self._lock:
            self._is_recording = False
            self._recording_issues = "[HLT-002]"
        log.error("[HLT-002] Recovery failed — could not start new segment")
        return False

    # ── helper backend private methods ────────────────────────────────────────

    def _start_recording_helper(self) -> bool:
        output_dir = RECORDER_OUTPUT_DIR
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        with self._lock:
            seg = self._segment_counter
        base_name = f"{ts}_seg{seg}"

        pre_snap = self._snapshot_output_dir(output_dir)
        now = datetime.now()

        if not self._helper.start_session(output_dir, base_name):
            return False

        with self._lock:
            self._is_recording = True
            self._started_at = now
            self._recording_success = False
            self._recording_issues = None
            self._restart_count = 0
            self._active_context = RecordingContext(
                pre_start_snapshot=pre_snap,
                start_marker=time.time(),
                started_at=now,
                output_dir=output_dir,
                segment_index=seg,
                output_path=None,
            )

        log.info("[RH] Recording started | segment=%s | base=%s", seg, base_name)
        return True

    def _stop_recording_helper(self) -> bool:
        self._helper.stop_session()

        with self._lock:
            self._is_recording = False
            ctx = self._active_context
            self._active_context = None
            if ctx is not None:
                self._completed_contexts.append(ctx)

        log.info("[RH] Stop dispatched | segment=%s", self._segment_counter)
        return True

    def _force_stop_recording_helper(self) -> bool:
        try:
            self._helper.force_stop_session()
        except Exception:
            log.exception("[RH-004] force_stop: helper send raised")

        with self._lock:
            self._is_recording = False
            ctx = self._active_context
            self._active_context = None
            if ctx is not None:
                self._completed_contexts.append(ctx)

        log.info("[RH] Force stop dispatched | segment=%s", self._segment_counter)
        return True

    def _resolve_final_files_helper(
        self, contexts: List[RecordingContext]
    ) -> List[str]:
        path = self._helper.wait_for_merged(self._helper._stop_timeout)

        if path is None:
            log.error("[RH-004] No merged output — merge failed or timed out")
            with self._lock:
                self._recording_success = False
                self._recording_issues = "[RH-004]"
            return []

        for ctx in contexts:
            ctx.output_path = path

        deadline = time.time() + 5.0
        found = False
        while time.time() < deadline:
            p = Path(path)
            if p.exists() and p.stat().st_size >= 100:
                found = True
                break
            time.sleep(0.1)

        if not found:
            log.error("[RH-006] Merged WAV never appeared | path=%s", path)
            with self._lock:
                self._recording_success = False
                self._recording_issues = "[RH-006]"
            return []

        try:
            with wave.open(path, "rb") as wf:
                frames = wf.getnframes()
                duration = frames / wf.getframerate()
            if duration < 0.5:
                log.warning("[RH-006] WAV too short | duration=%.2fs", duration)
                _safe_rename_corrupted(path)
                with self._lock:
                    self._recording_success = False
                    self._recording_issues = "[RH-006]"
                return []
        except Exception:
            log.exception("[RH-006] WAV corrupted | path=%s", path)
            _safe_rename_corrupted(path)
            with self._lock:
                self._recording_success = False
                self._recording_issues = "[RH-006]"
            return []

        # Format branching — applies only to helper backend WAV output.
        # PyAudio backend has its own conversion path via CaptureEngine.convert_to_mp3().
        fmt = RECORDER_FORMAT
        if fmt == "mp3":
            mp3 = self._convert_wav_to_mp3(path)
            if mp3:
                if not RECORDER_KEEP_WAV_AFTER_MP3:
                    try:
                        Path(path).unlink()
                    except Exception:
                        log.exception("[RH-MP3-005] WAV delete failed after MP3 | path=%s", path)
                with self._lock:
                    self._recording_success = True
                return [mp3]
            else:
                log.warning("[RH-MP3] Conversion failed — keeping WAV | path=%s", path)
                with self._lock:
                    self._recording_success = True
                return [path]
        elif fmt == "both":
            # Returns [wav, mp3] — both are uploaded/processed.
            # WAV is first to match the existing pyaudio "both" ordering.
            mp3 = self._convert_wav_to_mp3(path)
            if mp3:
                with self._lock:
                    self._recording_success = True
                return [path, mp3]
            else:
                log.warning("[RH-MP3] Conversion failed — returning WAV only | path=%s", path)
                with self._lock:
                    self._recording_success = True
                return [path]
        else:  # "wav"
            with self._lock:
                self._recording_success = True
            return [path]

    def _convert_wav_to_mp3(self, wav_path: str) -> Optional[str]:
        """Convert a WAV file to MP3 using FFmpeg. Returns mp3_path on success, None on failure."""
        wav = Path(wav_path)
        if not wav.exists() or wav.stat().st_size == 0:
            log.error("[RH-MP3-001] WAV missing or empty before conversion | path=%s", wav_path)
            return None

        ffmpeg = Path(RECORDER_FFMPEG_PATH)
        if not ffmpeg.exists():
            log.error("[RH-MP3-001] ffmpeg.exe not found | path=%s", ffmpeg)
            return None

        mp3_path = str(wav.with_suffix(".mp3"))
        log.info("[RH-MP3] Converting WAV to MP3 | wav=%s | bitrate=%sk",
                 wav_path, RECORDER_MP3_BITRATE)

        # Hide the console window on Windows — stdout/stderr are still captured.
        _startupinfo = None
        _creationflags = 0
        if sys.platform == "win32":
            _startupinfo = subprocess.STARTUPINFO()
            _startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            _startupinfo.wShowWindow = 0  # SW_HIDE
            _creationflags = subprocess.CREATE_NO_WINDOW

        try:
            result = subprocess.run(
                [
                    str(ffmpeg), "-y",
                    "-i", wav_path,
                    "-ab", f"{RECORDER_MP3_BITRATE}k",
                    "-ac", "1",
                    "-ar", "48000",
                    mp3_path,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=_startupinfo,
                creationflags=_creationflags,
            )
        except Exception:
            log.exception("[RH-MP3-004] Exception running ffmpeg | wav=%s", wav_path)
            return None

        if result.returncode != 0:
            stderr_tail = "\n".join(result.stderr.splitlines()[-10:])
            log.error(
                "[RH-MP3-002] ffmpeg failed | exit=%d | wav=%s\n%s",
                result.returncode, wav_path, stderr_tail,
            )
            return None

        mp3 = Path(mp3_path)
        if not mp3.exists() or mp3.stat().st_size == 0:
            log.error("[RH-MP3-003] MP3 missing or empty after conversion | path=%s", mp3_path)
            return None

        log.info("[RH-MP3] MP3 created | path=%s | size=%d bytes", mp3_path, mp3.stat().st_size)
        return mp3_path

    def _ensure_recording_alive_helper(self) -> bool:
        with self._lock:
            if not self._is_recording:
                return True

        if not self._helper.is_process_alive():
            log.error(
                "[RH-002] RecorderHelper process died unexpectedly | stderr=%s",
                self._helper.get_last_stderr(5),
                exc_info=False,
            )
            with self._lock:
                self._is_recording = False
                self._recording_issues = "[RH-002]"
            return False

        return True

    # ── USB hot-swap ──────────────────────────────────────────────────────

    def _has_usb_wasapi_input_device(self) -> bool:
        """
        Returns True if a USB WASAPI input device (not a [Loopback] device)
        is currently enumerable by PortAudio.
        """
        try:
            devices = self._device_manager.list_input_devices()
            return any(
                "usb" in d.get("name", "").lower()
                and "wasapi" in d.get("host_api_name", "").lower()
                and "[Loopback]" not in d.get("name", "")
                for d in devices
            )
        except Exception:
            return False

    def _usb_watcher_loop(self) -> None:
        """
        Polls every 2 seconds for USB audio device appearance/disappearance.
        On reconnect: if recording on a fallback, switches back to USB.
        Daemon thread — dies automatically when process exits.
        """
        try:
            last_had_usb = self._has_usb_wasapi_input_device()
        except Exception:
            last_had_usb = False

        while not self._usb_watcher_stop.wait(2.0):
            try:
                has_usb = self._has_usb_wasapi_input_device()

                if not has_usb and last_had_usb:
                    log.warning(
                        "[DEV-USB] USB audio device disconnected"
                        " | monitoring for return every 2s"
                    )
                    if self._engine.is_active:
                        self._engine.on_usb_disconnect()
                elif has_usb and not last_had_usb:
                    log.info("[DEV-USB] USB audio device reconnected")
                    self._on_usb_reconnect()

                last_had_usb = has_usb

            except Exception:
                log.exception("[DEV-CHG] USB watcher iteration failed")

    def _on_device_change(self) -> None:
        """
        Callback from DeviceManager IMMNotificationClient or polling.
        USB reconnect logic is handled by _usb_watcher_loop polling.
        This callback is kept minimal — the watcher handles the rest.
        """
        pass  # USB watcher handles reconnect via polling

    def _on_usb_reconnect(self) -> None:
        """
        Called by _usb_watcher_loop when USB device returns after absence.
        If recording: triggers non-blocking stream reconnect.
        If not recording: logs only — next call will use USB automatically.
        """
        try:
            with self._lock:
                recording = self._is_recording

            if not recording:
                log.info(
                    "[DEV-USB] USB audio device reconnected — next call will use USB"
                )
                return

            log.info(
                "[DEV-USB] USB audio device reconnected — triggering stream reconnect"
            )
            self._engine._try_reconnect_streams_async()

        except Exception:
            log.exception("[DEV-CHG] USB reconnect handler failed")

    def get_recording_metadata(self) -> dict:
        with self._lock:
            return {
                "success": self._recording_success,
                "recording_success": self._recording_success,
                "issues": self._recording_issues,
                "recording_issues": self._recording_issues,
                "restart_count": self._restart_count,
            }

    # ── helpers ─────────────────────────────────────────────────────────────

    def _do_mute_check(self, device_index: Optional[int], device_name: str) -> None:
        """Log OS and WhatsApp mute state. Purely informational — never raises."""
        os_muted = _check_os_mute(device_index)
        wa_muted = _check_whatsapp_mute()

        if os_muted is True:
            log.warning(
                "MUTE → OS microphone is MUTED | device=%s | recording will be silent",
                device_name,
            )
        elif os_muted is False:
            log.debug("MUTE → OS microphone active | device=%s", device_name)

        if wa_muted is True:
            log.warning("MUTE → WhatsApp mic MUTED by user | recording may be silent")
        elif wa_muted is False:
            log.debug("MUTE → WhatsApp mic active")

    def _log_all_devices(self) -> None:
        """Enumerate and log all audio devices once at startup (informational)."""
        pa = self._device_manager._pa
        if pa is None:
            return
        try:
            count = pa.get_device_count()
            log.info("DEVICES → enumerating %s audio devices", count)
            for i in range(count):
                try:
                    info = pa.get_device_info_by_index(i)
                    api_name = pa.get_host_api_info_by_index(
                        info["hostApi"]
                    )["name"]
                    has_in = int(info.get("maxInputChannels", 0)) > 0
                    has_out = int(info.get("maxOutputChannels", 0)) > 0
                    direction = (
                        "IN+OUT" if has_in and has_out
                        else "INPUT " if has_in
                        else "OUTPUT"
                    )
                    log.info(
                        "DEVICES [%s] index=%-3d | %-45s | in=%-2d out=%-2d | %.0fHz | %s",
                        direction, i, info.get("name", "?"),
                        info.get("maxInputChannels", 0),
                        info.get("maxOutputChannels", 0),
                        info.get("defaultSampleRate", 0), api_name,
                    )
                except Exception:
                    log.exception("DEVICES → failed to query device index=%d", i)

            try:
                wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                comm_idx = wasapi_info.get("defaultInputDevice", -1)
                out_idx = wasapi_info.get("defaultOutputDevice", -1)
                if comm_idx >= 0:
                    comm = pa.get_device_info_by_index(comm_idx)
                    log.info(
                        "DEVICES → COMM INPUT (loopback source): index=%d | %s",
                        comm_idx, comm.get("name", "?"),
                    )
                if out_idx >= 0:
                    out = pa.get_device_info_by_index(out_idx)
                    log.info(
                        "DEVICES → DEFAULT OUTPUT (loopback target): index=%d | %s",
                        out_idx, out.get("name", "?"),
                    )
            except Exception:
                log.exception("[DEV-001] Failed to query WASAPI default devices")
        except Exception:
            log.exception("[DEV-003] Device enumeration failed completely")

    def _resolve_output_dir(self) -> Optional[Path]:
        try:
            p = Path(RECORDER_OUTPUT_DIR)
            p.mkdir(parents=True, exist_ok=True)
            if p.is_dir():
                return p
        except OSError:
            log.exception("[REC-008] Output dir not accessible | path=%s", RECORDER_OUTPUT_DIR)
        return None

    def _generate_output_path(self, output_dir: str) -> str:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        with self._lock:
            seg = self._segment_counter
        return str(Path(output_dir) / f"{ts}_seg{seg}.wav")

    def _snapshot_output_dir(self, output_dir: str) -> Set[str]:
        try:
            return {str(f) for f in Path(output_dir).iterdir() if f.is_file()}
        except Exception:
            return set()


def _safe_rename_corrupted(path: str) -> None:
    try:
        Path(path).rename(path.replace(".wav", ".corrupted"))
    except Exception:
        log.exception("[REC-006] Rename to .corrupted failed | path=%s", path)


# ─────────────────────────────────────────────────────────────────────────────
# Mute detection helpers — INFORMATIONAL ONLY.
# Both functions are wrapped in try/except and return None on any failure.
# They must NEVER raise, NEVER stop recording, NEVER modify mute state.
# ─────────────────────────────────────────────────────────────────────────────

def _check_os_mute(device_index: Optional[int] = None) -> Optional[bool]:
    """
    Check if the default capture device is OS-muted via Windows Core Audio.
    Uses direct COM vtable dispatch — no pre-generated typelib needed.
    Returns True=muted, False=active, None=check unavailable. Never raises.
    """
    try:
        import ctypes

        CLSCTX_ALL = 23
        eCapture, eCommunications = 1, 2
        c_vp = ctypes.c_void_p
        c_vpp = ctypes.POINTER(c_vp)

        # Do NOT call CoInitializeEx here — comtypes/pyaudio already initialize COM
        # on the main thread and changing the apartment model would fail silently
        # or break pywinauto (which requires STA).
        ole32 = ctypes.windll.ole32
        ole32.CoCreateInstance.restype = ctypes.HRESULT

        _CLSID_MMDevEnum = comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        _IID_IMMDevEnum  = comtypes.GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _IID_IAudioEPVol = comtypes.GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")

        # Helper: dereference COM object pointer → vtable array
        def _vtbl(ptr: ctypes.c_void_p):
            return ctypes.cast(ctypes.cast(ptr, c_vpp)[0], c_vpp)

        # Helper: Release COM object (vtable slot 2)
        def _release(vtb, ptr: ctypes.c_void_p) -> None:
            try:
                ctypes.WINFUNCTYPE(ctypes.c_ulong, c_vp)(vtb[2])(ptr)
            except Exception:
                pass

        # Step 1 — CoCreateInstance → IMMDeviceEnumerator
        enum_p = c_vp()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_MMDevEnum), None, CLSCTX_ALL,
            ctypes.byref(_IID_IMMDevEnum), ctypes.byref(enum_p),
        )
        if hr < 0 or not enum_p.value:
            return None
        vt_e = _vtbl(enum_p)

        # Step 2 — GetDefaultAudioEndpoint(eCapture, eCommunications) → IMMDevice
        # IMMDeviceEnumerator vtable: [0]=QI [1]=AddRef [2]=Release [3]=Enum [4]=GetDefault
        _GDE = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, c_vp, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(c_vp),
        )
        dev_p = c_vp()
        hr = _GDE(vt_e[4])(enum_p, eCapture, eCommunications, ctypes.byref(dev_p))
        _release(vt_e, enum_p)
        if hr < 0 or not dev_p.value:
            return None
        vt_d = _vtbl(dev_p)

        # Step 3 — Activate(IID_IAudioEndpointVolume, CLSCTX_ALL, NULL) → IAudioEndpointVolume
        # IMMDevice vtable: [0]=QI [1]=AddRef [2]=Release [3]=Activate
        _ACT = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, c_vp,
            ctypes.POINTER(comtypes.GUID), ctypes.c_uint, c_vp, ctypes.POINTER(c_vp),
        )
        vol_p = c_vp()
        hr = _ACT(vt_d[3])(
            dev_p, ctypes.byref(_IID_IAudioEPVol), CLSCTX_ALL, None, ctypes.byref(vol_p),
        )
        _release(vt_d, dev_p)
        if hr < 0 or not vol_p.value:
            return None
        vt_v = _vtbl(vol_p)

        # Step 4 — GetMute(&bMuted)
        # IAudioEndpointVolume vtable slot 15 (after QI/AddRef/Release/Register/Unregister/
        # GetChannelCount/SetMasterVolumeLevel/SetMasterVolumeLevelScalar/GetMasterVolumeLevel/
        # GetMasterVolumeLevelScalar/SetChannelVolumeLevel/SetChannelVolumeLevelScalar/
        # GetChannelVolumeLevel/GetChannelVolumeLevelScalar/SetMute/GetMute)
        _GMU = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, c_vp, ctypes.POINTER(ctypes.c_bool),
        )
        muted = ctypes.c_bool(False)
        hr = _GMU(vt_v[15])(vol_p, ctypes.byref(muted))
        _release(vt_v, vol_p)

        return bool(muted.value) if hr >= 0 else None

    except Exception:
        log.debug("MUTE → OS mute check unavailable | device_index=%s", device_index)
        return None


def _check_whatsapp_mute() -> Optional[bool]:
    """
    Inspect the active WhatsApp call window for in-app mute button state via
    pywinauto UI Automation. Returns True=muted, False=active, None=unavailable.
    Never raises.
    """
    try:
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        # WhatsApp call controls appear in the main app window.
        # Mute button toggles between "Mute microphone" and "Unmute microphone"
        # (or locale variants). Presence of "Unmute" implies currently muted.
        wa_windows = desktop.windows(title_re=r"(?i).*whatsapp.*")
        for win in wa_windows:
            try:
                ctrls = win.descendants(control_type="Button")
                for ctrl in ctrls:
                    try:
                        title = (ctrl.window_text() or "").lower()
                        if "unmute" in title:
                            return True   # currently muted
                        if "mute" in title and "unmute" not in title:
                            return False  # microphone is active
                    except Exception:
                        continue
            except Exception:
                continue
        return None  # WhatsApp window not found or no mute button visible
    except Exception:
        log.debug("MUTE → WhatsApp mute check unavailable")
        return None
