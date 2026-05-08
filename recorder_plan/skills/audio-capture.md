# Skill: Audio capture + WAV/MP3 output

## Three-thread capture architecture (current as of 2026-05-08 rev3)

`_record_loop` has been removed. CaptureEngine now uses three independent threads:

| Thread | Class | Role |
|--------|-------|------|
| LoopbackReader | `_SourceReader` | Non-blocking-poll read for WASAPI loopback |
| MicReader | `_SourceReader` | Non-blocking-poll read for mic; per-second L/R channel analysis |
| Writer | `_AudioWriter` | `time.monotonic()` wall-clock scheduling; WAV writes; never calls `stream.read()` |

The writer thread is stored in `_record_thread` for watchdog compatibility.

### Why three threads

`loopback.read()` can block for 2+ seconds on a WASAPI stall. When the reader and writer shared one thread, the WAV grew at ~0.63× wall-clock rate (confirmed: `[REC-013] writer lag | behind_ms=2090.0`). Separate threads let the writer schedule at exact 20ms intervals regardless of source read timing.

### Why non-blocking readers (rev3)

Blocking `stream.read(chunk)` takes exactly `chunk_seconds` to return when the buffer is freshly drained. `sleep_for = chunk_seconds - elapsed ≈ 0` → immediate re-read → WASAPI ring buffer just drained → blocks again for another full period → alternating real/silence frames. Confirmed: 92-93% of active audio runs ≤ 40ms in both sources. Non-blocking poll loop with `get_read_available()` eliminates this by waiting up to the chunk budget before reading, or pushing silence on timeout.

## _JitterBuffer

Thread-safe FIFO buffer shared between one `_SourceReader` and `_AudioWriter`. Replaces `_FrameBuffer`.

```python
buf = _JitterBuffer(maxlen=4, source_name="LoopbackReader")
buf.push(frame)          # drops oldest + logs [REC-014] (throttled 1s) on overflow
frame, status = buf.pop_or_hold(source_online, last_frame, silence)
# status: "ok" | "hold" | "offline"
# "hold": buffer empty but source online — returns last_frame (up to MAX_HOLD=1 tick)
# "offline": source offline — returns silence
```

FIFO (oldest-first) maintains audio continuity. MAX_HOLD=1 means after 1 tick of underrun
the writer switches to silence rather than repeating stale audio.

## _SourceReader

```python
# Per-source thread. Non-blocking poll then read; downmixes; resamples; pushes to _JitterBuffer.
reader = _SourceReader(
    name="LoopbackReader",          # or "MicReader"
    stop_event=stop_event,
    chunk_size=960,                 # frames per chunk (20ms at 48 kHz)
    sample_width=2,                 # 16-bit PCM
    source_channels=2,              # stream's native channel count
    source_rate=48000,              # stream's native sample rate
    mix_rate=48000,                 # output/WAV rate
    frame_buf=lb_buf,               # _JitterBuffer owned by engine
)
reader.set_stream(stream, source_channels, source_rate)  # inject stream
reader.go_offline()   # unblocks read() via stop_stream(), marks offline
reader.is_online      # property
```

**Non-blocking poll loop (per tick):**
1. Poll `get_read_available()` every 2ms up to `max_polls = int(source_block_seconds / 0.002)`.
2. If `avail >= chunk_size`: exit poll loop, read immediately.
3. If budget exhausted: push `silence_raw` frame, log underflow (throttled), pace, `continue`.
4. If `get_read_available()` raises: set `_get_avail_unsupported = True`, fall through to blocking read.

**MicReader channel mode (per-second window):**
Accumulate L², R², LR cross-products. Every 1s compute L_rms, R_rms, LR_corr.
- If `min/max < 0.25`: use stronger channel
- Elif `corr >= 0.5`: average both channels
- Else: use stronger channel
Mode logged on change as `REC → mic channel mode | mode=... | L_rms=... | R_rms=... | corr=...`.

Overflow logged `[REC-014]` inside `_JitterBuffer.push()`.
Underflow logged `REC → source underflow | source=... | unavailable_ticks=N` (throttled 4s).

## _AudioWriter

```python
# Writer thread. Wall-clock scheduled, never calls stream.read().
writer = _AudioWriter(
    stop_event=stop_event,
    lb_buf=lb_buf,               # _JitterBuffer
    mic_buf=mic_buf,             # _JitterBuffer
    wav_file=wav_file,
    chunk_size=960, sample_width=2, mix_rate=48000,
    mic_gain=0.75,               # from config
    loopback_gain=0.65,          # from config
    debug_stems=True,
    mic_debug_wav=...,           # wave.Wave_write or None
    loopback_debug_wav=...,
    lb_reader=lb_reader,
    mic_reader=mic_reader,
    on_bytes_written=engine._add_bytes_written,
    reconnect_fn=engine._try_reconnect_streams_async,
)
```

Writer scheduling: `next_tick = time.monotonic() + block_seconds` (startup head-start, then
`next_tick += block_seconds` each iteration — absolute, no drift).
If behind by >10ms, logs `[REC-013]` and resets `next_tick` to prevent catch-up storm.

`_MAX_HOLD = 1`: writer holds last good frame for at most 1 tick on underrun, then writes silence.
Separate `_lb_hold` / `_mic_hold` counters; reset to 0 on each "ok" pop.

Pulls with `pop_or_hold()` — FIFO oldest-first; silence preferred over repeated stale frames.
Uses `writeframesraw()` for both main WAV and debug stems; `wave.close()` finalises header.

Mixing formula (int32 intermediate, clamped to int16):
```python
val = int(lb_sample * loopback_gain + mic_sample * mic_gain)
val = max(-32768, min(32767, val))
```

Level log every 4 seconds:
```
REC → levels | mic_rms=... | loopback_rms=... | mixed_rms=... | clipped=... | mic_online=Y/N | loopback_online=Y/N
```

## WAV file lifecycle

```python
# _open_wav() called in start() under lock
wav_file = wave.open(path, 'wb')
wav_file.setnchannels(1)       # always mono
wav_file.setsampwidth(2)       # 16-bit
wav_file.setframerate(mix_rate)

# _finalize_wav() called from stop() safety net and writer finally block
wav_file.close()
# also closes mic_debug_wav and loopback_debug_wav
```

## Debug stems

- Files: `*_mic_debug.wav`, `*_loopback_debug.wav`
- Written by **_AudioWriter only** — exact frames used for mixing
- Same duration as final WAV (one chunk per tick)
- Never uploaded, never in DB, not auto-deleted
- Orphan scan uses `glob("*_seg1.wav")` — does NOT match debug stems
- Controlled by `config.json recorder.debug_stems`

## MP3 conversion with lameenc

Called by `Recorder.resolve_final_files()` after WAV is finalized.
Only the final mixed WAV is converted. Debug stems remain WAV, local only.

```python
def _convert_to_mp3(self, wav_path: str) -> Optional[str]:
    mp3_path = wav_path.replace(".wav", ".mp3")
    with wave.open(wav_path, 'rb') as wf:
        frames = wf.readframes(wf.getnframes())
        rate = wf.getframerate()
        channels = wf.getnchannels()
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(RECORDER_MP3_BITRATE)  # 64 kbps default
    encoder.set_in_sample_rate(rate)
    encoder.set_channels(channels)
    encoder.set_quality(2)
    mp3_data = encoder.encode(frames) + encoder.flush()
    with open(mp3_path, "wb") as f:
        f.write(mp3_data)
    return mp3_path
```

## Watchdog thread

Monitors `_record_thread` (the writer). If writer dies:
1. `_trigger_recovery()` calls `_stop_readers()` (both readers go offline + join)
2. Marks `_recovery_exhausted = True`
3. `is_active` returns False
4. `Recorder.ensure_recording_alive()` detects this and starts a new segment

Watchdog does NOT try to restart the writer directly — the WAV is already finalized by the writer's `finally` block and cannot be reused.

## Stop sequence

```
stop() called
  → set stop_event + watchdog_stop
  → reader.go_offline() × 2   (calls stop_stream, unblocks blocking reads)
  → also stop_stream() on stream refs
  → reader.join(timeout=2s) × 2
  → writer.join(timeout=5s)
  → watchdog.join(timeout=2s)
  → stream.close() × 2
  → _finalize_wav()
```

## Mic device selection

`DeviceManager.select_best_mic_device()` calls `list_real_mic_devices()` which
excludes all `[Loopback]` devices.  Three outcomes:

| Situation | `select_best_mic_device()` | Behaviour |
|-----------|---------------------------|-----------|
| USB/headset present | USB/headset device | MicReader starts with stream |
| USB absent, built-in exists | Built-in device | Fallback; log `REC → using built-in microphone fallback` |
| No real mic | `None` | MicReader starts offline; log `REC → no real microphone available` |

Loopback selection is separate inside `_open_loopback_stream()` — never affects mic.

## USB disconnect/reconnect

On disconnect: `on_usb_disconnect()` → `lb_reader.go_offline()` + `mic_reader.go_offline()`
Writer fills silence for offline sources.

On reconnect: `_try_reconnect_streams()` reopens missing stream(s) using
`select_best_mic_device()` for mic (never loopback), then calls
`reader.set_stream(new_stream, channels, rate)` to inject the new stream.
PyAudio reinit only when BOTH streams are missing.

Stop/reconnect race: `_reconnect_disabled` flag is set `True` by `stop()` before
`stop_event.set()`.  `_try_reconnect_streams_async()` returns immediately if disabled.
If a stream was opened just before the race, it is closed best-effort before returning.

## Config

```json
"recorder": {
    "sample_rate": 48000,
    "chunk_size": 960,
    "format": "mp3",
    "mp3_bitrate": 64,
    "mic_gain": 0.75,
    "loopback_gain": 0.65,
    "debug_stems": true
}
```
