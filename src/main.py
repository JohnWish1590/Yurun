"""语润（Yurun）主程序：迷你浮窗 + 全局热键 + 录音→转写→润色→插入。

架构（修 RuntimeError: Calling Tcl from different apartment）：
- 主线程 = Tk root + after 循环：驱动 indicator / loading 动画、执行粘贴
- 托盘 pystray 在后台线程运行（set_icon 线程安全）
- 热键/录音/转写/润色在各自线程，通过队列把 UI 事件发给主线程

日志：%APPDATA%\\Yurun\\logs\\yurun.log
"""
import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 焦点锁定用（录音时把输入焦点固定在开始时的目标窗口）
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# DPI 锁死（防睡眠唤醒后系统 DPI 探测抖动导致 Tkinter 字体整体放大）：
# 进程启动后声明 System DPI Aware，Tk 内部 font scaling 不再跟随系统后续漂移。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # SYSTEM_AWARE
except Exception:
    pass

from config import get_config
from hotkey import get_hotkey
from logger import (
    get_logger,
    install_crash_handler,
    register_tk_error,
    log_startup_banner,
)
from pill import PillBubble
from gui import SettingsWindow  # 预导入：让 PyInstaller 在启动时即解开依赖，避免首次打开设置卡顿
import tray as tray_mod

# 以下重依赖（recorder/sounddevice、refiner/requests、sauc_asr/cloud_asr/websocket、
# transcriber/faster_whisper）全部改为"用到才 import"，云端模式启动零负担。

log = get_logger("yurun")

# 尽早安装全局崩溃捕获：后续任何 import 失败 / 子线程崩 / Tk 报错都写进日志，
# 用户把日志文件发回即可反馈问题。
install_crash_handler()
log_startup_banner()

APP_TITLE = "语润"


class App:
    def __init__(self):
        self.cfg = get_config()
        self.ui_q = queue.Queue()          # 子线程 → 主线程的 UI 事件
        self._quit = False
        self._rec_stop = None              # 当前录音 stop_event
        self._rec_thread = None
        self._paste_cb = None              # 等待粘贴的回调
        self._round_seq = 0                 # 每次按下热键 +1，用于作废过期的后台润色
        # pynput 纠错热键状态（Ctrl+反引号）
        self._kb = None
        self._kb_listener = None
        self._kb_ctrl = False
        self._kb_correct_fired = False
        # 焦点锁定（C1）：录音开始时锁定目标输入控件，切走自动抢回
        self._target_hwnd = None
        self._last_steal = 0.0

        self.root = tk.Tk()
        self.root.withdraw()
        # 锁死 font scaling，避免跨睡眠唤醒时 Tk 内部 scaling 被抬高导致字体放大。
        try:
            self.root.tk.call('tk', 'scaling', 1.0)
        except Exception:
            pass
        # Tk 回调异常也写进日志（崩溃可反馈）
        register_tk_error(self.root)
        self.indicator = PillBubble(self.root)

        self.tray = tray_mod.Tray(
            on_quit=self._on_quit,
            on_open_settings=self._open_settings,
        )
        tray_mod._tray_instance = self.tray

        # 热键
        self.hotkey = get_hotkey()
        self.hotkey.on_hold_start = self._on_hold_start
        self.hotkey.on_hold_end = self._on_hold_end
        self.hotkey.on_error = lambda m: self.ui_q.put(("toast", m))
        self.hotkey.on_toggle = self._on_toggle

    # ================= UI 事件泵 =================
    def pump(self):
        """主线程每 40ms：处理队列 + 驱动动画。"""
        try:
            while True:
                evt = self.ui_q.get_nowait()
                self._handle_ui(evt)
        except queue.Empty:
            pass
        # 焦点锁定：录音/输入期间若焦点被切走，抢回录音开始时的目标窗口
        # （用户要"固定产生文字的框"——字必须打进开始录音时所在的输入框）
        if self._target_hwnd and not self._quit:
            now = time.time()
            if now - self._last_steal > 0.4:
                try:
                    if _user32.GetForegroundWindow() != self._target_hwnd:
                        if self._steal_focus(self._target_hwnd):
                            self._last_steal = now
                except Exception:
                    pass
        self.indicator._tick()
        if not self._quit:
            self.root.after(40, self.pump)

    def _handle_ui(self, evt):
        kind = evt[0]
        try:
            if kind == "guide":
                self.indicator.show_guide("开始录音")
            elif kind == "recording":
                # 按下热键：显示「正在录音」+ 红点呼吸
                self.indicator.start_recording()
            elif kind == "transcribing":
                # 松手后、ASR 等待期间：显示「正在识别」（苹果蓝麦克风），
                # 不再误显「正在录音」，避免"框凭空跳出来"的错觉。
                self.indicator.show_transcribing()
            elif kind == "refining":
                self.indicator.show_refining()
            elif kind in ("done", "fallback"):
                # 完成：直接隐藏 + 粘贴，pill 不显示预览文字；释放焦点锁定
                self._target_hwnd = None
                self.indicator.force_idle()
            elif kind == "error":
                # 错误结束：释放焦点锁定
                self._target_hwnd = None
                self.indicator.show_error(evt[1] if len(evt) > 1 else "出错了")
            elif kind == "toast":
                self.indicator.show_error(evt[1])
            elif kind == "show_correction":
                self._show_correction_dialog()
            elif kind == "model_loading":
                pass  # 仅本地离线模式触发，不打扰
            elif kind in ("model_ready", "model_error"):
                # 模型加载完成/失败都直接隐藏，不在 pill 里显示文字
                self.indicator.force_idle()
            elif kind == "type_partial":
                self._do_type(evt[1])
            elif kind == "paste":
                self._do_paste(evt[1], hide=(evt[2] if len(evt) > 2 else True))
            elif kind == "replace_paste":
                # 方案B：润色完成走 ("paste", final, True)，不再产生 replace_paste 事件。
                # 此分支保留仅作防御；若意外触发，按原 replace 语义处理。
                self._do_paste(evt[1], hide=True, replace=True)
        except Exception as e:
            log.exception("UI 事件处理失败: %s", e)

    # ================= 加载模型（仅本地离线模式） =================
    # 云端 SAUC 模式不会走到这里；本地模式用迷你浮窗提示，不再弹独立加载窗。
    def _load_model_async(self):
        def job():
            from transcriber import get_transcriber
            cfg = get_config()
            tr = get_transcriber()
            self.ui_q.put(("model_loading", None))
            ok = tr.load(cfg.get("whisper_model", "base"))
            if ok:
                log.info("模型加载完成: %s", cfg.get("whisper_model"))
                self.ui_q.put(("model_ready", None))
            else:
                log.error("模型加载失败: %s", tr._load_error)
                self.ui_q.put(("model_error", tr._load_error or "未知错误"))
        threading.Thread(target=job, daemon=True).start()

    # ================= 热键 =================
    def _get_target_hwnd(self):
        """录音开始时的目标输入控件：GetGUIThreadInfo.hwndFocus 优先，fallback 前台窗口。"""
        try:
            fg = _user32.GetForegroundWindow()
            if not fg:
                return None
            tid = _user32.GetWindowThreadProcessId(fg, None)
            from pill import _GUITHREADINFO
            info = _GUITHREADINFO()
            info.cbSize = ctypes.sizeof(info)
            if _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
                hwnd = info.hwndFocus or info.hwndCaret or fg
            else:
                hwnd = fg
            return hwnd
        except Exception:
            return None

    def _steal_focus(self, hwnd):
        """把前台焦点抢回目标窗口：AttachThreadInput + Alt key trick 绕过前台锁定。"""
        try:
            fg = _user32.GetForegroundWindow()
            if fg == hwnd:
                return True
            cur_tid = _kernel32.GetCurrentThreadId()
            fg_tid = _user32.GetWindowThreadProcessId(fg, None)
            if cur_tid != fg_tid:
                _user32.AttachThreadInput(cur_tid, fg_tid, True)
            _user32.keybd_event(0x12, 0, 0, 0)   # Alt down（绕过 SetForegroundWindow 前台限制）
            _user32.SetForegroundWindow(hwnd)
            _user32.keybd_event(0x12, 0, 2, 0)   # Alt up
            if cur_tid != fg_tid:
                _user32.AttachThreadInput(cur_tid, fg_tid, False)
            return True
        except Exception:
            return False

    def _on_hold_start(self, _key):
        log.info("热键按下，开始录音")
        # 焦点锁定（C1）：记录开始时的目标输入控件；录音/输入期间焦点被切走会抢回
        self._target_hwnd = self._get_target_hwnd()
        log.info("锁定目标窗口 hwnd=%s", self._target_hwnd)
        # 不再拦截"已有录音进行中"：允许前一句还在识别/润色时按下热键录下一句
        # （重叠录音）。hold 模式物理上同一键按住中不会再触发 WM_HOTKEY，
        # 所以这里每次按下都是独立的一次录音，配独立临时文件互不干扰。
        stop = threading.Event()
        self._rec_stop = stop
        cfg = get_config()
        if cfg.get("asr_provider") == "sauc":
            # 真流式：边录边发（ws race 已在 sauc_asr 内修好，单线程发完再收）
            self._rec_thread = threading.Thread(
                target=self._record_job_sauc, args=(stop, cfg), daemon=True)
        else:
            # 云端 HTTP / 本地 Whisper：仍需整段 wav
            self._rec_thread = threading.Thread(
                target=self._record_job, args=(stop,), daemon=True)
        self._round_seq += 1
        self._rec_thread.start()
        self.ui_q.put(("recording", None))

    def _on_hold_end(self, _key):
        log.info("热键松开，停止录音")
        if self._rec_stop is not None:
            self._rec_stop.set()
            self._rec_stop = None

    def _on_toggle(self, _key, pressed):
        if pressed:
            self._on_hold_start(_key)
        else:
            self._on_hold_end(_key)

    def _on_correct_key(self, _key):
        """纠错热键（Ctrl+反引号）触发：转主线程弹「错误纠正」框。"""
        log.info("纠错热键触发")
        self.ui_q.put(("show_correction", None))

    def _pill_anchor(self):
        """返回 pill 气泡当前屏幕矩形 (l, t, r, b)；拿不到返回 None。"""
        try:
            geom = self.indicator.win.geometry()  # "132x50+X+Y"
            size, _, xy = geom.partition("+")
            if not xy or "x" not in size:
                return None
            w_s, h_s = size.split("x")
            x_s, y_s = xy.split("+")
            x, y, w, h = int(x_s), int(y_s), int(w_s), int(h_s)
            return x, y, x + w, y + h
        except Exception:
            return None

    def _clipboard_backup(self):
        """读当前剪贴板文本（用户原内容），失败返回 None。"""
        try:
            return self.root.clipboard_get()
        except Exception:
            return None

    def _clipboard_restore(self, backup):
        """把备份的原剪贴板内容写回去，避免污染用户剪贴板（方案A）。"""
        if backup is None:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(backup)
            self.root.update()
        except Exception:
            pass

    def _send_ctrl_c(self):
        """向当前焦点窗口发 Ctrl+C，把选中文本送入剪贴板。"""
        try:
            import pyautogui
            pyautogui.hotkey("ctrl", "c")
        except Exception as e:
            log.warning("自动 Ctrl+C 失败: %s", e)

    def _kb_on_press(self, key):
        """pynput 钩子（后台线程）：Ctrl+反引号 → 纠错弹窗。防重复触发。

        用 vk（虚拟键码 0xC0）判断反引号键：char 受键盘布局/输入法影响
        （中文输入法下反引号键 char 可能是「·」而非「`」），vk 恒定可靠。
        """
        try:
            kb = self._kb
            if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
                self._kb_ctrl = True
                return
            if self._kb_ctrl and not self._kb_correct_fired:
                if getattr(key, "vk", None) == 0xC0:
                    self._kb_correct_fired = True
                    self._on_correct_key(None)
        except Exception:
            pass

    def _kb_on_release(self, key):
        try:
            kb = self._kb
            if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
                self._kb_ctrl = False
            elif getattr(key, "vk", None) == 0xC0:
                self._kb_correct_fired = False
        except Exception:
            pass

    # ================= 纠错弹窗（词库学习入口） =================
    def _show_correction_dialog(self):
        """「错误纠正」弹窗：macOS/Apple 浅色风格（稳定 pack 布局版）。

        位置：贴 pill 气泡正上方 12px（水平居中于 pill）。B1 识别文本自动读
        选中文字（方案A：备份剪贴板 → Ctrl+C → 读选中 → 恢复剪贴板），打开时
        聚焦"正确写法"。
        """
        from gui import TEXT, TEXT_DIM, ACCENT, FONT, PillButton
        from pill import TRANSPARENT, _round_rect_items

        try:
            from dictionary import add_entry
        except Exception as e:
            log.error("词库模块加载失败: %s", e)
            self.ui_q.put(("error", "词库失败"))
            return

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-toolwindow", True)
        except Exception:
            pass
        win.configure(bg=TRANSPARENT)
        win.attributes("-transparentcolor", TRANSPARENT)

        W2 = 560                  # 弹窗宽度（放大版，给中文留余量）
        PAD = 28                  # 内容区内边距
        BODY_W = W2 - PAD * 2

        wrong_var = tk.StringVar()
        correct_var = tk.StringVar()
        status_var = tk.StringVar()

        # B1+方案A：自动复制选中（备份剪贴板 → Ctrl+C → 读选中 → 恢复剪贴板）
        # 此时弹窗仍 withdraw、焦点在外部 app，Ctrl+C 才能取到选中文字
        try:
            _backup = self._clipboard_backup()
            self._send_ctrl_c()
            time.sleep(0.08)
            _sel = self.root.clipboard_get()
        except Exception:
            _sel = ""
        finally:
            self._clipboard_restore(_backup)
        if _sel and _sel.strip():
            wrong_var.set(_sel.strip()[:120])

        # 圆角白底：canvas 用 place 铺满窗口作背景画圆角白底，body 不透明白底内缩
        # 8px 浮在上层。中间完全不透明遮住背后内容，外圈 8px 露出 canvas 圆角白底
        # 形成圆角边框（对齐 pill.py 写法）。
        canvas = tk.Canvas(win, bg=TRANSPARENT, highlightthickness=0, bd=0)
        canvas.place(x=0, y=0, relwidth=1, relheight=1)

        body = tk.Frame(win, bg="#FFFFFF")
        # body 不透明白底、内缩 8px 浮在 canvas 圆角白底之上：
        # 中间完全不透、遮住背后文字；外圈 8px 露出 canvas 圆角白底形成圆角边框。
        body.place(x=8, y=8, width=W2 - 16, height=10)

        # 窗口尺寸定好后画圆角白底（铺满，四角透明）
        def _draw_bg():
            w = win.winfo_width()
            h = win.winfo_height()
            if w <= 1 or h <= 1:
                win.after(10, _draw_bg)
                return
            canvas.configure(width=w, height=h)
            _round_rect_items(canvas, 0, 0, w, h, 18, "#FFFFFF")
            # 把 canvas 降到 body 之下。tk.Canvas.lower 是 tag_lower 别名（必须带
            # tagOrId），无参会 TclError；用 widget 级 lower 命令绕过别名。
            canvas.tk.call('lower', canvas._w)

        win.after(20, _draw_bg)

        # ===== Header（兼作拖动把手：overrideredirect 无标题栏，需手动绑拖拽）=====
        header = tk.Frame(body, bg="#FFFFFF", cursor="fleur")
        header.pack(fill="x", padx=PAD, pady=(PAD, 0))

        def _drag_start(e):
            win._drag_x = e.x_root - win.winfo_x()
            win._drag_y = e.y_root - win.winfo_y()

        def _drag_move(e):
            win.geometry(f"+{e.x_root - win._drag_x}+{e.y_root - win._drag_y}")

        header.bind("<ButtonPress-1>", _drag_start)
        header.bind("<B1-Motion>", _drag_move)

        tk.Label(header, text="错误纠正", bg="#FFFFFF", fg=TEXT,
                 font=FONT(24, "bold")).pack(anchor="w", pady=(0, 8))
        tk.Label(header, text="发现识别结果有误？在这里修正并加入词库",
                 bg="#FFFFFF", fg=TEXT_DIM, font=FONT(16)).pack(anchor="w", pady=(0, 22))

        # ===== 识别文本 =====
        tk.Label(body, text="识别文本", bg="#FFFFFF", fg="#333333",
                 font=FONT(15, "bold")).pack(anchor="w", padx=PAD, pady=(0, 8))
        wrong_lbl = tk.Label(body, textvariable=wrong_var, bg="#FAFAFC", fg=TEXT,
                             font=FONT(17), wraplength=BODY_W - 32, justify="left",
                             anchor="nw", padx=16, pady=16)
        wrong_lbl.pack(fill="x", padx=PAD, pady=(0, 22))

        # ===== 正确写法 =====
        tk.Label(body, text="正确写法", bg="#FFFFFF", fg="#333333",
                 font=FONT(15, "bold")).pack(anchor="w", padx=PAD, pady=(0, 8))
        e2 = tk.Entry(body, textvariable=correct_var, bg="#FFFFFF", fg=TEXT,
                      insertbackground=TEXT, relief="flat",
                      font=FONT(17), highlightthickness=1,
                      highlightbackground="#E0E0E0", highlightcolor=ACCENT, bd=0)
        e2.pack(fill="x", padx=PAD, pady=(0, 28), ipady=16)

        # ===== 状态 + 按钮 =====
        def _confirm():
            correct = correct_var.get().strip()
            wrong = wrong_var.get().strip()
            if not correct:
                status_var.set("正确写法不能为空")
                return
            try:
                entry = add_entry(correct, wrong, source="auto")
                log.info("词库新增: correct=%r wrong=%r count=%s",
                         correct, wrong, entry.get("count"))
            except Exception as ex:
                log.error("存入词库失败: %s", ex)
                status_var.set("存入失败")
                return
            status_var.set(f"已存入词库：{correct}")
            win.after(1200, win.destroy)

        footer = tk.Frame(body, bg="#FFFFFF")
        footer.pack(fill="x", padx=PAD, pady=(0, PAD))
        status = tk.Label(footer, textvariable=status_var, bg="#FFFFFF", fg="#34C759",
                          font=FONT(16))
        status.pack(side="left")

        # 占位 spacer 把按钮推到右侧；pack 顺序：先 right 的会后出现，因此先 pack 存入词库（最右），再 pack 取消（左侧）
        tk.Frame(footer, bg="#FFFFFF").pack(side="left", fill="x", expand=True)
        PillButton(footer, "存入词库", _confirm, primary=True, min_w=110).pack(side="right")
        PillButton(footer, "取消", lambda: win.destroy(), primary=False,
                   weight="normal", min_w=90).pack(side="right", padx=(0, 12))

        # 位置计算必须在 body 渲染后
        def _place():
            win.update_idletasks()
            H2 = body.winfo_reqheight() + 16   # 上下各 8px 内缩，给圆角白底边框
            rect = self._pill_anchor()
            if rect:
                pl, pt, pr, pb = rect
                pill_cx = (pl + pr) // 2
                x = pill_cx - W2 // 2
                y = pt - H2 - 12
            else:
                sw = win.winfo_screenwidth()
                sh = win.winfo_screenheight()
                x = (sw - W2) // 2
                y = max(60, (sh - H2) // 3)
            y = max(8, y)
            win.geometry(f"{W2}x{H2}+{x}+{y}")
            body.place_configure(width=W2 - 16, height=H2 - 16)
            _draw_bg()

        win.after(30, _place)

        # 打开时聚焦正确写法
        win.after(80, lambda: e2.focus_set())

        e2.bind("<Return>", lambda _e: _confirm())
        win.bind("<Escape>", lambda _e: win.destroy())
        win.deiconify()
        win.lift()

    # ================= 录音→转写→润色→粘贴 =================
    def _record_job(self, stop):
        cfg = get_config()
        # 统一路径：先录 wav 文件，再调 _transcribe 转写。
        # sauc/cloud/local 都走这条（_transcribe 内部按 provider 分派），
        # 避免真流式双线程 ws race 导致 indicator 卡死。
        # 每次录音用独立临时文件，支持"前句润色中按热键录下一句"的重叠场景，
        # 多个录音线程互不抢同一个 wav。
        import tempfile
        fd, tmp_wav = tempfile.mkstemp(suffix=".wav", prefix="yurun_")
        os.close(fd)
        try:
            from recorder import record_to_file
            log.info("录音开始 provider=%s tmp=%s", cfg.get("asr_provider", "sauc"), tmp_wav)
            ok, dur, err = record_to_file(
                tmp_wav, stop_event=stop,
                max_seconds=90, silence_timeout=0.0,
                on_level=self._on_level,
            )
            if not ok or dur < 0.3:
                log.warning("录音无效: ok=%s dur=%.2f err=%s", ok, dur, err)
                self.ui_q.put(("error", "识别失败"))
                return
            log.info("录音完成: %.2fs", dur)

            self.ui_q.put(("transcribing", None))
            try:
                log.info("进入 _transcribe provider=%s wav=%s", cfg.get("asr_provider", "sauc"), tmp_wav)
                t_asr0 = time.time()
                text = self._transcribe(tmp_wav, cfg)
                log.info("_transcribe 返回 text=%r 识别耗时=%.2fs", text, time.time() - t_asr0)
            except Exception as e:
                log.error("识别失败: %s", e)
                self.ui_q.put(("error", "识别失败"))
                return
            self._after_transcribe(text, round_id=self._round_seq)
        except Exception as e:
            log.error("录音异常: %s", e)
            self.ui_q.put(("error", "录音失败"))
            return
        finally:
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    # _record_job_sauc / sauc_transcribe_stream / record_chunks：真流式实现。
    # 原"双线程 ws race"已通过在 sauc_transcribe_stream 内改为单线程
    # "边录边发→发完再收"规避（见 sauc_asr.py），现由 _on_hold_start 在 sauc 模式下启用。

    def _record_job_sauc(self, stop, cfg):
        """SAUC 真流式分支：录音生成器边产出 PCM 边发往 WebSocket，并发收结果。"""
        try:
            from recorder import record_chunks
            from sauc_asr import sauc_transcribe_stream
            from dictionary import to_hotwords
        except Exception as e:
            log.error("导入 SAUC 流式模块失败: %s", e)
            self.ui_q.put(("error", "模块失败"))
            return
        round_id = self._round_seq
        self.ui_q.put(("transcribing", None))
        try:
            t_asr0 = time.time()
            text = sauc_transcribe_stream(
                record_chunks(stop, on_level=self._on_level, max_seconds=90),
                api_key=cfg.get("asr_sauc_key"),
                resource_id=cfg.get("asr_sauc_resource_id"),
                endpoint=cfg.get("asr_sauc_endpoint"),
                language=cfg.get("language", "auto"),
                proxy=cfg.get("proxy", ""),
                hotwords=to_hotwords(),
            )
            log.info("SAUC 识别耗时=%.2fs", time.time() - t_asr0)
        except Exception as e:
            log.error("识别失败: %s", e)
            self.ui_q.put(("error", "识别失败"))
            return
        self._after_transcribe(text, round_id=round_id)

    def _after_transcribe(self, text, round_id=None):
        """识别成功后的共用收尾。

        设计目标：原文立刻贴出（最低延迟），且仅在「润色真可能改动」时才显示
        「正在润色」并等待后台结果；其余情况（太短跳过 / 未配置 / 模型大概率
        返回 no_change）直接收尾隐藏，避免图标空挂 5s 的误导观感。
        """
        log.info("识别结果: %s", text)
        if not text or not text.strip():
            self.ui_q.put(("error", "没识别到"))
            return

        # ASR（火山 SAUC）自带标点预测，纯数字/手机号/订单号常被补末尾句号
        # （如「12345。」）。数字为主的文本先剥掉末尾标点再进润色/bypass 决策，
        # 否则免润色直接贴原文会把句号一起贴出来；正常句子数字占比低不受影响。
        from refiner import strip_numeric_trailing_punct
        text = strip_numeric_trailing_punct(text)
        # 词库本地替换（bypass 兜底）：错误变体命中即换成正确词（如「天气log」→changelog）。
        # 只对免润色短句生效；LLM 路径有词典 + prompt 双重处理。
        from dictionary import apply_local_replace
        text = apply_local_replace(text)

        if self._refine_will_change(text):
            # 方案B：不先贴原文，显示「正在润色」并后台润色，完成后一次性贴最终文本。
            # 无 replace 步骤 → 从根上避免误删/误覆盖输入框里之前的内容。
            self.ui_q.put(("refining", None))
            threading.Thread(
                target=self._refine_and_paste, args=(text, round_id), daemon=True
            ).start()
        else:
            # 不会改动（太短/未配置/无自定义指令且较短）：原文即最终结果，立即贴出+收尾。
            self.ui_q.put(("paste", text, True))

    def _refine_will_change(self, text) -> bool:
        """预估这次润色是否可能产生改动（用于决定是否显示「正在润色」）。

        返回 False 的情形：润色未启用 / 未配 key（no_api_key）、短句无自定义指令
        （bypass_short）。这些走原文本、不等 LLM，pill 立即收尾。
        """
        cfg = get_config()
        if not cfg.get("refine_enabled", True) or not cfg.get("api_key"):
            return False
        custom = cfg.get("custom_instructions", "") or ""
        if not custom:
            # 与 refiner._should_bypass_llm 同阈值：≤15 有效字符跳过
            from refiner import content_length, BYPASS_MAX_LENGTH
            if content_length(text) <= BYPASS_MAX_LENGTH:
                return False
        return True

    def _refine_and_paste(self, text, round_id):
        """后台润色并粘贴。

        优先走流式（边润边贴，首字即上屏）；流式关闭或非 type 插入时回退整段 refine_text。
        无 replace 步骤。
        """
        # 若已有更新的录音轮次，旧润色作废，避免回插覆盖新内容。
        if round_id is not None and round_id != self._round_seq:
            log.info("润色轮次过期（已有新录音），跳过粘贴")
            self.ui_q.put(("done", text))
            return
        cfg = get_config()
        use_stream = cfg.get("refine_streaming", True) and (cfg.get("insert_method") or "type") == "type"
        if use_stream:
            self._refine_stream_and_paste(text, round_id)
            return
        # 整段路径（原方案B）
        t_rf0 = time.time()
        result = self._refine(text)
        log.info("润色耗时=%.2fs ok=%s reason=%s", time.time() - t_rf0, result["ok"], result.get("reason"))
        final = result["text"] if result["ok"] else text
        # LLM 输出兜底：万一模型没遵守「数字不补句号」规则，同样剥掉。
        from refiner import strip_numeric_trailing_punct
        final = strip_numeric_trailing_punct(final)
        self.ui_q.put(("paste", final, True))

    def _refine_stream_and_paste(self, text, round_id):
        """流式润色：边收 delta 边逐段 SendInput，首字即上屏；首字前失败回退整段。"""
        from refiner import refine_stream
        from dictionary import to_llm_text
        cfg = get_config()
        t_rf0 = time.time()

        def on_delta(seg):
            # 过期则不再投递新分片（已贴部分保留，但不继续覆盖新内容）
            if round_id is not None and round_id != self._round_seq:
                return
            self.ui_q.put(("type_partial", seg))

        try:
            result = refine_stream(
                text=text,
                api_key=cfg.get("api_key"),
                api_base=cfg.get("api_base"),
                model=cfg.get("api_model"),
                custom_instructions=cfg.get("custom_instructions", ""),
                user_dictionary=to_llm_text(),
                language=cfg.get("language", "zh"),
                proxy=cfg.get("proxy", ""),
                on_delta=on_delta,
            )
        except Exception as e:
            log.error("流式润色异常: %s", e)
            result = {"ok": False, "text": text, "reason": "exception"}

        log.info("流式润色耗时=%.2fs ok=%s reason=%s", time.time() - t_rf0, result["ok"], result.get("reason"))

        if result["ok"]:
            # 已通过 on_delta 逐段贴出（完整或部分），直接收尾。
            self.ui_q.put(("done", text))
        else:
            # 首字前失败：回退整段润色（此时尚未贴任何字，安全）。
            fallback = self._refine(text)
            final = fallback["text"] if fallback["ok"] else text
            from refiner import strip_numeric_trailing_punct
            final = strip_numeric_trailing_punct(final)
            self.ui_q.put(("paste", final, True))

    def _transcribe(self, wav_path, cfg):
        """按配置选择识别引擎：cloud=云端 ASR / local=本地 Whisper。"""
        provider = cfg.get("asr_provider", "cloud")
        if provider == "sauc":
            from sauc_asr import sauc_transcribe
            return sauc_transcribe(
                wav_path,
                api_key=cfg.get("asr_sauc_key"),
                resource_id=cfg.get("asr_sauc_resource_id"),
                endpoint=cfg.get("asr_sauc_endpoint"),
                language=cfg.get("language", "auto"),
                proxy=cfg.get("proxy", ""),
            )
        if provider == "cloud":
            from cloud_asr import cloud_transcribe
            return cloud_transcribe(
                wav_path,
                api_key=cfg.get("asr_key"),
                api_base=cfg.get("asr_base_url"),
                model=cfg.get("asr_model"),
                language=cfg.get("language", "auto"),
                proxy=cfg.get("proxy", ""),
            )
        # 本地
        from transcriber import get_transcriber
        return get_transcriber().transcribe(
            wav_path,
            language=cfg.get("language", "auto"),
            beam_size=3,
        )

    def _on_level(self, level):
        self.indicator.set_level(level)

    def _refine(self, text):
        cfg = get_config()
        if not cfg.get("refine_enabled", True) or not cfg.get("api_key"):
            return {"ok": False, "text": text, "reason": "no_api_key"}
        try:
            from refiner import refine_text
            from dictionary import to_llm_text
            return refine_text(
                text=text,
                api_key=cfg.get("api_key"),
                api_base=cfg.get("api_base"),
                model=cfg.get("api_model"),
                custom_instructions=cfg.get("custom_instructions", ""),
                user_dictionary=to_llm_text(),
                language=cfg.get("language", "zh"),
                proxy=cfg.get("proxy", ""),
            )
        except Exception as e:
            log.error("润色调用异常: %s", e)
            return {"ok": False, "text": text, "reason": "exception"}

    def _do_type(self, text):
        """主线程：SendInput 逐字输入一段文本（流式分片），不隐藏 pill、不发 done。

        用 char_interval 逐字投递，模拟人打字节奏——即使模型生成很快，文字也按固定节奏
        逐字冒出，而不是整段瞬间蹦出（用户要的「像打字那样跳出来」）。
        """
        try:
            from typer import type_text
            type_text(text, char_interval=0.04)
        except Exception as e:
            log.warning("流式 SendInput 输入失败: %s", e)

    def _do_paste(self, text, hide=True, replace=False):
        """主线程：把 text 送进当前焦点窗口。

        - hide=True：粘贴后隐藏 pill（终态）。
        - replace=True（replace_paste 事件）：先 Ctrl+Z 撤销刚贴的原文，再 Ctrl+V
          粘贴润色版，确保是「替换」而非「追加」，绝不重复。
        - hide=False：先贴原文再后台润色，保留「正在润色」指示。

        按 config 的 insert_method 分流：
        - type（默认）：SendInput Unicode 逐字输入，不碰剪贴板，Win+V 历史零污染。
        - paste：写剪贴板 + Ctrl+V（原路径，会污染剪贴板历史，作兜底）。
        """
        cfg = get_config()
        method = (cfg.get("insert_method") or "type").lower()
        if method == "type" and not replace:
            # 主路径：SendInput 逐字 Unicode 输入，不碰剪贴板
            try:
                from typer import type_text
                # 短暂停顿让目标窗口焦点稳定
                time.sleep(0.02)
                sent = type_text(text)
                log.info("SendInput 已投递 %d 字（type 模式，零剪贴板污染）", sent)
                if hide:
                    self.ui_q.put(("done", text))
                return
            except Exception as e:
                log.warning("SendInput 输入失败: %s，回退剪贴板粘贴", e)
                # 落到下面的 paste 路径作兜底

        # paste 路径（兜底或用户显式选择）：写剪贴板 + Ctrl+V
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
            log.info("剪贴板已写入，准备粘贴")
        except Exception as e:
            log.error("剪贴板写入失败: %s", e)
        try:
            import pyautogui
            if replace:
                # 撤销刚粘贴的原文（Ctrl+Z），再粘贴润色版 → 原地替换
                pyautogui.hotkey("ctrl", "z")
                time.sleep(0.03)
                pyautogui.hotkey("ctrl", "v")
                log.info("Ctrl+Z+Ctr+V 已发送（替换原文）")
            else:
                pyautogui.hotkey("ctrl", "v")
                log.info("Ctrl+V 已发送")
            if hide:
                self.ui_q.put(("done", text))
        except Exception as e:
            log.warning("pyautogui 失败: %s", e)
            try:
                import subprocess
                ks = "^z^v" if replace else "^v"
                subprocess.Popen(["powershell", "-Command",
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "[System.Windows.Forms.SendKeys]::SendWait('" + ks + "')"])
                self.ui_q.put(("done", text))
            except Exception as e2:
                log.error("备用粘贴失败: %s", e2)
                self.ui_q.put(("error", "粘贴失败"))

    # ================= 托盘 =================
    def _on_quit(self):
        log.info("用户退出")
        self._quit = True
        try:
            self.hotkey.stop()
        except Exception:
            pass
        try:
            self.tray.stop()
        except Exception:
            pass
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            os._exit(0)

    def _open_settings(self):
        # Tkinter 非线程安全：托盘运行在后台线程，必须用 after(0) 切回主线程操作 Tk
        try:
            self.root.after(0, self._open_settings_ui)
        except Exception as e:
            log.error("打开设置失败: %s", e)

    def _open_settings_ui(self):
        try:
            SettingsWindow(master=self.root)
        except Exception as e:
            log.error("打开设置失败: %s", e)

    # ================= 启动 =================
    def _warmup(self):
        """后台预热重依赖，消灭首次按键的冷加载卡顿。"""
        try:
            import numpy, sounddevice, soundfile  # noqa
            import websocket  # noqa
            import pyautogui  # noqa
            from recorder import record_to_file, record_chunks  # noqa
            from sauc_asr import sauc_transcribe, sauc_transcribe_stream  # noqa
            from cloud_asr import cloud_transcribe  # noqa
            from refiner import refine_text  # noqa
            log.info("依赖预热完成（首次按键不再冷加载）")
        except Exception as e:
            log.warning("预热部分依赖失败，将在首次使用时按需加载: %s", e)

    def run(self):
        # 单实例锁：杀掉旧实例并接管，保证永远只有一个进程(也防僵尸占热键)
        try:
            from singleinstance import kill_old_and_takeover, kill_other_yurun_exe
            kill_old_and_takeover()   # PID 文件法：覆盖 dev 模式 + 已知旧 PID
            kill_other_yurun_exe()     # 进程名枚举法：兜底清理无 PID 文件的旧 exe 僵尸
        except Exception as e:
            log.warning("单实例检查失败: %s", e)
        log.info("语润启动（开发版）")
        # 仅本地离线模式才在启动时加载 Whisper 模型；云端 SAUC 用户无需等待/下载
        if self.cfg.get("asr_provider") == "local":
            self._load_model_async()
        else:
            log.info("识别引擎为 %s，跳过本地模型加载", self.cfg.get("asr_provider"))
        ok = self.hotkey.start(self.cfg.get("hotkey"), self.cfg.get("trigger_mode", "hold"))
        if not ok:
            self.ui_q.put(("toast", "热键无效"))
        # 纠错热键：Ctrl+反引号 —— 用 pynput 全局键盘钩子监听（不依赖 RegisterHotKey：
        # v0.1.17 实测第二热键注册失败且真实错误码被掩盖，pynput 钩子稳定可控；
        # 且实测无修饰反引号热键在带 Ctrl 时不会误触发录音，两键位共存安全）
        try:
            from pynput import keyboard as _kb
            self._kb = _kb
            self._kb_listener = _kb.Listener(
                on_press=self._kb_on_press, on_release=self._kb_on_release)
            self._kb_listener.daemon = True
            self._kb_listener.start()
            log.info("纠错热键监听已启动: Ctrl+`（pynput）")
        except Exception as e:
            log.warning("pynput 纠错热键监听启动失败: %s", e)
        # 启动托盘（后台线程）
        threading.Thread(target=self.tray.start, daemon=True).start()
        # 后台预热重依赖，避免首次按下热键才现场加载 numpy/sounddevice/websocket/pyautogui
        threading.Thread(target=self._warmup, daemon=True).start()
        # 主循环（不再弹首次启动引导气泡，避免文字显示不全的干扰）
        self.root.after(40, self.pump)
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()