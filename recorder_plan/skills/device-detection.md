# Skill: Audio device detection (Windows MMAPI)

## How device selection works

### Step 1 — Query Default Communications Device
Windows maintains a "Default Communications Device" separate from the default playback device.
When a USB headset is plugged in, Windows automatically assigns it to the communications role.

```python
import pyaudiowpatch as pyaudio

pa = pyaudio.PyAudio()

# Get the default communications INPUT device
# This is what Teams/Zoom/Webex all use
info = pa.get_default_input_device_info()  # fallback
# For communications-role specifically:
wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
default_comm_idx = wasapi_info.get("defaultInputDevice", -1)
```

### Step 2 — Enumerate and score all devices (fallback)
Used when Step 1 fails or returns built-in mic.

```python
def _score_device(name: str, host_api_name: str, is_comm_default: bool) -> int:
    score = 0
    name_lower = name.lower()
    if is_comm_default:
        score += 4
    if "usb" in name_lower or host_api_name == "Windows WASAPI":
        score += 3
    if any(w in name_lower for w in ("headset", "headphone", "earphone")):
        score += 2
    if "bluetooth" in name_lower or "bt" in name_lower:
        score += 1
    if any(w in name_lower for w in ("built-in", "internal", "realtek", "conexant", "idt", "laptop")):
        score -= 3
    return score
```

### Step 3 — Open stream with retries

```python
MAX_OPEN_RETRIES = 3
RETRY_DELAY = 0.5  # seconds

for attempt in range(1, MAX_OPEN_RETRIES + 1):
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=CHUNK_SIZE,
        )
        break  # success
    except OSError as exc:
        log.warning("[REC-002] [attempt %s/%s] Stream open failed: %s — %s",
                    attempt, MAX_OPEN_RETRIES, device_name, exc)
        if attempt < MAX_OPEN_RETRIES:
            time.sleep(RETRY_DELAY)
else:
    # All retries failed — try next device in scored list
```

---

## IMMNotificationClient — plug/unplug events

Register this COM callback to receive events when USB headsets plug/unplug.
`comtypes` is already a project dependency.

```python
import comtypes
import comtypes.client

# Load the MMAPI typelib
comtypes.client.GetModule("MMDevAPI.dll")
from comtypes.gen.MMDevAPILib import (
    IMMNotificationClient,
    IMMDeviceEnumerator,
    MMDeviceEnumerator,
)

class _DeviceNotificationClient(IMMNotificationClient):
    def OnDefaultDeviceChanged(self, flow, role, pwstrDefaultDeviceId):
        # role 1 = eCommunications
        if role == 1:
            self._callback()  # signal DeviceManager to re-select
        return comtypes.S_OK

    def OnDeviceAdded(self, pwstrDeviceId):
        self._callback()
        return comtypes.S_OK

    def OnDeviceRemoved(self, pwstrDeviceId):
        self._callback()
        return comtypes.S_OK

    def OnDeviceStateChanged(self, pwstrDeviceId, dwNewState):
        self._callback()
        return comtypes.S_OK

    def OnPropertyValueChanged(self, pwstrDeviceId, key):
        return comtypes.S_OK
```

### Registration

```python
def _register_notification_client(self, callback):
    try:
        enumerator = comtypes.CoCreateInstance(
            MMDeviceEnumerator,
            interface=IMMDeviceEnumerator,
        )
        self._notification_client = _DeviceNotificationClient(callback)
        enumerator.RegisterEndpointNotificationCallback(self._notification_client)
        log.info("DEV → IMMNotificationClient registered")
    except Exception:
        log.exception("[DEV-004] Failed to register IMMNotificationClient — polling fallback active")
        self._use_polling_fallback = True
```

### Polling fallback (DEV-004 case)
If registration fails, poll for device changes every 2 seconds in a daemon thread.
Compare current comm device index against stored — if different, trigger re-selection.

---

## Device change during active recording

When a device change event fires while `CaptureEngine` is recording:
1. Signal recording thread to stop gracefully (set a stop event)
2. Wait for thread to exit (max 2 seconds)
3. Close WAV file properly (write final header)
4. Save current RecordingContext to completed list
5. Re-select best device
6. Open new stream on new device
7. Open new WAV file (segment N+1)
8. Start new recording thread
9. Log DEV-CHG with old and new device names

This happens transparently — `is_recording` stays True throughout.
The WAV file for segment 1 is closed and valid. Segment 2 starts immediately.

---

## pyaudiowpatch vs standard pyaudio

Always use `import pyaudiowpatch as pyaudio` — NOT standard `pyaudio`.

pyaudiowpatch differences:
- WASAPI loopback (for recording system audio — not needed here, but available)
- Pre-compiled PortAudio DLL included — no separate install
- Same API as pyaudio otherwise

Import alias means code reads cleanly and switching back would be trivial.

---

## DeviceManager fail codes

| Code | When |
|------|------|
| DEV-001 | IMMDeviceEnumerator COM init failed |
| DEV-002 | Default comm device query returned nothing |
| DEV-003 | Device enumeration found zero input devices |
| DEV-004 | IMMNotificationClient registration failed (polling fallback) |
| DEV-CHG | Device change event received (always log, even clean switches) |
