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
