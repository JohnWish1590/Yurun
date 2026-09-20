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
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
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


# 修饰键显示顺序 = 位掩码从"强"到"弱"，与主流软件一致。
MOD_LABELS = (
    (MOD_CONTROL, "Ctrl"),
    (MOD_ALT, "Alt"),
    (MOD_SHIFT, "Shift"),
    (MOD_WIN, "Win"),
)


def vk_to_name(vk: int) -> str:
    """把虚拟键码还原成配置里使用的键名；无法识别返回空串。

    这是 `_vk_for` 的逆运算，供设置界面录制组合键时使用。字母统一返回大写，
    与 `_vk_for` 里 `ord(c.upper())` 的写法对应，保证来回转换稳定。
    """
    if not vk:
        return ""
    for name, code in VK_MAP.items():
        if code == vk:
            return name
    for name, code in PUNCT_VK.items():
        if code == vk:
            return name
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return chr(vk)
    return ""


def format_hotkey(modifiers: int, key_name: str) -> str:
    """生成组合键的可读名，如 ``Ctrl + Shift + K`` / ``Alt + ` `` / ``F8``。"""
    parts = [label for bit, label in MOD_LABELS if modifiers & bit]
    parts.append(key_name or "?")
    return " + ".join(parts)


def hotkey_id(modifiers: int, key_name: str) -> tuple:
    """组合键的唯一标识 (修饰位, VK)，用于查重两个热键是否冲突。

    查重必须基于 VK 而不是键名：``A`` 与 ``a`` 是同一个键，
    ``LAlt`` 与 ``Alt`` 走的是同一位掩码，用 (mods, vk) 比才可靠。
    """
    return (int(modifiers or 0), _vk_for(key_name))


def pynput_vk(key) -> int:
    """从 pynput 的按键对象取 Windows VK 码；取不到返回 0。

    ⚠️ 必须同时探查两处，只读 `.vk` 会**静默丢掉所有特殊键**：

    * 普通字符键 → pynput 给的是 ``KeyCode``，它直接有 ``.vk``。
    * F1-F12、方向键、Home/End/Delete 等 → pynput 给的是 ``Key`` **枚举成员**，
      成员本身**没有** ``.vk``（``getattr(Key.f9, 'vk') is None``），真正的 VK 在
      ``Key.f9.value.vk``（120 = 0x78）。

    只在 KeyCode 上取 vk 的写法表现为"F8 这类键录不进去 / 配成纠错热键后完全没
    反应"，而且不报错 —— 很难自查。
    """
    for candidate in (key, getattr(key, "value", None)):
        if candidate is None:
            continue
        vk = getattr(candidate, "vk", None)
        if isinstance(vk, int) and vk:
            return vk
    return 0


def pynput_mod_bit(key) -> int:
    """把 pynput 的按键对象映射成 MOD_* 位；不是修饰键则返回 0。

    供 pynput 路径（纠错热键回退、设置界面的组合键录制）共用。
    pynput 在 Windows 上把右 Alt 报成 ``alt_gr``，且不同版本暴露的属性名
    不完全一致，所以逐个用 getattr 探测；pynput 本身按需导入，避免变成
    模块级硬依赖。
    """
    try:
        from pynput import keyboard as _kb
    except Exception:
        return 0
    for names, bit in (
        (("ctrl_l", "ctrl_r"), MOD_CONTROL),
        (("alt_l", "alt_r", "alt_gr"), MOD_ALT),
        (("shift", "shift_l", "shift_r"), MOD_SHIFT),
        (("cmd", "cmd_l", "cmd_r"), MOD_WIN),
    ):
        for attr in names:
            candidate = getattr(_kb.Key, attr, None)
            if candidate is not None and key == candidate:
                return bit
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
user32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = ctypes.c_bool
user32.PostQuitMessage.argtypes = [ctypes.c_int]
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
WINDOW_CLASS_NAME = "YurunHotkeyWindow"
# 窗口类在一个进程里只能注册一次，所以 lpfnWndProc 必须是**进程级**函数，
# 绝不能绑到某个 listener 的 bound method 上：否则后来创建的窗口会共用第一个
# listener 的回调，"第二个热键"的 WM_HOTKEY 会被送进第一个 listener。
# 实测（生产顺序：主热键裸反引号先注册类、纠错键 Alt+反引号 后加入）：按下
# Alt+反引号 触发的是**主热键的录音回调**，纠错窗口根本收不到 —— 这正是
# "能录音但唤不出纠错窗口"的真因。这里按 hwnd 找到真正的 owner 再派发。
_CLASS_WNDPROC = None
_LISTENERS = {}
_LISTENERS_LOCK = threading.Lock()

# "请退出消息循环"的自定义消息（WM_APP 之后的第一个值）。
WM_APP_CLOSE = 0x8000 + 1


def _class_wndproc(hwnd, msg, wparam, lparam):
    """所有热键窗口共用的窗口过程：按 hwnd 派发给对应的 listener。

    消息由**拥有该窗口的线程**送进来（也就是各自 listener 的消息循环线程），
    所以取到 owner 后直接调用它是线程安全的。

    窗口过程绝不能让异常逃出去：ctypes 回调里抛异常会让返回值不确定，消息分发
    也就跟着乱掉。所有派发都包一层。
    """
    if msg == WM_HOTKEY and wparam == 1:
        try:
            with _LISTENERS_LOCK:
                target = _LISTENERS.get(hwnd)
            if target is not None:
                target._on_hotkey_pressed()
        except Exception:
            log.exception("热键窗口过程派发异常")
        return 0
    if msg == WM_APP_CLOSE:
        # stop() 从别的线程 Post 进来：窗口属于本线程，销毁必须在**本线程**做
        # （DestroyWindow 跨线程会失败，窗口泄漏的同时热键一直占着，用户改完
        # 快捷键会发现"新组合注册不上、旧组合还在响应"）。这里只置标志并让
        # 消息循环退出，真正的 UnregisterHotKey + DestroyWindow 在 _run 收尾。
        try:
            with _LISTENERS_LOCK:
                target = _LISTENERS.get(hwnd)
            if target is not None:
                target._running = False
            user32.PostQuitMessage(0)
        except Exception:
            log.exception("热键窗口关闭消息处理异常")
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class HotkeyListener:
    def __init__(self):
        self._hwnd = None
        self._vk = 0
        self._key_name = None
        self._modifiers = 0
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

    def start(self, key_name: str, trigger_mode: str = "hold", modifiers: int = 0) -> bool:
        """注册一个全局热键。

        modifiers 为 MOD_* 位掩码（0 = 裸键）。带修饰键时必须走
        RegisterHotKey：低层键盘钩子路径只服务裸反引号（钩子回调里
        `_modifier_down()` 会把带修饰键的按键直接放行）。
        """
        self._vk = _vk_for(key_name)
        if self._vk == 0:
            self._fail("按键无效")
            return False
        self._key_name = key_name
        self._modifiers = modifiers
        self.trigger_mode = trigger_mode
        self._pressed = False
        self._suppressed_key_down = False
        self._press_source = None
        # 仅为裸反引号启用钩子；其他热键（含带修饰键的组合）继续沿用系统热键注册。
        self._uses_keyboard_hook = (
            modifiers == 0 and self._vk == VK_MAP["`"] and key_name == "`")
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

            # 带修饰键的组合（例如纠错热键 左Alt+`）一律放行，交给 pynput 钩子处理。
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
        """停掉热键监听并**真正释放**热键与窗口。

        窗口和消息循环都在 self._thread 上，DestroyWindow 不能跨线程调用
        （会返回 FALSE，窗口泄漏、热键继续响应）。所以先 Post 一条自定义消息
        请消息循环退出，由它自己在收尾里 UnregisterHotKey + DestroyWindow。
        """
        self._running = False
        hwnd = self._hwnd
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_APP_CLOSE, 0, 0)
            except Exception:
                pass
        # 钩子可以跨线程卸下，先卸掉能更快地不再吃按键。
        try:
            if self._keyboard_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
        except Exception:
            pass
        self._keyboard_proc_obj = None

        if self._thread:
            self._thread.join(timeout=1)
        if self._poll_thread:
            self._poll_thread.join(timeout=1)
        if self._dispatch_thread:
            self._dispatch_thread.join(timeout=1)

        # 兜底：消息循环没按预期退出（例如线程早已异常结束）时再做一次清理，
        # 尽可能不让热键被"僵尸窗口"占住。
        if self._hwnd:
            try:
                with _LISTENERS_LOCK:
                    _LISTENERS.pop(self._hwnd, None)
                if self._registered_hotkey:
                    user32.UnregisterHotKey(self._hwnd, 1)
                    self._registered_hotkey = False
                user32.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None
        self._keyboard_hook = None

    def _run(self):
        global _CLASS_REGISTERED, _CLASS_WNDPROC
        if not _CLASS_REGISTERED:
            if _CLASS_WNDPROC is None:
                # 窗口过程必须一直活着，否则 ctypes 回收后指针悬空。
                _CLASS_WNDPROC = _WNDPROC(_class_wndproc)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(_CLASS_WNDPROC, ctypes.c_void_p)
            wc.hInstance = kernel32.GetModuleHandleW(None)
            wc.lpszClassName = WINDOW_CLASS_NAME
            if not user32.RegisterClassW(ctypes.byref(wc)):
                self._fail("启动失败")
                return
            _CLASS_REGISTERED = True

        self._hwnd = user32.CreateWindowExW(
            0, WINDOW_CLASS_NAME, "Yurun", 0, 0, 0, 0, 0,
            0, 0, kernel32.GetModuleHandleW(None), 0)
        if not self._hwnd:
            self._fail("启动失败")
            return
        # 让进程级窗口过程知道这个窗口归谁，WM_HOTKEY 才能派回正确的 listener。
        with _LISTENERS_LOCK:
            _LISTENERS[self._hwnd] = self

        # 保留系统热键作为反引号钩子的兜底：某个第三方程序若抢在钩子链前面
        # 截断事件，语润至少仍能开始/结束录音，不会失去主热键。
        registered = user32.RegisterHotKey(self._hwnd, 1, MOD_NOREPEAT | self._modifiers, self._vk)
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
        # 消息循环退出：清理（必须在本线程做，DestroyWindow 不能跨线程）
        try:
            if self._keyboard_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
                self._keyboard_hook = None
            self._keyboard_proc_obj = None
            if self._hwnd:
                with _LISTENERS_LOCK:
                    _LISTENERS.pop(self._hwnd, None)
                if self._registered_hotkey:
                    user32.UnregisterHotKey(self._hwnd, 1)
                    self._registered_hotkey = False
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
        except Exception:
            pass

    def _on_hotkey_pressed(self):
        """WM_HOTKEY 命中本 listener 的热键（由进程级窗口过程按 hwnd 派发）。

        `_running` 已在 stop() 时置 False：窗口销毁要等消息循环收尾，这段窗口期
        里可能还有排队中的 WM_HOTKEY 进来，必须丢掉，否则表现为"停了还在触发"
        （反复暂停/恢复时尤其明显）。
        """
        if not self._running:
            return
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
