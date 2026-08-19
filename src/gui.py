"""语润（Yurun）设置窗口 — Apple 风格（稳定版）。

目标：在 Windows tkinter 上稳定还原《语润设置-Apple风格.html》的视觉，
同时避免 Canvas+Frame 混合带来的尺寸/截字/层级问题。

设计取舍：
- 卡片/窗口背景：用 tk.Frame，白色矩形卡片放在 parchment 背景上（macOS 现代设置也大量使用直角卡片）。
- 按钮/分段/交通灯：仍用 Canvas 画圆角胶囊，但尺寸计算更保守。
- 字体：显式创建 tkfont.Font，优先 Inter，回退微软雅黑/雅黑/PingFang/SimHei，禁止回退到衬线。
- 布局：全部 pack/grid，禁止 place，避免绝对定位导致的重叠。
- 字号：按 Windows 可读基准整体放大（正文 16px，应用大标题 40px，卡片标题 20px 等）。
"""
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

from config import get_config
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
F_APP_TITLE = 40
F_SUBTITLE = 17
F_CARD_TITLE = 20
F_LABEL = 15
F_INPUT = 16
F_DESC = 14
F_LINK = 15
F_ROW = 17
F_SEG = 15
F_HOTKEY = 19
F_BTN = 16

# ---- 间距 ----
WIN_W = 640                 # 窗口固定宽度（放大版，给中文留余量）
PAD_X = 32                  # 内容区左右内边距
PAD_TOP = 12
PAD_BOTTOM = 28
CARD_PAD = 24
CARD_GAP = 24
FIELD_GAP = 18
BTN_GAP = 14
TITLE_H = 40

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
        self.root.overrideredirect(True)
        try:
            import os
            _ico = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "assets", "icon.ico")
            self.root.iconbitmap(_ico)
        except Exception:
            pass
        self.root.withdraw()

        self._var = {}
        self._init_vars()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.update_idletasks()
        self._center_window()
        self.root.deiconify()

    def _init_vars(self):
        c = self.cfg
        v = self._var
        v["asr_mode"] = tk.StringVar(
            value="cloud" if c.get("asr_provider") in ("cloud", "sauc", "") else "local")
        v["sauc_key"] = tk.StringVar(value=c.get("asr_sauc_key") or "")
        v["sauc_resource"] = tk.StringVar(
            value=c.get("asr_sauc_resource_id") or "volc.seedasr.sauc.duration")
        v["sauc_endpoint"] = tk.StringVar(
            value=c.get("asr_sauc_endpoint") or
            "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream")
        v["local_model"] = tk.StringVar(value=c.get("whisper_model") or "small")
        v["local_lang"] = tk.StringVar(value=c.get("language") or "auto")
        v["refine_key"] = tk.StringVar(value=c.get("api_key") or "")
        v["refine_base"] = tk.StringVar(value=c.get("api_base") or "https://ark.cn-beijing.volces.com/api/v3")
        v["refine_model"] = tk.StringVar(value=c.get("api_model") or "")
        v["hotkey"] = tk.StringVar(value=c.get("hotkey") or "`")
        v["trigger"] = tk.StringVar(value=c.get("trigger_mode") or "hold")
        v["insert"] = tk.StringVar(value=c.get("insert_method") or "type")

    # ================= 构建 =================
    def _build(self):
        self._build_title_bar()

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=PAD_X, pady=(PAD_TOP, PAD_BOTTOM))

        # Header
        tk.Label(self.body, text="语润", bg=BG, fg=TEXT,
                 font=FONT(F_APP_TITLE, "bold")).pack(anchor="w")
        tk.Label(self.body, text="按住快捷键说话。松开即润色输出",
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
        PillButton(btns, "保存", self._save).pack(side="right")

    def _make_card(self, parent):
        """白色卡片：Frame + 浅灰细边框（tkinter 无圆角 Frame，用直角矩形同样干净）。"""
        card = tk.Frame(parent, bg=CARD, highlightbackground=CARD_BORDER,
                        highlightthickness=1, bd=0)
        card.pack(fill="x", pady=(0, CARD_GAP))
        inner = tk.Frame(card, bg=CARD, padx=CARD_PAD, pady=CARD_PAD)
        inner.pack(fill="both", expand=True)
        return inner

    def _build_title_bar(self):
        tb = tk.Frame(self.root, bg=BG, height=TITLE_H)
        tb.pack(fill="x")
        tb.pack_propagate(False)
        tb.grid_columnconfigure(1, weight=1)  # 标题列居中拉伸

        lights = tk.Frame(tb, bg=BG)
        specs = [("#FF5F57", self._on_close), ("#FEBC2E", None), ("#28C840", None)]
        for color, act in specs:
            c = tk.Canvas(lights, width=13, height=13, bg=BG,
                          highlightthickness=0, bd=0, cursor="hand2")
            c.create_oval(1, 1, 12, 12, fill=color, outline="")
            if act:
                c.bind("<Button-1>", lambda e, a=act: a())
            c.pack(side="left", padx=(0, 8))
        lights.grid(row=0, column=0, padx=16, sticky="w")

        title = tk.Label(tb, text="语润 · 设置", bg=BG, fg=TEXT_DIM,
                         font=FONT(F_TITLE_BAR))
        title.grid(row=0, column=1)

        # 右侧 spacer 与左侧交通灯宽度对称，确保标题真正居中
        right_spacer = tk.Frame(tb, bg=BG, width=16 + 13 * 3 + 8 * 2)
        right_spacer.grid(row=0, column=2, sticky="e")

        for w in (tb, title, right_spacer, lights):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, e):
        self._drag_x = e.x_root - self.root.winfo_x()
        self._drag_y = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        x = e.x_root - self._drag_x
        y = e.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _compute_size(self):
        self.root.update_idletasks()
        req_h = self.body.winfo_reqheight()
        h = req_h + TITLE_H + PAD_TOP + PAD_BOTTOM
        sh = self.root.winfo_screenheight()
        return WIN_W, min(h, sh - 60)

    def _center_window(self):
        w, h = self._compute_size()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = max((sh - h) // 2, 0)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

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
        seg = Segmented(card, [("cloud", "云端火山"), ("local", "本地离线")],
                        self._on_asr_mode, v["asr_mode"].get())
        self._card_header(card, "语音引擎", right=seg)
        self._seg_asr = seg

        self._cloud_box = tk.Frame(card, bg=CARD)
        self._local_box = tk.Frame(card, bg=CARD)
        self._build_cloud(self._cloud_box)
        self._build_local(self._local_box)
        self._apply_asr_mode(v["asr_mode"].get())

    def _build_cloud(self, parent):
        v = self._var
        self._field(parent, "API Key", v["sauc_key"], show="*")
        tk.Label(parent, text="火山引擎语音技术 · 流式语音识别（SAUC），与 Cindy 同款",
                 bg=CARD, fg=TEXT_DIM, font=FONT(F_DESC)).pack(anchor="w", pady=(8, 0))

        self._adv_btn = tk.Label(parent, text="高级（端点 / 资源ID 已预填）", bg=CARD,
                                 fg=ACCENT, font=FONT(F_LINK), cursor="hand2")
        self._adv_btn.pack(anchor="w", pady=(FIELD_GAP, 0))
        self._adv_btn.bind("<Button-1>", lambda e: self._toggle_adv())
        self._adv_visible = False
        self._adv_box = tk.Frame(parent, bg=CARD)
        self._field(self._adv_box, "端点", v["sauc_endpoint"])
        self._field(self._adv_box, "资源ID", v["sauc_resource"])
        self._adv_box.pack_forget()

    def _build_local(self, parent):
        v = self._var
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(0, FIELD_GAP))
        tk.Label(row, text="模型", bg=CARD, fg=TEXT, font=FONT(F_ROW)).pack(side="left")
        seg = Segmented(row, [("small", "small 推荐"), ("base", "base 快")],
                        self._on_local_model, v["local_model"].get())
        seg.pack(side="right")
        self._seg_model = seg

        row2 = tk.Frame(parent, bg=CARD)
        row2.pack(fill="x", pady=(0, FIELD_GAP))
        tk.Label(row2, text="语言", bg=CARD, fg=TEXT, font=FONT(F_ROW)).pack(side="left")
        seg2 = Segmented(row2, [("auto", "自动"), ("zh", "中文"), ("en", "英语")],
                         self._on_local_lang, v["local_lang"].get())
        seg2.pack(side="right")
        self._seg_lang = seg2

        tk.Label(parent, text="首次使用会自动下载模型（small 约 460MB / base 约 140MB）",
                 bg=CARD, fg=TEXT_DIM, font=FONT(F_DESC)).pack(anchor="w", pady=(8, 0))

    # ---------- 润色 ----------
    def _build_refine(self, card):
        v = self._var
        self._card_header(card, "润色 API")
        self._field(card, "API Key", v["refine_key"], show="*")
        tk.Label(card, text="润色 API Key（OpenAI 兼容，sk-/ark- 等均可）",
                 bg=CARD, fg=TEXT_DIM, font=FONT(F_DESC)).pack(anchor="w", pady=(8, 0))

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
    def _build_hotkey(self, card):
        v = self._var
        self._card_header(card, "快捷键与行为")

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", pady=(0, FIELD_GAP))
        tk.Label(row, text="热键", bg=CARD, fg=TEXT, font=FONT(F_ROW)).pack(side="left")
        self._hotkey_entry = tk.Entry(row, textvariable=v["hotkey"], font=FONT(F_HOTKEY, "bold"),
                                      bg=CARD, fg=TEXT, insertbackground=TEXT,
                                      relief="flat", highlightthickness=1, bd=0,
                                      highlightbackground=HAIRLINE,
                                      highlightcolor=ACCENT_HOVER, width=6,
                                      justify="center")
        self._hotkey_entry.pack(side="right", ipady=10)

        row2 = tk.Frame(card, bg=CARD)
        row2.pack(fill="x", pady=(0, FIELD_GAP))
        tk.Label(row2, text="触发方式", bg=CARD, fg=TEXT, font=FONT(F_ROW)).pack(side="left")
        seg = Segmented(row2, [("hold", "按住说话"), ("toggle", "单击切换")],
                        self._on_trigger, v["trigger"].get())
        seg.pack(side="right")
        self._seg_trigger = seg

        row3 = tk.Frame(card, bg=CARD)
        row3.pack(fill="x", pady=(0, FIELD_GAP))
        tk.Label(row3, text="粘贴方式", bg=CARD, fg=TEXT, font=FONT(F_ROW)).pack(side="left")
        seg2 = Segmented(row3, [("type", "逐字输入"), ("paste", "剪贴板")],
                         self._on_insert, v["insert"].get())
        seg2.pack(side="right")
        self._seg_insert = seg2

        tk.Label(card, text="逐字输入不污染剪贴板历史（默认）；个别窗口不兼容时切「剪贴板」",
                 bg=CARD, fg=TEXT_DIM, font=FONT(F_DESC)).pack(anchor="w", pady=(8, 0))

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

    # ================= 交互 =================
    def _on_asr_mode(self, value):
        self._var["asr_mode"].set(value)
        self._apply_asr_mode(value)
        self._refit()

    def _apply_asr_mode(self, mode):
        if mode == "cloud":
            self._cloud_box.pack(fill="x")
            self._local_box.pack_forget()
        else:
            self._cloud_box.pack_forget()
            self._local_box.pack(fill="x")

    def _on_local_model(self, value):
        self._var["local_model"].set(value)

    def _on_local_lang(self, value):
        self._var["local_lang"].set(value)

    def _on_trigger(self, value):
        self._var["trigger"].set(value)

    def _on_insert(self, value):
        self._var["insert"].set(value)

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

    # ================= 保存 =================
    def _save(self):
        c = self.cfg
        v = self._var
        mode = v["asr_mode"].get()
        c.set("asr_provider", "sauc" if mode == "cloud" else "local")
        c.set("asr_sauc_key", v["sauc_key"].get().strip())
        c.set("asr_sauc_resource_id", v["sauc_resource"].get().strip())
        c.set("asr_sauc_endpoint", v["sauc_endpoint"].get().strip())
        c.set("whisper_model", v["local_model"].get())
        c.set("language", v["local_lang"].get().strip() or "auto")
        c.set("api_key", v["refine_key"].get().strip())
        c.set("api_base", v["refine_base"].get().strip())
        c.set("api_model", v["refine_model"].get().strip())
        c.set("hotkey", v["hotkey"].get().strip() or "`")
        c.set("trigger_mode", v["trigger"].get())
        c.set("insert_method", v["insert"].get())
        messagebox.showinfo("语润", "设置已保存", parent=self.root)
        self.root.destroy()

    def _on_close(self):
        try:
            self.root.destroy()
        except Exception:
            pass


# 保持旧导入兼容（部分测试或入口可能直接打开设置窗口）
if __name__ == "__main__":
    win = SettingsWindow()
    win.root.mainloop()
