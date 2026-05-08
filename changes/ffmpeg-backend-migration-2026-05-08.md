# FFmpeg recorder backend migration — 2026-05-08

## Status

**Documentation phase complete. No production code changed.**

Phase 0 anchors the repo on the FFmpeg migration plan. Implementation starts at Phase 1
after Ahmed approves.

---

## Root cause summary

The PyAudio/WASAPI audio pipeline has been patched across three major revisions:

| Revision | Fix attempted | Artifact remaining |
|----------|--------------|-------------------|
| rev1 (three-thread) | Separated blocking read from WAV write | 50% duty-cycle islands |
| rev2 (pacing + pop-latest) | Throttled readers; pop newest frame | Echo at call start; USB race |
| rev3 (non-blocking + JitterBuffer) | 2ms poll loop; FIFO + hold-last | Still robotic on production hardware |

**Confirmed from debug stem analysis:**
- 92-93% of active audio runs ≤ 40ms in both loopback and mic sources.
- Root cause: `stream.read(chunk)` returns in exactly one chunk period → sleep=0 →
  re-read immediately → WASAPI ring buffer just drained → blocks again → alternating
  real/silence frames at 20ms granularity.
- `_JitterBuffer` smoothed but did not eliminate the pattern.
- The problem is the Python-thread PCM read model, not a single fixable parameter.

**Decision:** Stop patching. Replace the recording backend with FFmpeg subprocess.

---

## Approved direction

FFmpeg owns audio capture, mixing, and encoding. Python orchestrates the process lifecycle.

```
Recorder (public API — unchanged)
    └── FFmpegCaptureEngine
            ├── DeviceResolver         ← runtime WASAPI device discovery
            ├── FFmpeg subprocess      ← capture + mix + encode
            ├── StderrDrainThread      ← daemon; never block on stderr
            ├── ProcessWatchdog        ← subprocess death detection
            ├── FileGrowthMonitor      ← stall detection
            └── USBRecoveryLoop        ← segment model for USB unplug/replug
    └── SegmentMerger
            ├── gap calculation        ← time.monotonic()
            ├── silence generation     ← ffmpeg -f lavfi -i anullsrc
            ├── concat list            ← ffmpeg -f concat
            ├── concat copy            ← -c copy (lossless first)
            └── re-encode fallback     ← -c:a libmp3lame on copy failure
```

---

## Architecture overview

### DeviceResolver

- Runs `ffmpeg -list_devices true -f wasapi -i dummy` on the local PC.
- Parses stderr output for WASAPI device name strings.
- Optionally uses a fresh `pyaudio.PyAudio()` instance for USB/headset scoring.
- Returns `ResolvedDevices(loopback_name, mic_name, mode)`.
- **No hardcoded device names. Every PC resolves its own devices at runtime.**

### FFmpegCaptureEngine

- Starts one FFmpeg subprocess per recording segment.
- Dual mode: `ffmpeg -f wasapi -i <loopback> -f wasapi -i <mic> -filter_complex amix ...`
- Loopback mode: single input `-f wasapi -i <loopback>`.
- stderr drained by daemon thread immediately after subprocess start.
- Stop: SIGTERM or `q\n` to stdin → wait for process to exit → save segment.
- `ProcessWatchdog`: polls `subprocess.poll()` every N seconds; handles unexpected death.
- `FileGrowthMonitor`: polls output file size; kills and recovers if file not growing for
  `ffmpeg_stall_threshold_seconds`.

### USBRecoveryLoop

- Disconnect: stop subprocess → record `end_mono = time.monotonic()` → save segment.
- Reconnect: re-run DeviceResolver → record `start_mono = time.monotonic()` → start new subprocess.
- Gap = `next_start_mono - prev_end_mono`.

### SegmentMerger

Called from `Recorder.resolve_final_files()`.

1. Collect all segments from the call (in order).
2. For each inter-segment gap ≥ `ffmpeg_gap_silence_threshold_seconds`:
   - Generate silence WAV: `ffmpeg -f lavfi -i anullsrc=r=48000:cl=mono -t <gap_s> silence.wav`
   - Mark as temp (not uploaded, not stored in DB).
3. Build concat list. Try `ffmpeg -f concat -safe 0 -i list.txt -c copy final.mp3`.
4. If copy fails: `ffmpeg -f concat -safe 0 -i list.txt -c:a libmp3lame -b:a 64k final.mp3`.
5. If both fail: log error, preserve all segments, return first segment as best-available.
6. On success: delete concat list + silence temps + individual segments.

---

## Files to keep stable (frozen)

| File | Reason |
|------|--------|
| `detector.py` | Session detection works; unrelated to audio |
| `state_machine.py` | Call state machine; unrelated to audio |
| `storage.py` | DB schema in use; schema changes need migration |
| `uploader.py` | Upload retry state machine; do not change retry counts |
| `report.py` | File naming used by external consumers |
| `launcher.py` | Restart loop works correctly |

---

## Recorder public interface compatibility list

The following must remain with identical signatures after migration:

```python
# Properties (no change)
is_recording: bool
current_recording_path: Optional[str]
started_at: Optional[datetime]

# Methods (no change)
start_recording() -> bool
stop_recording() -> bool
force_stop_recording() -> bool
detach_context() -> RecordingContext
detach_contexts() -> List[RecordingContext]
resolve_final_files(contexts: List[RecordingContext]) -> List[str]
resolve_final_file(ctx: RecordingContext) -> Optional[str]
ensure_recording_alive() -> bool
get_recording_metadata() -> dict
refresh_bandicam_paths() -> bool   # alias for main.py compat
```

Internals of `Recorder` may change. `RecordingContext` dataclass must remain compatible.

---

## Implementation phases

### Phase 0 — Documentation (complete)
- `CLAUDE.md` created at project root.
- `HANDOFF.md` updated with FFmpeg migration plan.
- `PROJECT_MEMORY.md` updated with decision history.
- `CHANGELOG.md` planned section added.
- `recorder_plan/skills/audio-capture.md` updated to FFmpeg direction.
- This file created.
- No production code changed.

### Phase 1 — Interface inspection and plan (next)
- Read `recorder.py` fully.
- Map all `main.py` → `Recorder` call sites with line numbers.
- Design `RecorderBackend` abstraction boundary.
- Confirm plan with Ahmed. No code yet.

### Phase 2 — Backend skeleton
- `DeviceResolver` class.
- `FFmpegCaptureEngine` class with basic start/stop.
- `bin/ffmpeg.exe` availability check at import.
- Config keys added to `config.py` and `config.json`.
- Compile check + `pytest tests/ -v`. Commit + push.

### Phase 3 — Recorder integration
- Wire `FFmpegCaptureEngine` into `Recorder`.
- All public interface methods implemented.
- PyAudio backend behind `"backend": "pyaudio"` config key.
- Tests pass. Compile check. Commit + push.

### Phase 4 — Health monitoring + USB recovery
- `ProcessWatchdog`, `FileGrowthMonitor`.
- `USBRecoveryLoop` with segment metadata tracking.
- Segment restart on death/stall/USB.
- Tests. Compile. Commit + push.

### Phase 5 — Segment merger
- `SegmentMerger` with gap preservation.
- Silence generation, concat list, copy + re-encode fallback.
- Failure behavior: preserve segments on error.
- Tests. Compile. Commit + push.

### Phase 6 — Validation and production switch
- Manual test on Ahmed's PC (all 7 test scenarios).
- Manual test on representative PCs.
- Switch `"backend": "ffmpeg"` as default.
- Final doc update. Commit + push.

---

## Config additions (planned)

Add to `config.json` and `config.py` DEFAULT_CONFIG during Phase 2:

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

`"backend": "pyaudio"` retains rollback. `"backend": "ffmpeg"` is target default.

---

## Deployment notes for bin\ffmpeg.exe

- Binary must exist at `<project_root>\bin\ffmpeg.exe` on every deployment target.
- Version-pin: use the same binary version on all PCs.
- Source: https://www.gyan.dev/ffmpeg/builds/ → `ffmpeg-release-essentials.zip` → `bin/ffmpeg.exe`.
- Verify: `bin\ffmpeg.exe -version` must print version without error.
- Verify WASAPI listing: `bin\ffmpeg.exe -list_devices true -f wasapi -i dummy` must list devices.
- Do NOT commit the binary to git if repo size is a concern (binary = ~100 MB).
  Add `bin/ffmpeg.exe` to `.gitignore` and deploy via GPO package or setup script.
- If committing internally (no GitHub size limit concern): commit and version-pin.

---

## PyInstaller / spec notes

`whatsapp_watcher.spec` must be updated during Phase 2+ to bundle the binary:

```python
datas=[
    ("config.json", "."),
    ("bin/ffmpeg.exe", "bin"),
],
```

Also review `collect_all` entries: `pyaudiowpatch` and `lameenc` may be removable once
FFmpeg handles encoding. Do not remove them until Phase 3+ is confirmed working.

The current spec bundles `pyaudiowpatch`, `lameenc`, `comtypes`. Keep all until
PyAudio rollback is no longer needed.

Read the current spec file before editing — do not modify from memory.

---

## Test checklist (Phase 6 sign-off)

- [ ] Device discovery: FFmpeg binary found, version logged, devices listed, mic + loopback selected.
- [ ] Normal outgoing call: one final file, both sides audible, no robotic audio.
- [ ] Normal incoming call: same.
- [ ] Back-to-back calls (≤2 s apart): two separate files, no merged recording.
- [ ] USB headset unplug mid-call: silence gap → replug → merged final file with silence in gap.
- [ ] Long call (10+ min): no size/duration limit, finalization works, upload works.
- [ ] Loopback-only (no real mic): recording produced, no crash.
- [ ] Built-in mic fallback (USB absent): correct device selected.
- [ ] App crash mid-call: direction latch restores on restart, no orphan recorder.
- [ ] Representative PC 1: USB headset.
- [ ] Representative PC 2: built-in mic only.
- [ ] Representative PC 3: different headset brand.

---

## Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| FFmpeg WASAPI device names differ from PyAudio names | Use FFmpeg device listing exclusively for FFmpeg inputs |
| FFmpeg startup latency on call start | Benchmark; if >2 s log [REC-011]; FFmpeg typically starts in <500 ms |
| FFmpeg process death mid-call | ProcessWatchdog restarts segment; final file merges segments |
| Segment merge codec mismatch | Fallback re-encode handles mixed inputs |
| Merge failure loses recording | Never delete segments on failure; return first segment as fallback |
| PyInstaller bundling of ffmpeg.exe | Test EXE build explicitly; check `bin/ffmpeg.exe` path in bundled app |
| PyAudio still needed for device scoring | Keep PyAudio as optional scoring-only dependency until confirmed not needed |
| WASAPI exclusive mode conflict | Test with other apps running; document known conflicts |
| Back-to-back calls during FFmpeg stop | `recorder.stop_recording()` must return quickly; FFmpeg SIGTERM is fast |
