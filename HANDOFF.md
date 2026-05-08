# WhatsApp Watcher — Handoff

## Current state (2026-05-08)

**PyAudio recorder backend rejected for production.**

After three major refactors (2026-05-08 rev1 → rev2 → rev3), the PyAudio/WASAPI audio
pipeline still produces robotic audio, duty-cycle artifacts, and USB reconnect failures
confirmed by debug stem analysis.

**Approved next step: FFmpeg recorder backend migration.**

No code changes have been made yet. The repo is anchored on the migration plan.
The next implementer must start Phase 1 (interface inspection and plan) before writing code.

---

## Architecture

```
launcher.py
  └── main.py  (poll loop)
        ├── detector.py      → scans WhatsApp call window via UIA / pywinauto
        ├── state_machine.py → tracks call state (IDLE → RINGING → ACTIVE → ENDED)
        ├── recorder.py      → audio capture (PyAudio now; FFmpeg soon)
        ├── storage.py       → SQLite local DB + optional central DB sync
        ├── report.py        → daily .log call report
        └── uploader.py      → copies recording files to network share
```

---

## What stays unchanged

| Component | Status |
|-----------|--------|
| `detector.py` | Frozen. Do not touch. |
| `state_machine.py` | Frozen. Do not touch. |
| `storage.py` | Frozen. Do not touch. |
| `uploader.py` | Frozen. Do not touch. |
| `report.py` | Frozen. Do not touch. |
| `launcher.py` | Frozen. Do not touch. |
| `main.py` poll logic | Frozen except minor recorder integration points. |
| Direction latch (`data/active_call_session.json`) | Keep as-is. |
| Session boundary design (hwnd + session_generation) | Keep as-is. |
| Back-to-back call split logic | Keep as-is. |
| Upload / DB / report / final file naming | Keep as-is. |
| `Recorder` public interface | Keep all method/property signatures identical. |

---

## What will be replaced

| Component | Replacement |
|-----------|-------------|
| `CaptureEngine` (PyAudio internals) | `FFmpegCaptureEngine` subprocess-based engine |
| `_SourceReader` (loopback/mic reader threads) | FFmpeg handles capture natively |
| `_AudioWriter` (PCM mix + WAV write thread) | FFmpeg handles mixing and output |
| `_JitterBuffer` | Not needed; FFmpeg manages buffering |
| Manual PCM read/mix/write Python logic | FFmpeg subprocess command |
| PyAudio `stream.read()` in any recording path | FFmpeg subprocess I/O |
| `lameenc` MP3 encoding (optional) | FFmpeg `-c:a libmp3lame` or equivalent |
| Debug stems as production diagnostic path | FFmpeg diagnostics via stderr log |

PyAudio may remain for device enumeration and scoring only. It must not be used for recording.

---

## Recorder public interface (must stay stable)

```python
# Properties
is_recording: bool
current_recording_path: Optional[str]
started_at: Optional[datetime]

# Methods
start_recording() -> bool
stop_recording() -> bool
force_stop_recording() -> bool
detach_context() -> RecordingContext
detach_contexts() -> List[RecordingContext]
resolve_final_files(contexts: List[RecordingContext]) -> List[str]
resolve_final_file(ctx: RecordingContext) -> Optional[str]
ensure_recording_alive() -> bool
get_recording_metadata() -> dict
refresh_bandicam_paths() -> bool   # alias kept for main.py compat
```

---

## Implementation phases

### Phase 0 — Documentation (current, complete)
- `CLAUDE.md` created.
- `HANDOFF.md`, `PROJECT_MEMORY.md`, `CHANGELOG.md` updated.
- `recorder_plan/skills/audio-capture.md` updated.
- `changes/ffmpeg-backend-migration-2026-05-08.md` created.
- No production code changed.

### Phase 1 — Interface inspection and plan
- Read `recorder.py` fully.
- Map exact `main.py` → `Recorder` call sites.
- Design backend abstraction boundary.
- Confirm plan with Ahmed before any code.

### Phase 2 — Backend skeleton
- `DeviceResolver`: discovers local WASAPI devices via FFmpeg device listing.
- `FFmpegCaptureEngine`: starts/stops FFmpeg subprocess, basic single segment.
- `bin/ffmpeg.exe` availability check on start.
- Config keys: `backend`, `ffmpeg_path`, `ffmpeg_mode`, `ffmpeg_stall_threshold_seconds`, etc.
- Compile + test + commit.

### Phase 3 — Recorder integration
- Wire `FFmpegCaptureEngine` into `Recorder` preserving all public interface.
- All `start_recording()`, `stop_recording()`, `force_stop_recording()`, `detach_contexts()`,
  `resolve_final_files()`, `ensure_recording_alive()` implemented.
- Tests pass. Commit.

### Phase 4 — Health monitoring + USB recovery
- `ProcessWatchdog`: detects FFmpeg process death.
- `FileGrowthMonitor`: detects FFmpeg stall (file not growing).
- `USBRecoveryLoop`: closes segment on unplug, starts new segment on replug.
- Segment metadata tracking. Commit.

### Phase 5 — Segment merge with gap preservation
- `SegmentMerger`: concat copy → fallback re-encode.
- Gap calculation via `time.monotonic()`.
- Generated silence WAV for USB unplug gap.
- Failure behavior: preserve all original segments.
- Commit.

### Phase 6 — Validation and production switch
- Manual test on Ahmed's PC.
- Manual test on representative PCs (USB headset, built-in mic, different headset brand).
- Switch `"backend": "ffmpeg"` as default in `config.json`.
- Final doc update. Commit.

---

## Testing requirements

### Automated
```
pytest tests/ -v
python -m py_compile main.py detector.py state_machine.py recorder.py uploader.py storage.py report.py launcher.py config.py
```

### Manual (before production)

1. Normal outgoing call → one final file, both sides audible, no robotic audio.
2. Normal incoming call → same.
3. Back-to-back calls (outgoing then incoming ≤2 s apart) → two separate files, no merge.
4. USB headset unplug during call → silence gap → replug → merged final file with silence.
5. Long call (10+ min) → no file size/duration limit issue, finalization works.
6. Loopback-only (no real mic) → recording still produced (loopback only).
7. Built-in mic fallback (USB absent) → correct device selected, no crash.
8. App restart mid-call → direction latch restores direction on crash-restart.

---

## Rollback rule

Keep PyAudio backend available behind `"backend": "pyaudio"` config key until FFmpeg backend
passes all manual tests above on Ahmed's PC and at least one other representative PC.

Do not remove the PyAudio backend until the rollback is no longer needed.

---

## Binary deployment

`bin/ffmpeg.exe` must be present in the project directory on every deployment target.

- Same version-pinned binary on all PCs.
- Not committed to the git repo (too large; add to `.gitignore` if not already excluded).
- Bundled manually via GPO deployment or package script.
- PyInstaller spec must be updated to include `("bin/ffmpeg.exe", "bin")` when building EXE.

---

## Session boundary design (unchanged from 2026-05-07)

A call session is identified by its **hwnd** (Windows handle of the WhatsApp call window).

When a new ring event arrives while a session is live, a split is triggered:
- Old session deep-copied, ended, finalized in background thread.
- State machine reset to IDLE; detector is NOT reset.
- New ring processed as fresh session.

`_session_generation` increments on every ring. Same-hwnd reuse detected by generation mismatch.

**Split condition:**
```python
is_live_session and is_new_call_event
    and not weak_call_started
    and (different_hwnd or strong_new_call or sm.state in ringing/connecting states)
```

---

## Direction no-downgrade guard (2026-05-08)

`_should_update_direction(new_dir, current_dir)` in `main.py`:
- Returns `False` when `new_dir == "unknown"` and `current_dir` is `"incoming"` or `"outgoing"`.
- Applied to both direction propagation blocks.
- Do NOT replace with a bare truthy check — `"unknown"` is truthy.

---

## USB hot-swap design (current PyAudio — reference for FFmpeg migration)

### Disconnect
1. `_usb_watcher_loop` detects removal.
2. Streams nulled under lock; `stop_stream()` only — NEVER `close()` after USB removal (access violation).
3. Record loop writes silence + calls `_try_reconnect_streams_async()`.

### Reconnect
1. Non-blocking daemon thread reopens loopback + mic.
2. `_reconnect_disabled = True` set by `stop()` to prevent reconnect during finalization.

**FFmpeg equivalent:** close current subprocess → save segment → start new subprocess when device returns.

---

## Immediate ENDED return and terminal finalize ownership (2026-05-07)

- `_do_mute_check()` must NEVER be called synchronously on the main poll thread.
- `ensure_recording_alive()` only called when `result.event is None`.
- REC-012 guard includes `and not sm.is_terminal_state()`.

---

## Log codes

| Code | Meaning |
|---|---|
| `[REC-001]` | No audio device found at recording start |
| `[REC-002]` | `pa.open()` failed (PyAudio era) |
| `[REC-003]` | All sample rates failed (PyAudio era) |
| `[REC-008]` | WAV file open failure |
| `[REC-009]` | Silence detected ≥ 6 s |
| `[REC-011]` | `recorder.start_recording()` took > 2 s |
| `[REC-012]` | Orphan recorder guard fired |
| `[REC-014]` | Buffer overflow (PyAudio era) |
| `[DEV-USB]` | USB audio device state change |
| `[DEV-003]` | No input device |
| `[LATCH]` | Session direction latch: save / restore / clear |

---

## How to run

```
python launcher.py
```

Or worker mode directly:
```
python launcher.py --worker
```

## How to build EXE

```
pyinstaller whatsapp_watcher.spec
```

Note: spec must be updated to bundle `bin/ffmpeg.exe` before Phase 2+ EXE builds.

## How to run tests

```
pytest tests/ -v
```

Compile check only:
```
python -m py_compile main.py detector.py state_machine.py recorder.py uploader.py storage.py report.py launcher.py config.py
```

---

## What NOT to change casually

| Area | Reason |
|---|---|
| `uploader.py` retry behavior | DB state machine for uploads |
| `storage.py` schema | Central and local DB must stay in sync |
| `report.py` naming | File naming used by external consumers |
| Recording start trigger | Must start at ring, not answer |
| `detector.reset()` in split path | Must NOT be called; detector tracks new window |
| Async recorder start | Must NOT reintroduce without session token/cancel |
| `_do_mute_check` | Must remain in daemon thread |
| Direction propagation | Must use `_should_update_direction()` |

---

## Known limitations

- hwnd-reuse by WhatsApp with no ENDED event between calls may rarely merge two calls.
- Call-waiting (simultaneous calls) may not produce two records if WhatsApp merges the UI.
- No video call support; audio-only recording.
