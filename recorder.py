from __future__ import annotations

import copy
import faulthandler
import logging
import math
import os
import shutil
import struct
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import pyaudiowpatch as pyaudio
import comtypes
import comtypes.client

from config import (
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
# CaptureEngine — owns the live PyAudio stream, the WAV file on disk, the
# record thread that drains the stream, and the watchdog thread that recovers
# from a dead record thread. CaptureEngine knows nothing about call lifecycle
# or segments; that's Recorder's job.
#
# Failure codes used here:
#   REC-002  stream open failed on a specific device (warning, retried)
#   REC-003  all devices in fallback chain exhausted (error)
#   REC-004  record thread / stream tear-down failure (error)
#   REC-005  WAV write error mid-recording (error)
#   REC-006  WAV finalization failed (error)
#   REC-007  MP3 conversion failed (warning, WAV is kept)
#   REC-008  failed to open WAV file for writing (error)
#   HLT-001  watchdog detected dead record thread, recovering (warning)
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
      lock only briefly (around `wave_file.writeframes`) to keep stream reads
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
            self._device_manager.reinit_pyaudio()
            self._watchdog_stop.clear()
            self._tried_devices = []
            self._recovery_attempts = 0
            self._recovery_exhausted = False
            self._bytes_written = 0
            self._device = device
            self._device_name = str(device.get("name", "?")) if device else ""
            self._silence_buffer = bytearray()
            self._silent_secs = 0
            self._mic_channels = 1
            self._mic_debug_wav = None
            self._loopback_debug_wav = None
            self._mic_debug_path = None
            self._loopback_debug_path = None

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

            self._record_started_monotonic = time.monotonic()
            self._thread_started_at = time.time()
            self._record_thread = threading.Thread(
                target=self._record_loop,
                daemon=True,
                name="CaptureEngineRecord",
            )
            self._record_thread.start()

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
        """Signal stop, then: stop_stream → join → close (safe teardown order)."""
        with self._lock:
            record_thread = self._record_thread
            watchdog_thread = self._watchdog_thread
            stream = self._stream
            loopback_stream = self._loopback_stream
            output_path = self._output_path
            if (record_thread is None and watchdog_thread is None
                    and stream is None and loopback_stream is None
                    and self._wav_file is None):
                return False
            self._record_thread = None
            self._watchdog_thread = None
            self._stream = None
            self._mic_stream = None
            self._loopback_stream = None

        # 1. Signal stop
        self._stop_event.set()
        self._watchdog_stop.set()

        # 2. stop_stream() on BOTH streams before joining
        #    This unblocks any pending blocking stream.read() call immediately.
        #    Safe to call while read() is active — does NOT free memory.
        for s in (loopback_stream, stream):
            if s is not None:
                try:
                    s.stop_stream()
                except Exception:
                    pass

        # 3. Join threads — exit quickly since stream.read() is now unblocked
        if record_thread is not None:
            record_thread.join(timeout=2.0)
            if record_thread.is_alive():
                log.warning(
                    "[REC-004] Record thread still alive after 2.0s | path=%s",
                    output_path,
                )
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=2.0)

        # 4. close() AFTER thread is confirmed dead — now safe
        for s in (loopback_stream, stream):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

        # Safety net: record thread's finally block calls _finalize_wav() too,
        # but if the thread never started or died before reaching it, do it here.
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
        Nulls stream references under lock so the record loop never reads a
        dead WASAPI stream (which would cause a Windows access violation).
        Does NOT call close() — the OS WASAPI topology has already changed;
        close() can access-violate on a removed device.  stop_stream() is
        best-effort only.
        """
        with self._lock:
            lb = self._loopback_stream
            mic = self._stream
            self._loopback_stream = None
            self._stream = None
            self._mic_stream = None

        for s in (lb, mic):
            if s is not None:
                try:
                    s.stop_stream()
                except Exception:
                    pass

        log.info(
            "REC → USB disconnect handled | streams detached"
            " | record loop will write silence until reconnect"
        )

    def _try_reconnect_streams_async(self) -> None:
        """
        Starts a non-blocking daemon thread to reopen streams after USB
        device return.  If a reconnect thread is already running, does nothing.
        """
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
            #    Do this BEFORE reinit so we don't disturb live streams.
            with self._lock:
                if self._stop_event.is_set():
                    return
                need_lb = self._loopback_stream is None
                need_mic = self._stream is None

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
                            # Recorder is stopping; close the newly opened stream
                            # (this is a valid stream we own — safe to close)
                            pass
                        elif self._loopback_stream is None:
                            self._loopback_stream = new_lb
                            self._mix_rate = self._loopback_rate
                            assigned = True
                        else:
                            # Another path already assigned a stream; discard ours
                            pass
                    if not assigned:
                        try:
                            new_lb.stop_stream()
                            new_lb.close()
                        except Exception:
                            pass
                        if self._stop_event.is_set():
                            return
                    else:
                        log.info("REC → loopback stream reopened after reconnect")
                else:
                    log.warning(
                        "REC → loopback reopen failed — record loop continues with silence"
                    )

            if need_mic:
                if self._stop_event.is_set():
                    return
                device = self._device_manager.select_best_device()
                if device:
                    with self._lock:
                        self._device = device
                        self._device_name = str(device.get("name", "?"))
                    opened = self._open_stream(device)  # sets self._stream on success
                    if opened:
                        discard_stream = None
                        with self._lock:
                            if self._stop_event.is_set():
                                discard_stream = self._stream
                                self._stream = None
                                self._mic_stream = None
                            else:
                                self._mic_stream = self._stream
                        if discard_stream is not None:
                            try:
                                discard_stream.stop_stream()
                                discard_stream.close()
                            except Exception:
                                pass
                            return
                        log.info(
                            "REC → mic stream reopened after reconnect | device=%s",
                            self._device_name,
                        )
                    else:
                        log.warning(
                            "REC → mic reopen failed — record loop continues with silence"
                        )
                else:
                    log.warning("REC → no mic device available during reconnect")

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
        """Nearest-neighbour resample — adequate quality for voice."""
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

    # ── internal: record + watchdog threads ──────────────────────────────────

    def _record_loop(self) -> None:
        log.info("REC → record thread started | device=%s | file=%s",
                 self._device_name, self._output_path)
        # Computed once — constant for the lifetime of this recording session.
        _chunk_bytes = self._chunk_size * self._sample_width
        _block_seconds = self._chunk_size / max(1, self._mix_rate)
        try:
            while not self._stop_event.is_set():
                _iter_start = time.monotonic()
                try:
                    loopback_data: Optional[bytes] = None
                    mic_data: Optional[bytes] = None

                    # ── Loopback read (blocking) ──────────────────────────────
                    lb_stream = self._loopback_stream
                    if lb_stream is not None:
                        try:
                            raw = lb_stream.read(
                                self._chunk_size,
                                exception_on_overflow=False,
                            )
                            # Validate pre-downmix byte count
                            _lb_expected = (
                                self._chunk_size * self._loopback_channels * self._sample_width
                            )
                            if len(raw) < _lb_expected:
                                log.warning(
                                    "REC → loopback read short | got=%d expected=%d | padding",
                                    len(raw), _lb_expected,
                                )
                                raw = raw + b"\x00" * (_lb_expected - len(raw))
                            elif len(raw) > _lb_expected:
                                log.warning(
                                    "REC → loopback read long | got=%d expected=%d | trimming",
                                    len(raw), _lb_expected,
                                )
                                raw = raw[:_lb_expected]
                            if self._loopback_channels == 2:
                                samples = struct.unpack(f"<{len(raw)//2}h", raw)
                                if len(samples) % 2 != 0:
                                    samples = samples[:-1]
                                mono = [
                                    max(-32768, min(32767,
                                        (int(samples[i]) + int(samples[i + 1])) // 2))
                                    for i in range(0, len(samples), 2)
                                ]
                                loopback_data = struct.pack(f"<{len(mono)}h", *mono)
                            else:
                                loopback_data = raw
                            if self._loopback_rate != self._mix_rate:
                                loopback_data = self._resample(
                                    loopback_data,
                                    self._loopback_rate,
                                    self._mix_rate,
                                )
                            # Normalize to exact output chunk size (handles resample rounding)
                            if len(loopback_data) < _chunk_bytes:
                                loopback_data = loopback_data + b"\x00" * (
                                    _chunk_bytes - len(loopback_data)
                                )
                            elif len(loopback_data) > _chunk_bytes:
                                loopback_data = loopback_data[:_chunk_bytes]
                        except OSError as exc:
                            if self._stop_event.is_set():
                                break
                            log.exception(
                                "[REC-005] Loopback read error | errno=%s", exc.errno
                            )
                            try:
                                lb_stream.stop_stream()
                                lb_stream.close()
                            except Exception:
                                pass
                            self._loopback_stream = None
                        except Exception:
                            if self._stop_event.is_set():
                                break
                            log.exception("[REC-005] Loopback read failed")
                            self._loopback_stream = None

                    # ── Mic read (non-blocking secondary) ────────────────────
                    # Loopback drives timing via its blocking read above.
                    # Mic only reads when data is already buffered — never blocks.
                    mic_stream = self._stream
                    if mic_stream is not None:
                        try:
                            available = mic_stream.get_read_available()
                            if available >= self._chunk_size:
                                raw = mic_stream.read(
                                    self._chunk_size,
                                    exception_on_overflow=False,
                                )
                                # Validate: expected bytes = chunk_size × channels × width
                                expected_bytes = (
                                    self._chunk_size * self._mic_channels * self._sample_width
                                )
                                if len(raw) < expected_bytes:
                                    log.warning(
                                        "REC → mic read short | got=%d expected=%d | padding",
                                        len(raw), expected_bytes,
                                    )
                                    raw = raw + b"\x00" * (expected_bytes - len(raw))
                                elif len(raw) > expected_bytes:
                                    log.warning(
                                        "REC → mic read long | got=%d expected=%d | trimming",
                                        len(raw), expected_bytes,
                                    )
                                    raw = raw[:expected_bytes]
                                # Stereo-to-mono downmix — mirrors the loopback path
                                if self._mic_channels == 2:
                                    samples = struct.unpack(f"<{len(raw)//2}h", raw)
                                    if len(samples) % 2 != 0:
                                        samples = samples[:-1]
                                    mono = [
                                        max(-32768, min(32767,
                                            (int(samples[i]) + int(samples[i + 1])) // 2))
                                        for i in range(0, len(samples), 2)
                                    ]
                                    raw = struct.pack(f"<{len(mono)}h", *mono)
                                if self._actual_sample_rate != self._mix_rate:
                                    raw = self._resample(
                                        raw, self._actual_sample_rate, self._mix_rate
                                    )
                                # Normalize to exact output chunk size (handles resample rounding)
                                if len(raw) < _chunk_bytes:
                                    raw = raw + b"\x00" * (_chunk_bytes - len(raw))
                                elif len(raw) > _chunk_bytes:
                                    raw = raw[:_chunk_bytes]
                                mic_data = raw
                        except OSError as exc:
                            if self._stop_event.is_set():
                                break
                            log.exception(
                                "[REC-005] Mic read error | errno=%s", exc.errno
                            )
                            try:
                                mic_stream.stop_stream()
                                mic_stream.close()
                            except Exception:
                                pass
                            self._stream = None
                        except Exception:
                            if self._stop_event.is_set():
                                break
                            log.exception("[REC-005] Mic read failed")
                            self._stream = None

                    # ── Debug stems ───────────────────────────────────────────
                    # Sources are normalized to exactly _chunk_bytes above.
                    # Write the normalized mono frames (or silence) per stem.
                    if self._debug_stems:
                        if self._loopback_debug_wav is not None:
                            try:
                                self._loopback_debug_wav.writeframes(
                                    loopback_data if loopback_data is not None
                                    else b"\x00" * _chunk_bytes
                                )
                            except Exception:
                                pass
                        if self._mic_debug_wav is not None:
                            try:
                                self._mic_debug_wav.writeframes(
                                    mic_data if mic_data is not None
                                    else b"\x00" * _chunk_bytes
                                )
                            except Exception:
                                pass

                    # ── Normalize: fill missing source with silence ───────────
                    # Every iteration writes exactly one mono chunk so the WAV
                    # grows at wall-clock rate regardless of which streams are live.
                    if loopback_data is None:
                        loopback_data = b"\x00" * _chunk_bytes
                    if mic_data is None:
                        mic_data = b"\x00" * _chunk_bytes

                    # ── Mix ───────────────────────────────────────────────────
                    count = min(len(loopback_data), len(mic_data)) // 2
                    lb_s = struct.unpack(f"<{count}h", loopback_data[:count * 2])
                    mc_s = struct.unpack(f"<{count}h", mic_data[:count * 2])
                    mixed = [
                        max(-32768, min(32767, int(lb_s[i]) + int(mc_s[i])))
                        for i in range(count)
                    ]
                    write_data = struct.pack(f"<{count}h", *mixed)

                    # ── Write ─────────────────────────────────────────────────
                    try:
                        self._wav_file.writeframes(write_data)
                        self._bytes_written += len(write_data)
                    except Exception:
                        log.exception(
                            "[REC-005] WAV write error | path=%s | bytes=%s",
                            self._output_path, self._bytes_written,
                        )
                        break

                    # ── Silence detection: check RMS every 3 seconds ──────────
                    self._silence_buffer.extend(write_data)
                    check_size = self._mix_rate * 3 * 2
                    if len(self._silence_buffer) >= check_size:
                        smp = struct.unpack(
                            f"<{check_size // 2}h",
                            bytes(self._silence_buffer[:check_size]),
                        )
                        rms = math.sqrt(sum(s * s for s in smp) / len(smp)) if smp else 0.0
                        if rms < 10:
                            self._silent_secs += 3
                            if self._silent_secs >= 6:
                                log.warning(
                                    "[REC-009] Recording silent | rms=%.1f | silent=%ds"
                                    " | streams=loopback:%s mic:%s",
                                    rms, self._silent_secs,
                                    "ON" if self._loopback_stream else "OFF",
                                    "ON" if self._stream else "OFF",
                                )
                        else:
                            if self._silent_secs > 0:
                                log.info("REC → audio detected | rms=%.1f", rms)
                            self._silent_secs = 0
                        self._silence_buffer = bytearray()

                    # ── Writer clock ──────────────────────────────────────────
                    # Pace each tick to wall-clock rate so the WAV duration
                    # matches real time regardless of which sources are live.
                    # When loopback is active its blocking read consumes most of
                    # _block_seconds; the wait here covers any remaining gap.
                    # When both streams are gone, the wait IS the pacing.
                    if lb_stream is None and mic_stream is None:
                        self._try_reconnect_streams_async()
                    _elapsed = time.monotonic() - _iter_start
                    _sleep = _block_seconds - _elapsed
                    if _sleep > 0:
                        if self._stop_event.wait(_sleep):
                            break
                    elif (_elapsed - _block_seconds) * 1000 > 10:
                        log.warning(
                            "[REC-013] writer lag | behind_ms=%.1f",
                            (_elapsed - _block_seconds) * 1000,
                        )

                except Exception:
                    if self._stop_event.is_set():
                        break
                    log.exception(
                        "[REC-004] Record loop error | bytes=%s",
                        self._bytes_written,
                    )
                    break

        finally:
            self._finalize_wav()
            self._log_duration_check()
            log.info("REC → record thread exiting | bytes_written=%s | normal=%s",
                     self._bytes_written, self._stop_event.is_set())

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
        with self._lock:
            if self._recovery_exhausted:
                return
            max_attempts = max(1, RECORDER_WATCHDOG_RECOVERY_ATTEMPTS)
            self._recovery_attempts += 1
            attempt = self._recovery_attempts
            if attempt > max_attempts:
                self._recovery_exhausted = True
                log.error(
                    "[HLT-002] Recovery failed — call continues without further recording"
                )
                return

            old_stream = self._stream
            self._stream = None

        if old_stream is not None:
            try:
                old_stream.stop_stream()
            except Exception:
                log.exception("[REC-004] watchdog: stream.stop_stream failed")
            try:
                old_stream.close()
            except Exception:
                log.exception("[REC-004] watchdog: stream.close failed")

        device = self._device_manager.select_best_device()
        if device is None:
            with self._lock:
                self._recovery_exhausted = True
            log.error("[HLT-002] Recovery failed — no input device available")
            return

        with self._lock:
            self._device = device
            self._device_name = str(device.get("name", "?"))
            log.warning(
                "[HLT-001] Thread dead | attempt %s/%s | restarting on %s",
                attempt, max_attempts, self._device_name,
            )

        if not self._open_stream(device):
            with self._lock:
                self._recovery_exhausted = True
            log.error("[HLT-002] Recovery failed — stream open failed on %s",
                      self._device_name)
            return

        with self._lock:
            self._thread_started_at = time.time()
            self._record_thread = threading.Thread(
                target=self._record_loop,
                daemon=True,
                name="CaptureEngineRecord",
            )
            self._record_thread.start()
        log.info("REC → recovery OK | device=%s | attempt=%s",
                 self._device_name, attempt)

        # Try to reopen loopback if it was lost (USB speaker reconnected)
        with self._lock:
            lb = self._loopback_stream
        if lb is None:
            new_lb = self._open_loopback_stream()
            if new_lb:
                with self._lock:
                    self._loopback_stream = new_lb
                log.info(
                    "REC → recovery: loopback stream reopened"
                    " (USB speaker back)"
                )
            else:
                log.warning(
                    "REC → recovery: loopback reopen failed"
                    " — recording mic-only until next call"
                )

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


class Recorder:
    """
    Public recording interface. Delegates to CaptureEngine for stream/WAV work.
    Manages call lifecycle: segment numbering, context tracking, format resolution.
    """

    def __init__(self) -> None:
        _crash_log = Path(__file__).parent / "logs" / "crash.log"
        _crash_log.parent.mkdir(exist_ok=True)
        try:
            faulthandler.enable(
                file=open(str(_crash_log), "a"),
                all_threads=True,
            )
            log.info("STARTUP → faulthandler enabled | crash_log=%s", _crash_log)
        except Exception:
            log.warning("STARTUP → faulthandler could not be enabled")

        self._lock = threading.RLock()
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

        device = self._device_manager.select_best_device()
        if device is None:
            log.error("[DEV-003] No audio input devices found — no microphone capture available")
            log.error("[REC-001] No audio input device — recording skipped")
            # Loopback-only recording would require device=None path; fall through if needed
            return False

        dev_name_for_log = device.get("name", "?")
        had_usb = self._has_usb_wasapi_input_device()
        if "usb" in dev_name_for_log.lower():
            log.info("[DEV-USB] USB headset selected for recording | device=%s", dev_name_for_log)
        elif not had_usb:
            log.warning(
                "[DEV-USB] USB headset not connected — using fallback device: %s",
                dev_name_for_log,
            )
        else:
            log.info("REC → using input device: %s", dev_name_for_log)

        _t_engine = time.monotonic()
        if not self._engine.start(output_path, device):
            return False
        _t_engine_elapsed = time.monotonic() - _t_engine

        dev_idx = device.get("index")
        dev_name = device.get("name", "?")

        # Mute check runs in a daemon thread — UIA traversal can take 20-30s
        # on some systems; must never block start_recording() return.
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
        device = self._device_manager.select_best_device()
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
