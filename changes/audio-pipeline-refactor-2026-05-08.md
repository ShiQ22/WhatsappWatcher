# Audio pipeline refactor — 2026-05-08

## Root cause

`loopback.read()` (WASAPI) can block for 2+ seconds during a stream stall.
The old `_record_loop` did both the blocking read and the WAV write in one thread.
When the read blocked, the WAV received zero frames for the stall duration.

**Evidence from production (2026-05-08 05:47 call):**
- Wall time: 19s. WAV duration: ~12s. Ratio: 0.63.
- Log: `[REC-013] writer lag | behind_ms=2090.0`
- Previous call (05:45, 43s): ratio ~0.71.

The post-read clock correction (`_elapsed = time.monotonic() - _iter_start; sleep = block_seconds - _elapsed`) cannot retroactively fill the gap — those frames are simply absent from the WAV.

## Fix

Replaced `_record_loop` with three independent daemon threads:

```
LoopbackReader (_SourceReader) ──── lb_queue (deque maxlen=24) ────┐
                                                                    ├─► _AudioWriter
MicReader      (_SourceReader) ──── mic_queue (deque maxlen=24) ───┘
```

**LoopbackReader / MicReader (`_SourceReader`):**
- Each owns one blocking `stream.read()`.
- Downmixes stereo→mono, resamples, normalises to `chunk_bytes`, appends to queue.
- On stream error: marks self offline, logs warning, continues loop (no crash, no break).
- Queue overflow: oldest dropped automatically by `deque(maxlen=24)`; `[REC-014]` logged (throttled 1s).

**Writer (`_AudioWriter`):**
- Absolute wall-clock scheduling: `next_tick += block_seconds` — no drift.
- Pulls from `lb_queue` / `mic_queue` or uses `b"\x00" * chunk_bytes` if empty.
- Gain mixing: `int(lb * loopback_gain + mic * mic_gain)`, float/int32 intermediate, int16 clamp.
- Default gains: `loopback_gain=0.65`, `mic_gain=0.75` (configurable).
- Writes debug stems (same exact frames used for mixing) if `debug_stems=true`.
- Level log every 4s: mic_rms, loopback_rms, mixed_rms, clipped, online states.
- If behind >10ms: logs `[REC-013]` and resets `next_tick` (prevents catch-up storm).
- Never calls `stream.read()`.

## USB disconnect/reconnect adaptation

- `on_usb_disconnect()` calls `reader.go_offline()` on both readers.
  `go_offline()` calls `stop_stream()` internally — unblocks any blocking read.
- `_try_reconnect_streams()` calls `reader.set_stream(new_stream, channels, rate)`
  to inject the new stream into the still-running reader thread. No thread restart needed.

## Stop sequence

```
stop_event.set()
reader.go_offline() × 2        (stop_stream → unblocks read)
reader.join(timeout=2s) × 2
writer.join(timeout=5s)
watchdog.join(timeout=2s)
stream.close() × 2
_finalize_wav()
```

## Watchdog recovery

Writer death → `_trigger_recovery()` → `_stop_readers()` + `recovery_exhausted = True`.
`Recorder.ensure_recording_alive()` detects `is_active == False` and creates a new segment.
The WAV finalized by the writer's `finally` block is kept as a valid (if short) recording.

## Follow-up fixes (2026-05-08 rev2)

### Problems addressed

1. **REC-014 flooding / writer starvation** — readers tight-looped when `stream.read()`
   returned immediately (WASAPI buffer pre-filled).  Fixed by pacing each reader:
   `stop_event.wait(source_block_seconds - elapsed)` after every successful push.

2. **Echo / delayed audio** — FIFO `popleft()` caused the writer to mix stale frames
   accumulated during a read burst.  Fixed by replacing raw deques with `_FrameBuffer`
   whose `pop_latest_or_silence()` always returns the newest frame and discards older ones.

3. **Loopback-as-mic** — `select_best_device()` could return `Speakers [Loopback]` as
   the mic device when USB was disconnected (USB score bonus).  Fixed by adding
   `select_best_mic_device()` which uses `list_real_mic_devices()` filtering `[loopback]`.

4. **Reconnect/stop race** — reconnect thread could open a stream after `stop()` began.
   Fixed by `_reconnect_disabled` flag set under lock in `stop()`.

### New symbols

| Symbol | Purpose |
|--------|---------|
| `_is_loopback_device_name(name)` | `"[loopback]" in name.lower()` |
| `_FrameBuffer` | Thread-safe frame store: `push()` + `pop_latest_or_silence()` + `clear()` |
| `DeviceManager.list_real_mic_devices()` | Mic candidates without loopback devices |
| `DeviceManager.select_best_mic_device()` | Highest-score real mic or `None` |
| `CaptureEngine._reconnect_disabled` | Set `True` by `stop()`, checked by reconnect paths |

### writeframesraw

`_AudioWriter` now uses `writeframesraw()` for both the main WAV and debug stems.
`wave.close()` writes the final frame-count header.  Saves a disk seek per 20 ms chunk.

## Config additions

`config.json` and `config.py` DEFAULT_CONFIG both now have:
```json
"mic_gain": 0.75,
"loopback_gain": 0.65
```

## Files changed

- `recorder.py`: removed `_record_loop`; added `_resample_audio()`, `_SourceReader`, `_AudioWriter`; updated `CaptureEngine.start/stop/on_usb_disconnect/_try_reconnect_streams/_trigger_recovery`; added `_stop_readers`, `_add_bytes_written`
- `config.py`: added `RECORDER_MIC_GAIN`, `RECORDER_LOOPBACK_GAIN`
- `config.json`: added `mic_gain`, `loopback_gain`
- `recorder_plan/skills/audio-capture.md`: replaced `_record_loop` description with three-thread architecture
- `CHANGELOG.md`, `HANDOFF.md`, `PROJECT_MEMORY.md`: updated

---

## Rev3: Non-blocking readers, jitter buffer, mic channel mode (2026-05-08)

### Root cause (rev3)

The 50% duty-cycle audio island pattern confirmed by debug stem analysis:
- Active audio runs ≤ 40ms in 92-93% of cases for **both** mic and loopback sources
- Root cause: `stream.read(chunk)` takes exactly 20ms → sleep_for = 0 → immediate re-read → WASAPI ring buffer just drained → blocks 20ms → writer sees alternating real/silence frames
- `_FrameBuffer.pop_latest_or_silence()` made this worse: discarding stale frames left the buffer empty on every other tick, forcing silence output

### Fix (rev3)

**Non-blocking readers (both LoopbackReader and MicReader):**
- Poll `get_read_available()` every 2ms up to one full chunk budget (20ms)
- If frames available before budget: read immediately → no blocking
- If budget exhausted without enough frames: push a silence frame, pace, continue
- `get_read_available` unsupported: fall through to blocking read (flag `_get_avail_unsupported`)

**_JitterBuffer replaces _FrameBuffer:**
- FIFO (oldest-first, not pop-latest). maxlen=4 (80ms capacity)
- `pop_or_hold(source_online, last_frame, silence)`:
  - Returns oldest queued frame when available ("ok")
  - Returns last good frame for up to MAX_HOLD=1 tick on underrun ("hold")
  - Returns silence after MAX_HOLD exceeded or source offline ("offline")
- Overflow drops oldest (logs [REC-014] throttled 1s)

**Mic channel mode selection (MicReader, per-second window):**
```python
L_rms, R_rms, LR_corr computed over 1s
if max/min > 4: use stronger channel
elif corr >= 0.5: average
else: use stronger channel
```
Mode logged on change. Accumulators reset each second.

**Evidence-based USB reinit:**
`DeviceManager.get_fresh_usb_mic_name_if_missing(live_names)` creates a temporary
`pyaudio.PyAudio()` instance, enumerates input devices, returns any USB/headset mic
not already in `live_names`. Reinit only triggered on confirmed mismatch.

**Writer startup head-start:**
`next_tick = time.monotonic() + block_seconds` before the write loop — gives readers
one full tick to populate their buffers before first pop.

### New symbols (rev3)

| Symbol | Purpose |
|--------|---------|
| `_JitterBuffer` | FIFO with hold-last; replaces `_FrameBuffer` |
| `_SourceReader._get_avail_unsupported` | Fallback to blocking read when API not available |
| `_SourceReader._mic_ch_mode` | `left`/`right`/`average` — updated per second |
| `_AudioWriter._MAX_HOLD` | `= 1`; hold last frame at most 1 tick then silence |
| `DeviceManager.get_fresh_usb_mic_name_if_missing()` | Fresh PA snapshot for USB mismatch detection |
