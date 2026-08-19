"""语润（Yurun）迷你语音气泡：跟随「I 形光标」的窄 pill，复刻 agent 内置语音输入的丝滑体验。

设计目标（对照参考截图）：
- 跟随当前活动输入框的「I 形光标」，浮在它正下方 GAP，且水平中线相对 caret.x 偏移 OFFSET_X（设计 C：飘落感）。
- caret 靠近任务栏/屏底时自动翻到光标上方（兑底），避免被遮。
- 智能跟背景：采样光标上方像素亮度，亮底用浅色 pill+深字，暗底用深色 pill+浅字。
- 录音中显示「正在录音」，润色中显示「正在润色」；完成/兜底后立即隐藏（不显示预览）。
- 五态 + 引导态，状态严格轮转，主线程 after 驱动动画，绝不卡死。
- 任何活动态超时就强制报错退出（心跳兜底）。
- 所有方法必须在 Tk 主线程调用（由 main 的 after 循环驱动 _tick）。
"""
import ctypes
import math
import time
import tkinter as tk
from ctypes import wintypes

from logger import get_logger

_log = get_logger("yurun.pill")

# 尺寸：宽度只装得下"正在润色"4 字 + 圆点，高度加高让 15px 字呼吸
W, H = 132, 50
R = 24                  # 圆角半径（胶囊形）
DOT_R = 6               # 录音红点半径
DOT_X = 18              # 红点圆心 X
SPIN_R = 9              # 转圈半径
SPIN_X = 18             # 转圈圆心 X
TEXT_X = 36             # 文字起点 X（圆点右侧留 18px 间距）
GAP = 10                # 气泡与光标垂直间隙（px）
OFFSET_X = 48           # 设计 C：气泡中线相对 caret.x 的水平偏移（飘落感）
TASKBAR_SAFE = 56       # 屏底预留给任务栏的安全高度（px）

# 主题配色
BG_DARK = "#0a0a0a"
TEXT_DARK = "#F2F2F2"
SPIN_DARK = "#CFCFCF"
BORDER_DARK = BG_DARK   # 深色主题无边（用同色隐藏）
BG_LIGHT = "#f7f7f7"
TEXT_LIGHT = "#222222"
SPIN_LIGHT = "#666666"
BORDER_LIGHT = "#dcdcdc"  # 浅色主题加细边框，白底上才看得见

TRANSPARENT = "#0d0d0d"  # 用于去四角的透明色（避免与主题色冲突）
FONT = ("Microsoft YaHei UI", 15)
FONT_GUIDE = ("Microsoft YaHei UI", 13)
ERROR_FONT_SIZES = (15, 13, 11)  # 错误文本自适应：统一从 15px 起缩，放得下就不截断（无省略号）

_user32 = ctypes.windll.user32
_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.c_void_p]
_user32.GetCaretPos.argtypes = [ctypes.c_void_p]
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.c_void_p]
_user32.GetDC.restype = wintypes.HANDLE
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HANDLE]
_user32.ReleaseDC.restype = ctypes.c_int
_gdi32 = ctypes.windll.gdi32
_gdi32.GetPixel.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_int]
_gdi32.GetPixel.restype = wintypes.COLORREF


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _cursor_screen_rect():
    """鼠标位置兜底：返回 (l, t, r, b)，pill 出现在用户最后点击处。拿不到返回 None。"""
    try:
        pt = wintypes.POINT()
        if _user32.GetCursorPos(ctypes.byref(pt)):
            return (pt.x, pt.y, pt.x + 2, pt.y + 18)
    except Exception:
        pass
    return None


def _caret_screen_rect():
    """返回光标（I 形插入符）的屏幕矩形 (l, t, r, b)。拿不到时返回 None。

    Chromium / Electron 应用的 caret 不一定暴露给 GetGUIThreadInfo，
    三个回退路径按可靠性排序：
      1. GUITHREADINFO.rcCaret —— 屏幕坐标，最准（GetGUIThreadInfo 已写好）
      2. GetCaretPos + ClientToScreen(caret_owner) —— 窗口坐标换算
         （关键修复：必须用 hwndCaret 而不是 hwndFocus，否则 Electron 子窗口的 caret 偏移会乱跑）
         还过滤掉 pt=(0,0) 假阳性（Chromium 偶发）。
      3. 鼠标位置 —— 兜底，pill 出现在用户最后点击处
    """
    try:
        fg = _user32.GetForegroundWindow()
        if not fg:
            _log.debug("caret: no foreground window → 鼠标兜底")
            return _cursor_screen_rect()
        tid = _user32.GetWindowThreadProcessId(fg, None)
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            _log.debug("caret: GetGUIThreadInfo 失败 fg=0x%x tid=%d", fg, tid)
            return _cursor_screen_rect()
        rc = info.rcCaret
        owner = info.hwndCaret or info.hwndFocus
        owner_v = owner if owner else 0
        _log.debug("caret: fg=0x%x tid=%d owner=0x%x focus=0x%x rcCaret=(%d,%d,%d,%d)",
                   fg, tid, owner_v, info.hwndFocus or 0,
                   rc.left, rc.top, rc.right, rc.bottom)
        # 路径 1：rcCaret 屏幕坐标。宽高都 ≥4 才算有效（过滤 1×1 退化态）
        rc_w = rc.right - rc.left
        rc_h = rc.bottom - rc.top
        if rc_w >= 4 and rc_h >= 4:
            return (rc.left, rc.top, rc.right, rc.bottom)
        # 路径 2：GetCaretPos + ClientToScreen 到 owner（不是 focus）
        if owner:
            pt = wintypes.POINT(0, 0)
            if _user32.GetCaretPos(ctypes.byref(pt)):
                # 过滤 Chromium 偶发的 (0,0) 假阳性
                if pt.x > 1 or pt.y > 1:
                    pt_s = wintypes.POINT(pt.x, pt.y)
                    _user32.ClientToScreen(owner, ctypes.byref(pt_s))
                    caret_h = max(rc.bottom - rc.top, 18)
                    return (pt_s.x, pt_s.y, pt_s.x + 2, pt_s.y + caret_h)
                _log.debug("caret: GetCaretPos 返回 (0,0) 假阳性，跳过")
        # 路径 3：兜底鼠标位置
        return _cursor_screen_rect()
    except Exception as e:
        _log.warning("caret 抓取异常: %r", e)
        return None


def _foreground_rect():
    """兜底：返回前台窗口的屏幕矩形。"""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return None
        rc = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rc))
        return (rc.left, rc.top, rc.right, rc.bottom)
    except Exception:
        return None


def _bg_is_light(carert_x, caret_top):
    """采样光标上方若干像素，判断背景是否亮（亮底→浅色 pill）。失败返回 None。"""
    try:
        dc = _user32.GetDC(0)
        if not dc:
            return None
        sample_y = max(0, caret_top - 30)  # 光标上方 30px，避开下方 pill
        rs = gs = bs = 0
        n = 0
        for dx in (-24, 0, 24):
            px = _gdi32.GetPixel(dc, carert_x + dx, sample_y)
            if px == 0xFFFFFFFF or px < 0:  # -1 / 失败
                continue
            rs += px & 0xFF
            gs += (px >> 8) & 0xFF
            bs += (px >> 16) & 0xFF
            n += 1
        _user32.ReleaseDC(0, dc)
        if n == 0:
            return None
        lum = (rs + gs + bs) / (3 * n)
        return lum > 135
    except Exception:
        return None


def _compute_anchor():
    """计算气泡屏幕坐标 (x, y)。

    设计 C：气泡中线 = caret.x + OFFSET_X；竖直浮在光标正下方 GAP。
    caret 贴任务栏/屏底时自动翻到光标上方（兑底）。
    兜底：屏幕底部居中。
    """
    sw = _user32.GetSystemMetrics(0)
    sh = _user32.GetSystemMetrics(1)

    caret = _caret_screen_rect()
    if caret:
        l, t, rr, b = caret
        caret_x = l
        caret_bot = b
        caret_top = t
        px = max(8, min(sw - W - 8, caret_x + OFFSET_X - W // 2))
        if caret_bot + GAP + H <= sh - TASKBAR_SAFE:
            py = caret_bot + GAP
        else:
            py = max(8, caret_top - H - GAP)
        return px, py

    # 无 caret：锚定到焦点窗口（输入框），贴其底部居中，固定不跟鼠标
    fr = _focus_rect() or _foreground_rect()
    if fr:
        l, t, rr, b = fr
        cx = (l + rr) // 2
        px = max(8, min(sw - W - 8, cx - W // 2))
        if b - H - GAP >= 8:
            py = b - H - GAP
        elif t + GAP + H <= sh - TASKBAR_SAFE:
            py = t + GAP
        else:
            py = max(8, (t + b) // 2 - H // 2)
        return px, py

    cur = _cursor_screen_rect()
    if cur:
        cx = (cur[0] + cur[2]) // 2
        px = max(8, min(sw - W - 8, cx - W // 2))
        py = max(8, min(sh - H - 8, cur[3] + GAP))
        return px, py
    return (sw - W) // 2, sh - 90


def _focus_rect():
    """焦点窗口矩形（比跟随鼠标稳）：用于无 caret 时锚定输入框。"""
    try:
        fg = _user32.GetForegroundWindow()
        if not fg:
            return None
        tid = _user32.GetWindowThreadProcessId(fg, None)
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            hwnd = info.hwndFocus or info.hwndActive or fg
        else:
            hwnd = fg
        rc = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rc))
        return (rc.left, rc.top, rc.right, rc.bottom)
    except Exception:
        return None


# 活动态硬超时（秒）：一旦超过立即强制报错退出，杜绝 indicator 卡死
# 注：识别/润色态不设硬超时——后台跑多久都等，不再误报「处理超时」。
# 活动态硬超时：录音不再设假报错（v0.1.18：40s 假报错与 max_seconds=90 矛盾，
# 用户实测"跳录音超时还能正常录"）。录音时长由 main max_seconds=90 自然终止，
# 80s 时气泡提示「还剩10秒」（见 _tick）。识别/润色态本就无硬超时。
STATE_TIMEOUT = {}
AUTO_HIDE = {
    "guide": 3500,
    "error": 2400,
}


def _round_rect_items(c, x, y, w, h, r, fill):
    """拼出圆角矩形，返回创建出的 canvas item id 列表。"""
    ids = []
    ids.append(c.create_rectangle(x + r, y, x + w - r, y + h, fill=fill, outline=""))
    ids.append(c.create_rectangle(x, y + r, x + w, y + h - r, fill=fill, outline=""))
    ids.append(c.create_arc(x, y, x + 2 * r, y + 2 * r, start=90, extent=90, fill=fill, outline=""))
    ids.append(c.create_arc(x + w - 2 * r, y, x + w, y + 2 * r, start=0, extent=90, fill=fill, outline=""))
    ids.append(c.create_arc(x, y + h - 2 * r, x + 2 * r, y + h, start=180, extent=90, fill=fill, outline=""))
    ids.append(c.create_arc(x + w - 2 * r, y + h - 2 * r, x + w, y + h, start=270, extent=90, fill=fill, outline=""))
    return ids


class PillBubble:
    def __init__(self, master: tk.Misc):
        self._state = "idle"
        self._state_entered = 0.0
        self._spin = 0
        self._hide_after = 0
        self._fade = None
        self._dot_pulse = 0.0
        self._warned_80 = False  # 录音 80s「还剩10秒」是否已提示
        self._light = False  # 当前主题（False=深色）

        self.win = tk.Toplevel(master)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-toolwindow", True)
        except Exception:
            pass
        self.win.configure(bg=TRANSPARENT)
        self.win.attributes("-transparentcolor", TRANSPARENT)

        x, y = _compute_anchor()
        self.win.geometry(f"{W}x{H}+{x}+{y}")

        self.canvas = tk.Canvas(self.win, width=W, height=H,
                                bg=TRANSPARENT, highlightthickness=0, bd=0)
        self.canvas.pack()
        # 先画边框（大一圈），再画填充（小一圈），形成细边框效果
        self._bg_border = _round_rect_items(self.canvas, 0, 0, W, H, R, BORDER_DARK)
        self._bg_fill = _round_rect_items(self.canvas, 1, 1, W - 2, H - 2, R - 1, BG_DARK)

        self.dot = self.canvas.create_oval(
            DOT_X - DOT_R, H // 2 - DOT_R, DOT_X + DOT_R, H // 2 + DOT_R,
            fill="#E53935", outline="")
        # 引导态图标：浅蓝点（表示"准备开始"，区别于录音红点）
        self.guide_dot = self.canvas.create_oval(
            DOT_X - DOT_R, H // 2 - DOT_R, DOT_X + DOT_R, H // 2 + DOT_R,
            fill="#007AFF", outline="")
        self.spinner = self.canvas.create_arc(
            SPIN_X - SPIN_R, H // 2 - SPIN_R, SPIN_X + SPIN_R, H // 2 + SPIN_R,
            start=0, extent=270, style="arc",
            outline=SPIN_DARK, width=2.4)
        # 错误态图标：圆底感叹号（canvas 画，不占文本空间，杜绝 ⚠ emoji 渲染挤压文字）
        self.err_bg = self.canvas.create_oval(
            DOT_X - 9, H // 2 - 9, DOT_X + 9, H // 2 + 9,
            fill="#E53935", outline="")
        self.err_text = self.canvas.create_text(
            DOT_X, H // 2, text="!", fill="#FFFFFF",
            font=("Microsoft YaHei UI", 12, "bold"))
        self.text = self.canvas.create_text(
            TEXT_X, H // 2, anchor="w",
            text="", fill=TEXT_DARK, font=FONT)
        self.canvas.itemconfig(self.dot, state="hidden")
        self.canvas.itemconfig(self.spinner, state="hidden")
        self.canvas.itemconfig(self.err_bg, state="hidden")
        self.canvas.itemconfig(self.err_text, state="hidden")
        self.canvas.itemconfig(self.guide_dot, state="hidden")

    # ============ 公共接口（主线程调用） ============
    def _apply_theme(self, light):
        if light == self._light:
            return
        self._light = light
        if light:
            fill, txt, spin, border = BG_LIGHT, TEXT_LIGHT, SPIN_LIGHT, BORDER_LIGHT
        else:
            fill, txt, spin, border = BG_DARK, TEXT_DARK, SPIN_DARK, BORDER_DARK
        for it in self._bg_fill:
            self.canvas.itemconfig(it, fill=fill)
        for it in self._bg_border:
            self.canvas.itemconfig(it, fill=border)
        self.canvas.itemconfig(self.text, fill=txt)
        self.canvas.itemconfig(self.spinner, outline=spin)

    def _enter(self, state):
        self._state = state
        self._state_entered = time.time()
        self._fade = None
        self._hide_after = 0
        self._warned_80 = False
        self._detect_and_apply_theme()
        self._reposition()
        try:
            self.win.attributes("-alpha", 1.0)
        except Exception:
            pass
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)

    def _detect_and_apply_theme(self):
        r = _caret_screen_rect()
        if r:
            light = _bg_is_light(r[0], r[1])
            if light is not None:
                self._apply_theme(light)

    def show_guide(self, text="开始录音"):
        self._enter("guide")
        self._set_icon("guide")
        self.canvas.itemconfig(self.text, text=text, font=FONT)
        self.canvas.coords(self.text, TEXT_X, H // 2)
        self._hide_after = AUTO_HIDE["guide"]

    def start_recording(self):
        """按下热键 / ASR 识别中：均显示「● 正在录音」，保持视觉连贯。"""
        self._enter("recording")
        self._set_icon("dot")
        self.canvas.itemconfig(self.text, text="正在录音", font=FONT)
        self.canvas.coords(self.text, TEXT_X, H // 2)
        self._dot_pulse = 0.0

    def show_refining(self):
        """LLM 润色中：显示「⟳ 正在润色」。"""
        self._enter("refining")
        self._set_icon("spinner")
        self.canvas.itemconfig(self.text, text="正在润色", font=FONT)
        self.canvas.coords(self.text, TEXT_X, H // 2)

    def show_transcribing(self):
        """松手后、ASR 等待期：显示「⟳ 正在识别」。"""
        self._enter("transcribing")
        self._set_icon("spinner")
        self.canvas.itemconfig(self.text, text="正在识别", font=FONT)
        self.canvas.coords(self.text, TEXT_X, H // 2)

    def show_error(self, msg="出错了"):
        self._enter("error")
        self._set_icon("error")
        # 纯文本错误消息 + 圆底感叹号，字体与「正在录音/润色」统一 15px
        # （不再用 "⚠ " 文本前缀：⚠ 在 Windows 渲染成 emoji 会挤压/顶出文字）
        self.canvas.itemconfig(self.text, text=msg, font=FONT)
        self.canvas.coords(self.text, TEXT_X, H // 2)
        self._fit_error_text(msg)
        self._hide_after = AUTO_HIDE["error"]

    def force_idle(self):
        """立即回到隐藏态（任何异常路径的兜底）。"""
        self._state = "idle"
        self._hide_after = 0
        self._fade = None
        try:
            self.win.attributes("-alpha", 1.0)
        except Exception:
            pass
        self.win.withdraw()

    def hide_now(self):
        self.force_idle()

    def set_level(self, level: float):
        """保留兼容接口（电平波形已并入呼吸红点，无需外部设值）。"""
        pass

    # ============ 内部 ============
    def _reposition(self):
        """将气泡定位到当前光标正下方（带 OFFSET_X 偏移），每次进入活动态时调用。"""
        x, y = _compute_anchor()
        try:
            self.win.geometry(f"{W}x{H}+{x}+{y}")
        except Exception:
            pass

    def _set_icon(self, which):
        self.canvas.itemconfig(self.dot, state="hidden" if which != "dot" else "normal")
        self.canvas.itemconfig(self.spinner, state="hidden" if which != "spinner" else "normal")
        show_err = which == "error"
        self.canvas.itemconfig(self.err_bg, state="normal" if show_err else "hidden")
        self.canvas.itemconfig(self.err_text, state="normal" if show_err else "hidden")
        show_gd = which == "guide"
        self.canvas.itemconfig(self.guide_dot, state="normal" if show_gd else "hidden")

    def _fit_error_text(self, msg):
        """错误文本自适应：优先缩小字体到放得下（13→11→10px），不加省略号。

        v0.1.17 用户反馈：长消息被截断成「没听到声音…」带省略号，要求只显示
        简短文本。方案：错误文案本身缩到 ≤6 字 + 这里字体缩小兜底，正常情况
        永不截断；仅 10px 仍超宽（几乎不可能）才截断加省略号。
        """
        if not msg:
            return msg
        for size in ERROR_FONT_SIZES:
            self.canvas.itemconfig(self.text, font=("Microsoft YaHei UI", size))
            try:
                self.win.update_idletasks()
                bb = self.canvas.bbox(self.text)
                w = (bb[2] - bb[0]) if bb else 0
            except Exception:
                w = 0
            if w <= W - TEXT_X - 10:
                return
        # 极端兜底：10px 仍超宽，截断
        self.canvas.itemconfig(self.text, text=msg[:6] + "…",
                               font=("Microsoft YaHei UI", ERROR_FONT_SIZES[-1]))

    def _center_text(self):
        try:
            self.win.update_idletasks()
        except Exception:
            return
        bb = self.canvas.bbox(self.text)
        if bb:
            tw = bb[2] - bb[0]
            self.canvas.coords(self.text, max(TEXT_X, (W - tw) // 2), H // 2)

    def _tick(self):
        """主线程每 40ms：动画 + 超时心跳 + 自动隐藏/淡出。"""
        if self._state == "idle":
            return
        now = time.time()

        if self._state == "recording":
            self._dot_pulse += 0.18
            r = DOT_R + 1.4 * (0.5 + 0.5 * math.sin(self._dot_pulse))
            self.canvas.coords(
                self.dot,
                DOT_X - r, H // 2 - r, DOT_X + r, H // 2 + r,
            )
            # 录音 80s 提示「还剩10秒」（max_seconds=90 自然终止；只换图标/文字不切状态）
            if now - self._state_entered >= 80 and not self._warned_80:
                self._warned_80 = True
                self._set_icon("error")
                self.canvas.itemconfig(self.text, text="还剩10秒", font=FONT)
                self.canvas.coords(self.text, TEXT_X, H // 2)

        if self._state == "refining":
            self._spin = (self._spin + 12) % 360
            self.canvas.itemconfig(self.spinner, start=self._spin)

        # 不做随动重定位：状态窗口（正在录音/识别/润色）停在第一次弹出的位置，
        # 直到本次结束（v0.1.18 用户要求：不要因为鼠标/光标移动跟着跑）。

        if self._hide_after > 0:
            self._hide_after -= 40
            if self._hide_after <= 0:
                self._fade = 10
        if self._fade is not None:
            self._fade -= 1
            try:
                self.win.attributes("-alpha", max(0.0, self._fade / 10))
            except Exception:
                pass
            if self._fade <= 0:
                self._fade = None
                self._state = "idle"
                self.win.withdraw()
                try:
                    self.win.attributes("-alpha", 1.0)
                except Exception:
                    pass
