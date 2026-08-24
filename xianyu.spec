# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / 'assets' / 'brand'), 'assets/brand'),
    (str(ROOT / 'static' / 'goofish_js_version_2.js'), 'static'),
    (str(ROOT / 'utils' / 'et_f.js'), 'utils'),
    (str(ROOT / 'utils' / 'gen_tfstk.js'), 'utils'),
]

# 如需打包成完全自包含版本，把对应平台的 node 可执行文件放到 node_bin/ 目录
binaries = []
node_bin = ROOT / 'node_bin'
if node_bin.exists():
    for item in node_bin.rglob('*'):
        if item.is_file():
            binaries.append((str(item), 'node'))

hiddenimports = (
    collect_submodules('websockets')
    + ['pydantic', 'typing_extensions', 'loguru', 'dashboard']
)

a = Analysis(
    ['gui.py'],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe_kwargs = dict(
    name='XianYuApis',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

if os.name == 'nt':
    exe_kwargs['version'] = str(ROOT / 'version_info.txt')
    exe_kwargs['icon'] = str(ROOT / 'assets' / 'brand' / 'fish-app-icon.ico')

if os.name == 'nt':
    # Windows 使用 onedir，避免每次启动都解压，显著加快打开速度
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        **exe_kwargs,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        name='XianYuApis',
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        **exe_kwargs,
    )
