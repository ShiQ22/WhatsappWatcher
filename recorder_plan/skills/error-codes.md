# Skill: Error codes — complete reference

Use these codes EXACTLY in log messages. Format: `[CODE]` as the first token in the message.

## Device Manager codes

| Code | Level | When | Example log |
|------|-------|------|-------------|
| DEV-001 | ERROR | IMMDeviceEnumerator COM init failed | `[DEV-001] IMMDeviceEnumerator init failed — no plug/unplug events` |
| DEV-002 | WARNING | Default comm device query returned no device | `[DEV-002] No comm device found — falling back to score list` |
| DEV-003 | ERROR | Zero input devices found on this machine | `[DEV-003] No audio input devices found` |
| DEV-004 | WARNING | IMMNotificationClient registration failed | `[DEV-004] Notification client failed — polling fallback active (2s interval)` |
| DEV-CHG | INFO | Device change event received | `DEV-CHG → removed: Plantronics USB | new comm: Realtek HD Audio` |

## Recorder / CaptureEngine codes

| Code | Level | When | Example log |
|------|-------|------|-------------|
| REC-001 | ERROR | No scoreable input device exists | `[REC-001] No audio input device available — recording skipped` |
| REC-002 | WARNING | Stream open failed on a specific device | `[REC-002] [attempt 1/3] Stream open failed: Plantronics USB — OSError(-9999)` |
| REC-003 | ERROR | All devices in fallback chain exhausted | `[REC-003] All devices failed — tried=[Plantronics USB, Realtek HD] — call saved without audio` |
| REC-004 | ERROR | Record thread died unexpectedly | `[REC-004] Record thread exited abnormally | uptime=43s` |
| REC-005 | ERROR | WAV write error mid-recording | `[REC-005] WAV write error | errno=28 (disk full) | path=... | bytes_written=10485760` |
| REC-006 | ERROR | WAV finalization failed | `[REC-006] WAV finalize failed | path=... | size=0` |
| REC-007 | WARNING | MP3 conversion failed | `[REC-007] MP3 conversion failed — keeping WAV | path=...` |
| REC-008 | ERROR | Output directory not accessible | `[REC-008] Output dir not accessible | path=C:/recordings | errno=2` |

## Health / Watchdog codes

| Code | Level | When | Example log |
|------|-------|------|-------------|
| HLT-001 | WARNING | Watchdog detected dead thread, attempting recovery | `[HLT-001] Thread dead | attempt 1/2 | restarting on Plantronics USB` |
| HLT-002 | ERROR | All recovery attempts exhausted | `[HLT-002] Recovery failed — call continues without further recording` |

## Normal operation (no code prefix)

These are NOT errors — log without code prefix:

```
REC → selected device: Plantronics USB Headset | score=8 | api=WASAPI
REC → stream opened | 44100Hz mono | segment=1
REC → recording started | file=2026-04-28_09-11-05.wav
REC → stop dispatched | segment=1 | bytes=31457280
REC → WAV finalized | path=... | bytes=31457280
REC → recovery OK | new segment: 2026-04-28_09-23-17_seg2.wav
```

## Code usage rules

1. Each code appears ONCE per occurrence — don't repeat it in the same log line
2. Always include device name and attempt number for REC-002
3. Always include `tried=[...]` list for REC-003
4. Always include `bytes_written` for REC-005
5. `log.exception()` for all ERROR codes when inside an except block — this adds the traceback
6. `log.warning()` for WARNING codes — these are recoverable
7. `log.error()` for ERROR codes outside an except block (no active exception to trace)
