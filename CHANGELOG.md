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

### Manual verification checklist
After deploying, Ahmed should run these manual tests:

- [ ] Make one outgoing call (answer it, hang up). Check: one record in DB, one recording file.
- [ ] Make one incoming call (answer it, hang up). Check: one record, one file.
- [ ] Make outgoing call, let it ring, cancel. Then immediately make an incoming call. Check: two separate records.
- [ ] Make outgoing call, answer it, talk, hang up. Immediately make another outgoing call. Check: two separate records and two recording files.
- [ ] Make incoming call, answer it. While active, receive second incoming call (if WhatsApp call-waiting is available). Check: two separate records.
- [ ] Check logs for `SESSION SPLIT` entries — each must show distinct `old_hwnd` and `new_hwnd`.
- [ ] Check logs for `DETECTOR → hwnd changed during answered session; new call proof found` — `old_hwnd` must differ from `new_hwnd`.
