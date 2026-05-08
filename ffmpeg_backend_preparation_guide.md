# WhatsApp Watcher — FFmpeg Backend Preparation Guide

## Goal

Start fresh from the approved FFmpeg migration direction and stop patching the broken PyAudio recorder.

The current project should keep:

- WhatsApp detection
- State machine
- Direction latch
- Call start/end/session handling
- Naming
- Local DB
- Central DB sync
- Upload
- Reports
- Launcher

The project should replace only the low-level audio recording backend.

The new backend should use FFmpeg as the recording engine and keep the existing `Recorder` public interface compatible with `main.py`.

---

# 1. What Ahmed Needs to Download

## 1.1 Download FFmpeg

Download FFmpeg for Windows.

Recommended:

- Windows build
- Essentials build is enough
- 64-bit
- Version-pinned; use the same binary on all PCs

Common source:

- https://www.gyan.dev/ffmpeg/builds/
- Download: `ffmpeg-release-essentials.zip`

After download:

1. Extract the zip.
2. Open the extracted folder.
3. Go to `bin`.
4. Copy `ffmpeg.exe`.

Create this folder in the project:

```text
<project root>\bin\
```

Put the binary here:

```text
<project root>\bin\ffmpeg.exe
```

Expected final path:

```text
WhatsappWatcher\
  main.py
  recorder.py
  config.json
  bin\
    ffmpeg.exe
```

## 1.2 Verify FFmpeg manually

Open Command Prompt in the project folder.

Run:

```bat
bin\ffmpeg.exe -version
```

Expected:

- FFmpeg version prints.
- No "not recognized" error.
- No Windows block/security prompt.

Then run:

```bat
bin\ffmpeg.exe -list_devices true -f wasapi -i dummy
```

Expected:

- FFmpeg prints available WASAPI audio devices to the console/stderr.
- Save or screenshot this output if needed.

Important:

FFmpeg command examples in the plan are placeholders until confirmed with the exact bundled FFmpeg build.

---

# 2. What Ahmed Needs to Prepare in the Repo

## 2.1 Add FFmpeg folder

Add:

```text
bin\ffmpeg.exe
```

Decision:

- If internal deployment can handle binary files in the repo/package, include it in the deployment package.
- If GitHub rejects or repo size is a concern, do not commit the binary; package it during deployment.

Recommended for GPO deployment:

- Include `ffmpeg.exe` in the deployed folder.
- Version-pin it.
- Use the same file on all PCs.

## 2.2 Add or update `.gitignore`

If not committing FFmpeg:

```gitignore
bin/ffmpeg.exe
bin/*.exe
```

If committing FFmpeg internally, do not ignore it.

## 2.3 Update PyInstaller spec later

When building EXE, ensure the FFmpeg binary is bundled.

Example idea:

```python
datas=[
    ("config.json", "."),
    ("bin/ffmpeg.exe", "bin"),
]
```

Claude must check the actual `.spec` file before editing.

## 2.4 Prepare config keys

Add these to the recorder section of `config.json`:

```json
{
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
}
```

Recommended first setting:

```json
"ffmpeg_mode": "dual"
```

Reason:

Do not assume loopback captures both voices. Use dual mode first unless the loopback-only test proves otherwise.

Possible future values:

```text
dual      = loopback + microphone
loopback  = loopback only
mic       = microphone only
auto      = future mode; do not rely on it until implemented/proven
```

---

# 3. Files to Give Claude

Give Claude these documents/files at the start of the new chat:

## Required

1. Current repo access:
   - GitHub repo: `ShiQ22/WhatsappWatcher`
   - Branch: `main`

2. Approved migration plan:
   - `WhatsApp Watcher — FFmpeg Migration Plan v2`

3. This preparation guide:
   - `ffmpeg_backend_preparation_guide.md`

4. Current project docs:
   - `HANDOFF.md`
   - `PROJECT_MEMORY.md`
   - `CHANGELOG.md`
   - `recorder_plan/skills/audio-capture.md`
   - `changes/audio-pipeline-refactor-2026-05-08.md`

5. Current code files Claude must read before editing:
   - `recorder.py`
   - `main.py`
   - `config.py`
   - `config.json`
   - `launcher.py`
   - `uploader.py`
   - `storage.py`
   - `report.py`
   - `detector.py`
   - `state_machine.py`
   - `whatsapp_watcher.spec` if present
   - `.gitignore` if present

## Optional but useful

1. Latest failed audio stems:
   - `*_mic_debug.wav`
   - `*_loopback_debug.wav`
   - final MP3 if available
   - `watcher.log`

2. FFmpeg device list output from Ahmed’s machine:
   - output of:
     ```bat
     bin\ffmpeg.exe -list_devices true -f wasapi -i dummy
     ```

---

# 4. Create / Update CLAUDE.md

If the repo has no `CLAUDE.md`, create one at project root.

If it already exists, append or replace the recorder section with this.

```markdown
# CLAUDE.md — WhatsApp Watcher Current Direction

## Critical current decision

Stop patching the PyAudio recorder backend.

The current PyAudio recorder has repeatedly failed with:
- robotic microphone audio
- robotic loopback audio
- echo
- short/empty recordings
- USB reconnect failure
- manual jitter-buffer artifacts

The current direction is to replace the audio recording backend with FFmpeg while preserving the rest of the app.

## Do not rewrite these systems

Do not rewrite:
- main.py call lifecycle
- detector.py
- state_machine.py
- storage.py
- uploader.py
- report.py
- launcher.py

Small targeted integration changes to main.py are allowed only if required for:
- temp segment recovery
- merge temp file exclusion
- final merged file handling

## Preserve the Recorder public interface

`main.py` must continue using the same recorder-level methods/properties:

- start_recording()
- stop_recording()
- force_stop_recording()
- detach_context()
- detach_contexts()
- resolve_final_files()
- resolve_final_file()
- ensure_recording_alive()
- get_recording_metadata()
- is_recording
- current_recording_path
- started_at

Internals may change. Public contract must stay stable.

## New backend direction

Implement an FFmpeg backend.

Keep PyAudio only for fresh device enumeration/scoring if useful.
Do not use PyAudio for audio recording.
Do not use Python threads to read PCM frames.
Do not use _SourceReader, _AudioWriter, or _JitterBuffer in the FFmpeg production path.

## Required FFmpeg backend behavior

- Start FFmpeg subprocess on call start.
- Stop FFmpeg subprocess on call end.
- Use FFmpeg to capture audio, mix sources, and write recording segment.
- Use DeviceResolver to discover device names at runtime.
- Use FFmpeg’s own device list for exact FFmpeg input names.
- Do not hardcode Ahmed’s device names.
- Support USB/headset mic, built-in mic fallback, and no-real-mic cases.
- Never use loopback as fake microphone.
- If USB disconnects, close current segment and start a new one when device returns.
- At call end, merge all segments into one final file.
- Preserve USB unplug gap by inserting generated silence segment if configured.
- Keep upload/DB/report/final naming unchanged.

## Required safety

- Do not delete temp segments if merge fails.
- Do not upload gap silence temp files.
- Do not upload concat list files.
- Log FFmpeg stderr safely without blocking the FFmpeg process.
- Use time.monotonic() for segment duration/gap calculations.
- Use wall-clock datetime only for names and human-readable logs.
- Keep rollback path until FFmpeg backend passes manual tests.

## Implementation workflow

Phase 1:
- Read current recorder.py and main.py contracts.
- Create backend plan.
- Confirm public interface compatibility.
- No code changes until plan is approved.

Phase 2:
- Implement backend abstraction and FFmpeg backend.
- Keep PyAudio backend temporarily for rollback if practical.
- Add config keys.
- Add docs.
- Run compile/tests.
- Commit and push.

Phase 3:
- Manual test on Ahmed’s PC.
- Manual test on representative PCs.
- Only then switch default backend to ffmpeg for production.
```

---

# 5. Work Structure for Claude

Claude should not do one giant risky rewrite without checkpoints.

Use this structure.

## Phase 1 — Inspection and Plan Only

Claude must:

1. Read current `recorder.py`.
2. Identify the exact current public `Recorder` interface.
3. Identify all places `main.py` calls recorder methods.
4. Identify current finalization flow.
5. Identify current segment naming and recovery rules.
6. Propose exact FFmpeg backend integration.
7. Explain what will stay and what will change.
8. Ask Ahmed to approve.

No code edits in Phase 1.

## Phase 2 — Backend Skeleton

Implement:

- `ResolvedDevices`
- `SegmentMetadata`
- `DeviceResolver`
- `FFmpegCaptureEngine`
- backend config constants
- FFmpeg binary availability check
- FFmpeg device listing command
- basic start/stop single segment

At end:

- run compile
- test DeviceResolver standalone if possible
- commit/push

## Phase 3 — Recorder Integration

Wire FFmpeg backend into `Recorder` while preserving public interface.

Implement:

- `start_recording()`
- `stop_recording()`
- `force_stop_recording()`
- `detach_contexts()`
- `resolve_final_files()`
- `ensure_recording_alive()`

At end:

- run tests
- compile
- commit/push

## Phase 4 — Health Monitor + USB Recovery

Implement:

- process watchdog
- stderr drain/logging
- file growth monitor
- segment restart on FFmpeg died/stalled
- USB recovery loop
- new segment metadata

At end:

- run tests
- compile
- commit/push

## Phase 5 — Segment Merge with Gap Preservation

Implement:

- gap calculation using `time.monotonic()`
- generated silence WAV for gaps
- concat list file
- FFmpeg concat copy attempt
- fallback re-encode
- failure behavior that preserves original segments
- temp file cleanup

At end:

- run tests
- compile
- commit/push

## Phase 6 — Docs and Final Validation

Update:

- `CHANGELOG.md`
- `HANDOFF.md`
- `PROJECT_MEMORY.md`
- `recorder_plan/skills/audio-capture.md`
- new `changes/ffmpeg-backend-migration-2026-05-08.md`
- optionally `CLAUDE.md`

At end:

- final test checklist
- commit/push

---

# 6. Exact Fresh Chat Prompt for Claude

Use this prompt in a new Claude chat.

```text
You are working on GitHub repo:

ShiQ22/WhatsappWatcher
branch: main

Start fresh. Do not continue patching the PyAudio recorder.

Important background:
The current PyAudio recorder backend has repeatedly failed with robotic mic audio, robotic loopback audio, echo, short/empty files, and USB reconnect problems. The issue is the audio recording backend, not detector/state_machine/upload/storage/report.

The approved decision:
Replace the low-level audio recorder backend with FFmpeg while preserving the existing app lifecycle.

Read these files first before making any plan:
- recorder.py
- main.py
- config.py
- config.json
- detector.py
- state_machine.py
- storage.py
- uploader.py
- report.py
- launcher.py
- CHANGELOG.md
- HANDOFF.md
- PROJECT_MEMORY.md
- recorder_plan/skills/audio-capture.md
- changes/audio-pipeline-refactor-2026-05-08.md
- whatsapp_watcher.spec if present
- .gitignore if present
- CLAUDE.md if present

Also read the migration document:
- WhatsApp Watcher — FFmpeg Migration Plan v2

Goal:
Keep all current non-audio systems:
- call detection
- state machine
- direction latch
- finalization
- naming
- local DB
- central DB sync
- upload
- reports
- launcher

Replace only the recording backend.

Hard requirements:
1. Do not use PyAudio for audio recording in the new production backend.
2. PyAudio may only be used for fresh local device enumeration/scoring.
3. FFmpeg must do the actual audio capture/mixing/encoding.
4. Do not hardcode Ahmed’s device names.
5. DeviceResolver must run locally on each PC and discover local devices at runtime.
6. Use FFmpeg’s own device list for exact names passed to FFmpeg.
7. Use fresh PyAudio instance during resolution if PyAudio scoring is used.
8. Keep Recorder public interface compatible with main.py:
   - start_recording()
   - stop_recording()
   - force_stop_recording()
   - detach_context()
   - detach_contexts()
   - resolve_final_files()
   - resolve_final_file()
   - ensure_recording_alive()
   - get_recording_metadata()
   - is_recording
   - current_recording_path
   - started_at
9. If USB disconnects during a call:
   - stop/close current segment safely
   - do not finalize the call
   - wait for device recovery
   - start a new segment when source returns
   - merge segments at call end into one final recording
10. Preserve gap duration:
   - use time.monotonic() for segment gap calculations
   - generate silence WAV for USB gap if configured
   - insert silence between segments before merge
11. Merge safely:
   - try FFmpeg concat copy first
   - fallback to re-encode if needed
   - if merge fails, preserve original real segments
   - do not upload gap silence files or concat list files
12. Add ProcessWatchdog and FileGrowthMonitor:
   - process death alone is not enough
   - stderr must be drained/logged safely so FFmpeg cannot block
13. Keep PyAudio backend temporarily behind config for rollback if practical.
14. Avoid changing main.py. Minor targeted integration only if necessary.
15. Update docs and tests.
16. Run compile/tests before commit.
17. Commit and push to origin/main after each approved implementation phase.

Phase 1 only:
Do not edit code yet.

First reply with a complete implementation plan:
- exact current Recorder public interface
- exact main.py dependencies on Recorder
- exact backend abstraction boundary
- exact FFmpeg command strategy, clearly marked as placeholder until tested
- DeviceResolver design
- FFmpeg stderr handling design
- FileGrowthMonitor design
- USB recovery and segment model
- gap-preserving SegmentMerger design
- what files you will edit
- what files you will not touch
- risk list
- staged implementation order
- ask Ahmed to approve before coding

After Ahmed approves Phase 1, proceed with implementation phase by phase.
```

---

# 7. Ahmed’s Local Checklist Before Claude Codes

Before telling Claude to implement, prepare this:

```text
[ ] FFmpeg downloaded
[ ] bin\ffmpeg.exe exists
[ ] bin\ffmpeg.exe -version works
[ ] bin\ffmpeg.exe -list_devices true -f wasapi -i dummy runs
[ ] Migration Plan v2 available to Claude
[ ] CLAUDE.md updated or ready to create
[ ] Current branch clean or intentionally ready
[ ] Latest working commit known
[ ] PyAudio backend kept as rollback until FFmpeg basic test passes
```

---

# 8. Manual Tests After First FFmpeg Implementation

Do not deploy to all PCs after first compile.

Test in this order:

## Test 1 — Device discovery only

Expected logs:

- FFmpeg binary found
- FFmpeg version logged
- FFmpeg audio devices listed
- selected mic device
- selected loopback device
- selected mode

## Test 2 — Normal incoming call

Expected:

- one final file
- both sides audible
- no robotic audio
- no echo worse than normal headset bleed
- upload/DB/report works

## Test 3 — Normal outgoing call

Same expectations.

## Test 4 — Back-to-back calls

Expected:

- separate call files
- no merge between separate calls
- no blocked finalization

## Test 5 — USB unplug/replug

Expected:

- segment 1 saved
- recovery starts
- segment 2 starts after replug
- final merged file has silence during unplug gap
- one final uploaded file

## Test 6 — Long call

Expected:

- 10+ minutes works
- no file size/duration limit
- finalization works
- upload works

## Test 7 — Representative PCs

Test at least:

- Ahmed’s PC
- one desktop with USB headset
- one laptop with built-in mic
- one different headset brand

Do not roll to all PCs before this.

---

# 9. Final Decision

The current PyAudio recorder should not be patched further for production.

The approved direction is:

```text
FFmpeg backend for recording.
Existing app for call lifecycle/finalization/upload/report.
Internal segments for USB changes.
Gap-aware merge into one final call file.
Runtime device discovery on each local PC.
```
