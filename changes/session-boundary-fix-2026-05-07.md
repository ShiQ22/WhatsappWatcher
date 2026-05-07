# Session Boundary Fix — 2026-05-07

## Summary

Fixed three bugs in `detector.py` that could cause back-to-back WhatsApp calls
to be merged into a single call record and recording.

## Local vs GitHub comparison

| Check | Result |
|---|---|
| `git status` | Clean (no uncommitted changes) |
| `git diff HEAD origin/main` | Empty — local and GitHub were identical |
| Commit before this fix | `938bdd6 Initial WhatsApp watcher project` |
| Source of truth | Local working tree (matched GitHub exactly) |

## Root cause

### Bug 1 — Wrong `previous_hwnd` in FIX-2 log (`detector.py`)

`self._call_hwnd = win.hwnd` ran at line 683 (inside `if new_window:`) **before**
the `_new_hwnd_during_answered` log block ran at line ~716.

Result: the warning log showed:
```
old_hwnd=22222 | new_hwnd=22222   ← both the NEW value
```
instead of:
```
old_hwnd=11111 | new_hwnd=22222   ← correct
```

Fix: added `previous_hwnd = self._call_hwnd` before the `if new_window:` block.
Used `previous_hwnd` in the FIX-2 log.

### Bug 2 — Missing `hwnd` on ANSWERED result (`detector.py`)

`DetectionResult` for `CallEvent.ANSWERED` did not carry `hwnd=win.hwnd`.
Every result emitted while a call window exists must carry the hwnd so
`main.py` can always know which window the event belongs to.

Fix: added `hwnd=win.hwnd` to the ANSWERED `DetectionResult`.

### Bug 3 — Missing `hwnd` on active no-event and ongoing-phase results (`detector.py`)

Same omission for the "answered active" (no-event) return and the "ongoing
phase" return at the bottom of `poll()`.

Fix: added `hwnd=win.hwnd` to both returns.

## Exact files changed

| File | Lines changed |
|---|---|
| `detector.py` | ~644 (add `previous_hwnd`), ~716 (use `previous_hwnd`), ~866 (ANSWERED `hwnd`), ~872 (active no-event `hwnd`), ~894 (ongoing `hwnd`) |
| `tests/test_session_lifecycle.py` | Added `TestDetectionResultHwndComplete` (5 tests) and `TestSingleCallRegressions` (2 tests) |
| `CHANGELOG.md` | Created |
| `HANDOFF.md` | Created |
| `PROJECT_MEMORY.md` | Created |
| `changes/session-boundary-fix-2026-05-07.md` | This file |

## Tests added / updated

### New: `TestDetectionResultHwndComplete` (5 tests)
- `test_answered_result_includes_hwnd`
- `test_active_noevent_result_includes_hwnd`
- `test_ongoing_phase_result_includes_hwnd`
- `test_previous_hwnd_used_in_fix2_log_not_new_hwnd`
- `test_hwnd_change_no_ring_proof_preserves_answered_session`

### New: `TestSingleCallRegressions` (2 tests)
- `test_single_outgoing_call_no_false_split`
- `test_single_incoming_call_no_false_split`

## Test result summary

```
165 passed, 0 failed, 1 warning (pywinauto COM threading — expected)
Run time: ~52s
```

## Manual test plan (Ahmed must run)

1. **Single outgoing call** — make a call, answer, hang up.
   - Expected: one record in `data/calls.db`, one recording file, no `SESSION SPLIT` in log.

2. **Single incoming call** — receive a call, answer, hang up.
   - Expected: one record, one file, no split log.

3. **Outgoing → incoming back-to-back** — make outgoing call (let it ring),
   cancel, then immediately receive an incoming call.
   - Expected: two separate records, two recording files, one `SESSION SPLIT` log entry.

4. **Incoming → outgoing back-to-back** — receive call (miss or answer+hang up),
   then immediately make an outgoing call.
   - Expected: two separate records.

5. **Outgoing → outgoing (rapid)** — make two outgoing calls in rapid succession.
   - Expected: two separate records.

6. **Incoming → incoming (rapid)** — receive two back-to-back calls.
   - Expected: two separate records.

7. **Active → new ring (different hwnd)** — while in an active call, receive a
   second call on a new WhatsApp window.
   - Expected: split triggered; old call finalized with its direction and recording;
     new call starts fresh.

8. **Log verification**:
   - `SESSION SPLIT → new call boundary` must show distinct `old_hwnd` and `new_hwnd`.
   - `DETECTOR → hwnd changed during answered session; new call proof found` must
     show `old_hwnd=<old_value>` and `new_hwnd=<different_value>`.

## Git commit hash

`11f46c5` — pushed to `origin/main`

## Notes for Ahmed

- `main.py` was NOT changed. The split logic was already correct.
- `recorder.py`, `state_machine.py`, `storage.py`, `report.py`, `uploader.py`,
  `config.py`, `launcher.py` were NOT changed.
- The DB schema was NOT changed.
- No recording start timing was changed (still starts at ring).
- Tests run in ~52s on this machine (PyAudio and pywinauto initialization).

---

# Follow-up fix — 2026-05-07 (phantom call-ended sessions + non-blocking recorder)

## Root cause 1 — Phantom CALL_STARTED for "Call ended" window

Live log evidence:
- `12:34:28` OUTGOING_RING → recorder.start_recording() called synchronously
- `12:34:54` recorder started (blocked ~26 s — WASAPI `pa.open()` exclusive mode)
- `12:34:55` detector saw new hwnd=853328 with UIA status "Call ended"
- Ended-by-UI guard: `(ring_event_emitted or answered_event_emitted)` → False
  (both were reset by the hwnd-change handler at line 676)
- Ring emission fired → `CALL_STARTED` with `is_strong_new_call=True`
- main.py split → phantom `ringing_unknown` session with no recording

## Fixes applied (detector.py)

1. **Extended ended-by-UI guard** — added `or (new_window and previous_hwnd is not None)`.
   `previous_hwnd` non-None proves a tracked session existed even after the reset.

2. **ENDED_LABELS guard in ring emission** — when `not ring_event_emitted` and window
   status is in `ENDED_LABELS`, return `DetectionResult(None, ...)` before emitting any ring.

3. **Conditional `is_strong_new_call`** — `CALL_STARTED` for unknown direction gets
   `is_strong_new_call=False` unless UIA state contains positive call proof.

4. **Preserve `previous_direction`** — captured `previous_direction = self._call_direction`
   before the hwnd-change reset; used as fallback in ENDED result so direction is not lost.

## Fixes applied (main.py)

5. **`weak_call_started` guard** — `CALL_STARTED` with `is_strong_new_call=False`
   cannot split a live session; can still start a session from IDLE.

6. **Non-blocking recorder start** — `_bg_start_recorder()` function runs
   `recorder.start_recording()` in a daemon thread. Main loop checks result each
   iteration. Logs `[REC-011]` if startup > 2 s. Join timeouts: 5 s (terminal), 3 s (split).

## Files changed in this follow-up

| File | Changes |
|---|---|
| `detector.py` | Extended ended-by-UI guard; ENDED_LABELS early-return; conditional `is_strong_new_call`; `previous_direction` fallback |
| `main.py` | `_bg_start_recorder()` helper; background thread + result box; `weak_call_started` guard; join-before-terminal/split; thread resets |
| `tests/test_session_lifecycle.py` | `TestPhantomCallEndedSession` (5 tests); `TestWeakCallStarted` (5 tests) |
| `tests/test_recorder.py` | `TestRecorderBackgroundStart` (5 tests) |

## Additional manual tests (beyond original checklist)

9. **Recorder blocks 26 s** — simulate WASAPI lock (e.g., exclusive audio app
   holding device). Start watcher, make an outgoing call.
   - Expected: `[REC-011]` logged; session recorded normally once device is free;
     no phantom "ringing_unknown" session.

10. **"Call ended" window immediately** — call WhatsApp from a second device, reject
    immediately so the window briefly shows "Call ended."
    - Expected: ENDED emitted, no CALL_STARTED. One record with direction=outgoing/incoming.

11. **Log verification (follow-up)**:
    - `[REC-011]` must show elapsed time in seconds.
    - No `SESSION SPLIT` must follow a window showing "Call ended" status.

## Git commit hash (follow-up)

`1419259` — pushed to `origin/main`

---

# Final lifecycle fix — 2026-05-07 (synchronous fast recorder start)

## Why async recorder start was wrong

`_bg_start_recorder()` ran `recorder.start_recording()` in a daemon thread.
If `detector.poll()` returned ENDED or RESET while the thread was inside
`CaptureEngine.start()` → `pa.open()`, main.py reset `current_session_hwnd`
and called `sm.transition(RESET)`.  The daemon thread then completed, set
`recorder._is_recording = True`, and created an active recording attached
to the post-reset (IDLE) session — an **orphan recorder**.

## Why that caused unknown files and merged calls

The orphan recorder kept recording through the entire next call and until
shutdown.  At shutdown, finalization used the stale IDLE session state
(direction="unknown").  Audio from multiple back-to-back calls merged into
one file.

## Why `_check_whatsapp_mute()` was the actual slow path

`start_recording()` called `_do_mute_check()` synchronously.
`_do_mute_check()` calls `_check_whatsapp_mute()` which does
`Desktop(backend="uia").windows(...)` + `win.descendants(control_type="Button")` —
full UIA tree traversal: 20–30 s on systems with many UI elements.

## Correct design: synchronous fast recorder start

1. `recorder.start_recording()` is called inline in the poll loop.
2. `_do_mute_check()` runs in a daemon thread (purely informational).
3. `[REC-011]` logged if engine or context phase > 2 s.
4. `[REC-012]` orphan guard stops any recorder running without a live session.
5. Log file handler set to INFO by default (was DEBUG); `log_backup_count` = 5.

## Files changed

| File | Change |
|---|---|
| `main.py` | Removed `_bg_start_recorder`, async thread vars, join-before-split/terminal; inline synchronous start with `[REC-011]` timing; `[REC-012]` orphan guard; `LOG_LEVEL` import/use for file handler |
| `recorder.py` | `_do_mute_check` moved to daemon thread; `_t0` / phase timing; `[REC-011]` per slow phase |
| `config.py` | `log_backup_count` default = 5; `log_level` config key added; `LOG_LEVEL` exported |

## Git commit hash (final lifecycle fix)

`b725fbd` — pushed to `origin/main`

---

# Fast back-to-back call boundary fix — 2026-05-07

## Root cause

`SESSION_WINDOW_GAP_SECONDS = 2.5` was applied to ALL sessions when the call window
disappeared.  For a ringing (unanswered) call:

1. Call A ringing (hwnd=X) → ring emitted, `_ring_event_emitted=True`.
2. Window disappears (call rejected/cancelled/missed).
3. Polls during 2.5 s gap: window gone → "preserving session" → no ENDED emitted.
4. Call B starts within 2.5 s on the same hwnd=X:
   - `new_window=False` (same hwnd) → `_ring_event_emitted=True` still → no ring emitted.
   - Calls A and B merged into one session/recording.

## Fixes

### detector.py — window-missing block

Split by `_session_answered_proof_seen`:

```python
if not self._session_answered_proof_seen:
    # Ringing session: emit ENDED immediately
    log.info("DETECTOR → ringing session ended by window disappearance | gap=%.1fs | ...")
    # save _last_ended_* ; reset ; return ENDED
else:
    # Active (answered) session: keep 2.5 s gap
    if gap <= SESSION_WINDOW_GAP_SECONDS:
        return DetectionResult(None, ...)  # preserve
    # gap > 2.5 s: ENDED
```

### detector.py — `_session_generation`

- Added to `__init__` (never reset by `_reset_internal_state`).
- Increments each time a ring event is emitted.
- Included in every ring `DetectionResult` as `session_generation`.

### main.py — `current_session_generation` + `different_generation`

```python
different_generation = (
    current_session_hwnd is not None
    and result_hwnd is not None
    and result_hwnd == current_session_hwnd
    and current_session_generation != 0
    and result.session_generation != current_session_generation
)
split_needed = (is_live_session and is_new_call_event and not weak_call_started
    and (different_hwnd or different_generation or strong_new_call or sm.state in RINGING...))
```

`current_session_generation` is reset to 0 alongside `current_session_hwnd` in split,
terminal, crash, and orphan-guard paths.

### detector.py — post-terminal cooldown improvements

- `state.ringing` added to `strong_new_session` (was missing; blocked ringing-label-only windows from bypassing cooldown).
- `DETECTOR → strong new call bypassed post-terminal cooldown` logged when bypass fires.
- `DETECTOR → same hwnd reused for new call` logged when WhatsApp recycles a window handle within 5 s.

### All ENDED paths — save `_last_ended_*`

`_last_ended_hwnd`, `_last_ended_ts`, `_last_ended_direction` saved before every
`_reset_internal_state()` call (window-missing, ended-by-UI-status, stale-ringing).

## Files changed

| File | Change |
|---|---|
| `detector.py` | Window-missing block split; `_session_generation`; `_last_ended_*`; `state.ringing` in cooldown; bypass log; same-hwnd log |
| `main.py` | `current_session_generation`; `different_generation` split condition; generation reset in all paths |
| `CHANGELOG.md` | New section |
| `HANDOFF.md` | New section |
| `PROJECT_MEMORY.md` | New section |
| `changes/session-boundary-fix-2026-05-07.md` | This entry |

## Git commit hash (fast back-to-back fix)

`91a8c0c` — pushed to `origin/main`

---

# Immediate ENDED return and terminal finalize ownership — 2026-05-07

## Root causes (live log evidence)

**33-second ENDED delay:**
- `detector.poll()` returned `ENDED` at 14:50:34 (logged "ended by UI status").
- `main.py` did not log/process it until 14:51:07.
- Cause: `recorder.ensure_recording_alive()` called `_do_mute_check()` **synchronously**,
  which does `Desktop(backend="uia").windows(...)` + `win.descendants(...)` — the same
  20-30 s UIA blocker already fixed for `start_recording()`, but was left in the
  health-check path.

**REC-012 stealing terminal finalization:**
- After `sm.transition(ENDED)`, `_should_start_recording()=False`, `recorder.is_recording=True`.
- REC-012 condition matched → stopped recorder → started `_finalize_call`.
- Terminal block then ran: `was_recording=False` → created a second `_finalize_no_recording`.
- Result: two finalize threads; recording in one, "no recording" marker in the other.

## Fixes

| Fix | File | Change |
|---|---|---|
| Async mute check in health path | `recorder.py` | `ensure_recording_alive()`: `_do_mute_check` spawned as daemon thread `mute-check-health` |
| Skip health check on events | `main.py` | Added `result.event is None` guard — any real event bypasses health check entirely |
| REC-012 skips terminal states | `main.py` | Added `and not sm.is_terminal_state()` |
| Detector ENDED paths — result-first | `detector.py` | All 4 ENDED paths build result, update state, reset, DEBUG-log, return — no INFO before return |
| [DET-001] timing guard | `detector.py` | Warns if `poll()` takes > 2 s (non-critical ongoing path only) |

## Git commit hash (immediate ENDED fix)

TBD — commit pending
