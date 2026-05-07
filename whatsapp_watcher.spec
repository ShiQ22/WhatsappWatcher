# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas_extra = []
binaries_extra = []
hiddenimports_extra = []

for pkg in ("pyaudiowpatch", "lameenc", "comtypes"):
    d, b, h = collect_all(pkg)
    datas_extra    += d
    binaries_extra += b
    hiddenimports_extra += h

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=binaries_extra,
    datas=datas_extra + [("config.json", ".")],
    hiddenimports=hiddenimports_extra,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WhatsAppWatcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
