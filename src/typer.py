"""SendInput Unicode 逐字输入：把文本直接送进当前焦点窗口，全程不碰剪贴板。

用于替代「写剪贴板 + Ctrl+V」的粘贴路径，避免污染 Windows 剪贴板历史（Win+V）。
原理：用 user32.SendInput 发 KEYEVENTF_UNICODE 事件，逐字符投递 Unicode 码点，
不经过虚拟键码、不经过 IME，中英文都能准确输入。
"""
import ctypes
import ctypes.wintypes as wintypes
import time

from logger import get_logger
log = get_logger("yurun.typer")

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_C = 0x43
VK_MENU = 0x12
KEYEVENTF_EXTENDEDKEY = 0x0001


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD),
    ]


class INPUT(ctypes.Structure):
    class _INPUT(ctypes.Union):
        _fields_ = [
            ("ki", KEYBDINPUT),
            ("mi", MOUSEINPUT),
            ("hi", HARDWAREINPUT),
        ]
    _anonymous_ = ("_input",)
    _fields_ = [("type", wintypes.DWORD), ("_input", _INPUT)]


_user32 = ctypes.windll.user32
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), wintypes.INT]
_user32.SendInput.restype = wintypes.UINT


def _make_unicode_input(code: int, up: bool) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = 0
    inp.ki.wScan = code
    inp.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    inp.ki.time = 0
    inp.ki.dwExtraInfo = None
    return inp


def _make_key_input(vk: int, up: bool) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.wScan = 0
    inp.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    inp.ki.time = 0
    inp.ki.dwExtraInfo = None
    return inp


def _character_inputs(ch: str) -> list[INPUT]:
    """Build key events for one Python character as UTF-16 code units."""
    if ch in ("\n", "\r"):
        return [_make_key_input(VK_RETURN, False), _make_key_input(VK_RETURN, True)]
    encoded = ch.encode("utf-16-le", "surrogatepass")
    units = [int.from_bytes(encoded[i:i + 2], "little")
             for i in range(0, len(encoded), 2)]
    events = []
    for unit in units:
        events.extend((_make_unicode_input(unit, False),
                       _make_unicode_input(unit, True)))
    return events


def input_event_count(text: str) -> int:
    """Return the number of SendInput events required for text."""
    return sum(len(_character_inputs(ch)) for ch in (text or ""))


def type_text(text: str, chunk_pause: float = 0.0, char_interval: float = 0.0, on_each=None) -> int:
    """把 text 逐字符 Unicode 输入到当前焦点窗口，不碰剪贴板。

    - 换行符 \\n / \\r 用 VK_RETURN 回车键发送（多数输入框才能正确换行）。
    - 其余字符用 KEYEVENTF_UNICODE 发 Unicode 码点。
    - chunk_pause：每批之间的停顿（秒），0 = 一次性发完。
    - on_each：每成功投递一个字后回调（用于浮窗"逐字吸走"动画与打字严格同步）。
    返回 SendInput 实际投递的事件数。
    """
    if not text:
        return 0
    if char_interval > 0:
        # 逐字发送，模拟人打字节奏（流式首字上屏用，避免整段瞬间蹦出）
        total = 0
        for ch in text:
            events = _character_inputs(ch)
            arr = (INPUT * len(events))(*events)
            sent = _user32.SendInput(len(events), ctypes.cast(arr, ctypes.POINTER(INPUT)), ctypes.sizeof(INPUT))
            total += sent
            if on_each:
                try:
                    on_each(ch)
                except Exception:
                    pass
            time.sleep(char_interval)
        return total
    inputs = []
    for ch in text:
        inputs.extend(_character_inputs(ch))

    # 一次性批量发送（SendInput 单次可处理大量事件，200 字 < 50ms）
    # 关键：用 cast(arr, POINTER(INPUT)) 转成 LP_INPUT；直接 byref 或传数组名会因类型不匹配抛
    # TypeError（v0.1.9 首发踩过这个坑：每次 SendInput 失败回退剪贴板）。
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = _user32.SendInput(n, ctypes.cast(arr, ctypes.POINTER(INPUT)), ctypes.sizeof(INPUT))
    if sent != n:
        log.warning("SendInput 仅投递 %d/%d 个事件", sent, n)
    return sent


def send_ctrl_c() -> int:
    """发送一次 Ctrl+C，把目标窗口的选中文字送入剪贴板。

    由**提权助手**调用：非提权进程的 SendInput 会被 UIPI 拦下，因此只有提权
    进程才能把提权程序（Cindy 等）的选区复制出来。返回实际投递的事件数，
    4 表示完整发出（Ctrl↓ C↓ C↑ Ctrl↑）。
    """
    seq = [
        _make_key_input(VK_CONTROL, False),
        _make_key_input(VK_C, False),
        _make_key_input(VK_C, True),
        _make_key_input(VK_CONTROL, True),
    ]
    arr = (INPUT * len(seq))(*seq)
    sent = _user32.SendInput(len(seq), ctypes.cast(arr, ctypes.POINTER(INPUT)),
                            ctypes.sizeof(INPUT))
    if sent != len(seq):
        log.warning("SendInput Ctrl+C 仅投递 %d/%d 个事件", sent, len(seq))
    return sent
