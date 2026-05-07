# WhatsApp Watcher — Recorder Replacement

## Mission
Replace `recorder.py` (Bandicam wrapper) with a native Python audio recorder.
Preserve 100% of existing behaviour visible to the rest of the app.

## Hard constraints
**NEVER modify:** `main.py` · `detector.py` · `state_machine.py` · `storage.py` · `uploader.py` · `report.py`  
**These files are frozen. Touch them = breaking the contract.**

---

## Scope checklist
Work through IN ORDER. Do not start the next item until tests pass for the current one.

```
[ ] 1. DeviceManager class        → skills/device-detection.md
[ ] 2. CaptureEngine class        → skills/audio-capture.md
[ ] 3. Recorder public class      → skills/recorder-interface.md
[ ] 4. config.json / config.py    → skills/config-changes.md
[ ] 5. requirements.txt           → skills/dependencies.md
[ ] 6. tests/ full suite          → skills/testing-guide.md
[ ] 7. whatsapp_watcher.spec      → skills/pyinstaller.md
```

---

## Public interface — MUST match exactly

`recorder.py` must export one class `Recorder` and one dataclass `RecordingContext`.

```python
@dataclass
class RecordingContext:
    pre_start_snapshot: Set[str]
    start_marker: Optional[float]
    started_at: Optional[datetime]
    output_dir: Optional[str] = None
    segment_index: int = 1

class Recorder:
    # Properties
    is_recording: bool
    current_recording_path: Optional[str]
    started_at: Optional[datetime]

    # Methods — signatures must be identical
    def refresh_recorder_paths(self) -> bool: ...
    def start_recording(self) -> bool: ...
    def stop_recording(self) -> bool: ...
    def force_stop_recording(self) -> bool: ...
    def detach_contexts(self) -> List[RecordingContext]: ...
    def detach_context(self) -> RecordingContext: ...
    def resolve_final_files(self, contexts: List[RecordingContext]) -> List[str]: ...
    def resolve_final_file(self, ctx: RecordingContext) -> Optional[str]: ...
    def ensure_recording_alive(self) -> bool: ...
```

`main.py` calls `recorder.refresh_bandicam_paths()` — add an alias:
```python
refresh_bandicam_paths = refresh_recorder_paths  # alias for main.py compatibility
```

---

## Architecture — three internal layers

```
Recorder (public API)
    └── CaptureEngine          ← manages stream + threads + WAV/MP3 file
            └── DeviceManager  ← selects device, handles plug/unplug events
```

Each layer catches its own exceptions. A failure in DeviceManager must NOT crash CaptureEngine.
A failure in CaptureEngine must NOT crash Recorder. Recorder returns False, logs the code, continues.

---

## Logging rules — apply everywhere

Log format (set in `main.py` formatter — do NOT change main.py; this is already applied project-wide):
```
%(asctime)s  %(levelname)-8s  [%(filename)s:%(lineno)d]  %(name)s  %(message)s
```

In `recorder.py`, all loggers must use: `log = logging.getLogger("watcher.recorder")`

Rules:
- `log.exception(msg)` whenever inside an except block — never `log.error(msg)` for exceptions
- Every error log MUST include the fail code: `log.error("[REC-002] [attempt %s/3] ...")`
- Never swallow exceptions silently — always log with code + context
- Info logs for every state transition: device selected, stream opened, recording started/stopped

Fail codes: read `skills/error-codes.md` — use exactly as specified there.

---

## Audio output

- Primary format: **WAV** (always — recorded live, never buffered in RAM)
- Secondary format: **MP3** (optional, converted after stop, using `lameenc`)
- Config drives format: `recorder.format` = `"wav"` | `"mp3"` | `"both"`
- If MP3 conversion fails → keep WAV, log warning, return WAV path. Never fail silently.
- Recording quality: `44100 Hz · mono · 16-bit PCM` (default, configurable)

---

## Session protocol

1. **Start of session:** read `session/handoff.md` — understand exact state
2. **One component at a time** — finish + test before moving on
3. **Tests alongside code** — write test as you write the function
4. **End of session:** update `session/handoff.md` with current state
5. **Never leave tests failing** — fix or explicitly mark as known issue in handoff

---

## Definition of done

- All tests in `tests/` pass: `pytest tests/ -v`
- No bare `except:` anywhere in `recorder.py`
- No `time.sleep()` on the main thread inside Recorder methods
- `refresh_bandicam_paths` alias present
- EXE builds without errors: `pyinstaller whatsapp_watcher.spec`
- Manual test on real machine: plug USB headset → start app → make call → unplug headset → call continues recording → stop → WAV file exists and is valid audio
