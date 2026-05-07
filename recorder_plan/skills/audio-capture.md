# Skill: Audio capture + WAV/MP3 output

## Recording thread

The recording thread is the core of CaptureEngine. It is the ONLY code that writes audio data.

```python
SAMPLE_RATE  = 44100   # Hz — configurable in config.json
CHANNELS     = 1       # mono — good for voice
SAMPLE_WIDTH = 2       # 16-bit PCM
CHUNK_SIZE   = 1024    # frames per read (~23ms at 44100Hz)

def _record_loop(self):
    """Runs in daemon thread. Reads PCM chunks and writes to WAV."""
    log.info("REC → record thread started | device=%s | file=%s",
             self._device_name, self._output_path)
    try:
        while not self._stop_event.is_set():
            try:
                data = self._stream.read(CHUNK_SIZE, exception_on_overflow=False)
                self._wav_file.writeframes(data)
                self._bytes_written += len(data)
            except OSError as exc:
                log.exception("[REC-005] WAV write error | path=%s | bytes_written=%s",
                              self._output_path, self._bytes_written)
                break  # exit thread — watchdog will detect and recover
    finally:
        self._finalize_wav()
        log.info("REC → record thread exiting | bytes_written=%s", self._bytes_written)
```

## WAV file lifecycle

```python
import wave

def _open_wav(self, path: str) -> bool:
    try:
        self._wav_file = wave.open(path, 'wb')
        self._wav_file.setnchannels(CHANNELS)
        self._wav_file.setsampwidth(SAMPLE_WIDTH)
        self._wav_file.setframerate(SAMPLE_RATE)
        self._output_path = path
        self._bytes_written = 0
        return True
    except OSError:
        log.exception("[REC-008] Failed to open WAV file for writing | path=%s", path)
        return False

def _finalize_wav(self):
    """Must be called from the record thread's finally block."""
    try:
        if self._wav_file:
            self._wav_file.close()
            self._wav_file = None
            log.info("REC → WAV finalized | path=%s | bytes=%s",
                     self._output_path, self._bytes_written)
    except Exception:
        log.exception("[REC-006] WAV finalization failed | path=%s", self._output_path)
```

WAV files written this way need NO settle wait. The file is closed the moment the thread exits.
`resolve_final_files()` only needs to confirm file exists and size > 0. Max wait: 3 seconds.

## File naming

```python
from datetime import datetime

def _make_output_path(self, output_dir: str, segment: int) -> str:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    suffix = f"_seg{segment}" if segment > 1 else ""
    return str(Path(output_dir) / f"{ts}{suffix}.wav")
```

## MP3 conversion with lameenc

lameenc is a pure-Python LAME MP3 encoder. No ffmpeg, no DLL. Works in PyInstaller.

```python
import lameenc

def _convert_to_mp3(self, wav_path: str) -> Optional[str]:
    mp3_path = wav_path.replace(".wav", ".mp3")
    try:
        # Read WAV PCM data
        with wave.open(wav_path, 'rb') as wf:
            frames = wf.readframes(wf.getnframes())
            rate = wf.getframerate()
            channels = wf.getnchannels()

        # Encode to MP3
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(128)         # 128kbps — good for voice
        encoder.set_in_sample_rate(rate)
        encoder.set_channels(channels)
        encoder.set_quality(2)            # 2=highest, 7=fastest
        mp3_data = encoder.encode(frames)
        mp3_data += encoder.flush()

        with open(mp3_path, "wb") as f:
            f.write(mp3_data)

        log.info("REC → MP3 converted | wav=%s | mp3=%s | size=%s bytes",
                 wav_path, mp3_path, len(mp3_data))
        return mp3_path

    except Exception:
        log.exception("[REC-007] MP3 conversion failed | wav=%s — keeping WAV", wav_path)
        return None
```

## Format config logic

```python
# In resolve_final_files(), after confirming WAV exists:

fmt = RECORDER_FORMAT  # "wav" | "mp3" | "both"

if fmt == "wav":
    return [wav_path]

elif fmt == "mp3":
    mp3 = self._convert_to_mp3(wav_path)
    if mp3:
        Path(wav_path).unlink(missing_ok=True)  # delete WAV
        return [mp3]
    else:
        return [wav_path]  # fallback: keep WAV, log already written

elif fmt == "both":
    mp3 = self._convert_to_mp3(wav_path)
    return [wav_path, mp3] if mp3 else [wav_path]
```

## Audio quality guide

| Setting | Value | File size/min | Use case |
|---------|-------|--------------|----------|
| 44100 Hz mono 16-bit | default | 5.2 MB | Full quality voice |
| 22050 Hz mono 16-bit | medium | 2.6 MB | Acceptable quality |
| 16000 Hz mono 16-bit | minimum | 1.9 MB | Telco standard |
| MP3 128kbps | converted | ~1 MB | Storage-efficient |

Default is 44100 Hz mono. This matches or exceeds Bandicam's typical voice recording quality.
For call centers: 22050 Hz mono + MP3 128kbps is a good compromise if disk space matters.

## Watchdog thread

```python
WATCHDOG_INTERVAL = 5.0  # seconds

def _watchdog_loop(self):
    while not self._watchdog_stop.is_set():
        time.sleep(WATCHDOG_INTERVAL)
        if self._watchdog_stop.is_set():
            break
        if self._record_thread and not self._record_thread.is_alive():
            log.warning("[HLT-001] Watchdog: record thread dead | uptime=%ss",
                        int(time.time() - self._thread_started_at))
            self._trigger_recovery()

def _trigger_recovery(self):
    # Called from watchdog thread
    # 1. Save current context to completed list
    # 2. Re-select device (may have changed)  
    # 3. Start new record thread (segment N+1)
    # 4. Log HLT-001 with attempt number
    # Max 2 recovery attempts — if both fail, log HLT-002, set recovery_exhausted flag
```
