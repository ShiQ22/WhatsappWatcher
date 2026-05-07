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

_Filled in after commit._

## Notes for Ahmed

- `main.py` was NOT changed. The split logic was already correct.
- `recorder.py`, `state_machine.py`, `storage.py`, `report.py`, `uploader.py`,
  `config.py`, `launcher.py` were NOT changed.
- The DB schema was NOT changed.
- No recording start timing was changed (still starts at ring).
- Tests run in ~52s on this machine (PyAudio and pywinauto initialization).
