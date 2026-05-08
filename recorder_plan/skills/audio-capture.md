# Skill: Audio capture + WAV/MP3 output

## Three-thread capture architecture (current as of 2026-05-08)

`_record_loop` has been removed. CaptureEngine now uses three independent threads:

| Thread | Class | Role |
|--------|-------|------|
| LoopbackReader | `_SourceReader` | Blocking `stream.read()` for WASAPI loopback only |
| MicReader | `_SourceReader` | Blocking `stream.read()` for mic only |
| Writer | `_AudioWriter` | `time.monotonic()` wall-clock scheduling; WAV writes; never calls `stream.read()` |

The writer thread is stored in `_record_thread` for watchdog compatibility.

### Why three threads

`loopback.read()` can block for 2+ seconds on a WASAPI stall. When the reader and writer shared one thread, the WAV grew at ~0.63× wall-clock rate (confirmed: `[REC-013] writer lag | behind_ms=2090.0`). Separate threads let the writer schedule at exact 20ms intervals regardless of source read timing.

## _FrameBuffer

Thread-safe frame store shared between one `_SourceReader` and `_AudioWriter`.

```python
buf = _FrameBuffer(maxlen=24, source_name="LoopbackReader")
buf.push(frame)                        # drops oldest + logs [REC-014] on overflow
frame, dropped = buf.pop_latest_or_silence(silence)  # newest frame; discards older
buf.clear()
len(buf)
```

`pop_latest_or_silence()` always returns the most recent frame.  `dropped` is the
number of older frames discarded in the same call.  This prevents stale/delayed audio.

## _SourceReader

```python
# Per-source thread. Reads one chunk, downmixes, resamples, pushes to _FrameBuffer.
# Paced to one chunk per source_block_seconds to prevent buffer flooding.
reader = _SourceReader(
    name="LoopbackReader",          # or "MicReader"
    stop_event=stop_event,
    chunk_size=960,                 # frames per chunk (20ms at 48 kHz)
    sample_width=2,                 # 16-bit PCM
    source_channels=2,              # stream's native channel count
    source_rate=48000,              # stream's native sample rate
    mix_rate=48000,                 # output/WAV rate
    frame_buf=lb_buf,               # _FrameBuffer owned by engine
)
reader.set_stream(stream, source_channels, source_rate)  # inject stream
reader.go_offline()   # unblocks read() via stop_stream(), marks offline
reader.is_online      # property
```

Pacing: `stop_event.wait(chunk_size/source_rate - elapsed)` after each push.
Overflow logged `[REC-014]` inside `_FrameBuffer.push()`.
Pace lag logged `REC → source pace lag` if `elapsed > 1.5 × source_block_seconds`.

## _AudioWriter

```python
# Writer thread. Wall-clock scheduled, never calls stream.read().
writer = _AudioWriter(
    stop_event=stop_event,
    lb_buf=lb_buf,               # _FrameBuffer
    mic_buf=mic_buf,             # _FrameBuffer
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

Writer scheduling: absolute `next_tick += block_seconds` each iteration (no drift).
If behind by >10ms, logs `[REC-013]` and resets `next_tick` to prevent catch-up storm.
Pulls with `pop_latest_or_silence()` — newest frame only, stale frames discarded.
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
