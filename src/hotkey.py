"""语润（Yurun）热键模块。

常规按键使用 Windows RegisterHotKey；单独的反引号（`）在部分中文输入
环境会被目标应用当作中点（·）输入。对此仅使用一个窄范围的低层键盘钩子：
只拦截未带修饰键的反引号按下/松开，不记录、不保存任何键盘内容。
"""
import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time

from logger import get_logger
log = get_logger("yurun.hotkey")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
LLKHF_INJECTED = 0x10
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C


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


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
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
_LOWLEVELPROC = ctypes.WINFUNCTYPE(wt.LPARAM, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _LOWLEVELPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HANDLE
user32.UnhookWindowsHookEx.argtypes = [wt.HANDLE]
user32.UnhookWindowsHookEx.restype = ctypes.c_bool
user32.CallNextHookEx.argtypes = [wt.HANDLE, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = wt.LPARAM
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
        self._keyboard_proc_obj = None
        self._keyboard_hook = None
        self._uses_keyboard_hook = False
        self._suppressed_key_down = False
        self._registered_hotkey = False
        self._press_source = None
        self._event_queue = None
        self._dispatch_thread = None
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
        self._suppressed_key_down = False
        self._press_source = None
        # 仅为裸反引号启用钩子；其他热键继续沿用系统热键注册。
        self._uses_keyboard_hook = self._vk == VK_MAP["`"] and key_name == "`"
        self._registered_hotkey = False
        self._start_error = None
        self._start_ready.clear()
        self._running = True
        if self._uses_keyboard_hook:
            # 低层键盘钩子必须尽快返回；录音启动/停止等较重操作放到这个
            # 串行队列，确保“按下 → 松开”永远按原顺序执行。
            self._event_queue = queue.Queue()
            self._dispatch_thread = threading.Thread(target=self._dispatch_events, daemon=True)
            self._dispatch_thread.start()
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

    @staticmethod
    def _modifier_down() -> bool:
        """是否有修饰键按住。组合键完全交给原有程序处理。"""
        return any(
            bool(user32.GetAsyncKeyState(vk) & 0x8000)
            for vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)
        )

    def _dispatch_events(self):
        """在钩子线程外，按顺序运行录音开始/结束回调。"""
        while self._running:
            try:
                event = self._event_queue.get(timeout=0.15)
            except queue.Empty:
                continue
            try:
                if event == "hold_start" and self.on_hold_start:
                    self.on_hold_start(self._key_name)
                elif event == "hold_end" and self.on_hold_end:
                    self.on_hold_end(self._key_name)
                elif event == "toggle" and self.on_toggle:
                    self.on_toggle(self._key_name, self._pressed)
            except Exception:
                log.exception("热键事件处理异常")

    def _queue_event(self, event: str):
        if self._event_queue is not None:
            self._event_queue.put(event)

    def _keyboard_hook_proc(self, n_code, wparam, lparam):
        """仅吞掉裸反引号，避免中文输入布局把它送进目标编辑器。"""
        try:
            if n_code != HC_ACTION or not self._running:
                return user32.CallNextHookEx(self._keyboard_hook, n_code, wparam, lparam)

            event = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            if event.vkCode != self._vk or event.flags & LLKHF_INJECTED:
                return user32.CallNextHookEx(self._keyboard_hook, n_code, wparam, lparam)

            is_down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_up = wparam in (WM_KEYUP, WM_SYSKEYUP)

            # 已经由本次裸按键接管时，连同其 key-up 一起吞掉。
            if is_up and self._suppressed_key_down:
                self._suppressed_key_down = False
                if (self.trigger_mode == "hold" and self._pressed
                        and self._press_source == "hook"):
                    self._pressed = False
                    self._press_source = None
                    self._queue_event("hold_end")
                return 1

            # Ctrl+` 等组合键不在本次修复范围，保持原有纠错快捷键等行为。
            if self._modifier_down():
                return user32.CallNextHookEx(self._keyboard_hook, n_code, wparam, lparam)

            if is_down:
                # Windows 会为按住的键发送重复 key-down；只能在第一下触发录音。
                if self._suppressed_key_down:
                    return 1
                self._suppressed_key_down = True
                if self.trigger_mode == "toggle":
                    self._pressed = not self._pressed
                    self._press_source = "hook"
                    self._queue_event("toggle")
                elif not self._pressed:
                    self._pressed = True
                    self._press_source = "hook"
                    self._queue_event("hold_start")
                return 1
        except Exception:
            # 钩子回调绝不能把异常带入其他程序；出错时放行按键，优先保证系统稳定。
            log.exception("反引号热键钩子异常，已放行按键")
        return user32.CallNextHookEx(self._keyboard_hook, n_code, wparam, lparam)

    def stop(self):
        self._running = False
        try:
            if self._keyboard_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
                self._keyboard_hook = None
            self._keyboard_proc_obj = None
            if self._hwnd:
                if self._registered_hotkey:
                    user32.UnregisterHotKey(self._hwnd, 1)
                    self._registered_hotkey = False
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=1)
        if self._poll_thread:
            self._poll_thread.join(timeout=1)
        if self._dispatch_thread:
            self._dispatch_thread.join(timeout=1)

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

        # 保留系统热键作为反引号钩子的兜底：某个第三方程序若抢在钩子链前面
        # 截断事件，语润至少仍能开始/结束录音，不会失去主热键。
        registered = user32.RegisterHotKey(self._hwnd, 1, MOD_NOREPEAT, self._vk)
        if registered:
            self._registered_hotkey = True
        elif not self._uses_keyboard_hook:
            err = ctypes.get_last_error() or 0
            log.warning("热键注册失败（错误码 %s），可能已被其他程序占用", err)
            self._fail("热键被占")  # pill 132 宽装不下长文案，显示 4 字短提示
            return

        if self._uses_keyboard_hook:
            self._keyboard_proc_obj = _LOWLEVELPROC(self._keyboard_hook_proc)
            self._keyboard_hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._keyboard_proc_obj, kernel32.GetModuleHandleW(None), 0)
            if not self._keyboard_hook:
                err = ctypes.get_last_error() or 0
                if not self._registered_hotkey:
                    log.warning("反引号热键钩子和系统热键均不可用（错误码 %s）", err)
                    self._fail("热键启动失败")
                    return
                log.warning("反引号热键钩子安装失败（错误码 %s），已回退系统热键", err)
            else:
                log.info("反引号专用拦截已启用（系统热键保留为兜底）")
        elif self._registered_hotkey:
            log.info("系统热键已启用: %s", self._key_name)

        self._start_ready.set()

        if self.trigger_mode == "hold" and self._registered_hotkey:
            self._poll_thread = threading.Thread(target=self._poll, daemon=True)
            self._poll_thread.start()

        msg = wt.MSG()
        while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        # 消息循环退出：清理
        try:
            if self._keyboard_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
                self._keyboard_hook = None
            self._keyboard_proc_obj = None
            if self._hwnd:
                if self._registered_hotkey:
                    user32.UnregisterHotKey(self._hwnd, 1)
                    self._registered_hotkey = False
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
        except Exception:
            pass

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_HOTKEY and wparam == 1:
            if self.trigger_mode == "toggle":
                self._pressed = not self._pressed
                self._press_source = "register" if self._pressed else None
                if self.on_toggle:
                    self.on_toggle(self._key_name, self._pressed)
            else:
                # hold 模式：按下即时触发（上升沿），无需等待阈值。
                # 这样"正在录音"气泡在按下瞬间就出现，用户不会误以为没按成功。
                if not self._pressed:
                    self._pressed = True
                    self._press_source = "register"
                    if self.on_hold_start:
                        self.on_hold_start(self._key_name)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _poll(self):
        """hold 模式：检测热键松开（GetAsyncKeyState）。

        连续 2 次采样都 not-down 才确认松开，过滤偶发误判——
        否则按住途中被误判会提前停录音。"""
        miss = 0
        while self._running:
            # 裸反引号由低层钩子吞掉时，GetAsyncKeyState 会被系统错误地
            # 报为已松开。钩子路径只相信自己的 key-up；轮询仅服务系统热键兜底。
            if self._pressed and self._press_source == "register":
                try:
                    down = bool(user32.GetAsyncKeyState(self._vk) & 0x8000)
                    if down:
                        miss = 0
                    else:
                        miss += 1
                        if miss >= 2:
                            self._pressed = False
                            self._press_source = None
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
