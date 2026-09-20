"""语润（Yurun）设置窗口。

目标：在 Windows tkinter 上稳定还原《语润设置-Apple风格.html》的视觉，
同时避免 Canvas+Frame 混合带来的尺寸/截字/层级问题。

设计取舍：
- 卡片/窗口背景：用 tk.Frame，白色矩形卡片放在 parchment 背景上（macOS 现代设置也大量使用直角卡片）。
- 按钮/分段/交通灯：仍用 Canvas 画圆角胶囊，但尺寸计算更保守。
- 字体：显式创建 tkfont.Font，优先 Inter，回退微软雅黑/雅黑/PingFang/SimHei，禁止回退到衬线。
- 布局：全部 pack/grid，禁止 place，避免绝对定位导致的重叠。
- 字号：按 Windows 可读基准整体放大（正文 16px，应用大标题 40px，卡片标题 20px 等）。
"""
import os
import queue
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

from config import get_config
from hotkey import (format_hotkey, hotkey_id, pynput_mod_bit, pynput_vk, vk_to_name, _vk_for)
from logger import get_logger

log = get_logger("yurun.gui")

# ---- 颜色令牌（与参考 HTML 保持一致）----
BG = "#F5F5F7"              # parchment 窗口背景
CARD = "#FFFFFF"            # canvas 卡片 / 输入框背景
CARD_BORDER = "#E8E8E8"     # 卡片细边框（tkinter 无法圆角，用浅边框区分）
TEXT = "#1D1D1F"            # ink 主文字
TEXT_DIM = "#7A7A7A"        # ink-muted 说明/副标题
TEXT_LABEL = "#333333"      # ink-muted-80 输入框标签
ACCENT = "#0066CC"          # primary 主按钮 / 链接
ACCENT_HOVER = "#0071E3"    # primary-focus
SURFACE_PEARL = "#FAFAFC"   # 分段控件未选中底
HAIRLINE = "#E0E0E0"        # 输入框/取消按钮描边

# ---- 字号（px，按 Windows 可读基准整体放大）----
F_TITLE_BAR = 15
F_APP_TITLE = 42
F_SUBTITLE = 17
F_CARD_TITLE = 20
F_LABEL = 15
F_INPUT = 16
F_DESC = 14
F_LINK = 15
F_ROW = 17
F_SEG = 15
F_HOTKEY = 22
F_BTN = 16

# ---- 间距 ----
WIN_W = 780                 # 设置窗口宽度。**别再动它**：曾为了把两个热键并排而
                            # 加宽到 1000px，观感很差被否掉。两个热键共用一个录制
                            # 控件，靠标题边上的选择框切换，780px 完全够用。
PAD_X = 40                  # 内容区左右内边距
PAD_TOP = 12
PAD_BOTTOM = 28
CARD_PAD = 28
# CARD_GAP / FIELD_GAP 从 22 / 18 收到 16 / 14：纯粹为了把内容高度压到屏幕能装下
# （见 _build 顶部注释）。只动两处间距各 4~6px，卡片内部留白、字号、窗口宽度都没动。
CARD_GAP = 16
FIELD_GAP = 14
BTN_GAP = 14

# ---- 字体解析（确保无衬线）----
def _resolve_font_family():
    """按优先级选一个存在的无衬线字体。"""
    families = set(tkfont.families())
    candidates = [
        "Inter",
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "PingFang SC",
        "SimHei",
        "Segoe UI",
        "Arial",
    ]
    for c in candidates:
        if c in families:
            return c
    # 兜底：返回 TkDefaultFont 的 family，通常是无衬线
    return tkfont.nametofont("TkDefaultFont").cget("family")


_FONT_FAMILY = None
_FONT_CACHE = {}


def FONT(size, weight="normal"):
    """返回缓存的 tkfont.Font 对象。"""
    global _FONT_FAMILY
    if _FONT_FAMILY is None:
        _FONT_FAMILY = _resolve_font_family()
    key = (size, weight, _FONT_FAMILY)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = tkfont.Font(family=_FONT_FAMILY, size=size, weight=weight)
    return _FONT_CACHE[key]


# ---- 窗口图标 ----
def app_icon_path():
    """语润图标（.ico）路径。打包后 __file__ 在 _MEIPASS 外的源码目录里同样可用。"""
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "assets", "icon.ico"))


def apply_app_icon(window):
    """给窗口设置语润图标。

    ⚠️ 子窗口（Toplevel）**不会**继承父窗口的图标 —— 每个有标题栏的窗口都必须
    自己设一次，否则标题栏和任务栏显示 Tk 默认图标（用户报过"管理词库最上面的
    图标没有换"）。所有新的 Toplevel 都请调用这个函数，不要各自抄那段 try/except。

    失败不抛：缺图标文件或平台不支持时，仅退回系统默认图标，不该影响窗口打开。
    """
    try:
        ico = app_icon_path()
        if os.path.exists(ico):
            window.iconbitmap(ico)
            return True
        log.warning("窗口图标不存在，退回默认图标: %s", ico)
    except Exception as exc:
        log.debug("设置窗口图标失败: %s", exc)
    return False


# ---- 通用绘制：圆角矩形 ----
def _rrect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


# ---- 胶囊按钮（Canvas 实现，尺寸保守）----
class PillButton(tk.Canvas):
    def __init__(self, parent, text, command, primary=True, height=42, min_w=110, weight="bold"):
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, bd=0,
                         height=height, cursor="hand2")
        self.text = text
        self.command = command
        self.primary = primary
        self.weight = weight
        self.height = height
        f = FONT(F_BTN, weight)
        self.text_w = f.measure(text)
        self.width = max(min_w, self.text_w + 40)
        self.configure(width=self.width)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: command())
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw())
        self._draw()

    def _draw(self, hover=False):
        self.delete("all")
        w = self.winfo_width() or self.width
        h = self.winfo_height() or self.height
        r = h // 2
        fill = ACCENT_HOVER if (hover and self.primary) else (ACCENT if self.primary else CARD)
        outline = "" if self.primary else HAIRLINE
        fg = "#FFFFFF" if self.primary else TEXT
        wt = self.weight
        _rrect(self, 1, 1, w - 1, h - 1, r, fill=fill, outline=outline)
        self.create_text(w // 2, h // 2, text=self.text, font=FONT(F_BTN, wt), fill=fg)


# ---- 分段控件（Canvas 实现，自动测量）----
class Segmented(tk.Canvas):
    def __init__(self, parent, options, command=None, value=None, height=36):
        self.options = [(str(v), lb) for v, lb in options]
        self.command = command
        self.value = value if value is not None else self.options[0][0]
        self.height = height
        self.pad_x = 14
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, bd=0,
                         height=height, cursor="hand2")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: None)
        self._measure()
        self._draw()

    def _measure(self):
        f = FONT(F_SEG)
        fb = FONT(F_SEG, "bold")
        # 用 bold 宽度作为上限，确保切到 bold 时不溢出
        self._seg_ws = [max(fb.measure(lb), f.measure(lb)) + self.pad_x * 2 for _, lb in self.options]
        self._total = sum(self._seg_ws) + 8  # 左右留 4px 呼吸空间
        self.configure(width=self._total)

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or self._total
        h = self.winfo_height() or self.height
        r = h // 2
        _rrect(self, 1, 1, w - 1, h - 1, r, fill=SURFACE_PEARL, outline="")
        x = 4
        for val, lb in self.options:
            sw = self._seg_ws[self.options.index((val, lb))]
            if val == self.value:
                _rrect(self, x + 2, 4, x + sw - 2, h - 4, r - 4,
                       fill=CARD, outline=HAIRLINE)
                self.create_text(x + sw // 2, h // 2, text=lb,
                                 font=FONT(F_SEG, "bold"), fill=TEXT)
            else:
                self.create_text(x + sw // 2, h // 2, text=lb,
                                 font=FONT(F_SEG), fill=TEXT_DIM)
            x += sw

    def _click(self, event):
        x = event.x - 4
        if x < 0:
            return
        cx = 0
        for i, (val, _) in enumerate(self.options):
            sw = self._seg_ws[i]
            if cx <= x < cx + sw:
                if val != self.value:
                    self.value = val
                    self._draw()
                    if self.command:
                        self.command(val)
                return
            cx += sw

    def set(self, value):
        s = str(value)
        if s != self.value and any(v == s for v, _ in self.options):
            self.value = s
            self._draw()


# ---- 组合键录制控件 ----
class HotkeyRecorder(tk.Frame):
    """「按什么就录什么」的组合键录制控件。

    设计要点（均为实测结论，别照直觉改）：

    * 用**临时 pynput 监听器**抓键，而不是 Tk 的 `<KeyPress>` 绑定。实测把
      Alt+F8 注入 Tk 窗口时 Tk 一个 KeyPress 都没收到（Alt 组合在 Windows 上
      走 WM_SYSKEYDOWN，Tk 不一定转成 KeyPress）；而 pynput 能稳定拿到
      VK 和修饰键对象，且已是本项目的运行依赖。
    * 取 VK 一律走 `pynput_vk()`：pynput 对特殊键（F1-F12、方向键…）返回的是
      没有 `.vk` 的 `Key` 枚举成员，直接读 `key.vk` 会静默丢掉这些键。
    * **不依赖 Tk 的 `event.state` 位掩码**：实测它不可靠（没按 Alt 也会置
      0x8）。改为自己跟踪修饰键的按下/释放。
    * pynput 回调跑在后台线程，**绝不直接碰 Tk**（Tkinter 非线程安全），只把
      事件推进队列，由 Tk 侧 `after` 轮询消费。
    * 录制期间通过 `on_capture(True/False)` 通知外部**暂停正在生效的热键**，
      否则用户按下当前组合时会真的触发录音或弹出纠错窗口。
    * `Esc` 固定用于取消录制，因此 Esc 本身不能配成热键。
    """

    def __init__(self, parent, *, on_capture=None):
        super().__init__(parent, bg=SURFACE_PEARL, highlightbackground=HAIRLINE,
                         highlightthickness=1, bd=0)
        self._key_name = ""
        self._modifiers = 0
        self._active = False
        self._held = 0
        self._listener = None
        self._poll_id = None
        self._queue = queue.Queue()
        self._on_capture = on_capture
        self._hint = ""

        inner = tk.Frame(self, bg=SURFACE_PEARL, padx=18, pady=14)
        inner.pack(fill="x")
        self._value = tk.Label(inner, text="—", bg=SURFACE_PEARL, fg=TEXT,
                               font=FONT(F_HOTKEY, "bold"), anchor="w")
        self._value.pack(side="left")
        self._button = tk.Label(inner, text="录制", bg=CARD, fg=ACCENT, cursor="hand2",
                                font=FONT(F_DESC, "bold"), padx=18, pady=9,
                                highlightbackground=HAIRLINE, highlightthickness=1)
        self._button.pack(side="right")
        self._button.bind("<Button-1>", lambda _e: self.toggle())

    # ---------- 对外接口 ----------
    def set_value(self, key_name, modifiers):
        self._key_name = key_name or ""
        self._modifiers = int(modifiers or 0)
        self._render()

    def value(self):
        """返回 (键名, 修饰位)。键名为空表示还没录到有效组合。"""
        return self._key_name, self._modifiers

    def display(self):
        if not self._key_name:
            return "—"
        return format_hotkey(self._modifiers, self._key_name)

    def toggle(self):
        self.stop_capture() if self._active else self.start_capture()

    def start_capture(self):
        if self._active:
            return
        try:
            from pynput import keyboard as _kb
        except Exception as exc:
            log.warning("无法录制按键（pynput 不可用）: %s", exc)
            self._hint = "无法录制按键"
            self._active = True
            self._render()
            self.after(1200, self.stop_capture)
            return

        self._drain_queue()
        self._active = True
        self._held = 0
        self._hint = "请按下组合键…"
        try:
            self._listener = _kb.Listener(on_press=self._queue_press,
                                          on_release=self._queue_release)
            self._listener.daemon = True
            self._listener.start()
        except Exception as exc:
            log.warning("按键录制监听启动失败: %s", exc)
            self._listener = None
            self._active = False
            self._hint = "无法录制按键"
            self._render()
            return

        self._notify_capture(True)
        self._render()
        self._poll()

    def stop_capture(self):
        """结束录制（含用户点「取消」和外部强制停止）。"""
        if not self._active:
            return
        self._active = False
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        self._held = 0
        self._hint = ""
        self._notify_capture(False)
        self._render()

    # ---------- 内部 ----------
    def _notify_capture(self, active):
        if not self._on_capture:
            return
        try:
            self._on_capture(active)
        except Exception as exc:
            log.warning("切换热键暂停状态失败: %s", exc)

    def _queue_press(self, key):
        try:
            self._queue.put(("press", key))
        except Exception:
            pass

    def _queue_release(self, key):
        try:
            self._queue.put(("release", key))
        except Exception:
            pass

    def _poll(self):
        if not self._active:
            return
        self._drain_queue()
        if self._active:
            self._poll_id = self.after(40, self._poll)

    def _drain_queue(self):
        while True:
            try:
                kind, key = self._queue.get_nowait()
            except queue.Empty:
                return
            self._handle(kind, key)
            if not self._active:
                return

    def _handle(self, kind, key):
        """在 Tk 主线程里处理一个按键事件。"""
        bit = pynput_mod_bit(key)
        if bit:
            self._held = (self._held | bit) if kind == "press" else (self._held & ~bit)
            self._render()
            return
        if kind != "press":
            return
        vk = pynput_vk(key)
        if vk == 0x1B:                      # Esc：取消录制
            self.stop_capture()
            return
        name = vk_to_name(vk)
        if not name:
            self._hint = "这个键不支持，请换一个"
            self._render()
            return
        self._key_name = name
        self._modifiers = self._held
        self.stop_capture()

    def _render(self):
        if self._active:
            hint = self._hint or "请按下组合键…"
            if self._held:
                mods = format_hotkey(self._held, "").rstrip(" +")
                hint = "%s（已按住 %s）" % (hint, mods)
            self._value.config(text=hint, fg=ACCENT)
            self._button.config(text="取消", fg=TEXT_DIM)
            return
        self._value.config(text=self.display(), fg=TEXT)
        self._button.config(text="录制", fg=ACCENT)


# ---- 设置窗口 ----
class SettingsWindow:
    def __init__(self, master: tk.Misc = None):
        self.cfg = get_config()
        self.master = master
        if master is None:
            self.root = tk.Tk()
        else:
            self.root = tk.Toplevel(master)
        self.root.title("语润 · 设置")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        apply_app_icon(self.root)
        self.root.withdraw()

        self._var = {}
        self._init_vars()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.update_idletasks()
        if not self._restore_window_position():
            self._center_window()
        self.root.deiconify()

    def _init_vars(self):
        c = self.cfg
        v = self._var
        v["sauc_key"] = tk.StringVar(value=c.get("asr_sauc_key") or "")
        v["sauc_resource"] = tk.StringVar(
            value=c.get("asr_sauc_resource_id") or "volc.seedasr.sauc.duration")
        v["sauc_endpoint"] = tk.StringVar(
            value=c.get("asr_sauc_endpoint") or
            "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream")
        v["refine_key"] = tk.StringVar(value=c.get("api_key") or "")
        v["refine_base"] = tk.StringVar(value=c.get("api_base") or "https://ark.cn-beijing.volces.com/api/v3")
        v["refine_model"] = tk.StringVar(value=c.get("api_model") or "")
        v["trigger"] = tk.StringVar(value=c.get("trigger_mode") or "hold")

    # ================= 构建 =================
    def _build(self):
        # ⚠️ body 必须放进 Canvas 滚动，**不能**直接 pack 进 root。
        # _compute_size() 把窗口高度截到 min(内容高度, 屏幕高度-60)：本机屏幕
        # 2560x1440（可用高 1392），窗口上限 1380。内容一旦超过上限，直接 pack
        # 就会把**底部**永久裁掉 —— 这正是"纠错热键不见了、连底部的「保存并应用」
        # 都点不到"的原因（正式版 v1.3.4 在本机就是这样，被裁掉的部分触达不了）。
        # 窗口仍不可缩放，靠滚动触达。
        #
        # 但滚动条本身是观感减分项，所以目标定成：**内容压到 ≤ 1340px**
        # （1340 + PAD_TOP 12 + PAD_BOTTOM 28 = 1380），此时画布内容与视口等高、
        # 窗口自己缩到刚好装下，滚动条根本不出现。压缩只允许两种手段：删/合并
        # 说明文字、微调卡片间距（CARD_GAP / FIELD_GAP）。字号、窗口宽度不许动。
        #
        # 2026-09-20 实测：说明文字精简 + CARD_GAP/FIELD_GAP 由 22/18 → 16/14 后，
        # 内容 1328px → 窗口 1368x780，滚动条不再出现（比正式版的 1556px 还矮）。
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True)
        self._scroll = tk.Canvas(shell, bg=BG, highlightthickness=0, bd=0)
        self._scrollbar = tk.Scrollbar(shell, orient="vertical", command=self._scroll.yview)
        self._scrollbar_visible = False
        self._scroll.configure(yscrollcommand=self._on_scroll_set)
        self._scroll.pack(side="left", fill="both", expand=True)

        self._scroll_host = tk.Frame(self._scroll, bg=BG)
        self._scroll_window = self._scroll.create_window(
            (0, 0), window=self._scroll_host, anchor="nw")
        self._scroll_host.bind("<Configure>", self._on_scroll_host_configure)
        self._scroll.bind("<Configure>", self._on_scroll_canvas_configure)
        self.root.bind_all("<MouseWheel>", self._on_settings_wheel, add="+")

        self.body = tk.Frame(self._scroll_host, bg=BG)
        self.body.pack(fill="both", expand=True, padx=PAD_X, pady=(PAD_TOP, PAD_BOTTOM))

        # Header
        tk.Label(self.body, text="语润", bg=BG, fg=TEXT,
                 font=FONT(F_APP_TITLE, "bold")).pack(anchor="w")
        tk.Label(self.body, text="按下即听，松手即现",
                 bg=BG, fg=TEXT_DIM, font=FONT(F_SUBTITLE)).pack(anchor="w", pady=(8, 24))

        # 卡片一：语音引擎
        self._card_asr = self._make_card(self.body)
        self._build_asr(self._card_asr)

        # 卡片二：润色 API
        self._card_refine = self._make_card(self.body)
        self._build_refine(self._card_refine)

        # 卡片三：快捷键与行为
        self._card_hotkey = self._make_card(self.body)
        self._build_hotkey(self._card_hotkey)

        # 底部按钮：取消左 / 保存右（右对齐）
        btns = tk.Frame(self.body, bg=BG)
        btns.pack(fill="x", pady=(4, 0))
        # 先放右边的 spacer，把按钮推到右侧
        tk.Frame(btns, bg=BG).pack(side="left", expand=True, fill="x")
        PillButton(btns, "取消", self._on_close, primary=False, weight="normal").pack(side="right", padx=(BTN_GAP, 0))
        PillButton(btns, "保存并应用", self._save, min_w=150).pack(side="right")

    def _make_card(self, parent):
        """白色卡片：Frame + 浅灰细边框（tkinter 无圆角 Frame，用直角矩形同样干净）。"""
        card = tk.Frame(parent, bg=CARD, highlightbackground=CARD_BORDER,
                        highlightthickness=1, bd=0)
        card.pack(fill="x", pady=(0, CARD_GAP))
        inner = tk.Frame(card, bg=CARD, padx=CARD_PAD, pady=CARD_PAD)
        inner.pack(fill="both", expand=True)
        return inner

    # ---------- 滚动（设置项总高度会超过屏幕）----------
    def _on_scroll_host_configure(self, _event=None):
        self._scroll.configure(scrollregion=self._scroll.bbox("all"))

    def _on_scroll_canvas_configure(self, event):
        # 内容宽度跟随画布，否则靠右的元素会跑到可视区外。
        self._scroll.itemconfigure(self._scroll_window, width=event.width)

    def _on_scroll_set(self, first, last):
        """按需显示滚动条：内容放得下就不占宽度。

        这里不会出现"显示滚动条 → 宽度变化 → 内容重排 → 高度变化 → 又隐藏"的
        来回抖动：所有换行宽度（wraplength）都是固定常量，内容高度与画布实际
        宽度无关。
        """
        need = not (float(first) <= 0.0 and float(last) >= 1.0)
        if need != self._scrollbar_visible:
            self._scrollbar_visible = need
            if need:
                self._scrollbar.pack(side="right", fill="y")
            else:
                self._scrollbar.pack_forget()
        self._scrollbar.set(first, last)

    def _on_settings_wheel(self, event):
        """滚轮只在设置窗口内容区生效，别抢词库窗口等其它窗口的滚轮。"""
        if not self._scrollbar_visible:
            return
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None and widget is not self._scroll:
            widget = getattr(widget, "master", None)
        if widget is None:
            return
        self._scroll.yview_scroll(int(-event.delta / 120) or -1, "units")

    def _compute_size(self):
        self.root.update_idletasks()
        req_h = self.body.winfo_reqheight()
        h = req_h + PAD_TOP + PAD_BOTTOM
        sh = self.root.winfo_screenheight()
        return WIN_W, min(h, sh - 60)

    def _center_window(self):
        w, h = self._compute_size()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = max((sh - h) // 2, 0)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _restore_window_position(self):
        """恢复上次位置；显示器布局变化后，避免把窗口放到不可见区域。"""
        saved = self.cfg.get("settings_window_position")
        if not isinstance(saved, dict):
            return False
        try:
            x, y = int(saved["x"]), int(saved["y"])
        except (KeyError, TypeError, ValueError):
            return False

        w, h = self._compute_size()
        # vroot 是 Windows 的整块虚拟桌面，包含扩展显示器和负坐标显示器。
        vx, vy = self.root.winfo_vrootx(), self.root.winfo_vrooty()
        vw, vh = self.root.winfo_vrootwidth(), self.root.winfo_vrootheight()
        visible_margin = 120
        if (x + visible_margin < vx or y + visible_margin < vy or
                x > vx + vw - visible_margin or y > vy + vh - visible_margin):
            return False
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        return True

    def _remember_window_position(self):
        """在关闭或保存前记录位置，下一次打开设置时恢复。"""
        try:
            self.root.update_idletasks()
            self.cfg.set("settings_window_position", {
                "x": self.root.winfo_x(),
                "y": self.root.winfo_y(),
            })
        except Exception:
            # 位置记忆不能影响正常保存或退出。
            pass

    def _refit(self):
        w, h = self._compute_size()
        self.root.geometry(f"{w}x{h}")

    # ---------- 卡片 header ----------
    def _card_header(self, card, title, right=None):
        hdr = tk.Frame(card, bg=CARD)
        hdr.pack(fill="x", pady=(0, FIELD_GAP))
        tk.Label(hdr, text=title, bg=CARD, fg=TEXT,
                 font=FONT(F_CARD_TITLE, "bold")).pack(side="left")
        if right is not None:
            right.pack(side="right")

    # ---------- 识别 ----------
    def _build_asr(self, card):
        v = self._var
        self._card_header(card, "语音引擎")
        self._build_cloud(card)

    def _build_cloud(self, parent):
        v = self._var
        # 「必填」并进字段名：单独一行说明要占 39px，而窗口高度已经卡在屏幕上限。
        self._key_field(parent, "API Key（必填）", v["sauc_key"], self._confirm_sauc_key)

        self._adv_btn = tk.Label(parent, text="高级（端点 / 资源ID 已预填）", bg=CARD,
                                 fg=ACCENT, font=FONT(F_LINK), cursor="hand2")
        self._adv_btn.pack(anchor="w", pady=(FIELD_GAP, 0))
        self._adv_btn.bind("<Button-1>", lambda e: self._toggle_adv())
        self._adv_visible = False
        self._adv_box = tk.Frame(parent, bg=CARD)
        self._field(self._adv_box, "端点", v["sauc_endpoint"])
        self._field(self._adv_box, "资源ID", v["sauc_resource"])
        self._adv_box.pack_forget()

    # ---------- 润色 ----------
    def _build_refine(self, card):
        v = self._var
        self._card_header(card, "智能整理 API（可选）")
        # 「兼容 OpenAI 的 sk-/ark-」并进字段名（同上：省掉一整行说明）。
        self._key_field(card, "API Key（兼容 OpenAI 的 sk-/ark-）", v["refine_key"],
                        self._confirm_refine_key)

        self._refine_adv_btn = tk.Label(card, text="高级（Base URL / 模型 已预填）", bg=CARD,
                                        fg=ACCENT, font=FONT(F_LINK), cursor="hand2")
        self._refine_adv_btn.pack(anchor="w", pady=(FIELD_GAP, 0))
        self._refine_adv_btn.bind("<Button-1>", lambda e: self._toggle_refine_adv())
        self._refine_adv_visible = False
        self._refine_adv_box = tk.Frame(card, bg=CARD)
        self._field(self._refine_adv_box, "Base URL", v["refine_base"])
        self._field(self._refine_adv_box, "模型", v["refine_model"])
        self._refine_adv_box.pack_forget()

    # ---------- 热键 ----------
    #
    # 一个录制控件 + 标题右边的选择框切换「录音热键 / 纠错热键」。
    #
    # 为什么不放两个录制控件（都试过，都被否）：并排两列要把窗口从 780 加宽到
    # 1000px，观感很差；上下堆叠又要多占约 190px 高度，本来就超出屏幕（见 _build
    # 顶部关于滚动的注释）。现在的做法**不改窗口宽度、不改任何原有文字的字号与
    # 尺寸**，选择框紧挨在标题右边，并且"选哪个就录哪个"比"两个都摆在眼前"更不
    # 容易误录。
    #
    # 待录的两个值存在 `self._hotkey_slots`（"main" / "correction"），切换时
    # **先回存当前槽、再载入目标槽**（`_stash_hotkey` / `_load_hotkey`）。
    def _build_hotkey(self, card):
        v = self._var
        c = self.cfg
        self._card_header(card, "输入控制")

        self._editing = "main"
        self._hotkey_slots = {
            "main": (c.get("hotkey") or "`", int(c.get("hotkey_modifiers") or 0)),
            "correction": (c.get("correction_hotkey") or "`",
                           int(c.get("correction_hotkey_modifiers") or 0)),
        }

        box = tk.Frame(card, bg=SURFACE_PEARL, highlightbackground=HAIRLINE,
                       highlightthickness=1, bd=0)
        box.pack(fill="x", pady=(0, FIELD_GAP))
        inner = tk.Frame(box, bg=SURFACE_PEARL, padx=20, pady=18)
        inner.pack(fill="x")

        pick_row = tk.Frame(inner, bg=SURFACE_PEARL)
        pick_row.pack(fill="x")
        # 标题沿用原来的字号/字体（F_ROW bold），只在它**右边**挨着放一个选择框。
        self._hotkey_title = tk.Label(pick_row, text="录音热键", bg=SURFACE_PEARL,
                                      fg=TEXT, font=FONT(F_ROW, "bold"))
        self._hotkey_title.pack(side="left")
        self._hotkey_seg = Segmented(
            pick_row, [("main", "录音"), ("correction", "纠错")],
            self._on_pick_hotkey_slot, "main", height=34)
        self._hotkey_seg.pack(side="left", padx=(16, 0))

        self._hotkey_desc = tk.Label(inner, bg=SURFACE_PEARL, fg=TEXT_DIM,
                                     font=FONT(F_DESC), wraplength=600,
                                     justify="left")
        self._hotkey_desc.pack(anchor="w", pady=(4, 12))
        self._update_hotkey_desc()

        self._hotkey_recorder = HotkeyRecorder(inner, on_capture=self._on_capture_hotkey)
        self._hotkey_recorder.pack(fill="x")
        self._load_hotkey("main")

        key_row = tk.Frame(inner, bg=SURFACE_PEARL)
        key_row.pack(fill="x", pady=(12, 0))
        tk.Label(key_row, text="常用", bg=SURFACE_PEARL, fg=TEXT_DIM,
                 font=FONT(F_DESC)).pack(side="left", padx=(0, 8))
        for label, key in (("反引号", "`"), ("Caps Lock", "CapsLock"), ("F8", "F8")):
            chip = tk.Label(key_row, text=label, bg=CARD, fg=ACCENT, cursor="hand2",
                            font=FONT(F_DESC, "bold"), padx=11, pady=7,
                            highlightbackground=HAIRLINE, highlightthickness=1)
            chip.pack(side="left", padx=(0, 8))
            chip.bind("<Button-1>", lambda _event, value=key: self._set_hotkey(value))

        tk.Label(card, text="触发方式", bg=CARD, fg=TEXT,
                 font=FONT(F_ROW, "bold")).pack(anchor="w", pady=(2, 10))
        # 原来这里还有一行「按住时录音，松开后…」提示（占 41px）。分段控件上的
        # 文案已经说清楚了，删掉这行纯粹为省高度。
        seg = Segmented(card, [("hold", "按住说话（推荐）"), ("toggle", "单击开始 / 再按结束")],
                        self._on_trigger, v["trigger"].get(), height=44)
        seg.pack(anchor="w")
        self._seg_trigger = seg

        memory_row = tk.Frame(card, bg=CARD)
        memory_row.pack(fill="x", pady=(FIELD_GAP, 0))
        memory_copy = tk.Frame(memory_row, bg=CARD)
        memory_copy.pack(side="left", fill="x", expand=True)
        tk.Label(memory_copy, text="个人记忆（只保存你确认的纠正）", bg=CARD, fg=TEXT,
                 font=FONT(F_ROW, "bold")).pack(anchor="w")
        PillButton(memory_row, "管理词库", self._open_dictionary_manager,
                   primary=False, weight="normal", min_w=118).pack(side="right", padx=(16, 0))

    # ---- 热键槽位（录音 / 纠错）----
    HOTKEY_SLOT_TITLE = {"main": "录音热键", "correction": "纠错热键"}
    # 每段都必须排得进**一行**（实际可用宽度约 600px）——多一行就是 25px，而窗口
    # 高度已经卡在屏幕上限（见 _build 顶部注释）。
    HOTKEY_SLOT_DESC = {
        "main": "按下它开始语音输入；点「录制」可换成任意键或组合键。",
        "correction": "选中文字后按下它，弹出「错误纠正」窗口；默认 Alt + `。",
    }

    def _update_hotkey_desc(self):
        """标题与说明都跟着当前选择变——否则标题会写着一个热键、录制控件在改另一个。"""
        self._hotkey_title.configure(text=self.HOTKEY_SLOT_TITLE[self._editing])
        self._hotkey_desc.configure(text=self.HOTKEY_SLOT_DESC[self._editing])

    def _stash_hotkey(self):
        """把录制控件当前显示的值回存到正在编辑的槽位。

        切换槽位和保存设置前都必须先调用 —— 否则用户在纠错热键上录完直接点
        「保存并应用」，录到的东西会被丢弃（录制控件只有一个，它承载的是当前槽位）。
        """
        self._hotkey_slots[self._editing] = self._hotkey_recorder.value()

    def _load_hotkey(self, slot):
        key, mods = self._hotkey_slots.get(slot, ("", 0))
        self._hotkey_recorder.set_value(key, mods)

    def _on_pick_hotkey_slot(self, slot):
        """切换「录音热键 / 纠错热键」：先回存当前，再载入目标。"""
        if slot == self._editing:
            return
        self._stash_hotkey()
        self._editing = slot
        self._load_hotkey(slot)
        self._update_hotkey_desc()

    # ================= 控件 =================
    def _entry(self, parent, var, width=None, show=None):
        e = tk.Entry(parent, textvariable=var, font=FONT(F_INPUT),
                     bg=CARD, fg=TEXT, insertbackground=TEXT,
                     relief="flat", highlightthickness=1, bd=0,
                     highlightbackground=HAIRLINE, highlightcolor=ACCENT_HOVER,
                     width=width or 40, show=show or "")
        return e

    def _field(self, parent, label, var, show=None, width=40):
        """字段组：label 在上、input 在下。"""
        grp = tk.Frame(parent, bg=CARD)
        grp.pack(fill="x", pady=(0, FIELD_GAP))
        tk.Label(grp, text=label, bg=CARD, fg=TEXT_LABEL,
                 font=FONT(F_LABEL, "bold")).pack(anchor="w")
        e = self._entry(grp, var, width=width, show=show)
        e.pack(fill="x", pady=(10, 0), ipady=12)
        return e

    def _key_field(self, parent, label, var, confirm):
        """API Key 专用行：输入框后提供独立的确认保存按钮。"""
        grp = tk.Frame(parent, bg=CARD)
        grp.pack(fill="x", pady=(0, FIELD_GAP))
        tk.Label(grp, text=label, bg=CARD, fg=TEXT_LABEL,
                 font=FONT(F_LABEL, "bold")).pack(anchor="w")
        row = tk.Frame(grp, bg=CARD)
        row.pack(fill="x", pady=(10, 0))
        entry = self._entry(row, var, show="*")
        entry.pack(side="left", fill="x", expand=True, ipady=12)
        PillButton(row, "确定", confirm, height=46, min_w=88).pack(side="right", padx=(12, 0))
        return entry

    # ================= 交互 =================
    def _on_trigger(self, value):
        self._var["trigger"].set(value)

    def _set_hotkey(self, value):
        """常用预设：只替换**键**，保留该热键已有的修饰键。

        修饰键还没设过时用各自的默认值：录音热键 = 裸键（沿用老行为，点「反引号」
        就是裸反引号）；纠错热键 = Alt（否则裸键会和录音热键撞成同一个组合）。
        """
        _old_key, mods = self._hotkey_recorder.value()
        if not mods:
            mods = 1 if self._editing == "correction" else 0
        self._hotkey_recorder.set_value(value, mods)
        self._stash_hotkey()

    def _on_capture_hotkey(self, active):
        """录制开始/结束：暂停或恢复正在生效的热键。

        必须这么做 —— 否则用户在设置界面按下**当前已生效的组合**时会真的触发
        录音或弹出纠错窗口，看起来像"设置界面坏了"。录音热键可能注册在提权助手
        里，所以由主程序统一处理（它会一并通知助手）。
        """
        app = getattr(self.master, "_yurun_app", None)
        if app is None:
            return
        try:
            app.set_hotkeys_suspended(bool(active))
        except Exception as exc:
            log.warning("切换热键暂停状态失败: %s", exc)

    def _confirm_sauc_key(self):
        key = self._var["sauc_key"].get().strip()
        if not key:
            messagebox.showwarning("语润", "请先输入语音识别 API Key。", parent=self.root)
            return
        self.cfg.set("asr_provider", "sauc")
        self.cfg.set("asr_sauc_key", key)
        messagebox.showinfo("语润", "语音识别 API Key 已保存。", parent=self.root)

    def _confirm_refine_key(self):
        key = self._var["refine_key"].get().strip()
        if not key:
            messagebox.showwarning("语润", "请先输入智能整理 API Key。", parent=self.root)
            return
        self.cfg.set("api_key", key)
        messagebox.showinfo("语润", "智能整理 API Key 已保存。", parent=self.root)

    def _toggle_adv(self):
        self._adv_visible = not self._adv_visible
        if self._adv_visible:
            self._adv_box.pack(fill="x", pady=(FIELD_GAP, 0))
        else:
            self._adv_box.pack_forget()
        self._refit()

    def _toggle_refine_adv(self):
        self._refine_adv_visible = not self._refine_adv_visible
        if self._refine_adv_visible:
            self._refine_adv_box.pack(fill="x", pady=(FIELD_GAP, 0))
        else:
            self._refine_adv_box.pack_forget()
        self._refit()

    def _open_dictionary_manager(self):
        """打开只管理语润自身纠错记录的本地词库窗口。"""
        try:
            self._memory_manager = DictionaryManager(self.root)
        except Exception as exc:
            log.error("打开词库管理失败: %s", exc)
            messagebox.showerror("语润", "词库管理无法打开，请查看日志。", parent=self.root)

    # ================= 保存 =================
    def _save(self):
        c = self.cfg
        v = self._var
        sauc_key = v["sauc_key"].get().strip()
        if not sauc_key:
            messagebox.showwarning("语润", "请先填写语音识别 API Key", parent=self.root)
            return
        # 录制控件只有一个：先把它当前显示的值回存到正在编辑的槽位，
        # 否则"在纠错热键上录完直接点保存"会丢掉刚录的组合。
        self._stash_hotkey()
        main_key, main_mods = self._hotkey_slots["main"]
        corr_key, corr_mods = self._hotkey_slots["correction"]
        trigger_mode = v["trigger"].get()
        if not _vk_for(main_key):
            messagebox.showwarning("语润", "录音热键还没录到有效按键，请点「录制」后按一次。",
                                   parent=self.root)
            return
        if not _vk_for(corr_key):
            messagebox.showwarning("语润", "纠错热键还没录到有效按键，请点「录制」后按一次。",
                                   parent=self.root)
            return
        if hotkey_id(main_mods, main_key) == hotkey_id(corr_mods, corr_key):
            messagebox.showwarning(
                "语润", f"录音热键和纠错热键不能是同一个组合（都是 {format_hotkey(main_mods, main_key)}），"
                        "请换一个。", parent=self.root)
            return

        # 保存前先确保热键没有被录制状态暂停着，否则后面应用会撞上"已被自己占用"。
        self._on_capture_hotkey(False)

        app = getattr(self.master, "_yurun_app", None)
        if app is not None:
            ok, reason = app.apply_hotkey_settings(main_key, trigger_mode, main_mods)
            if not ok:
                messagebox.showwarning("语润", reason, parent=self.root)
                return
            ok, reason = app.apply_correction_hotkey_settings(corr_key, corr_mods)
            if not ok:
                # 纠错键已自行回滚，配置必须跟着回滚，不能让配置与实际不一致。
                messagebox.showwarning("语润", reason, parent=self.root)
                corr_key = c.get("correction_hotkey") or "`"
                corr_mods = int(c.get("correction_hotkey_modifiers") or 0)
        c.set("asr_provider", "sauc")
        c.set("asr_sauc_key", sauc_key)
        c.set("asr_sauc_resource_id", v["sauc_resource"].get().strip())
        c.set("asr_sauc_endpoint", v["sauc_endpoint"].get().strip())
        refine_key = v["refine_key"].get().strip()
        c.set("api_key", refine_key)
        c.set("api_base", v["refine_base"].get().strip())
        c.set("api_model", v["refine_model"].get().strip())
        # 智能整理 Key 未填时，保留旧字段兼容，同时回到可用的快速输入模式。
        if not refine_key:
            c.set("refine_enabled", False)
            if c.get("input_mode", "direct") == "refine":
                c.set("input_mode", "direct")
        c.set("hotkey", main_key)
        c.set("hotkey_modifiers", int(main_mods))
        c.set("correction_hotkey", corr_key)
        c.set("correction_hotkey_modifiers", int(corr_mods))
        c.set("trigger_mode", trigger_mode)
        self._remember_window_position()
        applied = "热键已立即生效" if app is not None else "设置将在下次启动时生效"
        messagebox.showinfo("语润", f"设置已保存，{applied}", parent=self.root)
        self.root.destroy()

    def _on_close(self):
        try:
            # 关键：录制中途关窗必须把热键还回去，否则用户会以为语润彻底失灵。
            self._on_capture_hotkey(False)
            self._remember_window_position()
            self.root.destroy()
        except Exception:
            pass


class DictionaryManager:
    """本地个人记忆管理窗口。

    数据只来自用户主动执行的「替换并存入词库」操作或本窗口手动新增，
    不监听键盘、不读取剪贴板，也不上传到网络。
    """

    def __init__(self, master):
        from dictionary import get_entries
        from pill import _cursor_screen_rect, work_area_for_rect

        self.win = tk.Toplevel(master)
        # 与设置窗口保持同一流程：先隐藏，布局与定位完成后才显示。
        # 否则隐藏 root 作为 owner 时，Windows 会把新窗口重置到 (0, 0)。
        self.win.withdraw()
        self.win.title("语润 · 个人记忆")
        self.win.configure(bg=BG)
        # 子窗口不继承父窗口图标，必须自己设，否则标题栏/任务栏是 Tk 默认图标。
        apply_app_icon(self.win)
        # 隐藏的常驻 root 没有可靠的窗口位置。按当前鼠标所在显示器居中，
        # 避免双屏/显示器布局变化后新窗口跑到不可见区域。
        win_w, win_h = 780, 610
        wl, wt, wr, wb = work_area_for_rect(_cursor_screen_rect())
        x = wl + max(0, (wr - wl - win_w) // 2)
        y = wt + max(0, (wb - wt - win_h) // 2)
        self._initial_geometry = (win_w, win_h, x, y, (wl, wt, wr, wb))
        self.win.geometry(f"{win_w}x{win_h}")
        self.win.minsize(700, 540)
        # 不设 transient：常驻 root 是隐藏窗口，作为 owner 会让 Windows 把
        # 子窗口强制重置到左上角/后台。设置页同样不使用 transient。
        self.win.resizable(True, True)

        self._selected_original = None
        self._entries = []
        self.correct_var = tk.StringVar()
        self.aliases_var = tk.StringVar()
        self.enabled_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar()

        body = tk.Frame(self.win, bg=BG, padx=PAD_X, pady=28)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="个人记忆", bg=BG, fg=TEXT,
                 font=FONT(30, "bold")).pack(anchor="w")
        tk.Label(body, text="仅保存你主动确认的纠错。本地存储，不监听键盘，不上传。",
                 bg=BG, fg=TEXT_DIM, font=FONT(F_DESC)).pack(anchor="w", pady=(5, 20))

        content = tk.Frame(body, bg=BG)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1, minsize=280)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)

        left = tk.Frame(content, bg=CARD, highlightbackground=CARD_BORDER,
                        highlightthickness=1, bd=0, padx=18, pady=18)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        tk.Label(left, text="已学习的词条", bg=CARD, fg=TEXT,
                 font=FONT(F_CARD_TITLE, "bold")).pack(anchor="w", pady=(0, 10))
        list_box = tk.Frame(left, bg=CARD)
        list_box.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(list_box, bg="#FAFAFC", fg=TEXT, selectbackground="#DCEBFA",
                                  selectforeground=TEXT, activestyle="none", relief="flat", bd=0,
                                  font=FONT(14), exportselection=False)
        scroll = tk.Scrollbar(list_box, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        right = tk.Frame(content, bg=CARD, highlightbackground=CARD_BORDER,
                         highlightthickness=1, bd=0, padx=24, pady=20)
        right.grid(row=0, column=1, sticky="nsew")
        tk.Label(right, text="词条详情", bg=CARD, fg=TEXT,
                 font=FONT(F_CARD_TITLE, "bold")).pack(anchor="w", pady=(0, 14))
        self._memory_field(right, "正确写法", self.correct_var)
        self._memory_field(right, "错误写法（多个用顿号、逗号或换行分开）", self.aliases_var)
        check = tk.Checkbutton(right, text="启用这条记忆", variable=self.enabled_var,
                               bg=CARD, fg=TEXT, activebackground=CARD,
                               activeforeground=TEXT, selectcolor=CARD,
                               font=FONT(F_DESC))
        check.pack(anchor="w", pady=(0, 16))
        tk.Label(right, textvariable=self.status_var, bg=CARD, fg="#16803C",
                 font=FONT(F_DESC)).pack(anchor="w", pady=(0, 12))

        actions = tk.Frame(right, bg=CARD)
        actions.pack(fill="x", side="bottom")
        PillButton(actions, "清空全部", self._clear_all, primary=False,
                   weight="normal", min_w=106).pack(side="left")
        PillButton(actions, "删除", self._delete_selected, primary=False,
                   weight="normal", min_w=82).pack(side="right")
        PillButton(actions, "保存", self._save_selected, min_w=88).pack(side="right", padx=(0, 10))
        PillButton(actions, "新建", self._new_entry, primary=False,
                   weight="normal", min_w=82).pack(side="right", padx=(0, 10))

        self._refresh()
        # 根窗口是隐藏的常驻窗口；管理页创建后必须主动置前，否则部分 Windows
        # 环境会把它留在其它应用后面，用户会误以为没有打开。
        win_w, win_h, x, y, work_area = self._initial_geometry
        # 必须在全部控件创建后、deiconify 前设置坐标；这是 Tk/Windows 对
        # withdrawn Toplevel 最稳定的定位时机。
        self.win.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.win.update_idletasks()
        self.win.deiconify()
        # 新开窗口由隐藏 root 持有时，部分 Windows 环境不会自动带到前台。
        # 仅短暂置顶一次，然后立刻恢复普通窗口层级。
        self.win.attributes("-topmost", True)
        self.win.lift()
        self.win.focus_force()
        self.win.after(350, lambda: self.win.attributes("-topmost", False))
        # 等窗口首次映射后再写一次坐标，兼容高 DPI / 多屏下 Tk 首次 map 覆盖 geometry。
        self.win.after(80, lambda: self.win.geometry(f"{win_w}x{win_h}+{x}+{y}"))
        log.info("个人记忆窗口定位: expected=(%s,%s) actual=(%s,%s) size=%sx%s work_area=%s",
                 x, y, self.win.winfo_x(), self.win.winfo_y(), win_w, win_h, work_area)
        log.info("个人记忆管理窗口已显示")

    def _memory_field(self, parent, label, variable):
        group = tk.Frame(parent, bg=CARD)
        group.pack(fill="x", pady=(0, 16))
        tk.Label(group, text=label, bg=CARD, fg=TEXT_LABEL,
                 font=FONT(F_LABEL, "bold")).pack(anchor="w", pady=(0, 8))
        entry = tk.Entry(group, textvariable=variable, bg="#FFFFFF", fg=TEXT,
                         insertbackground=TEXT, relief="flat", bd=0,
                         highlightthickness=1, highlightbackground=HAIRLINE,
                         highlightcolor=ACCENT_HOVER, font=FONT(F_INPUT))
        entry.pack(fill="x", ipady=10)

    @staticmethod
    def _entry_label(entry):
        marker = "●" if entry.get("enabled", True) else "○"
        aliases = "、".join(a.get("text", "") for a in entry.get("aliases") or [])
        suffix = f" ← {aliases}" if aliases else ""
        return f"{marker} {entry.get('text', '')}{suffix}"

    def _refresh(self, select_text=None):
        from dictionary import get_entries

        self._entries = sorted(get_entries(), key=lambda e: e.get("text", "").lower())
        self.listbox.delete(0, "end")
        selected_index = None
        for index, entry in enumerate(self._entries):
            self.listbox.insert("end", self._entry_label(entry))
            if entry.get("text") == select_text:
                selected_index = index
        if selected_index is not None:
            self.listbox.selection_set(selected_index)
            self.listbox.activate(selected_index)
            self._load_entry(self._entries[selected_index])
        elif not self._entries:
            self._new_entry()

    def _on_select(self, _event=None):
        selection = self.listbox.curselection()
        if selection:
            self._load_entry(self._entries[selection[0]])

    def _load_entry(self, entry):
        self._selected_original = entry.get("text")
        self.correct_var.set(entry.get("text") or "")
        self.aliases_var.set("、".join(a.get("text", "") for a in entry.get("aliases") or []))
        self.enabled_var.set(bool(entry.get("enabled", True)))
        self.status_var.set("")

    def _new_entry(self):
        self._selected_original = None
        self.correct_var.set("")
        self.aliases_var.set("")
        self.enabled_var.set(True)
        self.status_var.set("填写后点击保存。")
        self.listbox.selection_clear(0, "end")

    @staticmethod
    def _parse_aliases(value):
        normalized = (value or "").replace("，", "、").replace(",", "、").replace("\n", "、")
        return [part.strip() for part in normalized.split("、") if part.strip()]

    def _save_selected(self):
        from dictionary import add_entry, update_entry

        correct = self.correct_var.get().strip()
        aliases = self._parse_aliases(self.aliases_var.get())
        if not correct:
            self.status_var.set("请填写正确写法。")
            return
        try:
            if self._selected_original is None:
                add_entry(correct, source="manual")
                original = correct
            else:
                original = self._selected_original
            update_entry(original, correct, aliases, self.enabled_var.get())
        except ValueError as exc:
            self.status_var.set(str(exc))
            return
        except Exception as exc:
            log.error("保存个人记忆失败: %s", exc)
            self.status_var.set("保存失败，请查看日志。")
            return
        self.status_var.set("已保存到本机。")
        self._refresh(select_text=correct)

    def _delete_selected(self):
        from dictionary import delete_entry

        if not self._selected_original:
            self.status_var.set("请先选择一条词库记录。")
            return
        if not messagebox.askyesno("删除词条", f"确定删除“{self._selected_original}”吗？", parent=self.win):
            return
        if delete_entry(self._selected_original):
            self._new_entry()
            self._refresh()
            self.status_var.set("已删除。")

    def _clear_all(self):
        from dictionary import clear_entries

        if not messagebox.askyesno("清空全部个人记忆", "这会删除所有本地词库记录，且无法自动恢复。确定继续吗？", parent=self.win):
            return
        clear_entries()
        self._new_entry()
        self._refresh()
        self.status_var.set("已清空全部本地记忆。")


# 保持旧导入兼容（部分测试或入口可能直接打开设置窗口）
if __name__ == "__main__":
    win = SettingsWindow()
    win.root.mainloop()
