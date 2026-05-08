# Changelog

## 2026-05-08 rev3 — Non-blocking readers, jitter buffer, mic channel mode, evidence-based USB reinit

### Root causes fixed

1. **50% duty-cycle audio islands (both sources):** Both LoopbackReader and MicReader were
   calling blocking `stream.read(chunk)` then sleeping 0ms (because `elapsed ≈ block_seconds`).
   On the next read, the WASAPI ring buffer had just been drained → blocked for another full
   `block_seconds`. Writer received alternating real/silence frames at 20ms granularity:
   active audio runs ≤40ms in 92-93% of cases (confirmed from debug stems).

   Fix (both readers): Non-blocking poll loop — check `get_read_available()` every 2ms up to
   one full chunk budget. If insufficient frames after budget: **push silence** (not skip),
   increment underflow counter. WASAPI loopback is allowed to fall through to blocking read
   immediately when `get_read_available` is unsupported.

2. **Pop-latest discards valid frames:** `_FrameBuffer.pop_latest_or_silence()` discarded
   all frames except the newest. When a reader was slightly late delivering its frame, the
   buffer had only 1 frame → writer took it → next tick buffer empty → silence → 50% pattern.

   Fix: Replace `_FrameBuffer` with `_JitterBuffer` (FIFO, maxlen=4). `pop_or_hold()` returns
   oldest frame (maintains continuity), holds last-good frame for up to `MAX_HOLD=1` tick on
   underrun, then silence. Overflow drops oldest (logs `[REC-014]`).

3. **Mic 2-channel mode — wrong channel selected:** Averaging L+R is correct only when both
   channels carry audio. If only one headset channel has audio (common with mono USB mics),
   averaging halves the amplitude vs the active channel.

   Fix: Per-1-second window, compute L_rms, R_rms, LR_corr. Use stronger channel if one is
   4× the other; average if both similar and corr ≥ 0.5; else use stronger channel.

4. **USB reinit on every reconnect attempt:** Prior code called `reinit_pyaudio()` on every
   reconnect cycle. On a live system with stable USB, this caused unnecessary PA teardown.

   Fix: Evidence-based reinit only. Compare live DeviceManager device names vs a fresh
   `pyaudio.PyAudio()` snapshot. Only force reinit when fresh PA sees a USB/headset mic
   that the live instance is missing.

5. **Writer starts before readers have frames:** Writer's first tick fired immediately on
   thread start, before readers had produced their first frame, causing one silent tick.

   Fix: `next_tick = time.monotonic() + block_seconds` (one tick head-start before first pop).

### Changes

| Symbol | Change |
|--------|--------|
| `_JitterBuffer` | Replaces `_FrameBuffer`; FIFO (oldest-first), maxlen=4, `pop_or_hold()` with MAX_HOLD=1 |
| `_SourceReader._get_avail_unsupported` | Fallback flag: if `get_read_available()` raises, skip to blocking read |
| `_SourceReader._max_polls` | `int(source_block_seconds / 0.002)` — poll budget per tick |
| `_SourceReader.run()` | Non-blocking 2ms-poll loop; underflow pushes silence frame; `_underflow_ticks` counter |
| `_SourceReader._is_mic` | True for MicReader; enables per-second L/R channel analysis |
| `_SourceReader._mic_ch_mode` | `"average"` / `"left"` / `"right"` — updated every second from rms+corr |
| `_AudioWriter._MAX_HOLD` | `= 1` — hold last frame at most 1 tick before writing silence |
| `_AudioWriter._lb_hold / _mic_hold` | Underrun counters; reset on `ok` frame; silence after MAX_HOLD |
| `_AudioWriter.run()` | `next_tick = time.monotonic() + block_seconds` (startup head-start) |
| `CaptureEngine._lb_buf / _mic_buf` | `_JitterBuffer(maxlen=4, ...)` |
| `CaptureEngine.start()` | Resets buffers to fresh `_JitterBuffer` on each call |
| `DeviceManager.get_fresh_usb_mic_name_if_missing()` | New — spawns temporary PA instance, returns missing USB/headset mic name |
| `_try_reconnect_streams()` | USB reinit only when `get_fresh_usb_mic_name_if_missing` confirms mismatch |

### New log lines

```
REC → source underflow | source=MicReader/LoopbackReader | unavailable_ticks=N
REC → get_read_available unsupported; using blocking read fallback | source=...
REC → mic channel mode | mode=left/right/average | L_rms=... | R_rms=... | corr=...
REC → source underrun | source=loopback/mic | hold_ticks=N
REC → fresh-vs-live PA mismatch | fresh sees mic not in live list | device=...
REC → USB replug confirmed via PA mismatch | device=... | forcing PyAudio reinit
REC → fresh PA also sees no new mic | mic stays offline
```

---

## 2026-05-08 — Source pacing, latest-frame anti-echo, real-mic selection

### Root causes fixed

1. **Reader flooding (REC-014 / REC-013):** `_SourceReader.run()` had no sleep after
   a successful read.  On WASAPI-buffered devices `stream.read()` can return in <1 ms,
   causing tight-loop flooding of `_FrameBuffer` and writer starvation.
   Fix: `iter_start` before read; `stop_event.wait(source_block_seconds - elapsed)` after
   push.  Readers now produce at most one frame per 20 ms.

2. **Echo / delayed voice:** `_AudioWriter` used `popleft()` (FIFO).  When the buffer
   had N stale frames the writer mixed 0–N×20 ms old audio, causing audible echo at
   call start and after reconnect bursts.
   Fix: `_FrameBuffer.pop_latest_or_silence()` discards all but the newest frame.

3. **Loopback-as-mic:** `select_best_device()` ranked `Speakers [Loopback]` above
   built-in mic when USB headset was removed (loopback gets `+3` USB score).
   Fix: `select_best_mic_device()` uses `list_real_mic_devices()` which filters out
   any device whose name contains `[loopback]`.

4. **Reconnect/stop race:** reconnect thread could open a new stream after `stop()`
   started, leaking a stream handle.
   Fix: `_reconnect_disabled = True` set under lock in `stop()` before
   `stop_event.set()`; all reconnect entry points return immediately if disabled.

### Changes

| Symbol | Change |
|--------|--------|
| `_is_loopback_device_name(name)` | New module-level helper — `"[loopback]" in name.lower()` |
| `_FrameBuffer` | New thread-safe frame store; replaces raw `deque` in readers + writer |
| `_SourceReader.__init__` | `frame_buf: _FrameBuffer` replaces `queue: deque`; `_source_block_seconds` added |
| `_SourceReader.run()` | Pacing: `wait(source_block_seconds - elapsed)`; throttled pace-lag log |
| `_AudioWriter.__init__` | `lb_buf/mic_buf: _FrameBuffer` replaces raw deques |
| `_AudioWriter.run()` | `pop_latest_or_silence()`; throttled stale-drop log; `writeframesraw()` |
| `DeviceManager.list_real_mic_devices()` | New — filters loopback from input list |
| `DeviceManager.select_best_mic_device()` | New — uses `list_real_mic_devices()`; never returns loopback |
| `CaptureEngine.__init__` | `_lb_buf/_mic_buf: _FrameBuffer`; `_reconnect_disabled: bool` |
| `CaptureEngine.start()` | Resets `_reconnect_disabled`; passes `frame_buf=` to readers |
| `CaptureEngine.stop()` | Sets `_reconnect_disabled = True` under lock before `stop_event.set()` |
| `_try_reconnect_streams_async()` | Returns immediately if `_reconnect_disabled or stop_event` |
| `_try_reconnect_streams()` | Uses `select_best_mic_device()`; post-open stop-race close; new logs |
| `Recorder.start_recording()` | Uses `select_best_mic_device()`; None device → loopback-only start |
| `Recorder.ensure_recording_alive()` | Uses `select_best_mic_device()` |

### Mic selection behaviour

| Situation | Result |
|-----------|--------|
| USB/headset present | USB/headset mic selected (high score) |
| USB absent, built-in exists | Built-in mic used as fallback; logs `REC → using built-in microphone fallback` |
| No real mic | `None` returned; recording starts loopback-only; logs no-mic warning |
| Reconnect USB mid-call | `REC → USB/headset mic restored` |
| Loopback device | Always excluded from mic candidates |

### New log lines

```
REC → excluding loopback device from mic candidates | name=...
REC → no real microphone available — mic source will be silence until reconnect
REC → using built-in microphone fallback | device=...
REC → USB/headset mic restored | device=...
REC → stale frames dropped | source=loopback/mic | dropped=N
REC → source pace lag | source=MicReader/LoopbackReader | behind_ms=...
REC → reconnect skipped; source still unavailable — no real mic found, mic stays offline
```

---

## 2026-05-08 — Three-thread audio pipeline refactor

### Root cause fixed

`loopback.read()` in a WASAPI stall blocks for 2+ seconds.  The old
single `_record_loop` thread did both the blocking read and the WAV write,
so the WAV fell behind wall clock by the full stall duration.  Confirmed in
production: `[REC-013] writer lag | behind_ms=2090.0`, WAV ratio=0.63 on a
19-second call.

### Architecture change

`_record_loop` removed.  Replaced by three independent daemon threads:

| Thread | Class | Owns |
|--------|-------|------|
| LoopbackReader | `_SourceReader` | `loopback.read()` — may block 2 s |
| MicReader | `_SourceReader` | `mic.read()` — blocking in own thread |
| Writer | `_AudioWriter` | WAV writes; `time.monotonic()` wall-clock scheduling |

Writer wakes at exact `next_tick += block_seconds` intervals (absolute, no drift).
Pulls from two `deque(maxlen=24)` queues or fills silence if empty.
Never calls `stream.read()`.

### Other changes in this commit

- `mic_gain=0.75`, `loopback_gain=0.65` config params; float/int32 mix math; clamp to int16
- Level log every 4s: `REC → levels | mic_rms=... | loopback_rms=... | mixed_rms=... | clipped=... | mic_online=... | loopback_online=...`
- `[REC-014]` queue overflow code (throttled logging)
- `on_usb_disconnect()` calls `reader.go_offline()` instead of directly nulling stream refs
- `_try_reconnect_streams()` calls `reader.set_stream()` to inject reconnected stream into running reader thread
- `_trigger_recovery()` simplified: stops readers + marks exhausted; Recorder creates new segment
- `_resample_audio()` promoted to module-level function

### New log patterns

| Pattern | Meaning |
|---------|---------|
| `REC → LoopbackReader started` | Loopback reader thread alive |
| `REC → MicReader started` | Mic reader thread alive |
| `REC → writer started \| mix_rate=...` | Writer thread alive |
| `REC → levels \| mic_rms=... \| ...` | Periodic level log from writer |
| `[REC-013] writer lag \| behind_ms=...` | Writer fell behind (now rare — means CPU stall) |
| `[REC-014] LoopbackReader queue overflow` | Queue full; oldest frame dropped |

---

## 2026-05-08 — Direction no-downgrade, USB silence loop, MP3/48 kHz

### Root causes

**Direction overwrite**: `"unknown"` is a truthy string; the propagation condition
`if result.direction and result.direction != sm.session.direction` fired when
`result.direction == "unknown"` and `sm.session.direction == "outgoing"`, overwriting
the proven direction.

**USB disconnect crash / short WAV**: `_record_loop` called `break` when both streams
were `None`.  The record thread exited; the WAV was finalized at the disconnect timestamp,
not the call end.  Worse: after device removal, the next `pyaudiowpatch.read()` on a dead
WASAPI stream could trigger a Windows access violation (C-level crash).

**Wrong sample rate**: USB device native rate is 48000 Hz; config said 44100.  Watcher
auto-fallback was triggered every call, causing a startup warning.

### Fixes

| Fix | File | Change |
|---|---|---|
| No-downgrade direction guard | `main.py` | `_should_update_direction()`: returns `False` when `new_dir == "unknown"` and `current_dir` is proven |
| Session direction latch | `main.py` | `_save/load/clear_session_latch()` to `data/active_call_session.json`; restored on crash-restart when hwnd or session_generation matches |
| `on_usb_disconnect()` | `recorder.py` | Nulls stream refs under lock; `stop_stream()` only (no `close()`) on USB removal path |
| Silence loop | `recorder.py` | `_record_loop` both-streams-gone: writes silence, calls `_try_reconnect_streams_async()`, sleeps, continues instead of breaking |
| Non-blocking reconnect | `recorder.py` | `_try_reconnect_streams_async()` starts daemon thread; `_try_reconnect_streams()` reinits PyAudio and reopens streams |
| USB watcher calls `on_usb_disconnect` | `recorder.py` | `_usb_watcher_loop` calls `engine.on_usb_disconnect()` immediately on USB removal |
| USB reconnect | `recorder.py` | `_on_usb_reconnect` calls `_try_reconnect_streams_async()` instead of `request_device_switch()` |
| MP3 + 48 kHz | `config.py`, `config.json` | `format=mp3`, `sample_rate=48000`, `chunk_size=960`, `mp3_bitrate=64` |

### New log patterns

| Pattern | Meaning |
|---|---|
| `[LATCH] Restored direction from latch` | Crash-recovery: direction recovered from prior session |
| `REC → USB disconnect handled \| streams detached` | Streams nulled; record loop will write silence |
| `REC → loopback stream reopened after reconnect` | Loopback reopen succeeded |
| `REC → mic stream reopened after reconnect` | Mic reopen succeeded |

---

## 2026-05-07 final² — Immediate ENDED return, async mute health-check, terminal finalize ownership

### Root causes (from live log)

**33-second ENDED delay:**
`detector.poll()` returned ENDED at 14:50:34 (logged "ended by UI status").
`main.py` did not process it until 14:51:07 — 33 seconds later.
Cause: `recorder.ensure_recording_alive()` called `_do_mute_check()` **synchronously**.
`_do_mute_check()` → `_check_whatsapp_mute()` → full UIA tree traversal (20–30 s).
This was the same blocker fixed in `start_recording()`, but the identical call was left in
the health-check path.

**REC-012 stealing terminal finalization:**
After `sm.transition(ENDED)`, `_should_start_recording()=False` and `recorder.is_recording=True`.
REC-012 fired first, stopped the recorder, and started `_finalize_call`.
The normal terminal block then saw `was_recording=False` and created a second `_finalize_no_recording`.
Result: two finalize entries, one with the recording and one marked "no recording."

### Fixes

| Fix | File | Change |
|---|---|---|
| Async mute check in health path | `recorder.py` | `ensure_recording_alive()` now spawns `_do_mute_check` in a daemon thread (`mute-check-health`) — same pattern as `start_recording()` |
| Skip health check on events | `main.py` | `ensure_recording_alive()` only called when `result.event is None`; any real event is processed without delay |
| REC-012 skips terminal states | `main.py` | Added `and not sm.is_terminal_state()` to REC-012 condition; normal ended calls are handled exclusively by the terminal finalization block |
| Detector ENDED paths — no INFO before return | `detector.py` | All four ENDED paths (UI-status, ringing-window-gone, active-window-gone, stale-ringing) now build `DetectionResult` first, update state, call `_reset_internal_state()`, log DEBUG, then return; `main.py` INFO log is the authoritative record |
| [DET-001] poll timing guard | `detector.py` | Logs warning if `detector.poll()` takes > 2 s (checked on the non-critical ongoing-phase return only) |

### Expected log order after fix

```
DETECTOR → event=CallEvent.ENDED   ← same second as detector detection (no 33 s gap)
STATE    → ended
CALL END → ended
RECORDER → stopping for terminal state ended
FINALIZE → starting | dir=outgoing
STATE    → reset to idle (finalize running in background)
```

No `[REC-012]`. No duplicate `FINALIZE → no-recording`.

---

## 2026-05-07 fast back-to-back — Immediate ENDED for ringing sessions, session_generation split

### Problem fixed

Fast back-to-back calls (1–3 seconds apart) were merging into one session and one recording.

### Root cause

`SESSION_WINDOW_GAP_SECONDS = 2.5` was applied to ALL sessions regardless of call phase.
When a ringing/calling window disappeared and a new call appeared within 2.5 s on the same
hwnd, the preservation kept the old session alive with `_ring_event_emitted=True`, so no
new ring event fired and the two calls were treated as one.

### Fixes

| Fix | File | Change |
|---|---|---|
| Immediate ENDED for ringing sessions | `detector.py` | Window-missing block split by `_session_answered_proof_seen`; ringing sessions emit ENDED immediately (no gap); active sessions keep 2.5 s gap |
| Session generation tracking | `detector.py` | `_session_generation` increments on each ring emission; included in `DetectionResult.session_generation` |
| Same-hwnd reuse split | `main.py` | `different_generation` condition: same hwnd + different generation → split triggered |
| `state.ringing` in cooldown bypass | `detector.py` | Added to `strong_new_session` so ringing-label-only windows bypass post-terminal cooldown |
| Post-terminal cooldown bypass log | `detector.py` | `DETECTOR → strong new call bypassed post-terminal cooldown` logged when bypass fires |
| Same-hwnd reuse log | `detector.py` | `DETECTOR → same hwnd reused for new call` logged when WhatsApp recycles a window handle quickly |
| Last-ended metadata | `detector.py` | `_last_ended_hwnd`, `_last_ended_ts`, `_last_ended_direction` saved in all ENDED paths for diagnosis |
| Generation reset in all reset paths | `main.py` | `current_session_generation = 0` on split, terminal, crash, and orphan guard paths |

### Behavior now guaranteed

- A ringing/calling session whose window disappears emits ENDED on the very next poll — no 2.5 s delay.
- A new call starting within 1–3 s of the previous ringing session gets a clean session boundary.
- Active (answered) sessions still tolerate a 2.5 s window gap (WhatsApp can briefly reopen the window during a live call).
- WhatsApp hwnd reuse (same handle for a new call) is detected via `session_generation` and triggers a split in main.py.
- `state.ringing` now bypasses post-terminal cooldown, matching `state.incoming` and `state.outgoing`.

### Manual verification checklist

1. Outgoing call → hang up immediately → new outgoing call within 2 s → two separate records, two files, no merged recording.
2. Outgoing call → hang up → incoming call within 2 s → two records.
3. Single outgoing call (answer + hang up) → one record, one file, no `[REC-012]`.
4. Log must show `DETECTOR → ringing session ended by window disappearance` (not `session ended (no timer proof)`).
5. Log must show `SESSION SPLIT → new call boundary | old_gen=X | new_gen=Y` when a back-to-back call happens via split path.

---

## 2026-05-07 final — Synchronous fast recorder start, orphan guard, log cap

### Problem fixed

After the previous async recorder start change, back-to-back calls became worse:
- Two calls ended up in one recording.
- Recording did not finalize until app was aborted.
- Final file was named "unknown".
- State/session was lost between calls.
- Recorder kept running after the real call was over.

### Root cause: recorder lifecycle ownership broken by async start

`_bg_start_recorder()` ran `recorder.start_recording()` in a daemon thread.
If the detector ended or the session was reset while that thread was still in
`CaptureEngine.start()` → `pa.open()`, main.py would reset the session and
clear `current_session_hwnd`.  The background thread then completed later,
set `recorder._is_recording = True`, and created an active recording with no
valid live session — an **orphan recorder**.

The orphan recorder kept running until app shutdown.  At shutdown, finalization
used the stale (post-reset, IDLE) session state — giving the recording the
direction "unknown" and merging audio from multiple calls into one file.

### Why `_check_whatsapp_mute()` was the actual slow path

The original `start_recording()` blocked because it called `_do_mute_check()`
synchronously, which calls `_check_whatsapp_mute()` → `Desktop(backend="uia")`
→ `desktop.windows(...)` → `win.descendants(control_type="Button")`.
Full UIA tree traversal on the active WhatsApp window takes 20–30 s.

### Fixes

| Fix | File | Change |
|---|---|---|
| Remove async recorder start | `main.py` | Deleted `_bg_start_recorder()`, `_recorder_start_thread`, `_recorder_start_result`, join-before-split, join-before-terminal |
| Synchronous recorder start with timing | `main.py` | Inline `recorder.start_recording()` with `time.monotonic()` wrapper; logs `[REC-011]` if > 2 s |
| Move mute check to background thread | `recorder.py` | `_do_mute_check()` runs in a daemon thread started after `engine.start()` |
| Phase timing in start_recording | `recorder.py` | Logs `REC → start timing | total=...s | engine=...s | context=...s`; `[REC-011]` per slow phase |
| Orphan recorder guard | `main.py` | `[REC-012]` fires if `recorder.is_recording` and not `_should_start_recording(sm)` — stops recorder and finalizes immediately |
| Log file handler to INFO | `main.py` | File handler now respects `LOG_LEVEL` from config (default INFO, not DEBUG) |
| Log rotation cap | `config.py` | `log_backup_count` default raised to 5; `log_level` config option added |

### Behavior now guaranteed

- `recorder.start_recording()` returns in < 2 s under normal conditions (no WASAPI exclusive lock).
- If engine open blocks > 2 s, `[REC-011]` identifies the exact phase.
- Mute check is purely informational; never blocks call recording.
- main.py owns recorder lifecycle synchronously: if session resets, recorder is already stopped or never started.
- `[REC-012]` orphan guard is a last-resort safety net; should never fire after this fix.
- Log files cap at ~25 MB total (5 × 5 MB files) at INFO level.
- `log_level: "DEBUG"` in config.json re-enables verbose debug logging.

### Manual verification checklist

1. Single outgoing call → one file, direction=outgoing, finalizes without closing app, no unknown file, no `[REC-012]`.
2. Single incoming call → one file, direction=incoming, finalizes normally.
3. Outgoing → incoming back-to-back → two separate files, no merged recording, old call finalized before new recorder starts.
4. Outgoing → outgoing → two separate files, no unknown.
5. Incoming → incoming → two separate files, no unknown.
6. Leave app running 30 min → log files rotate, total size stays bounded.
7. Log must show `REC → start timing | total=` within 2 s of call start.
8. No `[REC-012]` in normal operation.

---

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
