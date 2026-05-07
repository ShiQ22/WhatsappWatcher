# Skill: Recorder interface contract

## What main.py expects from Recorder

### RecordingContext
Unchanged from current codebase. Must be importable as:
```python
from recorder import Recorder, RecordingContext
```

### start_recording()
- Returns True if recording started successfully
- Returns False if no device, stream failed, or already recording
- Logs [REC-001] or [REC-002] on failure
- Sets: is_recording=True, started_at=now, pre_start_snapshot of output_dir
- Output dir = config RECORDER_OUTPUT_DIR, falls back to CWD/recordings/

### stop_recording()
- Returns True if stop dispatched
- Returns False if not recording
- Does NOT wait for file to finalize — that is resolve_final_files()'s job
- Sets: is_recording=False
- Logs: "REC → stop dispatched | segment=%s"

### force_stop_recording()
- Last resort — kills recording thread immediately
- Used by main.py when WhatsApp crashes mid-call
- Must always set is_recording=False even if thread kill fails
- Returns True if thread is confirmed dead

### detach_contexts()
- Returns deep copy of all completed RecordingContext objects PLUS the current active one
- Resets internal context list to empty
- Called immediately after stop_recording() by main.py finalize flow

### resolve_final_files(contexts)
- For EACH context: find the WAV/MP3 file written during that segment
- Wait for file to be non-zero size and stable (not being written)
- If MP3 enabled: file may be .wav or .mp3 — check both
- Returns list of file paths in segment order
- Called in a background thread — blocking is OK (but log progress)
- Unlike Bandicam: NO 8-second settle wait needed. File is closed by the engine at stop time.
  Wait max 3 seconds for file to appear/stabilize. That is enough.

### ensure_recording_alive()
- Called every poll cycle by main.py when in call state
- Checks if recording thread is alive
- If dead and is_recording=True → trigger recovery (new segment)
- Returns True if recording is healthy or not needed
- Must respect HEALTH_CHECK_INTERVAL (don't check every 0.8s)

### refresh_recorder_paths() + alias
```python
def refresh_recorder_paths(self) -> bool:
    # Re-read output_dir from config, check it exists
    # Return True if output_dir is valid
    ...

refresh_bandicam_paths = refresh_recorder_paths  # main.py calls this name
```

## Context lifecycle

```
start_recording()
  → _prepare_segment_locked()       # snapshot output_dir, set start_marker
  → CaptureEngine.start(path)       # starts thread + stream
  → is_recording = True

stop_recording()
  → CaptureEngine.stop()            # signals thread to exit, closes WAV
  → is_recording = False
  # context NOT detached yet

detach_contexts()                   # called by main.py after stop
  → returns contexts with file paths
  → resets internal state

resolve_final_files(contexts)       # called by main.py in background thread
  → matches each context to its file
  → converts to MP3 if configured
  → returns final path list
```

## Thread safety
All access to: `is_recording`, `started_at`, `_completed_contexts`, `_segment_counter`
must be inside `with self._lock:` blocks. Use `threading.RLock()`.

The recording thread itself does NOT hold the lock while writing audio data.
Lock is only for state mutation.
