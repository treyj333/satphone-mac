# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH).parent
esptool_datas, esptool_binaries, esptool_hiddenimports = collect_all("esptool")

a = Analysis(
    [str(project_root / "satphone_app.py")],
    pathex=[str(project_root)],
    binaries=esptool_binaries,
    datas=[
        (str(project_root / "README.md"), "."),
        (str(project_root / "discord-bridge.example.json"), "."),
    ] + esptool_datas,
    hiddenimports=esptool_hiddenimports + [
        "notecard",
        "serial",
        "serial.tools.list_ports",
        "periphery",
        "discord",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SATPHONE",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SATPHONE",
)

app = BUNDLE(
    coll,
    name="SATPHONE.app",
    icon=None,
    bundle_identifier="io.github.satphone-community.satphone",
    version="1.0.0",
    info_plist={
        "CFBundleDisplayName": "SATPHONE",
        "CFBundleName": "SATPHONE",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Community SATPHONE project",
    },
)
