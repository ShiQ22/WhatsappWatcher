# USB / Direction / MP3 fixes — 2026-05-08

## Summary

Four bugs fixed in this session:

1. **Direction overwrite** — `"unknown"` could overwrite a proven `"incoming"` / `"outgoing"` direction.
2. **USB disconnect causes short WAV** — record loop exited when both streams were lost; next watchdog segment merged calls.
3. **Access violation on USB removal** — `pyaudiowpatch.read()` on a dead WASAPI stream after device removal caused a Windows C-level crash.
4. **File size / quality** — switched from WAV to MP3 @ 64 kbps; corrected sample rate to 48 kHz (the USB device's native rate).

---

## Fix 1 — Direction no-downgrade guard (`main.py`)

### Root cause

Both direction-propagation blocks in `run()` used:
```python
if result.direction and result.direction != sm.session.direction:
    sm.session.direction = result.direction
```
`"unknown"` is a truthy non-empty string, so it satisfied `result.direction` and overwrote `"outgoing"` or `"incoming"`.

### Fix

New helper `_should_update_direction(new_dir, current_dir)` returns `False` when
`new_dir == "unknown"` and `current_dir` is already proven.  Applied to both
propagation blocks (pre-transition and post-transition).

---

## Fix 2 — Session direction latch (`main.py`)

### Purpose

Crash-recovery: if the watcher crashes mid-call (direction known), the next restart
can restore the proven direction when the ring event from the same call window is seen.

### Design

- `data/active_call_session.json` — written when direction first becomes proven.
  Contains `direction`, `hwnd`, `session_generation`, `started_at`, `saved_at`.
- **Save**: on first proven direction in a session (`_direction_latched = False` gate).
- **Restore**: at startup, on the first `is_new_call_event` where `result.direction`
  is `None` or `"unknown"`, AND (`hwnd` matches OR `session_generation` matches) AND
  `saved_at` is within 3600 s AND latch direction is `"incoming"` / `"outgoing"`.
  The latch is consumed once regardless of match.
- **Clear**: after `finalize_threads.add(t); t.start()` in ALL finalize paths:
  terminal, split, crash, orphan, shutdown.

---

## Fix 3 — USB disconnect → silence loop + non-blocking reconnect (`recorder.py`)

### Root causes

1. `_record_loop` broke out when both streams were `None` → WAV finalized short.
2. `pyaudiowpatch.read()` on a removed USB WASAPI stream can access-violate (C-level,
   uncatchable by Python `except`).
3. `_on_usb_reconnect` called `request_device_switch()` which only nulled `_stream`
   and relied on the watchdog — but the watchdog never fired if the record thread was
   still alive.

### Fixes

**`CaptureEngine.on_usb_disconnect()`** (new):
- Acquires `_lock`, saves both stream refs, nulls `_loopback_stream` / `_stream` /
  `_mic_stream`.  Calls `stop_stream()` on the saved refs outside the lock.
- Does **NOT** call `close()` — the WASAPI topology is already gone; `close()` can
  access-violate.

**`_record_loop` both-streams-gone path** (replaced `break`):
- Writes `chunk_size * sample_width` bytes of silence to WAV (preserves wall-clock duration).
- Calls `_try_reconnect_streams_async()` (non-blocking — starts a daemon thread if not already running).
- Sleeps `chunk_size / mix_rate` seconds to maintain timing rhythm.
- `continue`s the loop — never exits on stream loss alone.

**`CaptureEngine._try_reconnect_streams_async()`** (new):
- Uses `_reconnect_lock` to prevent two reconnect threads starting simultaneously.
- Throttles: skips if last attempt was < 2 s ago.
- Starts `_try_reconnect_streams` as a daemon thread.

**`CaptureEngine._try_reconnect_streams()`** (new, runs in daemon thread):
- Calls `_device_manager.reinit_pyaudio()`.
- Reopens loopback via `_open_loopback_stream()` if `_loopback_stream is None`.
- Selects best device and reopens mic via `_open_stream()` if `_stream is None`.
- Updates stream refs and `_mix_rate` under lock.

**`Recorder._usb_watcher_loop`** (updated):
- On USB disconnect AND engine is active: calls `self._engine.on_usb_disconnect()`.

**`Recorder._on_usb_reconnect`** (updated):
- Now calls `self._engine._try_reconnect_streams_async()` instead of
  `request_device_switch()`.

---

## Fix 4 — MP3 output / 48 kHz sample rate (`config.py`, `config.json`)

| Setting | Old | New |
|---|---|---|
| `format` | `wav` | `mp3` |
| `sample_rate` | `44100` | `48000` |
| `chunk_size` | `1024` | `960` |
| `mp3_bitrate` | `128` | `64` |

Rationale:
- The USB audio device's native WASAPI rate is 48000 Hz; 44100 was rejected at first
  attempt and fell back automatically — the config now matches the hardware.
- `chunk_size = 960` = exactly 20 ms at 48000 Hz (standard VoIP frame).
- MP3 @ 64 kbps is transparent for voice; reduces file size ~4× vs WAV @ 48000 Hz.

---

## Files changed

| File | Change |
|---|---|
| `main.py` | `import json`; `DATA_DIR` import; `_should_update_direction`; `_save/load/clear_session_latch`; latch restore on first ring; no-downgrade guard in both propagation blocks; latch save on proven direction; latch clear in all finalize paths |
| `recorder.py` | `_reconnect_lock/thread/ts` in `__init__`; `on_usb_disconnect()`; `_try_reconnect_streams_async()`; `_try_reconnect_streams()`; silence loop replacing `break`; `_usb_watcher_loop` calls `on_usb_disconnect`; `_on_usb_reconnect` calls `_try_reconnect_streams_async` |
| `config.py` | `format`→mp3, `sample_rate`→48000, `chunk_size`→960, `mp3_bitrate`→64 |
| `config.json` | Same four values |

## Manual test plan

1. **Direction persists through "Call ended" window** — make an outgoing call, let it
   ring briefly, answer, hang up.  Verify `direction=outgoing` in report (not `unknown`).

2. **USB disconnect mid-call** — unplug USB headset during an active call.
   - Expected: recording continues; log shows "USB disconnect handled | streams detached";
     record loop writes silence; no process crash.
   - Expected: when headset is replugged, mic and loopback streams reopen (check log
     for "loopback stream reopened after reconnect" / "mic stream reopened after reconnect").

3. **Crash recovery** — start a call (direction confirmed as outgoing in log), then
   kill the watcher process (Ctrl+C or Task Manager).  Restart.  The watcher detects
   the call still active.
   - Expected: `[LATCH] Restored direction from latch | dir=outgoing` in log.
   - Expected: call report shows `direction=outgoing`, not `unknown`.

4. **Normal call cycle** — single outgoing and single incoming call.
   - Expected: recordings saved as `.mp3` files (not `.wav`).
   - Expected: file size roughly 30–60 KB/minute (vs ~2 MB/minute for WAV).

5. **Back-to-back calls still split** — existing regression from 2026-05-07 fix.
   - Expected: two separate records and two files per back-to-back pair.

## Git commit hash

(populated after commit)
