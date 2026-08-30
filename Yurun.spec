# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['src\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('prompts', 'prompts'), ('assets', 'assets')],
    hiddenimports=['sounddevice', 'soundfile', 'websocket', 'pyautogui', 'pynput', 'pynput.keyboard', 'pynput.keyboard._win32', 'pystray._win32', 'PIL._tkinter_finder', 'singleinstance', 'typer', 'dictionary', 'voice_session', 'uiautomation', 'uiautomation.uiautomation', 'comtypes'],
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
    name='语润',
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
    icon=['assets\\icon.ico'],
)
