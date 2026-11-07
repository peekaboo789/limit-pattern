# -*- mode: python ; coding: utf-8 -*-
"""LimitPattern onedir 绿色版。入口是 desktop/launcher.py。"""

import sys

sys.path.insert(0, SPECPATH)

from pyi_bundle import analysis_kwargs, icon_path

a = Analysis(**analysis_kwargs())
pyz = PYZ(a.pure)

exe_kwargs = dict(
    exclude_binaries=True,
    name="LimitPattern",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
icon = icon_path()
if icon:
    exe_kwargs["icon"] = icon

exe = EXE(pyz, a.scripts, [], **exe_kwargs)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LimitPattern",
)
