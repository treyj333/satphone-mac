# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH).parent
app_name = "Tacthrift Notecard Satphone"
esptool_datas, esptool_binaries, esptool_hiddenimports = collect_all("esptool")

a = Analysis(
    [str(project_root / "satphone_app.py")],
    pathex=[str(project_root)],
    binaries=esptool_binaries,
    datas=[
        (str(project_root / "README.md"), "."),
        (str(project_root / "discord-bridge.example.json"), "."),
        (str(project_root / "satphone" / "assets"), "satphone/assets"),
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
    name=app_name,
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
    name=app_name,
)

app = BUNDLE(
    coll,
    name=f"{app_name}.app",
    icon=None,
    bundle_identifier="io.github.satphone-community.satphone",
    version="1.3.0",
    info_plist={
        "CFBundleDisplayName": app_name,
        "CFBundleName": app_name,
        "CFBundleShortVersionString": "1.3.0",
        "CFBundleVersion": "5",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Tacthrift Notecard Satphone community project",
    },
)
