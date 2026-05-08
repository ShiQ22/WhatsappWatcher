# CLAUDE.md — WhatsApp Watcher

## Current approved direction

**Stop patching the PyAudio recorder backend. Migrate to FFmpeg.**

The PyAudio-based `CaptureEngine` (`_SourceReader`, `_AudioWriter`, `_JitterBuffer`) has been
patched across many sessions (rev1 → rev2 → rev3) and continues to produce:

- robotic microphone audio
- robotic loopback/remote audio
- echo
- short or empty recordings
- USB reconnect failures
- manual jitter-buffer artifacts
- 50% duty-cycle audio islands (confirmed by debug stem analysis)

The root cause is the PyAudio/WASAPI reader+writer approach itself, not a single fixable bug.
Each patch trades one artifact for another. **Do not open recorder.py to fix audio problems.**

The approved replacement is an FFmpeg subprocess backend that lets FFmpeg own audio capture,
mixing, and encoding entirely.

---

## What NOT to change

Do not rewrite, refactor, or touch these files:

- `detector.py`
- `state_machine.py`
- `storage.py`
- `uploader.py`
- `report.py`
- `launcher.py`

These systems work correctly. The only problem is the audio recording backend.

`main.py` may receive **small targeted integration changes only** for:
- temp segment recovery scanning
- merge temp file exclusion from finalization
- final merged file path handling

Do not restructure `main.py` poll logic, session lifecycle, direction handling, or REC-012 guard.

---

## Preserve the Recorder public interface

`main.py` uses these `Recorder` methods and properties. They must remain with identical
signatures:

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
refresh_bandicam_paths() -> bool   # alias for backward compat
```

Internal recorder implementation may change completely. Public contract must stay stable.

---

## FFmpeg backend requirements

- FFmpeg binary lives at `bin/ffmpeg.exe` (relative to project root).
- Use FFmpeg's own device list (`-f wasapi -list_devices true`) for exact input names.
- Do not hardcode device names (not "Headset Microphone", not "Speakers").
- Each PC must discover its own devices at runtime via `DeviceResolver`.
- PyAudio may be used **only** for device scoring/enumeration; never for audio recording.
- Do not use `_SourceReader`, `_AudioWriter`, or `_JitterBuffer` in the FFmpeg production path.
- Use `time.monotonic()` for segment duration and gap calculations.
- Use wall-clock `datetime` only for file names and human-readable logs.

### USB unplug during a call

1. Detect USB removal.
2. Stop/close the current FFmpeg subprocess safely.
3. Save the current segment.
4. Do not finalize the call.
5. Wait for USB recovery.
6. Start a new segment when the device returns.
7. At call end: merge all segments into one final file.
8. Preserve the USB unplug gap using generated silence if `ffmpeg_preserve_usb_gap_silence=true`.

### Merge safety rules

- Try FFmpeg concat copy first; fallback to re-encode if copy fails.
- If merge fails: preserve all original segments; do not delete them.
- Do not upload gap-silence temp files.
- Do not upload concat list files.
- Final upload/DB/report paths are unchanged.

### Process safety rules

- Log FFmpeg stderr in a daemon drain thread — never block on stderr.
- Add `ProcessWatchdog` for FFmpeg process death detection.
- Add `FileGrowthMonitor` for FFmpeg stall detection (file not growing).
- On watchdog fire: save current segment, start new one.

---

## Implementation phases

**Phase 0 (current):** Documentation only. No code changes.

**Phase 1:** Inspect `recorder.py` public interface. Design backend boundary. Confirm plan with Ahmed. No code yet.

**Phase 2:** Implement `DeviceResolver` + `FFmpegCaptureEngine` skeleton. `bin/ffmpeg.exe` check. Basic start/stop single segment. Compile/test/commit.

**Phase 3:** Wire FFmpeg backend into `Recorder`. All public interface methods implemented. Tests pass. Commit.

**Phase 4:** `ProcessWatchdog` + `FileGrowthMonitor` + USB recovery loop. Segment restart on death/stall. Commit.

**Phase 5:** `SegmentMerger` with gap preservation (silence insertion). Concat copy + re-encode fallback. Commit.

**Phase 6:** Final docs update, test checklist, production validation.

Keep PyAudio backend available behind `"backend": "pyaudio"` config key as rollback until
FFmpeg backend passes manual tests on Ahmed's PC and representative PCs.

---

## PyInstaller notes

`whatsapp_watcher.spec` must be updated to bundle `bin/ffmpeg.exe`:

```python
datas=[
    ("config.json", "."),
    ("bin/ffmpeg.exe", "bin"),
],
```

Do not edit the spec until Phase 2+ code is in place. Read the current spec before editing.

---

## Key rules carried forward from previous sessions

- `_do_mute_check()` must never be called synchronously on the main poll thread (20-30 s UIA traversal).
- Never call `stream.close()` after USB removal on a WASAPI stream (access violation risk).
- Never call `pa.open()` on the record-loop thread (blocks 20-30 s).
- `recorder.start_recording()` is called synchronously in the poll loop — never reintroduce async start without a session token/cancel mechanism.
- `_should_update_direction()` guard must be used for direction propagation — never a bare truthy check (`"unknown"` is truthy).
- `current_session_generation` must be reset to 0 in every path that resets `current_session_hwnd`.
- Do not restore the 2.5 s window gap for ringing sessions.
- `detector.reset()` must NOT be called in the split path.
- Do not delete evidence temp segments on merge failure.
- `time.monotonic()` for durations/gaps; `datetime` only for names and logs.
