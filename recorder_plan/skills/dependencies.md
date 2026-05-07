# Skill: Dependencies

## requirements.txt — final state

```
# Core (unchanged)
psutil
comtypes
pywinauto
SQLAlchemy
PyMySQL
pyinstaller

# Audio recorder (new)
pyaudiowpatch
lameenc

# Testing (new)
pytest
pytest-cov
```

## pyaudiowpatch
- Drop-in replacement for pyaudio with WASAPI support
- Bundles its own portaudio DLL — no separate PortAudio install
- Import as: `import pyaudiowpatch as pyaudio`
- PyInstaller: add `--collect-binaries pyaudiowpatch` in spec

## lameenc
- Pure Python LAME MP3 encoder — no ffmpeg, no DLL
- Works in PyInstaller with zero special handling
- Import: `import lameenc`
- Only needed if `recorder.format` is `"mp3"` or `"both"`
- If import fails at runtime → log warning, fall back to WAV only

```python
try:
    import lameenc
    _LAMEENC_AVAILABLE = True
except ImportError:
    _LAMEENC_AVAILABLE = False
    log.warning("lameenc not available — MP3 output disabled, using WAV")
```

## comtypes (already present)
Used for IMMNotificationClient. The project already uses comtypes for UIA in detector.py.
No changes to import pattern — reuse the same `comtypes.client.GetModule()` pattern.

## What to REMOVE from requirements.txt
Nothing. Bandicam is external software, not a Python package.
The old requirements.txt has no Bandicam-specific Python dependency to remove.

## PyInstaller spec additions
See skills/pyinstaller.md for the full spec file.
Key additions vs current spec (if one exists):
```python
# In Analysis():
hiddenimports=['pyaudiowpatch', 'lameenc', 'wave', 'comtypes.gen'],
collect_binaries=['pyaudiowpatch'],
collect_all=['comtypes'],
```
