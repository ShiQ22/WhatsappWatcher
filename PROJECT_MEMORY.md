# Project Memory — WhatsApp Watcher

## Bandicam removed

Native audio recorder replaced Bandicam completely. Bandicam-related aliases
(`bandicam_path`, `bandicam_output_dir`, `refresh_bandicam_paths`) are kept as
backward-compat properties that return `None` / `False` so existing callers
don't crash. All recording goes through `CaptureEngine` (PyAudio loopback +
microphone).

## Audio fix history

- **Dual-stream mixing**: loopback (system audio) + microphone captured in
  parallel and mixed sample-by-sample with int16 clamping.
- **Stereo-to-mono conversion**: loopback opens as stereo when possible;
  converted to mono before mixing with mic.
- **Silence detection**: `[REC-009]` logged if both streams silent for >6s.
- **Duration ratio check**: `[REC-010]` logged if written frames < 70% of
  expected frames for elapsed time (indicates timing mismatch or buffer stall).
- **Watchdog recovery**: dead record thread is detected and restarted; new
  segment index allocated on each recovery. Gives up after
  `RECORDER_WATCHDOG_RECOVERY_ATTEMPTS`.

## Direction no-downgrade guard + session latch (2026-05-08)

**Guard**: `_should_update_direction(new_dir, current_dir)` returns `False` when
`new_dir == "unknown"` and `current_dir` is `"incoming"` or `"outgoing"`.
Applied to both direction-propagation blocks in `main.py run()`.

**Session latch** (`data/active_call_session.json`):
- Saved when direction first proven (gate: `_direction_latched` flag in `run()` scope).
- Restored on crash-restart if hwnd or session_generation matches and `saved_at` <= 3600 s ago.
- Cleared (file deleted) immediately after each finalize thread `.start()` in ALL paths.
- `_direction_latched` reset to `False` in every latch-clear path.

**Critical rule**: never restore latch for a clearly new call (no hwnd/gen match).

## USB hot-swap fix (2026-05-08)

**Root cause 1 — access violation**: `pyaudiowpatch.read()` on a dead WASAPI stream
after USB removal can crash the process (Windows C-level fault, uncatchable).
Fix: `CaptureEngine.on_usb_disconnect()` nulls stream refs under lock before the record
loop can read them.  `stop_stream()` only — never `close()` after USB removal.

**Root cause 2 — short WAV**: record loop broke out when both streams were `None`.
Fix: writes silence, calls `_try_reconnect_streams_async()`, sleeps, continues.
WAV wall-clock duration is preserved.

**Root cause 3 — robotic mic after reconnect**: `_on_usb_reconnect` triggered watchdog
path which only worked when the record thread was dead.
Fix: `_try_reconnect_streams_async()` / `_try_reconnect_streams()` always reopens
loopback + mic unconditionally after reinit_pyaudio().

**Critical rules**:
- NEVER `close()` a WASAPI stream after USB removal.
- NEVER call `pa.open()` on the record-loop thread (blocks 20-30 s).
- `_try_reconnect_streams_async()` is throttled: 2 s min between attempts.

## MP3 / 48 kHz output (2026-05-08)

Config: `format=mp3`, `sample_rate=48000`, `chunk_size=960`, `mp3_bitrate=64`.
USB device native rate is 48000 Hz; 44100 was always being auto-rejected and falling back.
MP3 @ 64 kbps is transparent for voice and reduces file size ~4× vs WAV.

## Immediate ENDED return + terminal finalize ownership fix (2026-05-07)

**Root cause 1 — 33-second delay:** `ensure_recording_alive()` called `_do_mute_check()` synchronously.
`_do_mute_check` does a full UIA traversal (20-30 s). Fixed: spawn in daemon thread (`mute-check-health`).

**Root cause 2 — REC-012 double finalize:** REC-012 guard ran for ENDED state (non-live,
`_should_start_recording=False`), stopping recorder before terminal block ran. Fixed: added
`and not sm.is_terminal_state()` to REC-012 condition.

**Defense:** `ensure_recording_alive()` only called when `result.event is None` — any real event
bypasses health checks and is processed immediately.

**Detector ENDED paths:** All four terminal paths (UI-status, ringing-window-gone,
active-window-gone, stale-ringing) now build result first, then reset state, then return.
No INFO log before return.

**Rule:** `_do_mute_check` MUST NEVER be called synchronously on the main poll thread.

## Fast back-to-back call boundary fix (2026-05-07)

**Root cause:** `SESSION_WINDOW_GAP_SECONDS = 2.5` preserved ALL sessions for 2.5 s after the
window disappeared.  For unanswered (ringing) calls, a new call starting within 2.5 s on the
same hwnd would find `_ring_event_emitted=True` and never fire a new ring event.

**Fix (detector.py window-missing block):**
- `_session_answered_proof_seen=False` (ringing): emit ENDED immediately on first missing-window poll.
- `_session_answered_proof_seen=True` (active call): keep 2.5 s gap as before.

**Defense-in-depth (detector.py + main.py):**
- `_session_generation` (int, `__init__` only) increments on each ring emission.
- `DetectionResult.session_generation` carries the generation value.
- `current_session_generation` in main.py tracks the current session's generation.
- `different_generation` split condition: same hwnd but different generation → split.
- `state.ringing` added to `strong_new_session` for post-terminal cooldown bypass.
- `DETECTOR → strong new call bypassed post-terminal cooldown` log added.
- `_last_ended_hwnd / _last_ended_ts / _last_ended_direction` saved in all ENDED paths.

**Critical rule:** Do NOT restore the 2.5 s window gap for ringing sessions.

## Session boundary bug history and final design

### Problem (fixed 2026-05-07)
Back-to-back WhatsApp calls could merge into one record/recording.

### Root causes found
1. FIX-2 log in `detector.py` showed `old_hwnd=NEW | new_hwnd=NEW` because
   `self._call_hwnd` was already reassigned before the log fired.
2. ANSWERED, active no-event, and ongoing-phase `DetectionResult` objects
   did not carry `hwnd=win.hwnd`.

### Final design
- **Session identity = hwnd** of the WhatsApp call window.
- Every `DetectionResult` while a call window exists carries `hwnd=win.hwnd`.
- Ring events (`INCOMING_RING`, `OUTGOING_RING`) always carry `is_strong_new_call=True`.
- `CALL_STARTED` (unknown direction): `is_strong_new_call=True` only when UIA state
  shows positive proof (`incoming`/`outgoing`/`ringing`/`connecting`/`has_end_call_button`
  or a RINGING_LABELS status text). Without proof → `False` (cannot split live session).
- `ANSWERED` carries `hwnd` but not `is_strong_new_call=True`.
- `previous_hwnd = self._call_hwnd` and `previous_direction = self._call_direction`
  are saved before the `if new_window:` block in `detector.poll()`.
- `main.py` `_TERMINAL_STATES = {IDLE, ENDED, RECORDER_ERROR, DETECTOR_ERROR}`.
  Any other state is "live"; a new ring event while live triggers a split.
  **A weak CALL_STARTED (`is_strong_new_call=False`) never triggers a split.**
- Split path: join recorder thread (3 s) → deepcopy old session → stop/detach recorder →
  finalize thread → `sm.transition(RESET)` → clear `current_session_hwnd`.
  Does **not** call `detector.reset()`.

### "Call ended" window defense (fixed 2026-05-07 follow-up)
When the detector sees a window whose UIA status is in `ENDED_LABELS`:
1. If `ring_event_emitted or answered_event_emitted or (new_window and previous_hwnd is not None)` →
   emit `ENDED` with `direction = self._call_direction or previous_direction or "unknown"`.
2. Else (first window ever, showing "Call ended") → return `DetectionResult(None, ...)`.
3. Ring emission block: if `not ring_event_emitted` and status in `ENDED_LABELS` → return
   `DetectionResult(None, ...)` before emitting any ring.

### Recorder lifecycle ownership (final fix 2026-05-07)
`recorder.start_recording()` is called **synchronously** in the poll loop — never
in a background thread.  Async start was reverted because it allowed an orphan
recorder to start after the session had already been reset.

`_do_mute_check()` (UIA traversal, 20-30 s) runs in a daemon thread started
inside `start_recording()` after engine.start() returns — this is the only
async part and it is purely informational (mute logging).

`[REC-011]` logged if engine or context phase of `start_recording()` > 2 s.
`[REC-012]` orphan guard fires if `recorder.is_recording` when `_should_start_recording(sm)`
is False — stops orphan recorder immediately and finalizes.

**NEVER reintroduce async recorder start without a session token/cancel mechanism.**

## USB selection behavior

- `DeviceManager` scores input devices; USB headsets get highest score.
- USB headset selected at call start is logged as `[DEV-USB] USB device selected`.
- On disconnect during call: `[DEV-USB]` warning logged; recording continues on
  whatever device survives.
- On reconnect while idle: `[DEV-USB]` info "next call will use USB device."
- On reconnect during recording: `[DEV-USB]` "USB reconnected; switch requested."

## Upload / local DB / central sync

- Local SQLite at `data/calls.db` (always written).
- Central DB (optional): tried immediately on `save_call()`; failures are
  retried in background `sync_unsynced_*` threads every
  `CENTRAL_SYNC_INTERVAL_SECONDS`.
- File uploads: `uploader.py` copies recording to a network path configured in
  `config.json`. Retries up to `UPLOAD_RETRY_COUNT` times with
  `UPLOAD_RETRY_DELAY_SECONDS` between attempts.
- Failed uploads remain in `pending_uploads` table and are retried on next
  background sync.

## Launcher / EXE behavior

- `launcher.py` runs as the outer process (restart loop).
- `--worker` flag runs `main.run()` once (inner process, no restart loop).
- Launcher restarts worker on non-zero exit; stops on exit code 0.
- Max `MAX_RESTARTS_PER_HOUR = 20` restarts before launcher gives up.
- PyInstaller spec: `whatsapp_watcher.spec`.

## Log code meanings

| Code | Meaning |
|---|---|
| `[REC-001]` | No audio device available at recording start |
| `[REC-002]` / `[REC-003]` | Loopback / mic stream open failed |
| `[REC-008]` | WAV file open failed |
| `[REC-009]` | Silence detected for >6s during recording |
| `[REC-010]` | Written frames < 70% of expected (timing/buffer issue) |
| `[REC-011]` | `recorder.start_recording()` engine or context phase > 2 s (WASAPI lock) |
| `[REC-012]` | Orphan recorder guard — recorder running without live session; stopped immediately |
| `[DEV-003]` | Zero input devices enumerated |
| `[DEV-USB]` | USB headset connect/disconnect/selection event |
| `[UPL-001]` | Unhandled exception in `process_pending_uploads` |
| `[UPL-002]` | File copy failure during upload |
| `[UPL-004]` | Local recording file missing at upload time |
| `SESSION SPLIT` | Back-to-back call boundary detected; old session finalized |
| `CALL END` | Terminal state reached; finalize thread spawned |
| `[LATCH]` | Session direction latch: save / restore / clear events |
