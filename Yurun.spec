# -*- mode: python ; coding: utf-8 -*-


main_a = Analysis(
    ['src\\main.py'],
    pathex=[],
    binaries=[],
    datas=[('prompts', 'prompts'), ('assets', 'assets')],
    hiddenimports=['sounddevice', 'soundfile', 'websocket', 'pyautogui', 'pynput', 'pynput.keyboard', 'pynput.keyboard._win32', 'pystray._win32', 'PIL._tkinter_finder', 'singleinstance', 'typer', 'dictionary', 'voice_session', 'uiautomation', 'uiautomation.uiautomation', 'comtypes', 'privileged_ipc', 'privileged_helper', 'input_helper_setup'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
main_pyz = PYZ(main_a.pure)

main_exe = EXE(
    main_pyz,
    main_a.scripts,
    main_a.binaries,
    main_a.datas,
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

helper_a = Analysis(
    ['src\\privileged_helper.py'],
    pathex=[], binaries=[], datas=[('assets', 'assets')],
    hiddenimports=['pynput', 'pynput.keyboard', 'pynput.keyboard._win32', 'typer', 'privileged_ipc'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
helper_pyz = PYZ(helper_a.pure)

helper_exe = EXE(
    helper_pyz,
    helper_a.scripts,
    helper_a.binaries,
    helper_a.datas,
    [],
    name='YurunInputHelper',
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

setup_a = Analysis(
    ['src\\input_helper_setup.py'],
    pathex=[], binaries=[], datas=[], hiddenimports=[],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
setup_pyz = PYZ(setup_a.pure)

setup_exe = EXE(
    setup_pyz,
    setup_a.scripts,
    setup_a.binaries,
    setup_a.datas,
    [],
    name='YurunHelperSetup',
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
