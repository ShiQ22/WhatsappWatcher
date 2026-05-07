# Project Memory — WhatsApp Watcher

## Bandicam removed

Native audio recorder replaced Bandicam completely. Bandicam-related aliases
(`bandicam_path`, `bandicam_output_dir`, `refresh_bandicam_paths`) are kept as
backward-compat properties that return `None` / `False` so existing callers
don't crash. All recording goes through `CaptureEngine` (PyAudio loopback +
microphone).

## Audio fix history

- **Dual-stream mixing**: loopback (system audio) + microphone captured in
  parallel and mixed sample-by-sample with int16 clamping.
- **Stereo-to-mono conversion**: loopback opens as stereo when possible;
  converted to mono before mixing with mic.
- **Silence detection**: `[REC-009]` logged if both streams silent for >6s.
- **Duration ratio check**: `[REC-010]` logged if written frames < 70% of
  expected frames for elapsed time (indicates timing mismatch or buffer stall).
- **Watchdog recovery**: dead record thread is detected and restarted; new
  segment index allocated on each recovery. Gives up after
  `RECORDER_WATCHDOG_RECOVERY_ATTEMPTS`.

## Session boundary bug history and final design

### Problem (fixed 2026-05-07)
Back-to-back WhatsApp calls could merge into one record/recording.

### Root causes found
1. FIX-2 log in `detector.py` showed `old_hwnd=NEW | new_hwnd=NEW` because
   `self._call_hwnd` was already reassigned before the log fired.
2. ANSWERED, active no-event, and ongoing-phase `DetectionResult` objects
   did not carry `hwnd=win.hwnd`.

### Final design
- **Session identity = hwnd** of the WhatsApp call window.
- Every `DetectionResult` while a call window exists carries `hwnd=win.hwnd`.
- Ring events (`INCOMING_RING`, `OUTGOING_RING`, `CALL_STARTED`) carry
  `is_strong_new_call=True`. `ANSWERED` does not.
- `previous_hwnd = self._call_hwnd` is saved before the `if new_window:` block
  in `detector.poll()` so all logs see the real old value.
- `main.py` `_TERMINAL_STATES = {IDLE, ENDED, RECORDER_ERROR, DETECTOR_ERROR}`.
  Any other state is "live"; a new ring event while live triggers a split.
- Split path: deepcopy old session → stop/detach recorder → finalize thread →
  `sm.transition(RESET)` → clear `current_session_hwnd`. Does **not** call
  `detector.reset()`.

## USB selection behavior

- `DeviceManager` scores input devices; USB headsets get highest score.
- USB headset selected at call start is logged as `[DEV-USB] USB device selected`.
- On disconnect during call: `[DEV-USB]` warning logged; recording continues on
  whatever device survives.
- On reconnect while idle: `[DEV-USB]` info "next call will use USB device."
- On reconnect during recording: `[DEV-USB]` "USB reconnected; switch requested."

## Upload / local DB / central sync

- Local SQLite at `data/calls.db` (always written).
- Central DB (optional): tried immediately on `save_call()`; failures are
  retried in background `sync_unsynced_*` threads every
  `CENTRAL_SYNC_INTERVAL_SECONDS`.
- File uploads: `uploader.py` copies recording to a network path configured in
  `config.json`. Retries up to `UPLOAD_RETRY_COUNT` times with
  `UPLOAD_RETRY_DELAY_SECONDS` between attempts.
- Failed uploads remain in `pending_uploads` table and are retried on next
  background sync.

## Launcher / EXE behavior

- `launcher.py` runs as the outer process (restart loop).
- `--worker` flag runs `main.run()` once (inner process, no restart loop).
- Launcher restarts worker on non-zero exit; stops on exit code 0.
- Max `MAX_RESTARTS_PER_HOUR = 20` restarts before launcher gives up.
- PyInstaller spec: `whatsapp_watcher.spec`.

## Log code meanings

| Code | Meaning |
|---|---|
| `[REC-001]` | No audio device available at recording start |
| `[REC-002]` / `[REC-003]` | Loopback / mic stream open failed |
| `[REC-008]` | WAV file open failed |
| `[REC-009]` | Silence detected for >6s during recording |
| `[REC-010]` | Written frames < 70% of expected (timing/buffer issue) |
| `[DEV-003]` | Zero input devices enumerated |
| `[DEV-USB]` | USB headset connect/disconnect/selection event |
| `[UPL-001]` | Unhandled exception in `process_pending_uploads` |
| `[UPL-002]` | File copy failure during upload |
| `[UPL-004]` | Local recording file missing at upload time |
| `SESSION SPLIT` | Back-to-back call boundary detected; old session finalized |
| `CALL END` | Terminal state reached; finalize thread spawned |
