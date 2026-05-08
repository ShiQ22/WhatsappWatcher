# WhatsApp Watcher — C# Migration Audit
# Phase 0 deliverable

**Date:** 2026-05-08
**Status:** Approved by Ahmed — documentation phase complete
**Next step:** Ahmed approval of this document → Phase 1 RecorderHelper prototype begins

---

## 0. Decision Summary

### What is approved

Build a standalone **C# .NET 8 RecorderHelper.exe** controlled by the existing Python app.

Python remains the sole source of truth for:

- WhatsApp process/window detection
- Call state machine
- Direction latch
- Session split logic
- Finalization
- File naming
- Local SQLite DB
- Central MySQL sync
- Pending upload queue
- SMB upload
- Daily reports
- Crash/orphan recovery
- Config loading and logging

C# RecorderHelper.exe is responsible only for:

- Listing audio devices
- Selecting microphone
- Selecting speaker/render device for loopback
- WASAPI microphone capture
- WASAPI loopback capture (speaker output)
- Writing internal recording segments (WAV)
- USB disconnect/replug segment recovery
- Inserting silence gaps between segments
- Merging all segments into one final WAV file
- Optional MP3 encode (FFmpeg post-capture only, if enabled later)

### What is rejected

**FFmpeg live capture is rejected.**
The tested FFmpeg build has no `-f wasapi` support. DirectShow only exposes the
microphone, not speaker loopback. FFmpeg cannot be the live capture engine on this hardware.

**PyAudio recording is rejected.**
The PyAudio/WASAPI pipeline has been patched across three major revisions (rev1 → rev2 → rev3)
and continues to produce robotic audio, 50% duty-cycle artifacts, and USB reconnect failures
confirmed by debug stem analysis. Root cause is the manual PCM read/mix/write model in Python,
not any single fixable bug.

**Full C# rewrite of the Python app is deferred.**
It is a valid later-phase option only after RecorderHelper.exe proves clear audio quality and
USB recovery in production. No detection, state machine, DB, upload, or report code will be
rewritten in this phase.

---

## 1. Why RecorderHelper.exe, Not a Full Rewrite

A full C# rewrite in one pass would require all 10+ behavioral subsystems to be correct
simultaneously before the first production call. The risk of a silent regression in detection,
direction logic, DB writes, or upload paths is high.

The RecorderHelper model isolates the only broken subsystem (audio capture) while leaving all
proven systems untouched.

| Approach | Risk | Benefit |
|---|---|---|
| Full C# rewrite | High: 10+ systems rewritten at once | Single binary |
| C# RecorderHelper.exe | Low: only audio capture changes | Python detection/DB/upload untouched |
| C# RecorderHelper.exe + later optional full rewrite | Lowest: prove audio first | Staged risk |

**Approved path:** RecorderHelper.exe first. Full C# rewrite as a later optional phase only after
RecorderHelper passes all acceptance tests in production.

---

## 2. Current Python Architecture (Frozen Systems)

```
launcher.py          restart loop (MAX_RESTARTS_PER_HOUR=20, RESTART_DELAY=5s)
  └── main.py        poll loop (0.8s interval)
        ├── detector.py         WhatsApp window/UIA polling — FROZEN
        ├── state_machine.py    call state transitions — FROZEN
        ├── recorder.py         audio capture — TO BE REPLACED by RecorderHelper.exe
        ├── storage.py          local SQLite + central MySQL sync — FROZEN
        ├── report.py           daily .log reports — FROZEN
        └── uploader.py         SMB copy with retry — FROZEN
```

Files that must not be modified in this phase:
`detector.py`, `state_machine.py`, `storage.py`, `uploader.py`, `report.py`, `launcher.py`

Files that will change in this phase:
`recorder.py` — new backend using RecorderHelper.exe subprocess
`config.py` / `config.json` — new recorder config section
`main.py` — minimal integration changes only (orphan seg scanning, temp file exclusion)

---

## 3. Exact Python Recorder Public Interface (must stay stable)

`main.py` uses only these methods and properties of `Recorder`. All signatures must remain
identical after the backend swap.

```python
# Properties
recorder.is_recording: bool
recorder.current_recording_path: Optional[str]
recorder.started_at: Optional[datetime]
recorder.bandicam_path: str            # backward compat — can return config.py __file__
recorder.bandicam_output_dir: Path     # must return actual recording output directory

# Methods
recorder.start_recording() -> bool
recorder.stop_recording() -> bool
recorder.force_stop_recording() -> bool
recorder.detach_context() -> RecordingContext
recorder.detach_contexts() -> List[RecordingContext]
recorder.resolve_final_files(contexts: List[RecordingContext]) -> List[str]
recorder.resolve_final_file(ctx: RecordingContext) -> Optional[str]
recorder.ensure_recording_alive() -> bool
recorder.get_recording_metadata() -> dict
recorder.refresh_bandicam_paths() -> bool    # backward compat alias; may be no-op
```

**Critical behaviors that must not change:**

- `start_recording()` must return within 2 s under normal conditions. If it takes > 2 s,
  main.py logs `[REC-011]`. Do not block the poll loop.
- `stop_recording()` / `force_stop_recording()` must never raise an unhandled exception.
- `detach_contexts()` must return all accumulated recording contexts for the current call,
  then clear the internal list so the next call starts clean.
- `resolve_final_files()` does the actual merge/finalize work. It runs in a background
  finalize thread and may block. That is acceptable.
- `ensure_recording_alive()` must NOT call any synchronous UIA/mute-check code. Any such
  check must run in a daemon thread.
- `bandicam_output_dir` is used by the orphan recovery code to scan for `*_seg1.wav` files.
  It must point to the same directory where RecorderHelper writes its segments.

---

## 4. RecorderHelper.exe — Design

### 4.1 Process model

RecorderHelper.exe runs as a **long-lived subprocess per call session**.

Python starts one instance at `recorder.start_recording()`.
Python sends a stop command at `recorder.stop_recording()` or `recorder.force_stop_recording()`.
RecorderHelper writes all segments and the final merged file, then exits.
Python reads the final file path from RecorderHelper's stdout before the process exits.

One RecorderHelper.exe instance = one call session.
No shared state between calls.

### 4.2 IPC protocol (stdin/stdout, newline-delimited JSON)

Python → RecorderHelper (via stdin):

```json
{"cmd": "start", "output_dir": "C:/Users/.../Recordings", "call_ts": "2026-05-08_13-20-44", "sample_rate": 48000, "channels": 1}
{"cmd": "stop"}
{"cmd": "force_stop"}
{"cmd": "ping"}
```

RecorderHelper → Python (via stdout):

```json
{"event": "ready", "mic": "USB Headset Microphone", "render": "USB Headset (Speakers)"}
{"event": "started", "seg": 1, "path": "C:/.../2026-05-08_13-20-44_seg1.wav"}
{"event": "seg_saved", "seg": 1, "path": "C:/.../2026-05-08_13-20-44_seg1.wav", "duration_ms": 45230}
{"event": "usb_lost", "seg": 1}
{"event": "usb_restored", "seg": 2, "path": "C:/.../2026-05-08_13-20-44_seg2.wav"}
{"event": "merged", "path": "C:/.../2026-05-08_13-20-44_seg1.wav", "segments": 2}
{"event": "merge_failed", "segments": ["...seg1.wav", "...seg2.wav"], "error": "..."}
{"event": "error", "code": "REC-001", "message": "No audio device found"}
{"event": "pong"}
```

All messages are one JSON object per line. RecorderHelper flushes stdout after every write.
Python reads lines until the process exits or a `merged`/`merge_failed` event is received.

stderr is used by RecorderHelper for diagnostic/verbose logging. Python drains it in a daemon
thread and writes to `logs/watcher.log` under `[HELPER]` prefix.

### 4.3 Segment file naming

RecorderHelper must name segments using this exact pattern:

```
{call_ts}_seg{N}.wav
```

Examples:
```
2026-05-08_13-20-44_seg1.wav
2026-05-08_13-20-44_seg2.wav
```

`call_ts` is provided by Python at start time. It is the same timestamp that will be used for
the final file name. Using the same timestamp means orphan recovery code in main.py that
glob-searches `*_seg1.wav` will continue to work without modification.

### 4.4 Internal temp files (must not be uploaded or recovered)

During capture, RecorderHelper may write:

```
{call_ts}_seg{N}_mic.wav         raw microphone capture
{call_ts}_seg{N}_loopback.wav    raw loopback capture
{call_ts}_seg{N}_mixed.wav       mixed segment (intermediate)
{call_ts}_silence_{N}.wav        generated silence for USB gap
{call_ts}_concat_list.txt        ffmpeg concat list (if used for encode)
```

None of these are the final file. Python's `_is_debug_stem()` filter in main.py currently
excludes files ending in `_mic_debug.wav` and `_loopback_debug.wav`. This filter will need
a targeted update to also exclude `_mic.wav`, `_loopback.wav`, `_silence_*.wav`, and
`_concat_list.txt` patterns.

This is the only required change to main.py other than recorder.py integration.

### 4.5 Device selection

DeviceResolver must:

1. Enumerate WASAPI capture devices via `MMDeviceEnumerator`.
2. Enumerate WASAPI render devices via `MMDeviceEnumerator`.
3. Score mic candidates: prefer default communications device, then USB/headset by name.
4. Never select a `[Loopback]` device as the microphone.
5. Select render device for loopback capture: prefer default communications render device.
6. If no mic found: start loopback-only (log warning, continue recording remote side).
7. If no render device found: log error, return failure event.
8. Log selected device names in `ready` event.
9. Re-enumerate devices on every call start and after USB reconnect.

Config keys (to be added to `config.json` under a `"recorder_helper"` section):

```json
{
  "recorder_helper": {
    "enabled": true,
    "exe_path": "bin/RecorderHelper.exe",
    "mic_name_patterns": ["USB", "Headset", "Microphone"],
    "render_name_patterns": ["USB", "Headset", "Speakers", "Headphones"],
    "prefer_default_communications_device": true,
    "allow_builtin_mic_fallback": true,
    "sample_rate": 48000,
    "channels": 1,
    "preserve_usb_gap_silence": true,
    "gap_silence_threshold_seconds": 0.5,
    "keep_temp_segments": false,
    "startup_timeout_seconds": 5.0,
    "stop_timeout_seconds": 60.0,
    "ping_interval_seconds": 5.0
  }
}
```

### 4.6 USB disconnect/replug behavior

1. RecorderHelper detects USB removal (MMNotificationClient or device error on WASAPI stream).
2. Current segment is closed and saved. `seg_saved` event emitted.
3. `usb_lost` event emitted.
4. RecorderHelper enters wait loop checking for device return.
5. When device returns, new segment starts. `usb_restored` event emitted.
6. At call end: merge all segments with silence gaps inserted between them.
7. Gap duration = monotonic time between `seg_saved` and `usb_restored`.
8. If gap > `gap_silence_threshold_seconds`: generate silence WAV of exact gap duration.
9. Insert silence between adjacent segments in the final merge.

RecorderHelper does NOT finalize the call on USB disconnect. It waits for the Python `stop`
command. Python never sees the segment boundary — it only sees the final merged file path.

### 4.7 Merge behavior

At stop command:

1. Close current segment. Write `seg_saved` event.
2. Build merge list: `[seg1, silence_gap_1, seg2, silence_gap_2, seg3, ...]` (only real segs + gaps above threshold).
3. Mix each segment: mic + loopback → mixed WAV (if separate source files were written).
4. Merge all mixed segments using NAudio `WaveFileWriter` concat.
5. On success: emit `merged` event with final file path. Delete temp files unless `keep_temp_segments=true`.
6. On merge failure: emit `merge_failed` with list of real segment paths. Never delete real segments.

Fallback: if merge fails, return all real mixed segment files individually. Python's
`resolve_final_files()` already handles multiple paths and will rename and upload each.

### 4.8 Mixing strategy

For each segment, mix mic and loopback into one mono WAV:

- Sample-by-sample mixing with configurable gain (default: mic=0.75, loopback=0.65).
- Clamp to int16 range.
- If only one source available (e.g., no mic, or loopback-only): use that source directly.
- Write mixed output as the segment WAV for merge.

Do not use FFmpeg for mixing. Use NAudio `ISampleProvider` mixing.

### 4.9 Process watchdog (internal to RecorderHelper.exe)

RecorderHelper monitors its own capture health:

- `FileGrowthMonitor`: checks that the current segment WAV is growing. If file size does not
  increase in 8 seconds, log warning and attempt stream restart.
- Unhandled exception in capture thread → log to stderr → attempt graceful segment save → emit
  `error` event → exit with non-zero code.
- Python's `ensure_recording_alive()` sends a `ping` command. RecorderHelper replies `pong`.
  If no pong within 3 seconds, Python treats the helper as dead and calls `force_stop_recording()`.

---

## 5. Python Integration Plan (recorder.py only)

### 5.1 Backend abstraction

`recorder.py` will gain a backend abstraction layer:

```python
class _RecorderBackend(ABC):
    @abstractmethod
    def start(self, output_dir: str, call_ts: str) -> bool: ...
    @abstractmethod
    def stop(self) -> List[str]: ...       # returns list of final file paths
    @abstractmethod
    def force_stop(self) -> List[str]: ...
    @abstractmethod
    def is_alive(self) -> bool: ...
    @abstractmethod
    def ping(self) -> bool: ...

class _PyAudioBackend(_RecorderBackend):
    """Legacy PyAudio backend. Active when config backend=pyaudio."""
    ...

class _RecorderHelperBackend(_RecorderBackend):
    """New C# RecorderHelper.exe subprocess backend."""
    ...
```

Config key `"recorder_helper.enabled": true` selects the new backend.
Config key `"recorder.backend": "pyaudio"` forces the legacy backend for rollback.

### 5.2 RecordingContext changes

`RecordingContext` gains one new field:

```python
@dataclass
class RecordingContext:
    pre_start_snapshot: Set[str]
    start_marker: Optional[float]
    started_at: Optional[datetime]
    output_dir: Optional[str] = None
    segment_index: int = 1
    output_path: Optional[str] = None
    helper_final_paths: List[str] = field(default_factory=list)  # NEW
```

`helper_final_paths` holds the final file paths returned by RecorderHelper. Used by
`resolve_final_files()` to return the correct paths without scanning the filesystem.

### 5.3 resolve_final_files() behavior change

When using the RecorderHelper backend, `resolve_final_files()` returns `ctx.helper_final_paths`
directly. No filesystem scan needed. The merge already happened inside RecorderHelper.

When using the PyAudio backend (rollback), existing behavior is unchanged.

### 5.4 ensure_recording_alive() change

When using the RecorderHelper backend, `ensure_recording_alive()` sends a `ping` command.
If RecorderHelper does not respond within 3 seconds, the backend is considered dead.
A dead backend triggers the same orphan-recorder recovery path as before.

### 5.5 start_recording() timing constraint

RecorderHelper.exe startup must produce a `ready` event within `startup_timeout_seconds` (5s).
If not received, `start_recording()` returns False and logs `[REC-011]`.
This preserves the existing 2s warning behavior — `[REC-011]` fires if total start > 2s.

### 5.6 main.py changes (minimal)

Only two targeted changes are allowed in main.py:

**Change 1 — temp file exclusion in _is_debug_stem():**
```python
def _is_debug_stem(path: Path) -> bool:
    name = path.name
    return (
        name.endswith("_mic_debug.wav")
        or name.endswith("_loopback_debug.wav")
        or name.endswith("_mic.wav")
        or name.endswith("_loopback.wav")
        or ("_silence_" in name and name.endswith(".wav"))
        or name.endswith("_concat_list.txt")
    )
```

**Change 2 — Seg file exclusion from finalization scan:**
The `_list_recoverable_recording_files()` function already excludes debug stems via
`_is_debug_stem()`. After Change 1, this function will also exclude RecorderHelper temp files.
No other change to that function is needed.

No other changes to main.py are permitted in this phase.

---

## 6. Exact Acceptance Tests (before Python integration)

RecorderHelper.exe must pass every test below before any Python integration begins.
Ahmed must run these tests manually on his PC and sign off.

### Test 1 — Device listing

```
RecorderHelper.exe --list-devices
```

Expected output (stdout):
```
Capture devices:
  [0] USB Headset Microphone (USB Audio Device) — default-communications
  [1] Microphone Array (Intel Smart Sound Technology)
Render devices:
  [0] USB Headset (USB Audio Device) — default-communications
  [1] Speakers (Realtek Audio)
Selected mic:    USB Headset Microphone
Selected render: USB Headset (USB Audio Device)
```

Pass criteria:
- At least one capture device listed
- At least one render device listed
- Selected mic is the USB headset (not loopback, not a render device)
- Selected render is the headset speakers

### Test 2 — 15-second recording

```
RecorderHelper.exe --record-test --seconds 15 --output-dir C:\Temp\RecTest
```

During test: speak into headset mic and play audio through headset speakers.

Pass criteria:
- File `C:\Temp\RecTest\test_seg1.wav` created
- File size > 0 bytes
- WAV is readable by any player
- Duration is approximately 15 seconds
- **Mic side is clear** (no robotic, no echo, no silence)
- **Loopback side is clear** (no robotic, no silence)
- Both voices audible in the mixed output

### Test 3 — USB unplug during recording

```
RecorderHelper.exe --record-test --seconds 30 --output-dir C:\Temp\RecTest
```

During test at second ~10: unplug USB headset. At second ~20: replug USB headset.

Pass criteria:
- `usb_lost` event emitted after unplug
- `seg_saved` event for seg1
- `usb_restored` event after replug
- `started` event for seg2
- After 30 seconds: `merged` event
- Final WAV contains: audio from seg1, silence for ~10s gap, audio from seg2
- Silence gap duration approximately matches actual unplug duration (±2s)
- seg1.wav and seg2.wav preserved if `keep_temp_segments=true`

### Test 4 — USB unplug with no replug

```
RecorderHelper.exe --record-test --seconds 30 --output-dir C:\Temp\RecTest
```

During test at second ~10: unplug USB headset. Do NOT replug.

Pass criteria:
- `usb_lost` event emitted
- `seg_saved` for seg1
- After 30 seconds: stop command sent
- Final merged WAV contains only seg1 audio (no second segment)
- OR: `merged` event with single-segment merge
- No crash, no hang

### Test 5 — No mic fallback

Run test on a machine with only a built-in mic (no USB headset) if available, or temporarily
with `"allow_builtin_mic_fallback": true` and USB disconnected.

Pass criteria:
- RecorderHelper selects built-in mic (logged as fallback)
- Recording still produces a file (loopback + built-in mic mixed)
- No crash

### Test 6 — Loopback-only mode (no real mic)

Configure `"allow_builtin_mic_fallback": false` and disconnect USB headset.

Pass criteria:
- `ready` event with mic=null or mic=none
- Recording still starts (loopback only)
- Produced WAV contains remote audio only
- No crash

### Test 7 — 10-minute call

```
RecorderHelper.exe --record-test --seconds 600 --output-dir C:\Temp\RecTest
```

Pass criteria:
- No stall detected (file growth monitor stays quiet)
- Final WAV is approximately 10 minutes long
- File size matches expected WAV size for 48000 Hz / 1 ch / 16-bit / 600s
- No truncation at any boundary

### Test 8 — Force stop

Send `{"cmd": "force_stop"}` within 2 seconds of start.

Pass criteria:
- RecorderHelper saves partial segment
- Emits `seg_saved` with duration < 3s
- Process exits cleanly (exit code 0)
- File is valid (even if very short)

---

## 7. Python Integration Phases

These phases begin only after all acceptance tests pass and Ahmed approves.

### Phase RH-1 — Backend skeleton in recorder.py

- Add `_RecorderHelperBackend` class stub.
- Add config keys to `config.py` and `config.json`.
- Backend selected by config; defaults to `pyaudio` (no production change yet).
- No call to RecorderHelper.exe yet.
- Compile check: `python -m py_compile recorder.py config.py`.

Approval gate: compile succeeds, existing PyAudio tests pass.

### Phase RH-2 — RecorderHelper subprocess start/stop

- Implement `_RecorderHelperBackend.start()` and `stop()`.
- Launch `RecorderHelper.exe`, send JSON commands, read events from stdout.
- Drain stderr in daemon thread → log under `[HELPER]`.
- Implement `ready` event handling → set device names in metadata.
- Implement `merged` / `merge_failed` event handling → return final paths.
- Set `"recorder_helper.enabled": true` in local test `config.json` only.
- Manual test: make one actual WhatsApp call. Verify file produced.

Approval gate: one call produces a clean WAV. Ahmed signs off on audio quality.

### Phase RH-3 — USB recovery and health check

- Implement `ensure_recording_alive()` ping/pong check.
- Implement USB lost/restored event handling (logging only; RecorderHelper handles recovery).
- Manual test: USB unplug mid-call → silence gap → replug → single merged final file.

Approval gate: USB test produces merged file with silence gap.

### Phase RH-4 — Rollback config and production switch

- Verify `"recorder.backend": "pyaudio"` still works (rollback path).
- Switch `"recorder_helper.enabled": true` in production `config.json` on test PC.
- Run all acceptance tests from Section 6 on live calls.
- Run on at least one additional representative PC.

Approval gate: Ahmed approves audio quality on production calls. No regressions in DB,
upload, report, or detection behavior.

### Phase RH-5 — Documentation

- Update `CHANGELOG.md`.
- Update `HANDOFF.md` recorder section.
- Update `PROJECT_MEMORY.md`.
- Update `CLAUDE.md` recorder direction.

---

## 8. Full C# Rewrite — Later Optional Phase

A full C# rewrite of the entire Python application is deferred.

It becomes viable only when:

1. RecorderHelper.exe has passed all acceptance tests in production.
2. Audio quality is confirmed clear on at least 3 representative PCs.
3. Ahmed explicitly decides to proceed.

When that decision is made, a new audit document will be created covering the full rewrite.
The current document does not govern that work.

The behavior inventory and parity map in Section 9 of this document will serve as the
starting input for that future audit.

---

## 9. Behavior Inventory (for future full rewrite reference)

### 9.1 State machine

States: `IDLE`, `RINGING_UNKNOWN`, `RINGING_INCOMING`, `RINGING_OUTGOING`, `CONNECTING`,
`ACTIVE`, `ENDED`, `RECORDER_ERROR`, `DETECTOR_ERROR`

Events: `CALL_STARTED`, `INCOMING_RING`, `OUTGOING_RING`, `ANSWERED`, `CONNECTING`, `ENDED`,
`MISSED`, `CANCELLED`, `DETECTOR_FAIL`, `RECORDER_FAIL`, `RESET`

Terminal states (trigger finalization): `ENDED`, `RECORDER_ERROR`, `DETECTOR_ERROR`
States where recording should be active: `RINGING_UNKNOWN`, `RINGING_INCOMING`,
`RINGING_OUTGOING`, `CONNECTING`, `ACTIVE`

### 9.2 Call detection

- Window title exact match: `"Voice call"` (English) or `"مكالمة صوتية"` (Arabic)
- Window class exact match: `"WinUIDesktopWin32WindowClass"`
- Primary UIA scanner: `comtypes` → `UIAutomationCore.dll` → `IUIAutomation`
- Fallback: `pywinauto` `Desktop(backend="uia")`
- UIA traversal limits: depth=15, max elements=500, max scan time=3.0s
- Critical AutomationIds: `CallStatusText`, `CallerTextBlock`, `NewEndCallButton`
- Direction: high-confidence from `ACCEPT_LABELS`+`DECLINE_LABELS` (incoming) or `RINGING_LABELS` on `CallStatusText` (outgoing)
- Ringing session: ENDED immediately on window disappear (no gap)
- Active session: 2.5s gap before ENDED
- Stale ringing timeout: 5.0s with no strong call UI
- Post-terminal hard suppress: 4.0s; soft cooldown: 15.0s
- Session generation: increments on each ring emit; never reset by reset(); used for same-hwnd rapid reuse detection

### 9.3 Direction latch

- File: `data/active_call_session.json`
- Fields: `direction`, `hwnd`, `session_generation`, `started_at`, `saved_at`
- Save: when direction first proven (`incoming`/`outgoing`) and hwnd is assigned
- Restore: on first ring after crash-restart, only if hwnd or generation matches and `saved_at` ≤ 1 hour ago
- Clear: inside `_maybe_clear_latch_for_session()` during finalize thread, compared by `started_at` to avoid cross-session clear

### 9.4 Session split condition

```python
split_needed = (
    is_live_session
    and is_new_call_event
    and not weak_call_started
    and (
        different_hwnd
        or different_generation
        or strong_new_call
        or sm.state in (RINGING_UNKNOWN, RINGING_INCOMING, RINGING_OUTGOING, CONNECTING)
    )
)
```

`weak_call_started`: CALL_STARTED with `is_strong_new_call=False` — cannot split live session.

### 9.5 Finalization order (non-negotiable)

```
stop_recording()
detach_contexts()
→ background thread:
    resolve_final_files()     # merge happens here (or inside RecorderHelper)
    rename_recording_for_session()
    storage.save_call()
    _maybe_clear_latch_for_session()
    uploader.upload_for_session()
    storage.update_uploaded_path()    # only after successful upload
    reporter.append_call()
```

### 9.6 File naming

```
{normalized_phone}-{user_slug}-{direction_slug}-{YYYY-MM-DD_HH-MM-SS}[-partN].{ext}
```

- `normalized_phone`: digits only, `+` → `00`
- `user_slug`: spaces → `-`, invalid filename chars stripped
- `direction_slug`: same slug rules
- `ext`: from source file suffix (currently `.mp3`; will be `.wav` with RecorderHelper)
- Collision: append `_1`, `_2`, etc.

### 9.7 Report format

File: `data/reports/call_report-YYYY-MM-DD.log`

Header line: `========...=======\nDATE: YYYY-MM-DD\n========...=======\n`

Call line:
```
[HH:MM:SS AM/PM]  user=X  machine=X  ip=X  direction=X  duration=Xs  started=HH:MM:SS AM/PM  ended=HH:MM:SS AM/PM  number=X  file=X  uploaded=X  error=X  status=X
```

### 9.8 Local SQLite schema

```sql
-- calls
id INTEGER PK AUTOINCREMENT
start_time TEXT          -- "YYYY-MM-DD HH:MM:SS"
end_time TEXT
duration INTEGER         -- seconds
status TEXT
direction TEXT
pc_user TEXT
machine_name TEXT
machine_ip TEXT
caller_number TEXT
recording_path TEXT
uploaded_path TEXT
error_details TEXT
central_synced INTEGER   -- 0 or 1
central_synced_at TEXT
central_sync_error TEXT

-- pending_uploads
id INTEGER PK AUTOINCREMENT
call_local_id INTEGER
local_path TEXT NOT NULL
dest_rel_path TEXT NOT NULL
pc_user TEXT
machine_name TEXT
machine_ip TEXT
retries INTEGER DEFAULT 0
max_retries INTEGER DEFAULT 3
last_error TEXT
uploaded_path TEXT
status TEXT DEFAULT 'pending'   -- pending | failed | uploaded
created_at TEXT DEFAULT CURRENT_TIMESTAMP
updated_at TEXT DEFAULT CURRENT_TIMESTAMP
```

### 9.9 Central MySQL schema

```sql
-- call_logs
id BIGINT AUTO_INCREMENT PK
source_call_uid VARCHAR(255) UNIQUE   -- "{machine_name}:{pc_user}:{local_id}"
start_time DATETIME
end_time DATETIME
duration INT
status VARCHAR(50)
direction VARCHAR(20)
pc_user VARCHAR(100)
machine_name VARCHAR(100)
machine_ip VARCHAR(50)
caller_number VARCHAR(50)
recording_path TEXT
uploaded_path TEXT
error_details TEXT
created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
```

Write strategy: `INSERT ... ON DUPLICATE KEY UPDATE` via `source_call_uid`.
Local write always first. Central tried immediately; failure → mark unsynced → background retry.

### 9.10 Upload path structure

```
{UPLOAD_ROOT_DIR}/{year}/{month_name}/{dd-mm-yyyy}/{pc_user}/{recordings_subdir}/{filename}
```

- `year`: 4-digit year
- `month_name`: full English month name (IMPORTANT: must use `en-US` culture on any C# implementation)
- `day_folder`: `dd-mm-yyyy` format
- `pc_user`: Windows login name

Upload write: copy to `{dst}.partial`, verify size stability (3 stable checks × 0.5s), then
`rename` to final. Delete local source if `delete_local_after_success=true`.

### 9.11 Launcher behavior

- `MAX_RESTARTS_PER_HOUR = 20`
- `RESTART_DELAY_SECONDS = 5`
- Worker mode: `--worker` flag runs `main.run()` once
- Launcher writes to `logs/launcher.log`
- Clean exit on exit code 0

---

## 10. Open Questions (must be answered before Phase RH-2)

1. **Output extension**: RecorderHelper produces WAV. Current Python app produces MP3. The file
   naming uses the source file's extension. After integration, all new recordings will have `.wav`
   extension. Are external consumers of the upload share / reports / DB ready to receive `.wav`
   files?

2. **`fallback_root_dir` in config.json**: Present in `config.json` but not consumed in
   `config.py`. Is this a planned feature or dead config? Should it be ported?

3. **Arabic locale on target PCs**: Upload path uses full English month name (e.g., "May").
   If any PC has Arabic Windows locale, a future C# implementation must force `CultureInfo("en-US")`
   for path generation. Not an issue for RecorderHelper (Python handles paths), but must be noted
   for future full rewrite.

4. **`debug_stems: true` in production `config.json`**: This generates `*_mic_debug.wav` and
   `*_loopback_debug.wav` per call. These are PyAudio-era debug files. Should the C# RecorderHelper
   produce equivalent per-source temp files, or should `debug_stems` be disabled in production?

5. **Test PC**: Is there a dedicated non-production PC available for Phase RH-2 live call testing?

6. **Central DB `source_call_uid`**: Existing rows in the central MySQL DB were generated by
   Python with format `{machine_name}:{pc_user}:{local_id}`. If a future C# full rewrite
   generates the same format, upserts will work. This does not affect RecorderHelper (Python
   still writes the DB), but must be confirmed for future phases.

---

## 11. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| RecorderHelper produces robotic audio | Medium | High | Acceptance tests (Sec 6) before Python integration |
| USB replug creates gap in wrong location | Medium | Medium | Test 3 acceptance criteria |
| IPC latency causes `[REC-011]` false positives | Low | Low | 5s startup timeout; startup measured in Python |
| RecorderHelper crashes mid-call | Low | High | Python force_stop_recording() + orphan recovery; seg files preserved |
| Merge failure loses recording | Low | High | `merge_failed` event returns real seg paths; Python uploads them individually |
| PyAudio rollback broken by backend abstraction change | Low | Medium | Backend abstraction must not change PyAudio code path; test with `backend=pyaudio` after RH-1 |
| Python debug stem filter missing new temp file patterns | Medium | Low | Change 1 in main.py (Section 5.6) |
| NAudio WASAPI not working on a specific PC | Unknown | High | Test on multiple PCs before production rollout |

---

*Document status: Phase 0 complete. Approved by Ahmed 2026-05-08.*
*Next action: Ahmed approves this document → RecorderHelper.exe prototype begins (Phase RH-1).*
