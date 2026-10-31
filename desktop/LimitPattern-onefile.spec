# -*- mode: python ; coding: utf-8 -*-
"""LimitPattern 单文件版。失败时构建脚本仍保留 onedir 绿色版。"""

import sys

sys.path.insert(0, SPECPATH)

from pyi_bundle import analysis_kwargs, icon_path

a = Analysis(**analysis_kwargs())
pyz = PYZ(a.pure)

exe_kwargs = dict(
    name="LimitPattern-onefile",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
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

exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **exe_kwargs)
