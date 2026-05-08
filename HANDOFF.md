# WhatsApp Watcher — Handoff

## Architecture

```
launcher.py
  └── main.py  (poll loop)
        ├── detector.py      → scans WhatsApp call window via UIA / pywinauto
        ├── state_machine.py → tracks call state (IDLE → RINGING → ACTIVE → ENDED)
        ├── recorder.py      → native audio capture (loopback + mic, WAV/MP3)
        ├── storage.py       → SQLite local DB + optional central DB sync
        ├── report.py        → daily .log call report
        └── uploader.py      → copies recording files to network share
```

### Poll loop flow (main.py `run()`)

```
result = detector.poll()
  ↓
[split check]  if live session + new ring event + strong call proof → split old session
               (weak CALL_STARTED — unknown dir, no UIA proof — never splits)
  ↓
sm.transition(result.event)
  ↓
if new ring event → current_session_hwnd = result.hwnd
  ↓
if should record and not recording → recorder.start_recording() [synchronous, fast]
  [REC-011] logged if start takes > 2 s
  ↓
[REC-012] orphan guard — if recorder running and no live session → stop + finalize immediately
  ↓
if terminal state → stop recorder → finalize → sm.reset()
```

**Recorder lifecycle ownership rule:**
`main.py` owns recorder lifecycle synchronously.  `start_recording()` is called
inline in the poll loop — never in a background thread.  When the session resets
or splits, the recorder is already either stopped or not yet started.  There is no
window where a background start can create an orphan recording.

## Session boundary design

A call session is identified by its **hwnd** (Windows handle of the WhatsApp call window).

When a new ring event arrives (`INCOMING_RING`, `OUTGOING_RING`, `CALL_STARTED`):
- If the state machine is in a **live state** (`RINGING_*`, `CONNECTING`, `ACTIVE`) — a split is triggered.
- The old session is deep-copied, ended, and finalized in a background thread.
- The state machine is reset to IDLE; the detector is **not** reset.
- The new ring is processed as a fresh session.

**`_TERMINAL_STATES`** = `{IDLE, ENDED, RECORDER_ERROR, DETECTOR_ERROR}`
Any state not in this set is "live" and subject to splitting.

**Split condition (`split_needed`):**
```python
is_live_session and is_new_call_event
    and not weak_call_started       # CALL_STARTED with is_strong_new_call=False never splits
    and (
        different_hwnd              # result.hwnd != current_session_hwnd
        or strong_new_call          # result.is_strong_new_call
        or sm.state in (RINGING_UNKNOWN, RINGING_INCOMING, RINGING_OUTGOING, CONNECTING)
    )
```

**`is_strong_new_call` rules (detector.py ring emission):**
- `INCOMING_RING` / `OUTGOING_RING`: always `True`.
- `CALL_STARTED` (direction unknown): `True` only if UIA state has `incoming`, `outgoing`,
  `ringing`, `connecting`, `has_end_call_button`, or a known RINGING_LABELS status text.
  Otherwise `False` — this prevents a bare unknown window from splitting a live session.

**"Call ended" window guard (detector.py):**
- If the new/current window's status text is in `ENDED_LABELS` and no ring was emitted
  for this window, return `DetectionResult(None, ...)` — never emit `CALL_STARTED`.
- If a session was tracked (previous hwnd non-None or ring/answered emitted), emit
  `CallEvent.ENDED` with the old session's direction preserved via `previous_direction`
  fallback.

### hwnd identity rules

- Every `DetectionResult` while a call window is present carries `hwnd=win.hwnd`.
- Ring events also carry `is_strong_new_call=True` and `is_new_window=True/False`.
- `ANSWERED` events carry `hwnd` but **not** `is_strong_new_call=True`.
- When hwnd changes during an answered session, `_new_hwnd_during_answered` is set.
  On the next poll, if the new window shows ring proof (`incoming/outgoing/ringing` and
  `not answered`), the ring state is reset and a new ring event is emitted.
  If no ring proof is seen, the answered session is preserved (same call, different window).

## Immediate ENDED return and terminal finalize ownership (2026-05-07)

### Root causes (live log evidence)
1. `detector.poll()` returned ENDED at 14:50:34; `main.py` processed it at 14:51:07 — 33 s gap.
   Cause: `recorder.ensure_recording_alive()` called `_do_mute_check()` **synchronously**.
   `_do_mute_check` → `_check_whatsapp_mute` → full UIA traversal — same 20-30 s blocker
   fixed for `start_recording()`, but was still synchronous in the health-check path.
2. `[REC-012]` fired for normal ENDED calls, stopping the recorder and starting a finalize
   before the terminal block ran.  Terminal block then created a second no-recording finalize.

### Fixes
- `recorder.py` `ensure_recording_alive()`: `_do_mute_check` is now spawned as a daemon thread
  (`mute-check-health`) — never blocks the caller.
- `main.py` health check: `ensure_recording_alive()` is only called when `result.event is None`.
  Any real event bypasses the health check entirely and is handled immediately.
- `main.py` REC-012: added `and not sm.is_terminal_state()` — REC-012 never fires when the
  terminal finalization block is about to run.
- `detector.py` ENDED paths: all four paths build `DetectionResult` first, then update state,
  then return — no INFO log before the return that could introduce ordering surprises.
- `detector.py` [DET-001]: `poll()` logs a WARNING if it takes > 2 s (non-critical path only).

### Critical rule added
`_do_mute_check` must NEVER be called synchronously from any path that runs on the main poll
thread.  It does a full UIA tree traversal.  Always spawn it as a daemon thread.

## Fast back-to-back call boundary (2026-05-07)

### Root cause
`SESSION_WINDOW_GAP_SECONDS = 2.5` was preserving ALL sessions for 2.5 s after the window
disappeared.  For ringing calls (not yet answered), this caused fast back-to-back calls on
the same hwnd to merge into one session.

### Fix
The window-missing block in `detector.py` now branches on `_session_answered_proof_seen`:

- `False` (ringing/calling/connecting): emit `ENDED` immediately — no gap wait.
- `True` (answered/active): keep the 2.5 s gap (WhatsApp can briefly reopen the window).

`_session_generation` (int, never reset) increments on every ring emission.  `DetectionResult`
carries it as `session_generation`.  `main.py` stores `current_session_generation` alongside
`current_session_hwnd`.  `different_generation` (same hwnd, different generation) is an
additional split trigger so that hwnd reuse is caught even if `different_hwnd=False`.

`state.ringing` was added to `strong_new_session` so ringing-label-only windows bypass the
post-terminal cooldown (previously only `incoming`, `outgoing`, `answered`, `connecting`).

### Design rules added
- Do NOT restore the 2.5 s window gap for ringing sessions — it causes fast back-to-back merges.
- `_session_generation`, `_last_ended_hwnd`, `_last_ended_ts`, `_last_ended_direction` are
  in `__init__` ONLY — never in `_reset_internal_state`.
- `current_session_generation` must be reset to 0 in EVERY path that resets `current_session_hwnd`.

## Direction no-downgrade guard (2026-05-08)

`_should_update_direction(new_dir, current_dir)` in `main.py`:
- Returns `False` when `new_dir == "unknown"` and `current_dir` is `"incoming"` or `"outgoing"`.
- Applied to **both** direction propagation blocks: pre-transition (line ~1199) and post-transition.
- **Do NOT replace this with a bare truthy check** — `"unknown"` is truthy.

## Session direction latch (2026-05-08)

File: `data/active_call_session.json`
- Written when direction first becomes proven in a session (`_direction_latched` gate).
- Restored on crash-restart on the first `is_new_call_event` where hwnd or
  session_generation matches the latch AND `saved_at` is within 3600 s.
- Cleared (file deleted) after every finalize-thread `.start()` call in ALL paths:
  terminal, split, crash, orphan, shutdown.
- `_direction_latched` is a `run()` scope bool, reset to `False` in every clear path.
- **Never** restore from latch for a clearly new call (no hwnd/gen match).

## USB hot-swap design (2026-05-08)

### Disconnect path (USB removed)

1. `Recorder._usb_watcher_loop` detects removal → calls `self._engine.on_usb_disconnect()`.
2. `CaptureEngine.on_usb_disconnect()`:
   - Under lock: saves refs, nulls `_loopback_stream` / `_stream` / `_mic_stream`.
   - Outside lock: calls `stop_stream()` on saved refs — NO `close()`.
3. `_record_loop` sees both streams `None`:
   - Writes silence bytes to WAV (preserves wall-clock duration).
   - Calls `_try_reconnect_streams_async()` (non-blocking).
   - Sleeps `chunk_size / mix_rate` seconds, then `continue`s.

### Reconnect path (USB returned)

1. `_usb_watcher_loop` detects return → calls `_on_usb_reconnect()`.
2. `_on_usb_reconnect()` calls `self._engine._try_reconnect_streams_async()`.
3. `_try_reconnect_streams_async()` starts daemon thread (throttled: 2 s min interval).
4. `_try_reconnect_streams()` (daemon): reinitializes PyAudio, reopens loopback and mic.

**Rules:**
- NEVER call `close()` on a WASAPI stream after USB removal — access violation risk.
- NEVER call `pa.open()` on the record-loop thread — it can block for 20-30 s.
- `_reconnect_lock` prevents two reconnect threads starting simultaneously.
- `_try_reconnect_streams_async` is throttled to avoid rapid retry loops.

## What NOT to change casually

| Area | Reason |
|---|---|
| `recorder.py` audio timing/mixing | Complex dual-stream sync; tested separately |
| `uploader.py` retry behavior | DB state machine for uploads; don't change retry counts |
| `storage.py` schema | Central and local DB in sync; schema changes need migration |
| `report.py` naming | File naming convention used by external consumers |
| Recording start trigger | Must start at ring (not answer) to capture full call |
| `detector.reset()` in split path | Must NOT be called; detector already tracks the new window |
| Async recorder start | Must NOT reintroduce unless it includes a session token/cancel mechanism. Previous async start caused orphan recordings when the session reset while start was in-flight. |
| `_do_mute_check` in bg thread | Must remain in daemon thread — UIA traversal takes 20-30 s and must never block `start_recording()` return |
| `close()` on USB removal path | Must NOT call `stream.close()` after USB removal — WASAPI topology is gone; triggers access violation |
| `_try_reconnect_streams` on record thread | Must NOT call `pa.open()` on the record-loop thread — blocks 20-30 s; always use `_try_reconnect_streams_async()` |
| Direction propagation truthy check | Must use `_should_update_direction()` — plain `if result.direction` passes `"unknown"` which can overwrite proven direction |

## Log codes

| Code | Meaning |
|---|---|
| `[REC-001]` | No audio device found at recording start |
| `[REC-002]` | `pa.open()` failed |
| `[REC-003]` | All sample rates failed |
| `[REC-008]` | WAV file open failure |
| `[REC-009]` | Silence detected ≥ 6 s |
| `[REC-011]` | `recorder.start_recording()` engine or context phase took > 2 s (WASAPI exclusive lock) |
| `[REC-012]` | Orphan recorder guard fired — recorder was running with no live session; stopped immediately |
| `[DEV-USB]` | USB audio device state change |
| `[DEV-003]` | No input device |
| `[LATCH]` | Session direction latch: save / restore / clear events |

## How to run

```
python launcher.py
```

Or in worker mode directly (no restart loop):
```
python launcher.py --worker
```

## How to build EXE

```
pyinstaller whatsapp_watcher.spec
```

## How to run tests

```
pytest tests/ -v
```

Or just compile-check:
```
python -m py_compile main.py detector.py state_machine.py recorder.py uploader.py storage.py report.py launcher.py config.py
```

## How to test back-to-back calls manually

1. Start `python launcher.py --worker` in a terminal.
2. Make an outgoing call from WhatsApp desktop.
3. Hang up the call.
4. Immediately have someone call you (or call yourself from another device).
5. Check logs for two separate `STATE → ringing_*` → `STATE → active` → `CALL END` cycles.
6. Check the local SQLite DB (`data/calls.db`) for two distinct rows.
7. Check the recordings output directory for two distinct files.
8. Inspect for `SESSION SPLIT → new call boundary` in the log.

## How to test USB disconnect/reconnect

1. Start watcher.
2. During a call, unplug the USB headset.
3. Observe `[DEV-USB]` log entries.
4. Reconnect the headset; the next call should pick it up.
5. Check `[DEV-USB]` log entry says "next call will use USB device."

## Known limitations

- hwnd-reuse by WhatsApp (same hwnd for two different calls in rapid succession)
  is handled only if `detector.reset()` was called between them (i.e., after a
  proper ENDED event). In the rare case of immediate hwnd reuse with no ENDED
  event, two calls could share a session. This is very rare in practice.
- Call-waiting (simultaneous calls on one device) may not produce two separate
  records if the second call appears as `answered=True` immediately on a new
  window (WhatsApp merges the UI).
- No video call support; audio-only recording.
