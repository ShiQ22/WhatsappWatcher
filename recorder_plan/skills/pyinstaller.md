# Skill: PyInstaller packaging

## whatsapp_watcher.spec

```python
# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config.json', '.'),
    ],
    hiddenimports=[
        'pyaudiowpatch',
        'lameenc',
        'wave',
        'comtypes.gen',
        'comtypes.gen.UIAutomationClient',
        'comtypes.gen.MMDevAPILib',   # IMMNotificationClient typelib
        'pywinauto',
        'pymysql',
        'sqlalchemy.dialects.mysql',
        'sqlalchemy.dialects.mysql.pymysql',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'PIL'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Collect pyaudiowpatch DLL
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_all
pya_binaries, pya_datas, pya_hiddenimports = collect_all('pyaudiowpatch')
a.binaries += pya_binaries
a.datas += pya_datas

# Collect comtypes (generates typelib cache at import time)
ct_binaries, ct_datas, ct_hiddenimports = collect_all('comtypes')
a.datas += ct_datas
a.hiddenimports += ct_hiddenimports

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='WhatsAppWatcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX can cause false positives in antivirus — skip
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # no console window (background app)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,          # add .ico path here if you have one
)
```

## Build command

```bash
pyinstaller whatsapp_watcher.spec --clean
```

Output: `dist/WhatsAppWatcher.exe` — single file, no installation needed.

## Known issues and fixes

### comtypes typelib generation
comtypes generates `.py` files in a `gen/` folder the first time an app runs.
In a frozen EXE, this path is read-only. Add to recorder.py init:

```python
import comtypes.client
comtypes.client.gen_dir = None  # disable typelib file generation in frozen mode
```

Or better — pre-generate the typelib files before freezing:
```python
# Run this once during development, check the generated files into the repo
comtypes.client.GetModule("MMDevAPI.dll")
comtypes.client.GetModule("UIAutomationCore.dll")
```
Then include `comtypes/gen/` in the spec's `datas`.

### PortAudio DLL
`pyaudiowpatch` ships `portaudio_x64.dll` inside its package.
The `collect_all('pyaudiowpatch')` call above handles this automatically.
Verify after build: `dist/WhatsAppWatcher/_internal/portaudio_x64.dll` should exist.

### Antivirus false positives
PyInstaller EXEs sometimes trigger AV on first run.
`upx=False` reduces false positives. If still triggered, sign the EXE with a code signing cert.

## Verify the build

```bash
# Run the built EXE with a test call scenario
dist\WhatsAppWatcher.exe

# Check it starts, logs, and doesn't crash on startup
# Check logs/watcher.log for startup messages
# Verify: "STARTUP → recorder ready | device=..." appears
```

## Size expectations

| Component | Approximate size |
|-----------|-----------------|
| Python runtime | ~8 MB |
| pyaudiowpatch + PortAudio DLL | ~4 MB |
| comtypes + gen | ~3 MB |
| lameenc | ~2 MB |
| pywinauto | ~5 MB |
| SQLAlchemy + PyMySQL | ~5 MB |
| psutil | ~1 MB |
| **Total EXE** | **~28-35 MB** |

This is reasonable for a background service EXE.
