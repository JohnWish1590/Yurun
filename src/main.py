"""语润（Yurun）主程序：迷你浮窗 + 全局热键 + 录音→转写→润色→插入。

架构（修 RuntimeError: Calling Tcl from different apartment）：
- 主线程 = Tk root + after 循环：驱动 indicator / loading 动画、执行粘贴
- 托盘 pystray 在后台线程运行（set_icon 线程安全）
- 热键/录音/转写/润色在各自线程，通过队列把 UI 事件发给主线程

日志：%APPDATA%\\Yurun\\logs\\yurun.log
"""
import ctypes
from collections import deque
import json
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

from config import get_config
from hotkey import get_hotkey
from voice_session import VoiceSession
from logger import (
    get_logger,
    install_crash_handler,
    logs_dir,
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
        # 每次语音拥有独立上下文。UI 只呈现最新一轮，旧轮次的迟到事件被安全丢弃，
        # 绝不把旧句子送进新一轮目标窗口。
        self._sessions = {}
        self._active_session_id = None
        self._recording_session_id = None
        # 可选高权限输入助手。存在时只由助手接收主热键和执行输入；
        # 不可用时完整回退到当前单进程热键路径。
        self.privileged_bridge = None
        # pynput 纠错热键状态（Ctrl+反引号）
        self._kb = None
        self._kb_listener = None
        self._kb_ctrl = False
        self._kb_correct_fired = False
        # 焦点锁定（C1）：录音开始时锁定目标输入控件，切走自动抢回
        self._target_hwnd = None
        self._last_steal = 0.0
        # Phase 1 验证浮窗：独立 Toplevel，实时显示 SAUC Partial（不抢焦点/不 SendInput/不碰 TSF）。
        self._partial_win = None
        self._partial_lbl = None
        # 浮窗逐字吸走动画：以“已经确认送入目标程序”的字符数为节拍，而非以
        # 猜测的毫秒倒计时为节拍。高权限输入助手逐字 IPC 较慢时，这能避免浮窗
        # 提前一大段消失；同时允许它以很小、渐进的领先量先于真实输入收尾。
        self._partial_finishing = False
        self._partial_finish_id = None
        self._draining = False
        self._drain_interval = 26
        self._preview_total_chars = 0
        self._preview_input_total = 0
        self._preview_sent_chars = 0
        self._preview_removed_chars = 0
        self._preview_lead_chars = 0
        # 异步打字：每句话拥有独立队列项。这样上一句尚在输出时开始下一句，
        # 也不会把两句字符混进同一个缓冲区或错误目标窗口。
        self._type_jobs = deque()
        self._type_job = None
        self._typing = False
        self._type_interval_ms = 40
        # Phase 0：仅记录插入体验，复用既有 round_id，不改变 _round_seq 的生命周期。
        self._keyup_times = {}
        self._insert_metrics = {}
        self._streaming_rounds = set()
        self._stream_complete_rounds = set()

        self.root = tk.Tk()
        self.root.withdraw()
        # 设置窗口通过这个引用安全地请求热键重新注册；不需要全局变量。
        self.root._yurun_app = self
        # DPI 缩放锁：捕获启动时的系统 font scaling（即 v1.0 原字号基准），
        # 睡眠唤醒/显示器变化导致 Tk 内部 scaling 漂移时还原回原值，字体不再整体放大或缩小。
        try:
            self._orig_scaling = float(self.root.tk.call('tk', 'scaling'))
        except Exception:
            self._orig_scaling = None
        self._watch_dpi_drift()
        # Tk 回调异常也写进日志（崩溃可反馈）
        register_tk_error(self.root)
        self.indicator = PillBubble(self.root)

        self.tray = tray_mod.Tray(
            on_quit=self._on_quit,
            on_open_settings=self._open_settings,
            on_open_dictionary=self._open_dictionary,
            on_set_input_mode=self._set_input_mode,
        )
        tray_mod._tray_instance = self.tray

        # 热键
        self.hotkey = get_hotkey()
        self.hotkey.on_hold_start = self._on_hold_start
        self.hotkey.on_hold_end = self._on_hold_end
        self.hotkey.on_error = lambda m: self.ui_q.put(("toast", m))
        self.hotkey.on_toggle = self._on_toggle

    def apply_hotkey_settings(self, key_name, trigger_mode):
        """立即验证并应用新的主热键/触发方式；失败时自动恢复旧热键。"""
        if self.privileged_bridge and self.privileged_bridge.connected:
            reply = self.privileged_bridge.request(
                "reconfigure", {"hotkey": key_name, "trigger_mode": trigger_mode}, timeout=1.2)
            if reply and reply.get("ok"):
                log.info("高权限输入助手热键已更新: %s (%s)", key_name, trigger_mode)
                return True, ""
            return False, "后台输入助手未能更新热键，设置未保存。"
        old_key = self.cfg.get("hotkey") or "`"
        old_mode = self.cfg.get("trigger_mode") or "hold"
        self.hotkey.stop()
        if self.hotkey.start(key_name, trigger_mode):
            log.info("主热键已即时更新: %s (%s)", key_name, trigger_mode)
            return True, ""

        # 新键不可用时，不让应用失去原先可用的录音入口。
        self.hotkey.stop()
        restored = self.hotkey.start(old_key, old_mode)
        if restored:
            log.warning("新主热键不可用，已恢复: %s (%s)", old_key, old_mode)
            return False, "该按键无法注册，已保留原来的热键。"
        log.error("新旧热键均无法注册: new=%s old=%s", key_name, old_key)
        return False, "该按键无法注册，原热键也未能恢复；请重启语润。"

    def _is_active_session(self, round_id):
        """只有最新语音轮次可以更新浮窗或执行输入；None 是非会话 UI 事件。"""
        return round_id is None or round_id == self._active_session_id

    def _event_round_id(self, evt):
        """提取会话事件携带的 round_id；兼容非会话 UI 事件。"""
        kind = evt[0]
        if kind in {"recording", "transcribing", "refining", "done", "stream_insert_done"}:
            return evt[1] if len(evt) > 1 else None
        if kind in {"error", "type_partial", "partial_preview"}:
            return evt[2] if len(evt) > 2 else None
        if kind == "paste":
            return evt[3] if len(evt) > 3 else None
        return None

    def _focus_lock_hwnd(self):
        """打字期间优先返回正在输出那一句自己的目标窗口。"""
        if self._type_job is not None:
            session = self._sessions.get(self._type_job["round_id"])
            if session and session.helper_session_id:
                # 高权限路径只在原窗口仍处于前台时输入，不从普通权限主程序抢焦点。
                return None
            if session and session.target_hwnd:
                return session.target_hwnd
        active = self._sessions.get(self._active_session_id)
        if active and active.helper_session_id:
            return None
        return self._target_hwnd

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
        # 使用每个会话记录的目标窗口，避免重叠语音时把上一句送到新目标。
        focus_hwnd = self._focus_lock_hwnd()
        if focus_hwnd and not self._quit:
            now = time.time()
            if now - self._last_steal > 0.4:
                try:
                    if _user32.GetForegroundWindow() != focus_hwnd:
                        if self._steal_focus(focus_hwnd):
                            self._last_steal = now
                except Exception:
                    pass
        self.indicator._tick()
        if not self._quit:
            self.root.after(40, self.pump)

    def _handle_ui(self, evt):
        kind = evt[0]
        try:
            round_id = self._event_round_id(evt)
            # 已完成的文字必须保留：旧会话的输入事件进入自己的队列并按其目标窗口输出；
            # 只有旧会话的状态浮窗/错误提示不能覆盖正在进行的新一轮。
            input_events = {"paste", "type_partial", "stream_insert_done"}
            if (round_id is not None and kind not in input_events
                    and not self._is_active_session(round_id)):
                log.info("丢弃过期 UI 事件: kind=%s round_id=%s active=%s",
                         kind, round_id, self._active_session_id)
                return
            if kind == "guide":
                self.indicator.show_guide("开始录音")
            elif kind == "open_settings":
                log.info("主线程打开设置窗口")
                self._open_settings_ui()
            elif kind == "open_dictionary":
                log.info("主线程打开个人记忆窗口")
                self._open_dictionary_ui()
            elif kind == "recording":
                # 按下热键：显示「正在录音」+ 红点呼吸
                self.indicator.start_recording()
                self._hide_partial()          # 新一轮录音：清掉上一次的 Partial 浮窗
            elif kind == "transcribing":
                # 松手后、ASR 等待期间：显示「正在识别」（苹果蓝麦克风），
                # 不再误显「正在录音」，避免"框凭空跳出来"的错觉。
                self.indicator.show_transcribing()
            elif kind == "refining":
                self.indicator.show_refining()
            elif kind in ("done", "fallback"):
                # 完成：只在文字已完整输入后进入很短的「已输入」收尾态；释放焦点锁定。
                self._target_hwnd = None
                self._hide_partial()
                self.indicator.show_complete()
            elif kind == "error":
                # 错误结束：释放焦点锁定
                self._target_hwnd = None
                self.indicator.set_anchor_hwnd(None)
                self.indicator.set_uia_anchor_rect(None)
                self.indicator.set_uia_caret_rect(None)
                self.indicator.set_frozen_mouse_rect(None)
                self._hide_partial()
                self.indicator.show_error(evt[1] if len(evt) > 1 else "出错了")
            elif kind == "toast":
                self.indicator.show_error(evt[1])
            elif kind == "show_correction":
                self._show_correction_dialog()
            elif kind == "bridge_hotkey":
                self._handle_privileged_hotkey(evt[1] if len(evt) > 1 else {})
            elif kind == "model_loading":
                pass  # 仅本地离线模式触发，不打扰
            elif kind in ("model_ready", "model_error"):
                # 模型加载完成/失败都直接隐藏，不在 pill 里显示文字
                self.indicator.force_idle()
            elif kind == "type_partial":
                self._do_type(evt[1], round_id=(evt[2] if len(evt) > 2 else None))
            elif kind == "stream_insert_done":
                self._complete_stream_insert(evt[1] if len(evt) > 1 else None)
            elif kind == "partial_preview":
                # Phase 1：SAUC 实时中间结果，仅打测试浮窗，不进输入主路径
                self._show_partial(evt[1])
            elif kind == "paste":
                self._do_paste(
                    evt[1],
                    hide=(evt[2] if len(evt) > 2 else True),
                    round_id=(evt[3] if len(evt) > 3 else None),
                )
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

    def _watch_dpi_drift(self):
        """DPI 漂移守卫：睡眠唤醒/显示器 DPI 变化会触发 Windows 广播 WM_DPICHANGED，
        Tk 收到后会按"当前 DPI 感知模式"重算 font scaling，导致所有 tkfont.Font 整体
        放大或缩小。这里捕获启动原值，并用 WndProc 子类化拦截 WM_DPICHANGED——
        收到即把 scaling 还原回原值并吞掉该消息，阻止 Tk 在唤醒时重算字体，
        保证睡眠前后字号完全一致。另加每 2s 周期兜底（其他路径改了 scaling 也能纠正）。"""
        if self._orig_scaling is None:
            return

        def _restore():
            try:
                cur = float(self.root.tk.call('tk', 'scaling'))
                if abs(cur - self._orig_scaling) > 1e-3:
                    self.root.tk.call('tk', 'scaling', self._orig_scaling)
                    log.info("DPI 漂移已还原: %.3f -> %.3f", cur, self._orig_scaling)
            except Exception:
                pass
            # 周期兜底：每 2s 检查一次
            try:
                self.root.after(2000, _restore)
            except Exception:
                pass

        # 启动首次调度（周期兜底）
        try:
            self.root.after(2000, _restore)
        except Exception:
            pass

        # === WndProc 子类化：拦截 WM_DPICHANGED (0x02E0) ===
        # 收到该消息时先还原 scaling，再 return 0 告诉系统"已处理"，
        # 阻止 Tk 默认重算字体（即睡眠唤醒后字号不再跳变）。
        try:
            import ctypes.wintypes as _wt

            WM_DPICHANGED = 0x02E0
            GWL_WNDPROC = -4

            _user32 = ctypes.windll.user32
            _SetWindowLongW = _user32.SetWindowLongW
            _SetWindowLongW.restype = ctypes.c_void_p
            _SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            _CallWindowProcW = _user32.CallWindowProcW
            _CallWindowProcW.restype = ctypes.c_int64
            _CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int64, ctypes.c_int64]

            _WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_int64, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int64, ctypes.c_int64)

            hwnd = int(self.root.wm_frame(), 16)
            self._orig_wndproc = None

            def _new_wndproc(h, msg, wp, lp):
                if msg == WM_DPICHANGED:
                    # 先还原字体缩放，再吞掉消息，不让 Tk 重算
                    try:
                        self.root.tk.call('tk', 'scaling', self._orig_scaling)
                        log.info("WM_DPICHANGED 拦截：scaling 已锁回 %.3f", self._orig_scaling)
                    except Exception:
                        pass
                    return 0
                # 其他消息转交 Tk 原 WndProc
                if self._orig_wndproc:
                    return _CallWindowProcW(self._orig_wndproc, h, msg, wp, lp)
                return 0

            _proc = _WNDPROC(_new_wndproc)
            self._wndproc_ref = _proc  # 保活，避免被 GC
            old = _SetWindowLongW(hwnd, GWL_WNDPROC, _proc)
            self._orig_wndproc = old
            log.info("WM_DPICHANGED 拦截已安装 (hwnd=%s)", hwnd)
        except Exception as _e:
            log.warning("WM_DPICHANGED 拦截安装失败，仅用周期兜底: %s", _e)

    def _on_hold_start(self, _key, helper_event=None):
        log.info("热键按下，开始录音")
        # 焦点锁定（C1）：记录开始时的目标输入控件；录音/输入期间焦点被切走会抢回
        target_hwnd = None
        helper_session_id = None
        if isinstance(helper_event, dict):
            target_hwnd = helper_event.get("target_hwnd") or None
            helper_session_id = helper_event.get("session_id") or None
        target_hwnd = target_hwnd or self._get_target_hwnd()
        self._target_hwnd = target_hwnd
        log.info("锁定目标窗口 hwnd=%s", self._target_hwnd)
        # 只用于 pill / Partial 浮窗定位，避免 Tk 窗口短暂成为前台时退回屏幕左上角。
        self.indicator.set_anchor_hwnd(self._target_hwnd)
        # Electron/Chromium 可能不暴露文字 caret 或可用输入控件。只在热键按下
        # 的这一刻冻结鼠标位置，作为整窗回退之前的稳定锚点；不会持续跟着鼠标跑。
        try:
            from pill import _cursor_screen_rect
            self.indicator.set_frozen_mouse_rect(_cursor_screen_rect())
        except Exception:
            self.indicator.set_frozen_mouse_rect(None)
        # 只读一次 UIA：优先取系统提供的真实插入点；不支持时保留控件边界作为下一层回退。
        # 两者都只读属性，不会激活窗口、点击或读取用户输入的文字。
        try:
            from pill import capture_uia_caret_rect, capture_uia_focused_rect
            self.indicator.set_uia_caret_rect(capture_uia_caret_rect(self._target_hwnd))
            self.indicator.set_uia_anchor_rect(capture_uia_focused_rect(self._target_hwnd))
        except Exception as exc:
            log.debug("UIA 锚点读取跳过: %r", exc)
            self.indicator.set_uia_caret_rect(None)
            self.indicator.set_uia_anchor_rect(None)
        # 不再拦截"已有录音进行中"：允许前一句还在识别/润色时按下热键录下一句
        # （重叠录音）。hold 模式物理上同一键按住中不会再触发 WM_HOTKEY，
        # 所以这里每次按下都是独立的一次录音，配独立临时文件互不干扰。
        self._round_seq += 1
        round_id = self._round_seq
        # 配置在按下时冻结：中途改设置不会改变这一句的识别/润色请求。
        cfg = dict(get_config().data)
        session = VoiceSession(
            round_id=round_id,
            target_hwnd=target_hwnd,
            stop_event=threading.Event(),
            config=cfg,
            helper_session_id=helper_session_id,
        )
        self._sessions[round_id] = session
        self._active_session_id = round_id
        self._recording_session_id = round_id
        self._rec_stop = session.stop_event
        # 单独记录会话生命线，不写转写内容、窗口标题或 API 信息。
        # 插入 KPI 只能从 insert_start 开始；这份轨迹能定位“录到了、却还没进输入”的丢失。
        self._trace_session(round_id, "hold_start", target_captured=bool(target_hwnd))
        log.info("语音会话创建: round_id=%s target_hwnd=%s", round_id, target_hwnd)
        if cfg.get("asr_provider") == "sauc":
            # 真流式：边录边发（ws race 已在 sauc_asr 内修好，单线程发完再收）
            self._rec_thread = threading.Thread(
                target=self._record_job_sauc, args=(session,), daemon=True)
        else:
            # 云端 HTTP / 本地 Whisper：仍需整段 wav
            self._rec_thread = threading.Thread(
                target=self._record_job, args=(session,), daemon=True)
        # 先投递录音态，再启动可能极快的 SAUC 工作线程。
        # 否则首次冷启动时，工作线程会抢先投递“正在识别”，覆盖“正在录音”。
        self.ui_q.put(("recording", round_id))
        self._rec_thread.start()

    def _on_hold_end(self, _key):
        log.info("热键松开，停止录音")
        session = self._sessions.get(self._recording_session_id)
        if session is not None and self._rec_stop is not None:
            round_id = session.round_id
            self._keyup_times[round_id] = session.mark_keyup()
            self._trace_session(round_id, "keyup")
            log.info("insert_metric round_id=%s event=keyup", round_id)
            # SAUC 流式线程从按下即启动，因此“正在识别”必须在松开时才切换；
            # 非 SAUC 分支会在录音文件写完后自行投递该状态。
            if session.config.get("asr_provider") == "sauc":
                self.ui_q.put(("transcribing", round_id))
            session.stop_event.set()
            self._rec_stop = None
            self._recording_session_id = None

    def _on_toggle(self, _key, pressed):
        if pressed:
            self._on_hold_start(_key)
        else:
            self._on_hold_end(_key)

    def _on_privileged_bridge_event(self, event):
        """桥接线程只投递事件，由 Tk 主线程顺序创建/停止语音会话。"""
        self.ui_q.put(("bridge_hotkey", event))

    def _handle_privileged_hotkey(self, event):
        if event.get("event") == "hotkey_down":
            self._on_hold_start(None, helper_event=event)
        elif event.get("event") == "hotkey_up":
            self._on_hold_end(None)

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

    def _replace_correction_selection(self, correct, target_hwnd):
        """将弹窗打开前的选区替换为 correct，并恢复用户原有剪贴板文本。"""
        if not correct or not target_hwnd:
            return False
        backup = self._clipboard_backup()
        try:
            # 这是纠正操作本身的必要聚焦，不属于录音/插入流程的焦点策略。
            if _user32.GetForegroundWindow() != target_hwnd:
                self._steal_focus(target_hwnd)
            self.root.clipboard_clear()
            self.root.clipboard_append(correct)
            self.root.update()
            import pyautogui
            pyautogui.hotkey("ctrl", "v")
            # Ctrl+V 消费完内容后再恢复，既不污染剪贴板也不打断替换。
            self.root.after(180, lambda: self._clipboard_restore(backup))
            return True
        except Exception as exc:
            self._clipboard_restore(backup)
            log.warning("纠正替换当前选区失败: %s", exc)
            return False

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

        # 在创建弹窗前保存原应用窗口；确认时只把已选中的原文字替换掉。
        try:
            selection_hwnd = _user32.GetForegroundWindow()
        except Exception:
            selection_hwnd = None

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
        selected_text = (_sel or "").strip()
        if selected_text:
            wrong_var.set(selected_text[:120])

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
                log.info("词库新增: correct_length=%s wrong_length=%s count=%s",
                         len(correct), len(wrong), entry.get("count"))
            except Exception as ex:
                log.error("存入词库失败: %s", ex)
                status_var.set("存入失败")
                return
            # 只替换弹窗打开前实际选中的完整文本；没有选中或选区过长被界面截断时，
            # 仍可安全保存词库，但绝不猜测并覆盖当前输入内容。
            replaced = False
            if selected_text and wrong == selected_text:
                replaced = self._replace_correction_selection(correct, selection_hwnd)
            if replaced:
                status_var.set(f"已替换并存入词库：{correct}")
                log.info("纠正已替换当前选区: wrong_length=%s correct_length=%s",
                         len(wrong), len(correct))
            elif selected_text:
                status_var.set(f"已存入词库（当前文字未替换）：{correct}")
                log.warning("纠正仅存词库，当前选区替换失败: wrong_length=%s correct_length=%s",
                            len(wrong), len(correct))
            else:
                status_var.set(f"已存入词库：{correct}")
            win.after(1200, win.destroy)

        footer = tk.Frame(body, bg="#FFFFFF")
        footer.pack(fill="x", padx=PAD, pady=(0, PAD))
        status = tk.Label(footer, textvariable=status_var, bg="#FFFFFF", fg="#34C759",
                          font=FONT(16))
        status.pack(side="left")

        # 占位 spacer 把按钮推到右侧；pack 顺序：先 right 的会后出现，因此先 pack 存入词库（最右），再 pack 取消（左侧）
        tk.Frame(footer, bg="#FFFFFF").pack(side="left", fill="x", expand=True)
        PillButton(footer, "替换并存入词库", _confirm, primary=True, min_w=150).pack(side="right")
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
    def _record_job(self, session):
        """非 SAUC 录音任务；只使用本会话冻结的配置与轮次。"""
        cfg = session.config
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
                tmp_wav, stop_event=session.stop_event,
                max_seconds=90, silence_timeout=0.0,
                on_level=self._on_level,
            )
            if not ok or dur < 0.3:
                self._trace_session(session.round_id, "recording_invalid")
                log.warning("录音无效: ok=%s dur=%.2f err=%s", ok, dur, err)
                self.ui_q.put(("error", "识别失败", session.round_id))
                return
            log.info("录音完成: %.2fs", dur)

            self.ui_q.put(("transcribing", session.round_id))
            try:
                log.info("进入 _transcribe provider=%s wav=%s", cfg.get("asr_provider", "sauc"), tmp_wav)
                t_asr0 = time.time()
                text = self._transcribe(tmp_wav, cfg)
                log.info("_transcribe 返回 text_length=%s 识别耗时=%.2fs",
                         len(text or ""), time.time() - t_asr0)
            except Exception as e:
                self._trace_session(session.round_id, "asr_error")
                log.error("识别失败: %s", e)
                self.ui_q.put(("error", "识别失败", session.round_id))
                return
            self._after_transcribe(text, round_id=session.round_id)
        except Exception as e:
            self._trace_session(session.round_id, "recording_error")
            log.error("录音异常: %s", e)
            self.ui_q.put(("error", "录音失败", session.round_id))
            return
        finally:
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    # _record_job_sauc / sauc_transcribe_stream / record_chunks：真流式实现。
    # 原"双线程 ws race"已通过在 sauc_transcribe_stream 内改为单线程
    # "边录边发→发完再收"规避（见 sauc_asr.py），现由 _on_hold_start 在 sauc 模式下启用。

    def _record_job_sauc(self, session):
        """SAUC 真流式分支：录音生成器边产出 PCM 边发往 WebSocket，并发收结果。"""
        cfg = session.config
        try:
            from recorder import record_chunks
            from sauc_asr import sauc_transcribe_stream
            from dictionary import to_hotwords
        except Exception as e:
            log.error("导入 SAUC 流式模块失败: %s", e)
            self.ui_q.put(("error", "模块失败", session.round_id))
            return
        round_id = session.round_id
        try:
            # Phase 1：Partial 经 UI 队列打到测试浮窗（不碰 SendInput 主路径）。
            # Phase 0：on_timeline 收集 T0-T7 时间戳，待识别结束打印耗时分解。
            session.timeline.clear()
            text = sauc_transcribe_stream(
                record_chunks(session.stop_event, on_level=self._on_level, max_seconds=90),
                api_key=cfg.get("asr_sauc_key"),
                resource_id=cfg.get("asr_sauc_resource_id"),
                endpoint=cfg.get("asr_sauc_endpoint"),
                language=cfg.get("language", "auto"),
                proxy=cfg.get("proxy", ""),
                hotwords=to_hotwords(),
                on_partial=lambda t: self.ui_q.put(("partial_preview", t, round_id)),
                on_timeline=lambda m, t: session.timeline.__setitem__(m, t),
            )
            self._log_timeline(session.timeline, round_id)
        except Exception as e:
            self._trace_session(session.round_id, "sauc_error")
            log.error("识别失败: %s", e)
            self.ui_q.put(("error", "识别失败", round_id))
            return
        self._after_transcribe(text, round_id=round_id)

    def _log_timeline(self, timeline, round_id):
        """Phase 0：打印 SAUC 识别 T0-T7 时间戳分解（相对 T0 的毫秒）。"""
        m = timeline or {}
        if "T0" not in m:
            return
        t0 = m["T0"]
        def rel(k):
            return (m[k] - t0) * 1000.0 if k in m else float("nan")
        def span(a, b):
            return (m[b] - m[a]) * 1000.0 if (a in m and b in m) else float("nan")
        log.info(
            "SAUC 时间戳 round_id=%s (相对T0, ms): T1=%.0f T2=%.0f T3=%.0f T4=%.0f T5=%.0f T6=%.0f T7=%.0f",
            round_id, rel("T1"), rel("T2"), rel("T3"), rel("T4"), rel("T5"), rel("T6"), rel("T7"),
        )
        log.info(
            "SAUC 派生: 首字延迟(T0→T3)=%.0fms 松手→Final(T5→T6)=%.0fms 纯收尾(T4→T6)=%.0fms",
            span("T0", "T3"), span("T5", "T6"), span("T4", "T6"),
        )

    # ---- Phase 1：SAUC Partial 测试浮窗（验证用，不改输入主路径） ----
    def _ensure_partial_window(self):
        if self._partial_win is not None:
            return
        try:
            w = tk.Toplevel(self.root)
            w.overrideredirect(True)          # 无标题栏，避免抢焦点
            w.attributes("-topmost", True)    # 置顶但不抢输入焦点
            # 临时识别文字是“正在流入输入框”的预览，不应像终端日志一样发绿发小。
            # 用更深、更稳的底色承托 17px 近白微绿字；绿色只留给前导输入竖线。
            w.attributes("-alpha", 0.96)
            w.configure(bg="#1b1c24")
            inner = tk.Frame(w, bg="#1b1c24", padx=12, pady=8)
            inner.pack()
            # 输入竖线独立成一个控件，才能只给它绿色；正文不再整段发绿。
            bar = tk.Label(
                inner, text="▌", bg="#1b1c24", fg="#7EE787",
                font=("Microsoft YaHei UI", 17), anchor="n",
            )
            bar.pack(side="left", anchor="n", padx=(0, 7))
            lbl = tk.Label(
                inner, text="", bg="#1b1c24", fg="#E5F3E8",
                font=("Microsoft YaHei UI", 17),
                wraplength=535, justify="left", anchor="w",
            )
            lbl.pack(side="left", anchor="w")
            w.geometry("+%d+%d" % (40, 40))
            w.withdraw()
            self._partial_win = w
            self._partial_lbl = lbl
        except Exception as e:
            log.warning("Partial 测试浮窗创建失败: %s", e)

    def _show_partial(self, text):
        self._ensure_partial_window()
        # 新一轮录音/新结果：取消进行中的"逐字吸走"收尾动画，避免串台
        if self._partial_finish_id:
            try:
                self.root.after_cancel(self._partial_finish_id)
            except Exception:
                pass
            self._partial_finish_id = None
            self._partial_finishing = False
        if self._partial_win is None:
            return
        try:
            # 只有输入竖线保留绿色，正文保持高对比的近白微绿，阅读更轻松。
            self._partial_lbl.configure(text=(text or ""), fg="#E5F3E8")
            self._partial_win.deiconify()
            self._partial_win.update_idletasks()  # 让 geometry 尺寸先算出来再定位
            pw = self._partial_win.winfo_width()
            ph = self._partial_win.winfo_height()
            # 方案2修正：优先贴在语润自己的 indicator（正在录音/润色气泡）正上方。
            # 定位改为「钉左缘」：浮窗左缘固定对齐 indicator 左缘，文字只往右/往下长，
            # 不再随字数变化而左右两边同时扩大 —— 根除录音阶段浮窗「向两边胀」的跳动感。
            p = self.indicator.get_rect() if self.indicator else None
            if p:
                px, py, pill_w, pill_h = p
                from pill import work_area_for_rect
                wl, wt, wr, wb = work_area_for_rect((px, py, px + pill_w, py + pill_h))
                LEFT_OFFSET = 0
                max_w = 560 + 24  # wraplength(560) + 左右 padding(12*2)，用于超右屏的一次性钳制
                x = px + LEFT_OFFSET
                # 边界按 pill 所在那块屏幕的工作区计算；副屏在左侧时 x 可以为负数。
                usable_w = max(1, wr - wl - 8)
                span_w = min(max_w, usable_w)
                if x + span_w > wr - 4:
                    x = wr - span_w - 4  # 超右边缘则整体左移（一次性，不随字数跳）
                x = max(wl + 4, x)
                y = py - ph - 8
                if y < wt + 4:
                    y = py + pill_h + 8  # indicator 贴屏顶时改放其正下方
                if y + ph > wb - 4:
                    y = max(wt + 4, wb - ph - 4)
                self._partial_win.geometry("+%d+%d" % (x, y))
                return

            # fallback：贴在当前输入窗口头顶正中
            hwnd = self._target_hwnd or _user32.GetForegroundWindow()
            if hwnd:
                import ctypes.wintypes as _wt
                rect = _wt.RECT()
                if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    from pill import work_area_for_rect
                    wl, wt, wr, wb = work_area_for_rect(
                        (rect.left, rect.top, rect.right, rect.bottom))
                    win_w = rect.right - rect.left
                    x = rect.left + max(0, (win_w - pw) // 2)
                    x = max(wl + 4, min(x, wr - pw - 4))
                    y = rect.top - ph - 12
                    if y < wt + 4:
                        y = rect.top + 24
                    if y + ph > wb - 4:
                        y = max(wt + 4, wb - ph - 4)
                    self._partial_win.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def _hide_partial(self):
        # 浮窗收尾动画（慢速删字定时器）进行中：让它自然播完（字被逐个吸进文本框），不强制隐藏。
        # 必须同时检查 _draining —— 否则 done 事件的 withdraw 会在打字一结束就把浮窗瞬间隐藏，
        # 慢速删字定时器在"已隐藏"的不可见窗口上跑完，表现为"一闪而逝"。
        if self._partial_finishing or self._draining:
            return
        if self._partial_win is not None:
            try:
                self._partial_win.withdraw()
            except Exception:
                pass

    def _start_drain(self):
        """异常兜底收尾：正常路径由已确认的输入进度直接驱动。

        这个定时器只负责极少量残字，绝不再和真实输入并行“猜速度”。
        """
        if self._draining:
            return
        if self._partial_win is None:
            return
        cur = self._partial_lbl.cget("text") or ""
        if not cur:
            self._partial_win.withdraw()
            return
        log.debug("浮窗兜底收尾: 当前长度=%d 间隔=%dms", len(cur), self._drain_interval)
        self._draining = True
        self.root.after(self._drain_interval, self._drain_timer_step)

    def _drain_timer_step(self):
        try:
            if self._partial_win is None:
                self._draining = False
                return
            cur = self._partial_lbl.cget("text") or ""
            if not cur:
                self._draining = False
                self._partial_win.withdraw()
                return
            self._partial_lbl.configure(text=cur[1:])
            self._partial_win.update_idletasks()
            self.root.after(self._drain_interval, self._drain_timer_step)
        except Exception:
            self._draining = False

    def _finish_partial_drain(self):
        """打字结束后的极端兜底；正常情况下浮窗会已略早于输入完成。"""
        if self._partial_finishing:
            return
        self._partial_finishing = True
        try:
            self._start_drain()
        finally:
            self._partial_finishing = False

    def _after_transcribe(self, text, round_id=None):
        """识别成功后的共用收尾。

        设计目标：原文立刻贴出（最低延迟），且仅在「润色真可能改动」时才显示
        「正在润色」并等待后台结果；其余情况（太短跳过 / 未配置 / 模型大概率
        返回 no_change）直接收尾隐藏，避免图标空挂 5s 的误导观感。
        """
        log.info("识别结果: text_length=%s", len(text or ""))
        if not text or not text.strip():
            self._trace_session(round_id, "asr_empty")
            self.ui_q.put(("error", "没识别到", round_id))
            return
        self._trace_session(round_id, "asr_final", text_length=len(text))

        # ASR（火山 SAUC）自带标点预测，纯数字/手机号/订单号常被补末尾句号
        # （如「12345。」）。数字为主的文本先剥掉末尾标点再进润色/bypass 决策，
        # 否则免润色直接贴原文会把句号一起贴出来；正常句子数字占比低不受影响。
        from refiner import strip_numeric_trailing_punct
        text = strip_numeric_trailing_punct(text)
        # 词库本地替换（bypass 兜底）：错误变体命中即换成正确词（如「天气log」→changelog）。
        # 只对免润色短句生效；LLM 路径有词典 + prompt 双重处理。
        from dictionary import apply_local_replace
        text = apply_local_replace(text)

        # Phase 0 总闸：Direct 是默认主路径；智能整理仅在用户主动选择时进入。
        # Direct 不显示「正在润色」气泡，轻清洗后立即输入。
        cfg = get_config()
        if self._input_mode() == "direct":
            from refiner import light_clean
            final = light_clean(text)
            log.info("轻清洗直出: text_length=%s", len(final or ""))
            self.ui_q.put(("paste", final, True, round_id))
            return

        if self._refine_will_change(text):
            # 方案B：不先贴原文，显示「正在润色」并后台润色，完成后一次性贴最终文本。
            # 无 replace 步骤 → 从根上避免误删/误覆盖输入框里之前的内容。
            self.ui_q.put(("refining", round_id))
            threading.Thread(
                target=self._refine_and_paste, args=(text, round_id), daemon=True
            ).start()
        else:
            # 不会改动（太短/未配置/无自定义指令且较短）：原文即最终结果，立即贴出+收尾。
            self.ui_q.put(("paste", text, True, round_id))

    def _refine_will_change(self, text) -> bool:
        """预估这次润色是否可能产生改动（用于决定是否显示「正在润色」）。

        返回 False 的情形：润色未启用 / 未配 key（no_api_key）、短句无自定义指令
        （bypass_short）。这些走原文本、不等 LLM，pill 立即收尾。
        """
        cfg = get_config()
        if self._input_mode() != "refine" or not cfg.get("api_key"):
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
        # 已经录下的话必须完成。即使之后开始了新一轮，也只把本句排进自己的
        # 输入队列项；由队列按会话目标窗口输出，旧浮窗不会覆盖新浮窗。
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
        self.ui_q.put(("paste", final, True, round_id))

    def _refine_stream_and_paste(self, text, round_id):
        """流式润色：边收 delta 边逐段 SendInput，首字即上屏；首字前失败回退整段。"""
        from refiner import refine_stream
        from dictionary import to_llm_text
        cfg = get_config()
        t_rf0 = time.time()
        if round_id is not None:
            self._streaming_rounds.add(round_id)
            self._insert_metrics.setdefault(round_id, {"text_length": 0, "streaming": True})

        def on_delta(seg):
            # 每个分片带自己的 round_id；UI 不显示旧浮窗，但输入队列不能丢句。
            self.ui_q.put(("type_partial", seg, round_id))

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
            # 等最后一个流式分片真正投递完，再记录 insert_done 并进入收尾态。
            self.ui_q.put(("stream_insert_done", round_id))
        else:
            if round_id is not None:
                self._streaming_rounds.discard(round_id)
                self._stream_complete_rounds.discard(round_id)
            # 首字前失败：回退整段润色（此时尚未贴任何字，安全）。
            fallback = self._refine(text)
            final = fallback["text"] if fallback["ok"] else text
            from refiner import strip_numeric_trailing_punct
            final = strip_numeric_trailing_punct(final)
            self.ui_q.put(("paste", final, True, round_id))

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
        if self._input_mode() != "refine" or not cfg.get("api_key"):
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

    def _animation_interval_ms(self, text_length):
        """保留短句 40ms/字手感；中长文本按总预算自动加速。"""
        if text_length <= 0:
            return 0
        if text_length <= 15:
            budget_ms = text_length * 40
        elif text_length <= 40:
            budget_ms = 600
        elif text_length <= 80:
            budget_ms = 700
        else:
            # 81 字起从 800ms 平滑增长，161 字及以上封顶 1000ms。
            budget_ms = min(1000, 800 + max(0, text_length - 81) * 2.5)
        # 8ms 是 SendInput/Tk 调度的安全下限；40ms 保留短句基准手感。
        return max(8, min(40, int(round(budget_ms / text_length))))

    def _record_insert_start(self, round_id, text):
        if round_id is None:
            return
        metric = self._insert_metrics.setdefault(round_id, {"text_length": len(text or "")})
        if metric.get("streaming"):
            metric["text_length"] = metric.get("text_length", 0) + len(text or "")
        if metric.get("start") is not None:
            return
        now = time.perf_counter()
        metric["start"] = now
        if not metric.get("streaming"):
            metric["text_length"] = len(text or "")
        log.info("insert_metric round_id=%s mode=%s text_length=%s event=insert_start",
                 round_id, self._input_mode(), metric["text_length"])
        self._write_insert_metric("insert_start", round_id, metric)

    def _record_first_insert(self, round_id):
        if round_id is None:
            return
        metric = self._insert_metrics.get(round_id)
        if not metric or metric.get("first") is not None:
            return
        now = time.perf_counter()
        metric["first"] = now
        keyup = self._keyup_times.get(round_id)
        ttfi_ms = (now - keyup) * 1000.0 if keyup is not None else float("nan")
        log.info("insert_metric round_id=%s mode=%s text_length=%s event=first_insert ttfi_ms=%.0f",
                 round_id, self._input_mode(), metric.get("text_length", 0), ttfi_ms)
        self._write_insert_metric("first_insert", round_id, metric, ttfi_ms=ttfi_ms)

    def _record_insert_done(self, round_id):
        if round_id is None:
            return
        metric = self._insert_metrics.get(round_id)
        if not metric or metric.get("done") is not None:
            return
        now = time.perf_counter()
        metric["done"] = now
        keyup = self._keyup_times.get(round_id)
        ttci_ms = (now - keyup) * 1000.0 if keyup is not None else float("nan")
        log.info("insert_metric round_id=%s mode=%s text_length=%s event=insert_done ttci_ms=%.0f",
                 round_id, self._input_mode(), metric.get("text_length", 0), ttci_ms)
        self._write_insert_metric("insert_done", round_id, metric, ttci_ms=ttci_ms)
        self._trace_session(round_id, "input_done", text_length=metric.get("text_length", 0))

    def _trace_session(self, round_id, event, **facts):
        """把一次语音的关键交接点写入独立、脱敏的诊断轨迹。

        这里绝不写语音原文、窗口标题、句柄、剪贴板或密钥；仅保存轮次、时间、事件
        和文本长度。它不参与 TTFI/TTCI 统计，避免污染已有性能基线。
        """
        if round_id is None:
            return
        record = {
            "timestamp_ms": int(time.time() * 1000),
            "round_id": round_id,
            "event": event,
        }
        record.update(facts)
        try:
            with (logs_dir() / "session-trace.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("会话轨迹落盘失败: %s", exc)

    def _write_insert_metric(self, event, round_id, metric, **durations):
        """KPI 专用脱敏落盘：不依赖通用日志，绝不保存转写文本。"""
        record = {
            "timestamp_ms": int(time.time() * 1000),
            "round_id": round_id,
            "event": event,
            "input_mode": self._input_mode(),
            "text_length": metric.get("text_length", 0),
        }
        record.update({key: round(value, 1) for key, value in durations.items()})
        try:
            with (logs_dir() / "insert-metrics.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("插入指标落盘失败: %s", exc)

    def _complete_stream_insert(self, round_id):
        """流式润色结束信号：等待缓冲区清空后才算完整输入，再触发浮窗收尾。"""
        if round_id is None:
            return
        self._stream_complete_rounds.add(round_id)
        queued_for_round = any(job["round_id"] == round_id for job in self._type_jobs)
        current_for_round = self._type_job and self._type_job["round_id"] == round_id
        if not self._typing and not queued_for_round and not current_for_round:
            self._record_insert_done(round_id)
            self._streaming_rounds.discard(round_id)
            self._stream_complete_rounds.discard(round_id)
            self.ui_q.put(("done", round_id))

    def _do_type(self, text, round_id=None):
        """主线程：SendInput 逐字输入一段文本（流式分片），不隐藏 pill、不发 done。

        用 char_interval 逐字投递，模拟人打字节奏——即使模型生成很快，文字也按固定节奏
        逐字冒出，而不是整段瞬间蹦出（用户要的「像打字那样跳出来」）。
        on_each 把"浮窗从开头删一字"绑进打字循环，使吸走动画与打字严格同步（开头对齐）。
        """
        try:
            # 异步打字链由 tk 主循环驱动：逐字打字的同时，独立的慢速删字定时器
            # （_start_drain → _drain_timer_step）从浮窗开头逐字吸走，明显慢于打字。
            # 这里绝不能再 withdraw 浮窗，否则第一段 chunk enqueue 完就 withdraw，浮窗会"一下全没"。
            try:
                log.debug("流式打字链收到片段: len=%d", len(text) if text else 0)
            except Exception:
                pass
            self._enqueue_type(text, round_id=round_id)
        except Exception as e:
            log.warning("流式 SendInput 输入失败: %s", e)

    def _enqueue_type(self, text, on_done=None, round_id=None):
        """把文本放入所属会话的打字队列，单链输出但绝不混句。"""
        if not text:
            if on_done:
                self.root.after(0, on_done)
            return
        self._record_insert_start(round_id, text)
        interval_ms = self._animation_interval_ms(len(text or ""))
        interval_ms = interval_ms or self._type_interval_ms
        # 同一流式润色会话的 delta 追加到自己的末尾；不同会话则独立排队。
        if self._type_job is not None and self._type_job["round_id"] == round_id:
            self._type_job["buffer"] += text
            self._type_job["total_chars"] += len(text)
            self._type_job["interval_ms"] = interval_ms
            if on_done is not None:
                self._type_job["on_done"] = on_done
        elif self._type_jobs and self._type_jobs[-1]["round_id"] == round_id:
            job = self._type_jobs[-1]
            job["buffer"] += text
            job["total_chars"] += len(text)
            job["interval_ms"] = interval_ms
            if on_done is not None:
                job["on_done"] = on_done
        else:
            self._type_jobs.append({
                "round_id": round_id,
                "buffer": text,
                "total_chars": len(text),
                "sent_chars": 0,
                "interval_ms": interval_ms,
                "on_done": on_done,
            })
        if not self._typing:
            self._typing = True
            self.root.after(0, self._type_step)

    def _begin_preview_progress(self, job):
        """绑定浮窗到一个输入任务，预览在末段以小幅领先量结束。"""
        if self._partial_win is None or not self._partial_win.winfo_viewable():
            return
        text = self._partial_lbl.cget("text") or ""
        self._preview_total_chars = len(text)
        self._preview_input_total = max(1, int(job.get("total_chars") or 1))
        self._preview_sent_chars = 0
        self._preview_removed_chars = 0
        # 领先量按文本长度渐进累积：短句只领先 1 字，长句最多 10 字。
        # 首字不会一下吞掉一段，最后几字前才自然完成。
        self._preview_lead_chars = min(10, max(1, round(self._preview_total_chars * 0.10)))
        log.debug(
            "浮窗进度绑定: preview=%d input=%d lead=%d",
            self._preview_total_chars, self._preview_input_total, self._preview_lead_chars,
        )

    def _advance_preview_progress(self, sent_chars):
        """根据已成功输入的字符数，平滑删掉对应的浮窗前缀。"""
        if self._preview_total_chars <= 0 or self._partial_win is None:
            return
        self._preview_sent_chars = max(self._preview_sent_chars, sent_chars)
        # 领先量随已输入进度逐步增加：开始时仍是一字对一字，末段才略早结束。
        desired = (self._preview_sent_chars *
                   (self._preview_total_chars + self._preview_lead_chars)) // self._preview_input_total
        desired = min(self._preview_total_chars, max(0, desired))
        count = desired - self._preview_removed_chars
        if count <= 0:
            return
        cur = self._partial_lbl.cget("text") or ""
        # 单次最多删两字，避免因个别慢 IPC 回调让视觉突然跳一大段。
        count = min(count, 2, len(cur))
        if not count:
            return
        self._partial_lbl.configure(text=cur[count:])
        self._preview_removed_chars += count
        try:
            self._partial_win.update_idletasks()
            if not (self._partial_lbl.cget("text") or ""):
                self._partial_win.withdraw()
        except Exception:
            pass

    def _abort_type_job(self, job, reason):
        """停止一个无法确认完整投递的会话，绝不自动重试同一字符。"""
        round_id = job["round_id"]
        self._trace_session(round_id, "input_failed")
        log.error("SendInput 输入中止: round_id=%s reason=%s", round_id, reason)
        self._type_jobs = deque(
            pending for pending in self._type_jobs if pending["round_id"] != round_id)
        self._streaming_rounds.discard(round_id)
        self._stream_complete_rounds.discard(round_id)
        self._type_job = None
        self.ui_q.put(("error", "输入失败", round_id))
        self.root.after(0, self._type_step)

    def _type_step(self):
        if self._type_job is None:
            if not self._type_jobs:
                self._typing = False
                return
            self._type_job = self._type_jobs.popleft()
            self._type_interval_ms = self._type_job["interval_ms"]
            self._drain_interval = 26
            if self._is_active_session(self._type_job["round_id"]):
                self._begin_preview_progress(self._type_job)

        job = self._type_job
        if not job["buffer"]:
            round_id = job["round_id"]
            if round_id in self._stream_complete_rounds:
                self._record_insert_done(round_id)
                self._streaming_rounds.discard(round_id)
                self._stream_complete_rounds.discard(round_id)
                self.ui_q.put(("done", round_id))
            elif round_id not in self._streaming_rounds:
                self._record_insert_done(round_id)
            cb = job["on_done"]
            self._type_job = None
            # 浮窗只属于最新会话；旧会话收尾不能隐藏正在录制的新一轮浮窗。
            if self._is_active_session(round_id):
                self._finish_partial_drain()
            if cb:
                try:
                    cb()
                except Exception:
                    pass
            self.root.after(0, self._type_step)
            return

        ch = job["buffer"][0]
        job["buffer"] = job["buffer"][1:]
        try:
            from typer import type_text
            session = self._sessions.get(job["round_id"])
            target_hwnd = session.target_hwnd if session else None
            if session and session.helper_session_id and self.privileged_bridge:
                # 高权限目标由助手输入。助手会二次确认原窗口仍在前台；不满足则安全取消，
                # 绝不写入用户后来切换到的新窗口。
                sent = self.privileged_bridge.type_character(session.helper_session_id, ch)
            else:
                # 每个队列项只回到自己录音开始时的目标窗口，不能借用最新一轮的全局目标。
                if target_hwnd and _user32.GetForegroundWindow() != target_hwnd:
                    self._steal_focus(target_hwnd)
                sent = type_text(ch)
            if sent >= 2:
                self._record_first_insert(job["round_id"])
                job["sent_chars"] += 1
                if self._is_active_session(job["round_id"]):
                    self._advance_preview_progress(job["sent_chars"])
            else:
                # 一个 Unicode 字需要 key-down + key-up 两个事件。少于两个就不能确认
                # 该字已完整进入目标程序；此时绝不自动重试，避免重复字或半个代理对。
                self._abort_type_job(job, f"sent={sent}")
                return
        except Exception as e:
            log.warning("逐字 SendInput 失败: %s", e)
            self._abort_type_job(job, "exception")
            return
        # 删字不再绑定打字步（那样会和打字同速，显得"一下没"）；
        # 改由独立的慢速定时器（_drain_timer_step）驱动，明显慢于打字，逐字吸走可见。
        self.root.after(job["interval_ms"], self._type_step)

    def _do_paste(self, text, hide=True, replace=False, round_id=None):
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
        session = self._sessions.get(round_id)
        if session and session.helper_session_id and method != "type":
            # 高权限路径不写剪贴板；统一走助手 SendInput，避免普通权限 Ctrl+V 被 UIPI 拦截。
            log.info("高权限目标强制使用 type 输入路径")
            method = "type"
        if method == "type" and not replace:
            # 主路径：SendInput 逐字 Unicode 输入，不碰剪贴板
            try:
                from typer import type_text
                # 走统一的异步打字链（_enqueue_type 已含抢焦点），由 tk 主循环驱动，
                # 保证浮窗逐字吸走动画可见；done 由 on_done 在打字完成后发出
                log.info("SendInput（type 模式，异步，零剪贴板污染）")
                self._trace_session(round_id, "input_enqueued", text_length=len(text or ""))
                self._enqueue_type(
                    text,
                    on_done=(lambda: self.ui_q.put(("done", round_id))) if hide else None,
                    round_id=round_id,
                )
                return
            except Exception as e:
                log.warning("SendInput 输入失败: %s，回退剪贴板粘贴", e)
                # 落到下面的 paste 路径作兜底

        # paste 路径（兜底或用户显式选择）：写剪贴板 + Ctrl+V
        self._trace_session(round_id, "input_enqueued", text_length=len(text or ""), method="paste")
        self._record_insert_start(round_id, text)
        # ③ 切窗口兜底：粘贴前回到本会话录音开始时的目标窗口，不能借用最新一轮。
        session = self._sessions.get(round_id)
        target_hwnd = session.target_hwnd if session else self._target_hwnd
        if target_hwnd and _user32.GetForegroundWindow() != target_hwnd:
            self._steal_focus(target_hwnd)
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
            self._record_first_insert(round_id)
            self._record_insert_done(round_id)
            # 浮窗收尾（若有剩字），再发 done（done 不会打断收尾）
            pass  # 收尾交给异步打字链（_enqueue_type → _type_step → _finish_partial_drain）
            if hide:
                self.ui_q.put(("done", round_id))
        except Exception as e:
            log.warning("pyautogui 失败: %s", e)
            try:
                import subprocess
                ks = "^z^v" if replace else "^v"
                subprocess.Popen(["powershell", "-Command",
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "[System.Windows.Forms.SendKeys]::SendWait('" + ks + "')"])
                self._record_first_insert(round_id)
                self._record_insert_done(round_id)
                self.ui_q.put(("done", round_id))
            except Exception as e2:
                log.error("备用粘贴失败: %s", e2)
                self.ui_q.put(("error", "粘贴失败", round_id))

    # ================= 托盘 =================
    def _on_quit(self):
        log.info("用户退出")
        self._quit = True
        try:
            if self.privileged_bridge:
                self.privileged_bridge.close()
        except Exception:
            pass
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
        # 托盘回调来自其他线程；Queue 由 Tk 主线程 pump 消费，避免跨线程触碰 Tk。
        self.ui_q.put(("open_settings",))

    def _open_settings_ui(self):
        try:
            SettingsWindow(master=self.root)
        except Exception as e:
            log.error("打开设置失败: %s", e)

    def _open_dictionary(self):
        """托盘直达个人记忆管理；由 Tk 主线程的 UI 队列创建窗口。"""
        log.info("托盘请求打开个人记忆，已进入 UI 队列")
        self.ui_q.put(("open_dictionary",))

    def _open_dictionary_ui(self):
        try:
            from gui import DictionaryManager
            self._dictionary_manager = DictionaryManager(self.root)
            log.info("个人记忆窗口已创建")
        except Exception as e:
            log.error("打开个人记忆界面失败: %s", e)

    def _input_mode(self):
        mode = self.cfg.get("input_mode", "direct")
        if mode not in ("direct", "refine"):
            log.warning("无效 input_mode=%r，按 direct 处理", mode)
            return "direct"
        return mode

    def _set_input_mode(self, mode):
        """托盘模式选择回调：立即持久化，且永远只保留一种输入模式。"""
        if mode not in ("direct", "refine"):
            log.warning("拒绝未知输入模式: %r", mode)
            return
        try:
            cfg = get_config()
            old = cfg.get("input_mode", "direct")
            cfg.set("input_mode", mode)
            self.cfg = cfg
            log.info("输入模式切换: %s -> %s", old, mode)
        except Exception as e:
            log.error("切换输入模式失败: %s", e)

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
        # 正式版仅接管自己的旧实例，避免旧进程占用热键。
        try:
            from singleinstance import kill_old_and_takeover, kill_other_yurun_exe
            kill_old_and_takeover()
            kill_other_yurun_exe()
        except Exception as e:
            log.warning("单实例检查失败: %s", e)
        log.info("语润启动（开发版）")
        # 仅本地离线模式才在启动时加载 Whisper 模型；云端 SAUC 用户无需等待/下载
        if self.cfg.get("asr_provider") == "local":
            self._load_model_async()
        else:
            log.info("识别引擎为 %s，跳过本地模型加载", self.cfg.get("asr_provider"))
        try:
            from privileged_ipc import PrivilegedBridge
            bridge = PrivilegedBridge(on_event=self._on_privileged_bridge_event)
            if bridge.connect():
                self.privileged_bridge = bridge
                log.info("主热键与高权限输入已由后台助手接管")
            else:
                bridge.close()
        except Exception as exc:
            log.debug("高权限输入助手连接跳过: %s", exc)
        if self.privileged_bridge is None:
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
        # 先在主线程提交托盘，再进入 Tk mainloop。run_detached 会自行管理
        # Windows 的托盘消息循环；不要再额外套一层后台线程。
        self.tray.start(APP_TITLE)
        # 后台预热重依赖，避免首次按下热键才现场加载 numpy/sounddevice/websocket/pyautogui
        threading.Thread(target=self._warmup, daemon=True).start()
        # 主循环（不再弹首次启动引导气泡，避免文字显示不全的干扰）
        self.root.after(40, self.pump)
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
