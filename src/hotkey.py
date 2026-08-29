"""语润（Yurun）热键模块：Windows RegisterHotKey + 隐藏窗口消息循环。
标准 GUI 热键方案，稳定可靠，无需管理员权限。
- 按下：RegisterHotKey 收到 WM_HOTKEY
- 松开（hold 模式）：GetAsyncKeyState 轮询检测
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time

from logger import get_logger
log = get_logger("yurun.hotkey")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


# 键 → VK 码映射
VK_MAP = {
    "`": 0xC0, "CapsLock": 0x14, "LWin": 0x5B, "RWin": 0x5C,
    "LAlt": 0xA4, "RAlt": 0xA5, "LShift": 0xA0, "RShift": 0xA1,
    "LControl": 0xA2, "RControl": 0xA3, "Space": 0x20, "Tab": 0x09,
    "Esc": 0x1B, "Enter": 0x0D,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}
PUNCT_VK = {
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}


def _vk_for(name: str) -> int:
    name = (name or "").strip()
    if not name:
        return 0
    if name in VK_MAP:
        return VK_MAP[name]
    if len(name) == 1:
        c = name[0]
        if c.isalpha():
            return ord(c.upper())
        if c.isdigit():
            return ord(c)
        if c in PUNCT_VK:
            return PUNCT_VK[c]
    return 0


# ---- Win32 函数签名（防 64 位指针截断）----
user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = wt.LPARAM
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.RegisterHotKey.restype = ctypes.c_bool
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.CreateWindowExW.argtypes = [ctypes.c_uint, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_uint,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
user32.RegisterClassW.restype = ctypes.c_ushort
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE

_WNDPROC = ctypes.WINFUNCTYPE(wt.LPARAM, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)
_CLASS_REGISTERED = False


class HotkeyListener:
    def __init__(self):
        self._hwnd = None
        self._vk = 0
        self._key_name = None
        self._wndproc_obj = None
        self.trigger_mode = "hold"
        self.on_hold_start = None
        self.on_hold_end = None
        self.on_toggle = None
        self.on_error = None
        self._running = False
        self._thread = None
        self._poll_thread = None
        self._pressed = False
        # start() 需要知道系统是否真的接受了 RegisterHotKey，不能只看线程是否已创建。
        self._start_ready = threading.Event()
        self._start_error = None

    def start(self, key_name: str, trigger_mode: str = "hold") -> bool:
        self._vk = _vk_for(key_name)
        if self._vk == 0:
            self._fail("按键无效")
            return False
        self._key_name = key_name
        self.trigger_mode = trigger_mode
        self._pressed = False
        self._start_error = None
        self._start_ready.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # 等待后台线程完成 RegisterHotKey，保存设置时才能真实地反馈“能否使用”。
        if not self._start_ready.wait(timeout=1.5):
            self.stop()
            self._start_error = "启动超时"
            return False
        return self._running and self._start_error is None

    def _fail(self, msg: str):
        self._running = False
        self._start_error = msg
        self._start_ready.set()
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception:
                pass

    def stop(self):
        self._running = False
        try:
            if self._hwnd:
                user32.UnregisterHotKey(self._hwnd, 1)
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=1)
        if self._poll_thread:
            self._poll_thread.join(timeout=1)

    def _run(self):
        global _CLASS_REGISTERED
        if not _CLASS_REGISTERED:
            wc = _WNDCLASSW()
            self._wndproc_obj = _WNDPROC(self._wndproc)
            wc.lpfnWndProc = ctypes.cast(self._wndproc_obj, ctypes.c_void_p)
            wc.hInstance = kernel32.GetModuleHandleW(None)
            wc.lpszClassName = "YurunHotkeyWindow"
            if not user32.RegisterClassW(ctypes.byref(wc)):
                self._fail("启动失败")
                return
            _CLASS_REGISTERED = True

        self._hwnd = user32.CreateWindowExW(
            0, "YurunHotkeyWindow", "Yurun", 0, 0, 0, 0, 0,
            0, 0, kernel32.GetModuleHandleW(None), 0)
        if not self._hwnd:
            self._fail("启动失败")
            return

        if not user32.RegisterHotKey(self._hwnd, 1, MOD_NOREPEAT, self._vk):
            err = ctypes.get_last_error() or 0
            log.warning("热键注册失败（错误码 %s），可能已被其他程序占用", err)
            self._fail("热键被占")  # pill 132 宽装不下长文案，显示 4 字短提示
            return

        self._start_ready.set()

        if self.trigger_mode == "hold":
            self._poll_thread = threading.Thread(target=self._poll, daemon=True)
            self._poll_thread.start()

        msg = wt.MSG()
        while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        # 消息循环退出：清理
        try:
            if self._hwnd:
                user32.UnregisterHotKey(self._hwnd, 1)
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
        except Exception:
            pass

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_HOTKEY and wparam == 1:
            if self.trigger_mode == "toggle":
                self._pressed = not self._pressed
                if self.on_toggle:
                    self.on_toggle(self._key_name, self._pressed)
            else:
                # hold 模式：按下即时触发（上升沿），无需等待阈值。
                # 这样"正在录音"气泡在按下瞬间就出现，用户不会误以为没按成功。
                if not self._pressed:
                    self._pressed = True
                    if self.on_hold_start:
                        self.on_hold_start(self._key_name)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _poll(self):
        """hold 模式：检测热键松开（GetAsyncKeyState）。

        连续 2 次采样都 not-down 才确认松开，过滤偶发误判——
        否则按住途中被误判会提前停录音。"""
        miss = 0
        while self._running:
            if self._pressed:
                try:
                    down = bool(user32.GetAsyncKeyState(self._vk) & 0x8000)
                    if down:
                        miss = 0
                    else:
                        miss += 1
                        if miss >= 2:
                            self._pressed = False
                            miss = 0
                            if self.on_hold_end:
                                self.on_hold_end(self._key_name)
                except Exception:
                    pass
            time.sleep(0.01)


# 全局单例
_hotkey = None
def get_hotkey() -> HotkeyListener:
    global _hotkey
    if _hotkey is None:
        _hotkey = HotkeyListener()
    return _hotkey
