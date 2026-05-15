# -*- mode: python ; coding: utf-8 -*-
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(SPEC)))
from version import APP_VERSION
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# customtkinter — collect_all이 assets를 못 잡는 경우 대비해 디렉터리 통째로 복사
import customtkinter as _ctk
datas += [(os.path.dirname(_ctk.__file__), 'customtkinter')]

# cv2
_d, _b, _h = collect_all('cv2')
datas += _d
binaries += _b
hiddenimports += _h

# certifi — SSL 인증서 번들 (다른 PC에서 HTTPS 실패 방지)
from PyInstaller.utils.hooks import collect_data_files
datas += collect_data_files('certifi')

# Cruyff Sans Condensed Medium — 숫자/콤마 reference matching 폰트
datas += [('CruyffSansCondensed-Medium.ttf', '.')]


a = Analysis(
    ['gui_main.py'],
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
    a.binaries,
    a.datas,
    [],
    name=f'ImageReactor_v{APP_VERSION}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['resources\\icon\\icon.ico'],
    uac_admin=True,
)
