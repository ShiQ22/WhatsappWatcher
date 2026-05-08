# Skill: Audio capture — FFmpeg backend (production direction)

## Status

**The PyAudio three-thread capture architecture described in earlier versions of this file
has been rejected for production use.**

The FFmpeg subprocess backend is the approved production direction as of 2026-05-08.
PyAudio may remain for device enumeration/scoring only.

See `CLAUDE.md` and `changes/ffmpeg-backend-migration-2026-05-08.md` for the full decision
and implementation plan.

---

## Production direction: FFmpeg backend

### Architecture overview

```
Recorder (public API — unchanged interface)
    └── FFmpegCaptureEngine
            ├── DeviceResolver          ← runtime device discovery via ffmpeg -list_devices
            ├── FFmpeg subprocess(es)   ← capture, mix, encode (FFmpeg owns audio I/O)
            ├── StderrDrainThread       ← daemon thread; drains stderr so FFmpeg never blocks
            ├── ProcessWatchdog         ← detects FFmpeg process death
            ├── FileGrowthMonitor       ← detects FFmpeg stall (file not growing)
            └── USBRecoveryLoop         ← segment close on unplug, new segment on replug
    └── SegmentMerger (called by resolve_final_files)
            ├── gap calculation         ← time.monotonic() segment gaps
            ├── silence WAV generation  ← fills USB unplug gap if configured
            ├── concat list builder     ← ffmpeg -f concat
            ├── concat copy attempt     ← ffmpeg -c copy (lossless)
            └── re-encode fallback      ← ffmpeg re-encode if copy fails
```

### Why FFmpeg

- FFmpeg owns the WASAPI audio I/O entirely — no manual PCM read/mix/write in Python.
- FFmpeg handles buffering, resampling, mixing, and encoding without Python thread timing.
- FFmpeg's `-f wasapi` input uses the OS audio stack correctly.
- Debug via FFmpeg stderr — no debug stem WAV files needed in production.

---

## DeviceResolver

Discovers WASAPI device names at runtime on the local PC.

```
ffmpeg -list_devices true -f wasapi -i dummy 2>&1
```

Parses output to extract available WASAPI device names.
Optionally uses PyAudio for scoring (USB bonus, name matching) to pick best mic.
Returns `ResolvedDevices(loopback_name, mic_name, mode)` where mode is `dual`/`loopback`/`mic`.

**Rules:**
- Use FFmpeg's exact device name strings for FFmpeg `-i` arguments.
- Do not use PyAudio device names directly as FFmpeg inputs.
- Do not hardcode device names.
- Do not share device names between PCs — each PC discovers its own at runtime.
- If no real mic: mode = `loopback`; mic input omitted from FFmpeg command.
- Never use a `[Loopback]` device as the mic input.

---

## FFmpegCaptureEngine

Manages one FFmpeg subprocess per recording segment.

### Start (single segment)

```python
# Example dual-source command (exact flags confirmed with bundled ffmpeg build)
ffmpeg_cmd = [
    "bin/ffmpeg.exe",
    "-f", "wasapi", "-i", loopback_device_name,
    "-f", "wasapi", "-i", mic_device_name,
    "-filter_complex", "amix=inputs=2:duration=first:dropout_transition=3",
    "-c:a", "libmp3lame", "-b:a", "64k",
    "-y", output_segment_path,
]
```

- FFmpeg command is a placeholder until confirmed with the exact bundled binary.
- Stderr must be drained in a daemon thread (`StderrDrainThread`) immediately after
  subprocess start so FFmpeg never blocks on stderr pipe overflow.

### Stop (end of segment)

Send `SIGTERM` to the FFmpeg subprocess (or write `q\n` to stdin if FFmpeg is started
with `-stdin`). Wait for process to exit. Save the segment path + end timestamp.

Never use `kill()` as first stop attempt — FFmpeg needs to finalize the output file.

---

## ProcessWatchdog

Monitors the FFmpeg subprocess in a daemon thread.

- Polls `subprocess.poll()` every N seconds.
- If process died unexpectedly: log error, mark segment complete (partial), trigger
  `USBRecoveryLoop` or start new segment depending on context.

---

## FileGrowthMonitor

Monitors output segment file size in a daemon thread.

- Records file size every `ffmpeg_stall_threshold_seconds / 2` seconds.
- If file size has not grown for `ffmpeg_stall_threshold_seconds`: FFmpeg has stalled.
- On stall: kill process, mark segment complete (partial), start new segment.

---

## USBRecoveryLoop

Handles USB headset unplug/replug during a call.

### Disconnect
1. Detect USB removal (WMI event or device list poll).
2. Stop current FFmpeg subprocess cleanly (SIGTERM or `q\n`).
3. Record segment end time via `time.monotonic()`.
4. Save segment metadata (path, start_mono, end_mono).
5. Mark recovery pending.

### Reconnect
1. Detect USB return.
2. Re-run `DeviceResolver` to get new device name.
3. Record segment start time via `time.monotonic()`.
4. Start new FFmpeg subprocess for new segment.
5. Clear recovery pending.

### Gap
`gap_seconds = new_segment_start_mono - previous_segment_end_mono`

---

## SegmentMerger

Called by `Recorder.resolve_final_files()` after call ends.

### Gap preservation

1. For each gap between consecutive segments:
   - `gap_seconds = next_segment.start_mono - prev_segment.end_mono`
   - If `gap_seconds >= ffmpeg_gap_silence_threshold_seconds` and
     `ffmpeg_preserve_usb_gap_silence=true`:
     - Generate a WAV silence file of exactly `gap_seconds` duration.
     - Mark silence file as temp (do not upload, do not store in DB).

2. Build concat list:
   ```
   file 'segment_1.mp3'
   file 'silence_gap_1.wav'
   file 'segment_2.mp3'
   ```

3. FFmpeg concat copy attempt:
   ```
   ffmpeg -f concat -safe 0 -i concat_list.txt -c copy final_output.mp3
   ```

4. If copy fails: fallback re-encode:
   ```
   ffmpeg -f concat -safe 0 -i concat_list.txt -c:a libmp3lame -b:a 64k final_output.mp3
   ```

5. If both fail:
   - Log error with full detail.
   - Keep all original segments.
   - Return first segment path as best-available output.
   - Do not delete any evidence.

### Temp file cleanup (on success only)

- Delete concat list file.
- Delete silence temp WAV files.
- Delete individual segment files.
- Keep only the final merged file.

---

## Config additions (planned for Phase 2+)

```json
"recorder": {
    "backend": "ffmpeg",
    "ffmpeg_path": "bin/ffmpeg.exe",
    "ffmpeg_mode": "dual",
    "ffmpeg_stall_threshold_seconds": 8.0,
    "ffmpeg_gap_silence_threshold_seconds": 0.5,
    "ffmpeg_preserve_usb_gap_silence": true,
    "ffmpeg_keep_temp_segments": false,
    "ffmpeg_diagnostics": true
}
```

`"backend": "pyaudio"` retains the old PyAudio engine as rollback.
`"backend": "ffmpeg"` is the target production default.

---

## PyAudio (enumeration/scoring only)

PyAudio may be used temporarily in `DeviceResolver` to:
- List input devices and score them (USB bonus, headset name patterns).
- Detect if a USB mic is newly present vs the live device list (evidence-based reinit).

Use a fresh `pyaudio.PyAudio()` instance for each enumeration call.
Never hold a persistent PyAudio instance when FFmpeg backend is active.
Never open a PyAudio stream for recording in the FFmpeg production path.

---

## Recorder public interface (unchanged)

```python
# Properties
is_recording: bool
current_recording_path: Optional[str]
started_at: Optional[datetime]

# Methods
start_recording() -> bool
stop_recording() -> bool
force_stop_recording() -> bool
detach_context() -> RecordingContext
detach_contexts() -> List[RecordingContext]
resolve_final_files(contexts: List[RecordingContext]) -> List[str]
resolve_final_file(ctx: RecordingContext) -> Optional[str]
ensure_recording_alive() -> bool
get_recording_metadata() -> dict
refresh_bandicam_paths() -> bool   # alias
```

---

## Timing rules

- `time.monotonic()`: all segment duration and gap calculations.
- `datetime.now()`: file names, human-readable log lines only.
- Never mix monotonic and wall-clock for arithmetic.

---

## PyAudio three-thread architecture (historical reference)

The following was the production architecture before the FFmpeg migration decision.
It is preserved here for reference and as the rollback backend implementation guide.

### _SourceReader

Non-blocking poll loop: check `get_read_available()` every 2ms up to one chunk budget.
If frames ready → read. If budget exhausted → push silence frame.
MicReader: per-second L/R channel analysis → `_mic_ch_mode` (left/right/average).

### _AudioWriter

Wall-clock scheduled (`next_tick += block_seconds`). Pulls via `_JitterBuffer.pop_or_hold()`.
FIFO, oldest-first. MAX_HOLD=1 tick before silence. Startup: one tick head-start.
Mix: `int(lb * loopback_gain + mic * mic_gain)`, clamped int16. `writeframesraw()`.

### _JitterBuffer

Thread-safe FIFO buffer, maxlen=4. `push()` drops oldest on overflow (logs [REC-014]).
`pop_or_hold(online, last_frame, silence)` → ok / hold / offline.

### Stop sequence (PyAudio backend)

```
stop() called
  → stop_event + watchdog_stop
  → reader.go_offline() × 2
  → reader.join(2s) × 2
  → writer.join(5s)
  → watchdog.join(2s)
  → stream.close() × 2
  → _finalize_wav()
```

### USB rules (PyAudio backend — carry forward to FFmpeg)

- NEVER call `close()` on a WASAPI stream after USB removal.
- NEVER call `pa.open()` on the record-loop thread.
- `_reconnect_disabled` must be set by `stop()` before reconnect threads can fire.
