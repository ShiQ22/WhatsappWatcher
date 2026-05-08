# Project Memory — WhatsApp Watcher

## Decision: FFmpeg recorder backend migration (2026-05-08)

**The PyAudio recorder backend has been rejected for production use.**

Despite three major refactor cycles (rev1 three-thread architecture, rev2 pacing fixes,
rev3 non-blocking readers + jitter buffer), the PyAudio/WASAPI pipeline continued to produce
robotic audio artifacts confirmed by debug stem analysis. Each patch resolved one artifact
while introducing another, and the root cause is the approach itself (manual PCM
read/mix/write in Python threads), not a single fixable bug.

**Approved direction:** Replace the recording backend with FFmpeg subprocess while keeping
all other systems (detection, state machine, finalization, upload, DB, reports, launcher)
exactly as they are.

---

## Why PyAudio recorder was stopped

### Failure modes observed in production

1. **50% duty-cycle audio islands** — both LoopbackReader and MicReader alternated real/silence
   frames at 20ms granularity. Confirmed from debug stems: 92-93% of active audio runs ≤ 40ms.
   Root cause: blocking `stream.read(chunk)` takes exactly one chunk period, then sleep=0,
   then re-read hits empty WASAPI ring buffer and blocks again. Alternating real/silence.

2. **Robotic microphone** — USB mono mics expose one active channel. Averaging L+R halved
   amplitude. Per-second channel mode selection (rev3) reduced severity but did not eliminate.

3. **Robotic loopback** — same 50% duty cycle from WASAPI loopback stalls.

4. **Echo at call start** — FIFO deque kept stale frames; writer mixed them out-of-phase.

5. **Short recordings** — record thread broke out early on USB disconnect; WAV finalized
   at disconnect time, not call end.

6. **USB access violation** — `pyaudiowpatch.read()` on a dead WASAPI stream after USB
   removal triggered a Windows C-level fault (uncatchable).

7. **Reconnect failures** — race between reconnect thread and `stop()` leaked stream handles.

8. **Manual jitter buffer** — `_JitterBuffer` was added to smooth the duty-cycle problem but
   introduced hold-stale and overflow patterns of its own.

### Why further patching is not viable

Each of rev1, rev2, rev3 fixed a confirmed root cause but revealed another underneath.
The pattern of "patch → new artifact → patch" indicates the PyAudio/WASAPI read-in-Python
model cannot reliably handle WASAPI timing constraints on real consumer hardware.

---

## FFmpeg design principles

1. **FFmpeg owns audio capture.** Python is the orchestrator; FFmpeg is the recorder.
2. **Device names from FFmpeg.** Use `ffmpeg -list_devices true -f wasapi` for exact names.
   Never hardcode device names. Never use PyAudio device names as FFmpeg inputs directly.
3. **Runtime device discovery.** `DeviceResolver` runs on each PC at call start.
   No per-PC configuration needed.
4. **PyAudio for scoring only.** PyAudio may be used to enumerate and score devices
   (USB bonus, name matching). It must not be used for recording.
5. **No manual PCM in Python.** FFmpeg handles mixing, resampling, encoding.
6. **Subprocess safety.** FFmpeg stderr must be drained in a daemon thread.
   Never block on stderr.
7. **Segment model for USB.** USB disconnect → close segment → wait → new segment on replug.
8. **Gap preservation.** `time.monotonic()` measures segment gap. Generated silence fills
   the gap before merge if `ffmpeg_preserve_usb_gap_silence=true`.
9. **Merge safety.** Concat copy first; re-encode fallback. Original segments never deleted
   on merge failure.
10. **Rollback.** PyAudio backend stays behind `"backend": "pyaudio"` config until FFmpeg
    passes manual tests.

---

## Known requirements (invariants that must be preserved)

| Requirement | Detail |
|-------------|--------|
| One final file per call | After merge, one file is uploaded/stored/reported |
| Back-to-back calls stay separate | Session split in main.py must produce two separate files |
| USB unplug creates internal segments | Disconnect → segment closed; replug → new segment |
| Gap preserved in merged file | USB unplug gap filled with silence before merge |
| Upload/DB/report unchanged | Final file path, naming, upload flow identical to current |
| Recorder public interface stable | All method/property signatures in `Recorder` unchanged |
| No temp file uploads | Gap silence files and concat list files must not be uploaded |
| Temp segments preserved on merge failure | Never delete evidence on error |

---

## Bandicam removed (historical — pre-2026-05-07)

Native audio recorder replaced Bandicam completely. Bandicam aliases (`bandicam_path`,
`bandicam_output_dir`, `refresh_bandicam_paths`) kept as backward-compat properties.
`refresh_bandicam_paths` alias still required for `main.py` compatibility.

---

## Audio pipeline architecture (PyAudio — now rejected for production)

Three independent daemon threads inside `CaptureEngine`. Architecture confirmed working for
session management but audio quality not acceptable.

- **LoopbackReader / MicReader** (`_SourceReader`): non-blocking poll loop.
- **Writer** (`_AudioWriter`): wall-clock scheduled, WAV writes.
- **`_JitterBuffer`**: FIFO with hold-last. Replaced `_FrameBuffer` in rev3.

These classes (`_SourceReader`, `_AudioWriter`, `_JitterBuffer`) are NOT part of the FFmpeg
backend. They may remain in `recorder.py` temporarily as the PyAudio rollback backend, but
they must not be further patched as a production path.

---

## Direction no-downgrade guard + session latch (2026-05-08)

**Guard**: `_should_update_direction(new_dir, current_dir)` returns `False` when
`new_dir == "unknown"` and `current_dir` is `"incoming"` or `"outgoing"`.
Applied to both direction-propagation blocks in `main.py run()`.

**Session latch** (`data/active_call_session.json`):
- Saved when direction first proven (gate: `_direction_latched` flag in `run()` scope).
- Restored on crash-restart if hwnd or session_generation matches and `saved_at` ≤ 3600 s ago.
- Cleared (file deleted) immediately after each finalize thread `.start()` in ALL paths.
- `_direction_latched` reset to `False` in every latch-clear path.

**Critical rule**: never restore latch for a clearly new call (no hwnd/gen match).

---

## USB hot-swap fix (2026-05-08, PyAudio era)

**Root causes:**
1. `close()` after USB removal → Windows access violation.
2. Record loop broke out when both streams `None` → short WAV.
3. Reconnect/stop race → stream handle leak.

**Fixes (PyAudio backend):**
- `on_usb_disconnect()` nulls streams under lock; `stop_stream()` only, never `close()`.
- Record loop writes silence on missing streams instead of breaking.
- `_reconnect_disabled` flag prevents reconnect during `stop()`.

**For FFmpeg backend:** close subprocess cleanly on USB removal; preserve partial segment.

---

## MP3 / 48 kHz output (2026-05-08)

Config: `format=mp3`, `sample_rate=48000`, `chunk_size=960`, `mp3_bitrate=64`.
USB device native rate is 48000 Hz; 44100 was always auto-rejected.
MP3 @ 64 kbps reduces file size ~4× vs WAV.

FFmpeg backend will support MP3 output natively without `lameenc`.

---

## Immediate ENDED return + terminal finalize ownership fix (2026-05-07)

**Root cause 1 — 33-second delay:** `ensure_recording_alive()` called `_do_mute_check()` synchronously.
Fixed: spawn in daemon thread (`mute-check-health`).

**Root cause 2 — REC-012 double finalize:** REC-012 fired before terminal block.
Fixed: `and not sm.is_terminal_state()` added to REC-012 condition.

**Rule:** `_do_mute_check` MUST NEVER be called synchronously on the main poll thread.

---

## Fast back-to-back call boundary fix (2026-05-07)

**Root cause:** `SESSION_WINDOW_GAP_SECONDS = 2.5` applied to all sessions including ringing.

**Fix:**
- Ringing sessions (`_session_answered_proof_seen=False`): emit ENDED immediately on window disappearance.
- Active sessions (`_session_answered_proof_seen=True`): keep 2.5 s gap.
- `_session_generation` increments on each ring; `different_generation` is a split trigger.

**Critical rule:** Do NOT restore the 2.5 s window gap for ringing sessions.

---

## Session boundary design (2026-05-07)

Session identity = hwnd of WhatsApp call window.
Every `DetectionResult` while a call window exists carries `hwnd=win.hwnd`.
Split condition: `is_live_session and is_new_call_event and not weak_call_started and (different_hwnd or strong_new_call or ringing/connecting states)`.
Split path does NOT call `detector.reset()`.

---

## Upload / local DB / central sync

- Local SQLite at `data/calls.db` (always written).
- Central DB (optional): tried immediately on `save_call()`.
- File uploads: `uploader.py` copies recording to network path from `config.json`.
- Failed uploads remain in `pending_uploads` and retried on background sync.

---

## Launcher / EXE behavior

- `launcher.py` runs as outer process (restart loop).
- `--worker` flag runs `main.run()` once (inner process).
- Max `MAX_RESTARTS_PER_HOUR = 20` restarts before launcher gives up.
- PyInstaller spec: `whatsapp_watcher.spec` (needs update for `bin/ffmpeg.exe`).

---

## Log code meanings

| Code | Meaning |
|---|---|
| `[REC-001]` | No audio device available at recording start |
| `[REC-002]` / `[REC-003]` | Loopback / mic stream open failed (PyAudio era) |
| `[REC-008]` | WAV file open failed |
| `[REC-009]` | Silence detected for >6s during recording |
| `[REC-010]` | Written frames < 70% of expected |
| `[REC-011]` | `recorder.start_recording()` > 2 s |
| `[REC-012]` | Orphan recorder guard fired |
| `[REC-014]` | Buffer overflow (PyAudio era) |
| `[DEV-003]` | Zero input devices enumerated |
| `[DEV-USB]` | USB headset connect/disconnect/selection event |
| `[UPL-001]` | Unhandled exception in `process_pending_uploads` |
| `[UPL-002]` | File copy failure during upload |
| `[UPL-004]` | Local recording file missing at upload time |
| `SESSION SPLIT` | Back-to-back call boundary detected |
| `CALL END` | Terminal state reached; finalize thread spawned |
| `[LATCH]` | Session direction latch: save / restore / clear |
