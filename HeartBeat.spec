# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(SPECPATH)  # spec 所在目录（项目根），避免硬编码本机路径
APP_ICON = os.path.join(ROOT, 'assets', 'HeartBeat.icns')
if sys.platform == 'win32':
    APP_ICON = os.path.join(ROOT, 'assets', 'HeartBeat.ico')

datas = [(os.path.join(ROOT, 'plugins'), 'plugins'), (os.path.join(ROOT, 'brain'), 'brain')]
# 离线嵌入模型（可选）：存在才打包；首启由 rag._ensure_bundled_model 注入用户目录
MODELS_DIR = os.path.join(ROOT, 'models', 'fastembed')
if os.path.isdir(MODELS_DIR):
    datas.append((MODELS_DIR, os.path.join('models', 'fastembed')))
binaries = []
hiddenimports = []
tmp_ret = collect_all('fastembed')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('sqlite_vec')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    [os.path.join(ROOT, 'main.py')],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='HeartBeat',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[APP_ICON],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='HeartBeat',
)
app = BUNDLE(
    coll,
    name='HeartBeat.app',
    icon=APP_ICON,
    bundle_identifier=None,
)
