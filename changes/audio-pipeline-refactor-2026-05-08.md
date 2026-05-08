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
