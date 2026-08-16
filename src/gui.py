"""语润（Yurun）设置窗口 — 苹果风格。

- 一屏显示全部内容，无滚动条（窗口高度自适应）
- 白色圆角卡片 + 圆角分段控件（Canvas 绘制）
- 字体统一加大，分段控件字大清晰
- 默认值全部预填，只需填 Key
"""
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

from config import get_config
from logger import get_logger
log = get_logger("yurun.gui")

# ---- 苹果风格配色 ----
BG = "#F5F5F7"
CARD = "#FFFFFF"
CARD_BORDER = "#E3E3E8"
TEXT = "#1D1D1F"
TEXT_DIM = "#86868B"
ACCENT = "#0071E3"
BORDER_INPUT = "#D2D2D7"
SEG_BG = "#E9E9EC"
FONT = "Microsoft YaHei"
F = 17        # 正文/标签（与 Tab 18 接近，整窗一致）
F_SMALL = 14  # 提示/次要（加大，便于阅读）
F_TITLE = 24  # 大标题
F_CARD = 20   # 卡片标题
F_SEG = 18    # 分段控件（加大，清晰可读；略大于正文，保留层级差）


def _rrect(canvas, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上绘制圆角矩形（平滑多边形）。"""
    pts = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r,
           x2, y2-r, x2, y2, x2-r, y2, x1+r, y2,
           x1, y2, x1, y2-r, x1, y1+r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class Segmented(tk.Canvas):
    """圆角分段控件：胶囊底 + 白色选中块 + 蓝色选中字。"""

    def __init__(self, parent, options, command=None, value=None, height=44):
        self.options = list(options)          # [(value, label), ...]
        self.command = command
        self.value = value if value is not None else self.options[0][0]
        self._pad = 30
        super().__init__(parent, bg=parent["bg"], highlightthickness=0,
                         bd=0, height=height, cursor="hand2")
        self._measure()
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._click)
        self._draw()

    def _measure(self):
        f = tkfont.Font(font=(FONT, F_SEG, "bold"))
        self._lbl_w = [f.measure(lb) for _, lb in self.options]
        self._seg_ws = [w + self._pad * 2 for w in self._lbl_w]
        self._seg_w = sum(self._seg_ws)
        self.configure(width=self._seg_w)

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or self._seg_w
        h = self.winfo_height() or 34
        r = h // 2
        _rrect(self, 1, 1, w - 1, h - 1, r, fill=SEG_BG, outline="")
        x = 0
        for (val, lb), sw in zip(self.options, self._seg_ws):
            if val == self.value:
                _rrect(self, x + 2, 2, x + sw - 2, h - 2, r - 2,
                       fill=CARD, outline=CARD_BORDER)
                self.create_text(x + sw // 2, h // 2, text=lb,
                                 font=(FONT, F_SEG, "bold"), fill=ACCENT)
            else:
                self.create_text(x + sw // 2, h // 2, text=lb,
                                 font=(FONT, F_SEG), fill=TEXT_DIM)
            x += sw

    def _click(self, event):
        x = event.x
        cx = 0
        for (val, _), sw in zip(self.options, self._seg_ws):
            if cx <= x < cx + sw:
                if val != self.value:
                    self.value = val
                    self._draw()
                    if self.command:
                        self.command(val)
                return
            cx += sw

    def set(self, value):
        if value != self.value:
            self.value = value
            self._draw()


class Card(tk.Canvas):
    """圆角白色卡片：标题 + 内容区（body）。"""

    def __init__(self, parent, title, radius=12, padx=20, pady=16):
        super().__init__(parent, bg=BG, highlightthickness=0, bd=0)
        self.radius = radius
        self.padx = padx
        self.pady = pady
        self._title = title
        self.body = tk.Frame(self, bg=CARD)
        self._body_win = None
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event=None):
        w = self.winfo_width()
        if w <= 1:
            return
        h = self.winfo_height()
        self.body.update_idletasks()
        self.delete("bg", "title")
        _rrect(self, 1, 1, w - 1, h - 1, self.radius,
               fill=CARD, outline=CARD_BORDER, tags="bg")
        self.create_text(self.padx, 28, text=self._title, anchor="w",
                         font=(FONT, F_CARD, "bold"), fill=TEXT, tags="title")
        if self._body_win is None:
            self._body_win = self.create_window(self.padx, 56,
                                                window=self.body, anchor="nw")
        bw = max(w - self.padx * 2, self.body.winfo_reqwidth())
        self.itemconfigure(self._body_win, width=bw)
        self.body.configure(width=bw)
        self.tag_lower("bg")
        self.tag_lower("title")

    def fit(self):
        self.update_idletasks()
        bh = self.body.winfo_reqheight()
        self.configure(height=56 + bh + self.pady)
        self._on_resize()


class PillButton(tk.Canvas):
    """圆角按钮：主按钮蓝底白字 / 次按钮白底蓝字。"""

    def __init__(self, parent, text, command, primary=True, height=38, min_w=110):
        super().__init__(parent, bg=BG, highlightthickness=0, bd=0,
                         height=height, cursor="hand2")
        self.text = text
        self.command = command
        self.primary = primary
        f = tkfont.Font(font=(FONT, F, "bold"))
        self.configure(width=max(min_w, f.measure(text) + 60))
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: command())
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        r = h // 2
        if self.primary:
            _rrect(self, 1, 1, w - 1, h - 1, r, fill=ACCENT, outline="")
            fg = "#FFFFFF"
        else:
            _rrect(self, 1, 1, w - 1, h - 1, r, fill=CARD, outline=CARD_BORDER)
            fg = ACCENT
        self.create_text(w // 2, h // 2, text=self.text,
                         font=(FONT, F, "bold"), fill=fg)


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
        # 设置窗口图标（与程序/任务栏一致：assets/icon.ico）
        try:
            import os
            _ico = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "assets", "icon.ico")
            self.root.iconbitmap(_ico)
        except Exception:
            pass
        self.root.withdraw()   # 先藏起来：建完界面、刷完渲染再显示，避免"打开后点不动"的假死窗口期

        self._var = {}
        self._init_vars()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.update_idletasks()   # 关键：强制把 StringVar 的值推到各 Entry，否则字段会空白
        self._center_window()
        self.root.deiconify()          # 全部就绪后再显示，用户一打开即可点

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
        v["refine_base"] = tk.StringVar(value=c.get("api_base") or "https://api.deepseek.com/v1")
        v["refine_model"] = tk.StringVar(value=c.get("api_model") or "deepseek-chat")
        v["hotkey"] = tk.StringVar(value=c.get("hotkey") or "`")
        v["trigger"] = tk.StringVar(value=c.get("trigger_mode") or "hold")
        v["insert"] = tk.StringVar(value=c.get("insert_method") or "type")

    # ================= 构建 =================
    def _build(self):
        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=28, pady=14)

        tk.Label(self.body, text="语润", bg=BG, fg=TEXT,
                 font=(FONT, F_TITLE, "bold")).pack(anchor="w")
        tk.Label(self.body, text="按住快捷键说话。松开即润色输出",
                 bg=BG, fg=TEXT_DIM, font=(FONT, F_SMALL)).pack(anchor="w", pady=(2, 10))

        # ===== 识别 =====
        self._card_asr = Card(self.body, "识别")
        self._card_asr.pack(fill="x", pady=(0, 10))
        self._build_asr(self._card_asr.body)
        self._card_asr.fit()

        # ===== 润色 =====
        self._card_refine = Card(self.body, "润色")
        self._card_refine.pack(fill="x", pady=(0, 10))
        self._build_refine(self._card_refine.body)
        self._card_refine.fit()

        # ===== 热键 =====
        self._card_hotkey = Card(self.body, "热键")
        self._card_hotkey.pack(fill="x", pady=(0, 10))
        self._build_hotkey(self._card_hotkey.body)
        self._card_hotkey.fit()

        # ===== 按钮 =====
        btns = tk.Frame(self.body, bg=BG)
        btns.pack(fill="x", pady=(6, 0))
        PillButton(btns, "取消", self._on_close, primary=False).pack(side="right", padx=(10, 0))
        PillButton(btns, "保存", self._save).pack(side="right")

    def _compute_size(self):
        self.root.update_idletasks()
        req_w = max(self.body.winfo_reqwidth(), 600)
        w = min(req_w + 56, 720)
        req_h = self.body.winfo_reqheight()
        h = req_h + 40
        sh = self.root.winfo_screenheight()
        h = min(h, sh - 60)
        return w, h

    def _center_window(self):
        """仅首次打开：居中定位（算 x/y）。之后切 Tab 不动位置。"""
        w, h = self._compute_size()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = max((sh - h) // 2, 0)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _refit(self):
        """切 Tab / 展开高级 / 收起：只刷新窗口尺寸，绝不动 x/y。
        保留用户拖到的位置，实现零抖动。"""
        for c in (self._card_asr, self._card_refine, self._card_hotkey):
            c.fit()
        w, h = self._compute_size()
        self.root.geometry(f"{w}x{h}")

    # ---------- 识别 ----------
    def _build_asr(self, body):
        v = self._var
        seg = Segmented(body, [("cloud", "云端火山"), ("local", "本地离线")],
                        self._on_asr_mode, v["asr_mode"].get())
        seg.pack(anchor="w", pady=(4, 14))
        self._seg_asr = seg

        self._cloud_box = tk.Frame(body, bg=CARD)
        self._local_box = tk.Frame(body, bg=CARD)
        self._build_cloud(self._cloud_box)
        self._build_local(self._local_box)
        self._apply_asr_mode(v["asr_mode"].get())

    def _build_cloud(self, parent):
        v = self._var
        self._field(parent, "API Key", v["sauc_key"], show="*")
        tk.Label(parent, text="火山引擎语音技术 · 流式语音识别（SAUC），与 Cindy 同款",
                 bg=CARD, fg=TEXT_DIM, font=(FONT, F_SMALL)).pack(anchor="w", pady=(2, 0))

        self._adv_btn = tk.Label(parent, text="▸ 高级（端点 / 资源ID 已预填）", bg=CARD,
                                 fg=ACCENT, font=(FONT, F_SMALL), cursor="hand2")
        self._adv_btn.pack(anchor="w", pady=(10, 0))
        self._adv_btn.bind("<Button-1>", lambda e: self._toggle_adv())
        self._adv_visible = False
        self._adv_box = tk.Frame(parent, bg=CARD)
        self._field(self._adv_box, "端点", v["sauc_endpoint"])
        self._field(self._adv_box, "资源ID", v["sauc_resource"])
        self._adv_box.pack_forget()   # 确保默认折叠，避免初始即显示

    def _build_local(self, parent):
        v = self._var
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(4, 12))
        tk.Label(row, text="模型", bg=CARD, fg=TEXT, font=(FONT, F)).pack(side="left")
        seg = Segmented(row, [("small", "small 推荐"), ("base", "base 快")],
                        self._on_local_model, v["local_model"].get())
        seg.pack(side="right")
        self._seg_model = seg
        # 语言：圆角三选一（自动 / 中文 / 英语），比裸输入框直观
        row2 = tk.Frame(parent, bg=CARD)
        row2.pack(fill="x", pady=(4, 12))
        tk.Label(row2, text="语言", bg=CARD, fg=TEXT, font=(FONT, F)).pack(side="left")
        seg2 = Segmented(row2, [("auto", "自动"), ("zh", "中文"), ("en", "英语")],
                         self._on_local_lang, v["local_lang"].get())
        seg2.pack(side="right")
        self._seg_lang = seg2
        tk.Label(parent, text="首次使用会自动下载模型（small 约 460MB / base 约 140MB）",
                 bg=CARD, fg=TEXT_DIM, font=(FONT, F_SMALL)).pack(anchor="w", pady=(6, 0))

    # ---------- 润色 ----------
    def _build_refine(self, body):
        v = self._var
        self._field(body, "API Key", v["refine_key"], show="*")
        tk.Label(body, text="DeepSeek 开放平台 API Key（sk- 开头）",
                 bg=CARD, fg=TEXT_DIM, font=(FONT, F_SMALL)).pack(anchor="w", pady=(2, 0))

        self._refine_adv_btn = tk.Label(body, text="▸ 高级（Base URL / 模型 已预填）", bg=CARD,
                                        fg=ACCENT, font=(FONT, F_SMALL), cursor="hand2")
        self._refine_adv_btn.pack(anchor="w", pady=(10, 0))
        self._refine_adv_btn.bind("<Button-1>", lambda e: self._toggle_refine_adv())
        self._refine_adv_visible = False
        self._refine_adv_box = tk.Frame(body, bg=CARD)
        self._field(self._refine_adv_box, "Base URL", v["refine_base"])
        self._field(self._refine_adv_box, "模型", v["refine_model"])
        self._refine_adv_box.pack_forget()   # 确保默认折叠

    # ---------- 热键 ----------
    def _build_hotkey(self, body):
        v = self._var
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=(2, 8))
        tk.Label(row, text="热键", bg=CARD, fg=TEXT, font=(FONT, F)).pack(side="left")
        self._hotkey_entry = self._entry(row, v["hotkey"], width=10)
        self._hotkey_entry.pack(side="right")
        tk.Label(body, text="默认反引号（`），位于 Tab 上方",
                 bg=CARD, fg=TEXT_DIM, font=(FONT, F_SMALL)).pack(anchor="w", pady=(2, 10))

        row2 = tk.Frame(body, bg=CARD)
        row2.pack(fill="x")
        tk.Label(row2, text="触发方式", bg=CARD, fg=TEXT, font=(FONT, F)).pack(side="left")
        seg = Segmented(row2, [("hold", "按住说话"), ("toggle", "单击切换")],
                        self._on_trigger, v["trigger"].get())
        seg.pack(side="right")
        self._seg_trigger = seg

        row3 = tk.Frame(body, bg=CARD)
        row3.pack(fill="x", pady=(10, 0))
        tk.Label(row3, text="粘贴方式", bg=CARD, fg=TEXT, font=(FONT, F)).pack(side="left")
        seg2 = Segmented(row3, [("type", "逐字输入"), ("paste", "剪贴板")],
                         self._on_insert, v["insert"].get())
        seg2.pack(side="right")
        self._seg_insert = seg2
        tk.Label(body, text="逐字输入不污染剪贴板历史（默认）；个别窗口不兼容时切「剪贴板」",
                 bg=CARD, fg=TEXT_DIM, font=(FONT, F_SMALL)).pack(anchor="w", pady=(2, 10))

    # ================= 控件 =================
    def _entry(self, parent, var, width=None, show=None):
        e = tk.Entry(parent, textvariable=var, font=(FONT, F),
                     bg="#FFFFFF", fg=TEXT, insertbackground=TEXT,
                     relief="flat", highlightthickness=1, bd=0,
                     highlightbackground=BORDER_INPUT, highlightcolor=ACCENT,
                     width=width or 40, show=show or "")
        return e

    def _field(self, parent, label, var, show=None, width=40):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(5, 7))
        tk.Label(row, text=label, bg=CARD, fg=TEXT, font=(FONT, F),
                 width=9, anchor="w").pack(side="left")
        e = self._entry(row, var, width=width, show=show)
        e.pack(side="right", fill="x", expand=True)
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
            self._adv_box.pack(fill="x", pady=(4, 0))
            self._adv_btn.configure(text="▾ 高级（端点 / 资源ID 已预填）")
        else:
            self._adv_box.pack_forget()
            self._adv_btn.configure(text="▸ 高级（端点 / 资源ID 已预填）")
        self._refit()

    def _toggle_refine_adv(self):
        self._refine_adv_visible = not self._refine_adv_visible
        if self._refine_adv_visible:
            self._refine_adv_box.pack(fill="x", pady=(4, 0))
            self._refine_adv_btn.configure(text="▾ 高级（Base URL / 模型 已预填）")
        else:
            self._refine_adv_box.pack_forget()
            self._refine_adv_btn.configure(text="▸ 高级（Base URL / 模型 已预填）")
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