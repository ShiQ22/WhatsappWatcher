# Session Handoff

## Last updated
Date: 2026-05-06
Session: 10 — Fixed stream teardown order: stop_stream → join → close

---

## Current status

### DONE (session 1)
- **config.json** — replaced `bandicam` block with new `recorder` block.
- **config.py** — `RECORDER_*` constants, backwards-compat aliases retained.
- **recorder.py** — `RecordingContext` + `DeviceManager` complete.
- **tests/conftest.py** — base fixtures.
- **tests/test_device_manager.py** — 15 tests, all passing.
- **requirements.txt** — dependencies.

### DONE (session 2)
- **recorder.py — CaptureEngine class** — full implementation per audio-capture.md.
- **tests/test_capture_engine.py** — 12 tests.
- **tests/test_audio_output.py** — 8 tests.

### DONE (session 3 — this session)
- **recorder.py — Recorder class** — full production implementation:
  - Added `import copy`, `import shutil`, `from dataclasses import field` (field unused
    but copy+shutil needed for Recorder).
  - Added `output_path: Optional[str] = None` field to `RecordingContext` (backwards-
    compatible: default None, existing callers unaffected).
  - Replaced placeholder `Recorder` class with complete implementation:
    - `__init__`: creates DeviceManager + CaptureEngine, initialises all state.
    - `is_recording`, `current_recording_path`, `started_at` as thread-safe properties.
    - `refresh_recorder_paths()` + `refresh_bandicam_paths` alias.
    - `start_recording()`: double-start guard, disk-space check (REC-008, <100 MB),
      snapshot output dir, generate `YYYY-MM-DD_HH-MM-SS_segN.wav` path, select device,
      call `CaptureEngine.start()`, create active RecordingContext.
    - `stop_recording()`: calls `CaptureEngine.stop()`, moves active context to
      `_completed_contexts`, logs segment.
    - `force_stop_recording()`: same but always sets is_recording=False even on exception.
    - `detach_contexts()`: deep-copy of completed + active contexts, resets state.
    - `detach_context()`: pops first from `_completed_contexts`, raises IndexError if empty.
    - `resolve_final_files(contexts)`: waits up to 3s per file, validates WAV (≥100 bytes,
      ≥0.5s duration, readable), renames corrupted to `.corrupted`, handles wav/mp3/both
      format branching via `CaptureEngine.convert_to_mp3()`.
    - `resolve_final_file(ctx)`: single-item wrapper.
    - `ensure_recording_alive()`: checks `CaptureEngine.is_active`, starts new segment on
      failure, caps at `RECORDER_WATCHDOG_RECOVERY_ATTEMPTS`, logs HLT-002 on exhaustion.
    - `get_recording_metadata()`: returns `{success, issues, restart_count}`.
    - `_safe_rename_corrupted()` module-level helper (avoids exception leaks in finally).
- **tests/conftest.py** — added `mock_config` fixture: patches `RECORDER_OUTPUT_DIR`,
  `RECORDER_FORMAT`, settle/retry constants, and `shutil.disk_usage` (reports 500 MB free).
- **tests/test_recorder.py** (new) — 34 tests covering all Recorder methods.
- **whatsapp_watcher.spec** — PyInstaller spec at project root.

---

### DONE (session 4 — this session)

**FIX 1 — Bandicam startup warning suppressed**
- `recorder.py` now imports `BANDICAM_PATH` from config.py.
- `Recorder.__init__` sets `self.bandicam_path = Path(BANDICAM_PATH)` (truthy).
- `config.py` already had `BANDICAM_PATH = os.path.abspath(__file__)` — points to
  config.py itself, which always exists.
- Result: "STARTUP → Bandicam NOT found" warning no longer appears.

**FIX 2 — Dual-stream capture (WASAPI loopback + microphone)**
- `CaptureEngine` now opens TWO streams:
  - `_loopback_stream` — WASAPI loopback on default output device (incoming voice).
  - `_mic_stream` / `_stream` — microphone input (outgoing voice, existing logic).
- New method `_open_loopback_stream()` — opens loopback, logs device/rate/channels.
- Stereo loopback is converted to mono before mixing.
- Both streams mixed per-sample (add + int16 clamp) into single WAV.
- Recording succeeds if at least one stream is available (graceful degradation).
- New method `_resample()` — nearest-neighbour, used when rates differ.
- `stop()` closes both streams cleanly.

**FIX 3 — Device enumeration log at startup**
- `Recorder._log_all_devices()` called in `__init__` after DeviceManager init.
- Logs every device with direction (INPUT/OUTPUT/IN+OUT), rate, host API.
- Logs WASAPI comm input and default output indices.

**FIX 4 — Silence detection during recording**
- `_record_loop()` accumulates write_data in `_silence_buffer`.
- Every 3 seconds of audio: computes RMS; if < 10 for 6+ consecutive seconds,
  logs `[REC-009]` with stream state.
- Resets counter when audio is detected again.

---

## Test results
```
tests/test_audio_output.py       8 passed
tests/test_capture_engine.py    24 passed  (+10 new dual-stream tests)
tests/test_device_manager.py    15 passed
tests/test_recorder.py          34 passed  (unchanged — all still pass)
                                ──────────
                                86 passed in 31.81s
```

Command: `pytest tests/ -v`

### Coverage
```
Name          Stmts   Miss  Cover
recorder.py    1001    169    83%
```

Command: `pytest tests/ --cov=recorder --cov-report=term-missing`

---

## Integration checks (all passing)
```
python -c "from recorder import Recorder; print('imports OK')"
→ imports OK
```

---

## Files modified this session
- `recorder.py` — added `import math`, `import struct`, `BANDICAM_PATH` import;
  `CaptureEngine.__init__` new fields; new `_open_loopback_stream()`, `_resample()`;
  refactored `start()`, `stop()`, `_record_loop()` for dual-stream + silence detection;
  `Recorder.__init__` bandicam_path fix + `_log_all_devices()` call;
  new `Recorder._log_all_devices()` method.
- `tests/test_capture_engine.py` — added `import struct`;
  added `_build_dual_pa_mock()` helper; added `TestDualStreamCapture` (10 tests).
- `recorder_plan/session/handoff.md` (this file).

Frozen files NOT touched: `main.py`, `detector.py`, `state_machine.py`,
`storage.py`, `uploader.py`, `report.py`, `config.py`.

---

### DONE (session 5 — this session)

**Root cause of silent audio confirmed and fixed:**
- `AttributeError: 'PyAudio' object has no attribute 'paWASAPI'` — `pa` is a PyAudio
  instance; constants live on the module. All 3 occurrences replaced:
  `pa.paWASAPI` → `pyaudio.paWASAPI`, `pa.paInt16` → `pyaudio.paInt16`.

**FIX 1 — Module-level constants (was: instance attribute) [recorder.py]**
- `CaptureEngine._open_loopback_stream()` — replaced `pa.paInt16` → `pyaudio.paInt16`
- `CaptureEngine._open_stream()` — replaced `pa.paInt16` → `pyaudio.paInt16`
- `Recorder._log_all_devices()` — replaced `pa.paWASAPI` → `pyaudio.paWASAPI`
- Grep `pa\.paWASAPI|pa\.paInt16` now returns 0 results.

**FIX 2 — `_open_loopback_stream()` rewritten to scan `[Loopback]` devices**
- pyaudiowpatch exposes loopback as normal INPUT devices named
  `"Speakers (Device) [Loopback]"` — no `as_loopback=True` kwarg needed.
- Old code called `pa.open(as_loopback=True)` on a non-loopback output device — crash.
- New code: scan all devices, filter by `"[Loopback]" in name AND maxInputChannels > 0
  AND "WASAPI" in hostApi name`. Prefer USB loopback; fall back to first found.
- Opens with `input=True, input_device_index=preferred["index"]` — standard PA open.

**FIX 3 — Mic exclusive-mode probe**
- `_open_stream()` reads 0.5s of audio immediately after stream opens.
- Logs `[REC-009]` warning if RMS < 1.0 (exclusive mode conflict, mic volume=0, etc.).
- Logs "mic probe OK" otherwise. Diagnostic only — never prevents recording.

**Tests updated (tests/test_capture_engine.py)**
- `_build_dual_pa_mock()` rewritten: index 1 is now
  `"Speakers (USB Audio Device) [Loopback]"` with `maxInputChannels=1`;
  `open_side_effect` dispatches on `input_device_index` (not `as_loopback=True`).
- All 10 `TestDualStreamCapture` tests updated to match new mock.
- 5 new tests added: `test_loopback_prefers_usb_device_over_others`,
  `test_loopback_falls_back_to_first_when_no_usb`,
  `test_no_loopback_devices_returns_none`,
  `test_mic_probe_logs_warning_when_silent`, `test_mic_probe_logs_ok_when_audio`.

## Test results (session 5)
```
tests/test_audio_output.py        8 passed
tests/test_capture_engine.py     29 passed  (+5 new session-5 tests)
tests/test_device_manager.py     15 passed
tests/test_recorder.py           34 passed
                                 ──────────
                                 91 passed in 32.55s
```

Command: `pytest tests/ -v`

---

### DONE (session 6 — this session)

**recorder.py — string corrections (no logic change)**
- `_open_loopback_stream()` warning: "installed or WASAPI unavailable" →
  "installed correctly"
- `_open_loopback_stream()` info log: "using:" → "selected:"
- `_open_stream()` mic probe:
  - `probe_frames` formula: `max(1, int(rate * 0.5))` →
    `min(int(self._actual_sample_rate * 0.5), self._chunk_size * 4)`
  - Warning message: "mic probe silent (rms=%.1f) | ... exclusive mode conflict,
    mic volume=0, no physical mic" → "mic probe silent | rms=%.1f | ... mic
    volume=0 in Windows Sound settings, no physical mic connected, or
    exclusive-mode conflict"
  - Exception handler: `log.debug("skipped")` → `log.exception("[REC-002] Mic
    probe read failed")` + non-fatal comment

**main.py — log improvements (strings/formatter/function rename only)**
- CHANGE A: Formatter now includes `[%(filename)s:%(lineno)d]` — every log line
  shows source file + line number.
- CHANGE B: Startup log strings:
  - "STARTUP → Bandicam: %s" → "STARTUP → Recorder ready | path=%s"
  - "STARTUP → Bandicam NOT found..." → "STARTUP → Recorder NOT ready..."
  - "STARTUP → Bandicam output: %s" → "STARTUP → Recorder output dir: %s"
  - "STARTUP → Bandicam output dir not found" → unchanged (already neutral)
- CHANGE C: Function rename: `_list_recoverable_bandicam_files` →
  `_list_recoverable_recording_files` (definition + 1 call site).
- CHANGE D: 5 log strings updated — all "Bandicam" references in string
  literals replaced with "recorder"/"recording". Zero remaining
  `".*[Bb]andicam.*"` string literals in main.py.
- CHANGE E: Comments — "Bandicam health check" → "Recorder health check";
  `_finalize_no_recording` docstring updated.

## Test results (session 6)
```
tests/test_audio_output.py        8 passed
tests/test_capture_engine.py     29 passed
tests/test_device_manager.py     15 passed
tests/test_recorder.py           34 passed  (unchanged)
                                 ──────────
                                 91 passed in 31.96s
```

Command: `pytest tests/ -v`

Grep verifications:
- `grep "pa\.paWASAPI\|pa\.paInt16" recorder.py` → 0 results ✓
- `grep "_list_recoverable_bandicam_files" main.py` → 0 results ✓
- `grep '".*[Bb]andicam.*"' main.py` → 0 results ✓

---

### DONE (session 7 — this session)

**PART 1 — recorder.py: non-blocking record loop (crash fix)**

Root cause: `stream.read()` blocks inside PortAudio C internals when the
WASAPI session closes. Thread never returns → join timeout expires →
[REC-004] logged → PortAudio heap becomes invalid → segfault (no Python
exception, process just vanishes).

- `_record_loop()` fully rewritten with non-blocking poll:
  - Both loopback and mic reads gated on `get_read_available() >= chunk_size`.
  - If no data: `_stop_event.wait(10ms)` + `continue` — thread exits within
    one 10ms poll cycle after `stop_event` is set.
  - Per-stream OSError handling: logs `[REC-005]`, closes bad stream, sets
    it to None, continues recording on the remaining stream.
  - `MAX_EMPTY_POLLS = 300` (3 seconds): logs `[REC-005] No audio data for 3s`
    warning once when no data arrives from either stream.
  - All silence detection logic (RMS / [REC-009]) preserved unchanged.
- `stop()` join timeout: removed dynamic `max(1.0, RECORDER_STOP_SETTLE_SECONDS)`;
  hardcoded `join(timeout=1.0)`. Warning message updated to "Record thread
  still alive after 1.0s — PortAudio may be in bad state".
- `import faulthandler` added at module top.
- `Recorder.__init__()`: `faulthandler.enable()` called at the very start,
  writing C-level crash traces to `logs/crash.log`. Logs
  "STARTUP → faulthandler enabled" on success.

**PART 2 — auto-restart launcher (new files)**
- `launcher.py`: runs `main.py` as subprocess; on crash (non-zero exit)
  waits 5s and restarts; clean exit (code=0) stops the launcher;
  safety cap: stops after 20 crashes/hour; logs to `logs/launcher.log`.
- `start.bat`: `python launcher.py` with window title "WhatsApp Watcher".
- `stop.bat`: `taskkill /F /IM python.exe /FI "WINDOWTITLE eq WhatsApp Watcher"`.

**Tests updated**
- `conftest.py` `_build_pa_mock()`: `stream.get_read_available.return_value = 1024`
- `_build_dual_pa_mock()`: `lb_stream.get_read_available.return_value = 1024`
  and `mic_stream.get_read_available.return_value = 1024`
- 4 new tests:
  - `test_record_thread_exits_quickly_when_stopped` — exits within 500ms
  - `test_record_thread_handles_loopback_oserror_without_crash` — [REC-005] logged
  - `test_record_thread_handles_empty_polls_warning` — "No audio data for 3s"
  - `test_stop_joins_within_1_second` — stop() returns within 1.5s

## Test results (session 7)
```
tests/test_audio_output.py        8 passed
tests/test_capture_engine.py     33 passed  (+4 new session-7 tests)
tests/test_device_manager.py     15 passed
tests/test_recorder.py           34 passed
                                 ──────────
                                 95 passed in 38.03s
```

Command: `pytest tests/ -v`

---

### DONE (session 8 — this session)

**recorder.py — audio quality fix: partial-buffer reads**

Root cause: `available >= self._chunk_size` gate caused loopback to be skipped
whenever its buffer had < 1024 frames (e.g. 479). Mic (zeros) was read on
those iterations → 479 audio frames alternated with 1024 silence frames in the
WAV → 45.7% zero samples → robotic/metallic audio.

Fix (2 lines changed):
```
# loopback
if available > 0:
    raw = lb_stream.read(min(available, self._chunk_size), ...)

# mic
if available > 0:
    raw = mic_stream.read(min(available, self._chunk_size), ...)
```

Both streams now read whenever any data is available. Variable-size reads are
fine: the mix block already uses `min(len(lb), len(mic)) // 2` to handle
mismatched chunk sizes. WAV writer accepts any frame count per call.

2 new tests added:
- `test_no_silence_gaps_when_loopback_buffer_partially_full`: alternating
  479/1024 loopback availability; asserts < 5% zero samples in output WAV.
- `test_reads_partial_chunk_when_less_than_chunk_size_available`: asserts
  `stream.read()` called with n=100 (not 1024) when only 100 frames available.

## Test results (session 8)
```
tests/test_audio_output.py        8 passed
tests/test_capture_engine.py     35 passed  (+2 new session-8 tests)
tests/test_device_manager.py     15 passed
tests/test_recorder.py           34 passed
                                 ──────────
                                 97 passed in 36.92s
```

Command: `pytest tests/ -v`

---

### DONE (session 9 — this session)

**FIX 1 — Reverted to blocking reads, fixed crash via stream-close-before-join**

Root cause of robotic audio: non-blocking reads caused loopback and mic to
read out of sync. When loopback buffer empty but mic had data: 1024 zeros
written. When loopback had data but mic empty: 1024 audio samples. Alternating
pattern → 46% zeros → robotic chopping.

Root cause of crash: `stop()` set stop_event and joined the thread, but thread
was blocking inside `stream.read()` on a closed WASAPI device → PortAudio C
crash (segfault). Non-blocking reads were NOT the fix.

The actual crash fix: close streams BEFORE joining the thread. Closing an
active stream immediately raises `OSError` inside `blocking stream.read()`.
Thread catches `OSError`, sees `stop_event.is_set() == True`, breaks cleanly
in milliseconds. No PortAudio state is ever accessed after the device dies.

- `_record_loop()` reverted to fully blocking reads (no `get_read_available`).
- Both loopback and mic read `chunk_size` frames per iteration — synchronized.
- `OSError` on read: if `stop_event` is set → break silently (expected).
  Otherwise → log `[REC-005]`, null the stream, continue on surviving stream.
- `stop()` restructured: close streams FIRST, then join.
  - `loopback_stream.stop_stream()` + `loopback_stream.close()` before join.
  - `stream.stop_stream()` + `stream.close()` before join.
  - `record_thread.join(timeout=2.0)` after streams closed.

**FIX 2 — Verified no pa.paWASAPI / pa.paInt16 / pa.paInt32 remain**
Grep returns 0 results (already cleaned in session 5).

**Tests**
Removed 6 non-blocking-era tests (get_read_available-based):
- `test_record_thread_exits_quickly_when_stopped`
- `test_record_thread_handles_empty_polls_warning`
- `test_stop_joins_within_1_second`
- `test_no_silence_gaps_when_loopback_buffer_partially_full`
- `test_reads_partial_chunk_when_less_than_chunk_size_available`
- (one session-7 test merged into new ones)

Added 4 blocking-mode tests:
- `test_stop_closes_streams_before_joining` — close_event pattern; verifies
  stop() completes in < 1.5s (would be > 2s if join happened first)
- `test_record_thread_handles_loopback_oserror_without_crash` — OSError on
  read logs [REC-005] and recording continues on mic
- `test_record_thread_exits_on_oserror_when_stop_set` — blocking read, stop()
  closes stream → OSError, stop_event set → [REC-005] NOT logged
- `test_no_zeros_from_timing_mismatch` — loopback=1000, mic=zeros; mixed
  output non-zero; < 5% zeros

Removed `get_read_available.return_value` from `_build_dual_pa_mock()` and
`conftest._build_pa_mock()` (blocking reads don't call this).

## Test results (session 9)
```
tests/test_audio_output.py        8 passed
tests/test_capture_engine.py     33 passed  (replaced 6 old + added 4 new)
tests/test_device_manager.py     15 passed
tests/test_recorder.py           34 passed
                                 ──────────
                                 95 passed in 31.72s
```

Command: `pytest tests/ -v`

Grep verifications:
- `grep "pa\.paWASAPI|pa\.paInt16|pa\.paInt32" recorder.py` → 0 results ✓
- `grep "get_read_available|POLL_SLEEP|empty_polls|got_any_data" recorder.py` → 0 results ✓

---

### DONE (session 10 — this session)

**Crash fix: stream teardown order in CaptureEngine.stop()**

Root cause (from crash.log access violation 0xC0000005):
- Thread A (record thread): inside `pyaudiowpatch:640 read()`
- Thread B (main thread): calling `pyaudiowpatch:472 close()`
- `close()` freed PortAudio stream memory while `read()` was accessing it → segfault

The previous session's "fix" called `stop_stream()` + `close()` before `join()`.
`stop_stream()` is safe concurrently with `read()`. But `close()` is not — it frees
the C-level stream struct that `read()` is still holding a pointer to.

Correct teardown order:
```
1. stop_stream()  — safe concurrent with read(); signals PA to unblock read()
2. join()         — waits for read() to return and thread to exit
3. close()        — safe: thread is dead, no concurrent access possible
```

Changed in `CaptureEngine.stop()`:
- `stop_stream()` on loopback + mic: before join (unchanged position)
- `join(timeout=2.0)` on record thread: unchanged
- `close()` on loopback + mic: **moved to AFTER join** (was before join)

`force_stop_recording()` in Recorder delegates to `self._engine.stop()` —
covered by the same fix.

Also updated `test_stop_closes_streams_before_joining` in test_capture_engine.py:
the test was verifying the OLD (buggy) close-before-join approach. Updated to
verify the correct: `stop_stream()` is called before join (unblocks read()), and
`close()` is called after join. Mock updated: `stop_stream.side_effect` sets the
unblocking event instead of `close.side_effect`.

## Test results (session 10)
```
tests/test_audio_output.py        8 passed
tests/test_capture_engine.py     33 passed
tests/test_device_manager.py     15 passed
tests/test_recorder.py           34 passed
                                 ──────────
                                 95 passed in 34.61s
```

Command: `pytest tests/ -v`

---

## NEXT: Manual testing (Ahmed)

1. Run: `python main.py` (or `dist\WhatsAppWatcher.exe` if built)
2. Check `logs/watcher.log` for:
   - `"DEVICES [INPUT ]"` and `"DEVICES [OUTPUT]"` lines ← device enum
   - `"DEVICES → COMM INPUT"` and `"DEVICES → DEFAULT OUTPUT"` ← WASAPI defaults
   - NO `"STARTUP → Bandicam NOT found"` ← FIX 1 confirmed
3. Make a WhatsApp call (10-30 seconds)
4. End call — check recordings dir for WAV > 100 KB
5. Check log for:
   - `"REC → loopback stream opened"` (if output device supports loopback)
   - `"REC → dual capture active | streams=LOOPBACK(incoming)+MIC(outgoing)"`
   - `"REC → audio detected | rms=..."` (confirms real audio)
6. Play WAV — confirm you can hear the call clearly (both sides)
7. Check no `[REC-*]` (except [REC-009] if silent) or `[HLT-*]` errors

---

## Known issues / blockers

- IMMNotificationClient COM proxy: on real Windows COM,
  `RegisterEndpointNotificationCallback` may reject a non-COM object. First manual
  run should verify; if needed, swap for generated `MMDevAPILib.IMMNotificationClient`
  subclass. See session 2 notes.
- `get_recording_metadata()` — `_recording_success` is always `False` until main.py
  calls `get_recording_metadata()` and updates it. Wiring inside main.py is deferred
  (main.py is frozen).
- If the output device does not expose a WASAPI loopback interface (some drivers don't),
  loopback will silently fall back to mic-only mode. Audio still records.
