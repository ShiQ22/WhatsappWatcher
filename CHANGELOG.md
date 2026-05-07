# Changelog

## 2026-05-07 — Fix back-to-back call session boundaries

### Problem fixed
Back-to-back WhatsApp calls could be merged into a single call record and
recording instead of producing two separate records.

### Root cause
Three bugs in `detector.py`:

1. **Wrong `previous_hwnd` in FIX-2 log** — `self._call_hwnd` was reassigned
   to the *new* hwnd at line 683 before the `_new_hwnd_during_answered` log
   block ran at line ~716. Both `old_hwnd=` and `new_hwnd=` in the log
   showed the same (new) value, obscuring the real hwnd transition.

2. **Missing `hwnd` on ANSWERED result** — the `DetectionResult` returned for
   `CallEvent.ANSWERED` did not carry `hwnd=win.hwnd`.

3. **Missing `hwnd` on active no-event and ongoing-phase results** — two more
   `DetectionResult` returns while a call window exists did not carry
   `hwnd=win.hwnd`, violating the field contract required by future
   hwnd-based split detection.

`main.py`'s split logic (`split_needed` condition, deepcopy snapshot,
finalize thread, no `detector.reset()` in the split path) was already
correct and required no changes.

### Files changed
| File | Change |
|---|---|
| `detector.py` | Save `previous_hwnd = self._call_hwnd` before `if new_window:`; use `previous_hwnd` in FIX-2 log; add `hwnd=win.hwnd` to ANSWERED, active no-event, and ongoing-phase results |
| `tests/test_session_lifecycle.py` | New class `TestDetectionResultHwndComplete` (5 tests); new class `TestSingleCallRegressions` (2 tests) |
| `CHANGELOG.md` | This file |
| `HANDOFF.md` | New |
| `PROJECT_MEMORY.md` | New |
| `changes/session-boundary-fix-2026-05-07.md` | New |

### Behavior now guaranteed
- Every `DetectionResult` emitted while a call window exists carries
  `hwnd=win.hwnd`.
- The FIX-2 log correctly shows `old_hwnd=<old> | new_hwnd=<new>`.
- `main.py` correctly splits sessions for all back-to-back patterns:
  outgoing→incoming, incoming→outgoing, outgoing→outgoing (different hwnd),
  incoming→incoming (different hwnd), active→new ring.
- A single call starting from IDLE never triggers a false split.

### Tests added (7 new)
| Test | Verifies |
|---|---|
| `test_answered_result_includes_hwnd` | ANSWERED result carries `hwnd=win.hwnd` |
| `test_active_noevent_result_includes_hwnd` | Active no-event result carries `hwnd=win.hwnd` |
| `test_ongoing_phase_result_includes_hwnd` | Ongoing-phase result carries `hwnd=win.hwnd` |
| `test_previous_hwnd_used_in_fix2_log_not_new_hwnd` | FIX-2 log shows old hwnd (11111), not new hwnd (22222) |
| `test_hwnd_change_no_ring_proof_preserves_answered_session` | No ring proof on new window → session preserved, no false split |
| `test_single_outgoing_call_no_false_split` | Single outgoing call from IDLE: `is_live_session=False` → no split |
| `test_single_incoming_call_no_false_split` | Single incoming call from IDLE: `is_live_session=False` → no split |

**Total test count: 165 passed, 0 failed.**

---

## 2026-05-07 follow-up — Phantom call-ended sessions + non-blocking recorder

### Problems fixed

**Bug A — phantom "Call ended" session (live production log):**
After `recorder.start_recording()` blocked the poll loop for ~26 s (WASAPI
exclusive mode on a USB device), the detector saw a new hwnd=853328 already
showing UIA status "Call ended."  The ended-by-UI guard
`(ring_event_emitted or answered_event_emitted)` evaluated to `False` (both
had been reset by the hwnd-change handler).  Ring emission fired
unconditionally, emitting `CALL_STARTED` with `is_strong_new_call=True`.
`main.py` split the live outgoing session and created a phantom
`ringing_unknown` call that finalized with no recording file.

**Bug B — blocking recorder start (WASAPI lock):**
`recorder.start_recording()` → `CaptureEngine.start()` → `pa.open()`
(exclusive mode) blocked the main poll loop for 10–30 s.  Any events that
occurred during that window were missed entirely.

### Root causes & fixes

| Layer | Change |
|---|---|
| `detector.py` — extended ended-by-UI guard | Added `or (new_window and previous_hwnd is not None)` to the guard — when hwnd changes during a non-answered session `ring_event_emitted` is reset before the check, but `previous_hwnd` non-None proves a tracked session existed |
| `detector.py` — ENDED_LABELS guard in ring emission | Early return when `not ring_event_emitted` and the new window's status is already in `ENDED_LABELS` (handles Case B: first window ever shows "Call ended") |
| `detector.py` — conditional `is_strong_new_call` | Unknown-direction ring with no UIA proof (`incoming`/`outgoing`/`ringing`/`connecting`/`has_end_call_button`/`RINGING_LABELS`) gets `is_strong_new_call=False` |
| `detector.py` — preserve `previous_direction` | Captured `previous_direction = self._call_direction` before the hwnd-change reset; used as fallback in ENDED result so direction is not lost |
| `main.py` — `weak_call_started` guard | `CALL_STARTED` with `is_strong_new_call=False` cannot split a live session; can still start a session from IDLE |
| `main.py` — background recorder start | `_bg_start_recorder()` helper runs `recorder.start_recording()` in a daemon thread; main loop checks result each iteration; logs `[REC-011]` if startup > 2 s |
| `main.py` — join-before-finalize | Before terminal-state finalization: `_recorder_start_thread.join(timeout=5.0)` |
| `main.py` — join-before-split | Before session split: `_recorder_start_thread.join(timeout=3.0)` |

### Files changed

| File | Change |
|---|---|
| `detector.py` | Extended ended-by-UI guard; ENDED_LABELS early-return; conditional `is_strong_new_call`; `previous_direction` fallback in ENDED result |
| `main.py` | `_bg_start_recorder()` function; background thread vars; `weak_call_started` split guard; join-before-terminal/split; thread reset after terminal/split |
| `tests/test_session_lifecycle.py` | New `TestPhantomCallEndedSession` (5 tests); new `TestWeakCallStarted` (5 tests) |
| `tests/test_recorder.py` | New `TestRecorderBackgroundStart` (5 tests, incl. [REC-011]) |

### Behavior now guaranteed

- A "Call ended" window (any hwnd, any language) never emits `CALL_STARTED`.
- An unknown-direction ring with no UIA proof never splits a live session.
- `recorder.start_recording()` never blocks the detector poll loop.
- If recorder startup takes > 2 s, `[REC-011]` is logged with elapsed time.
- `_bg_start_recorder()` absorbs any exception, stores False, never re-raises.

### Tests added (15 new — total: 180 passed, 0 failed)

| Test | Verifies |
|---|---|
| `TestPhantomCallEndedSession::test_ended_status_new_hwnd_does_not_emit_call_started` | Exact live-bug reproduction: old hwnd→new "Call ended" hwnd never fires CALL_STARTED |
| `TestPhantomCallEndedSession::test_arabic_ended_status_new_hwnd_does_not_emit_call_started` | Arabic "انتهت المكالمة" also blocked |
| `TestPhantomCallEndedSession::test_ended_status_new_hwnd_with_tracked_session_emits_ended` | Case A: old session → new "Call ended" hwnd → ENDED with correct direction |
| `TestPhantomCallEndedSession::test_ended_status_without_tracked_session_returns_none` | Case B: first window ever shows "Call ended" → None (no ENDED, no ring) |
| `TestPhantomCallEndedSession::test_ended_status_same_window_ring_emitted_still_emits_ended` | Regression: same-window existing behavior preserved |
| `TestWeakCallStarted::test_call_started_unknown_no_proof_is_not_strong_new_call` | Bare window, no proof → `is_strong_new_call=False` |
| `TestWeakCallStarted::test_call_started_with_ringing_proof_is_strong` | `state.ringing=True` → `is_strong_new_call=True` |
| `TestWeakCallStarted::test_weak_call_started_does_not_split_live_ringing_session` | Weak CALL_STARTED cannot split a live session in main.py |
| `TestWeakCallStarted::test_strong_outgoing_ring_still_splits_live_session` | Strong OUTGOING_RING still splits correctly |
| `TestWeakCallStarted::test_weak_call_started_from_idle_still_creates_session` | Weak CALL_STARTED from IDLE starts a new session (no split) |
| `TestRecorderBackgroundStart::test_bg_start_recorder_stores_true_on_success` | Success path stores True in result_box |
| `TestRecorderBackgroundStart::test_bg_start_recorder_stores_false_on_failure` | Failure path stores False |
| `TestRecorderBackgroundStart::test_bg_start_recorder_stores_false_on_exception` | Exception path stores False, no re-raise |
| `TestRecorderBackgroundStart::test_bg_start_recorder_logs_rec011_when_slow` | Elapsed > 2 s → [REC-011] logged |
| `TestRecorderBackgroundStart::test_bg_start_recorder_does_not_log_rec011_when_fast` | Elapsed ≤ 2 s → [REC-011] not logged |

### Manual verification checklist
After deploying, Ahmed should run these manual tests:

- [ ] Make one outgoing call (answer it, hang up). Check: one record in DB, one recording file.
- [ ] Make one incoming call (answer it, hang up). Check: one record, one file.
- [ ] Make outgoing call, let it ring, cancel. Then immediately make an incoming call. Check: two separate records.
- [ ] Make outgoing call, answer it, talk, hang up. Immediately make another outgoing call. Check: two separate records and two recording files.
- [ ] Make incoming call, answer it. While active, receive second incoming call (if WhatsApp call-waiting is available). Check: two separate records.
- [ ] Check logs for `SESSION SPLIT` entries — each must show distinct `old_hwnd` and `new_hwnd`.
- [ ] Check logs for `DETECTOR → hwnd changed during answered session; new call proof found` — `old_hwnd` must differ from `new_hwnd`.
