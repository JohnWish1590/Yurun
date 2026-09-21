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
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 焦点锁定用（录音时把输入焦点固定在开始时的目标窗口）
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

def _window_rect(hwnd):
    """读取目标窗口矩形；失败时返回 None。"""
    if not hwnd:
        return None
    try:
        rc = wintypes.RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            return rc.left, rc.top, rc.right, rc.bottom
    except Exception:
        pass
    return None


def _monitor_work_area(hwnd):
    """返回 hwnd 所在显示器的工作区 (left, top, right, bottom)。"""
    try:
        from pill import work_area_for_rect
        return work_area_for_rect(_window_rect(hwnd))
    except Exception:
        pass
    return None


def _monitor_work_area_for_point(x, y):
    """返回坐标所在显示器工作区；用于恢复用户上次拖动的位置。"""
    try:
        from pill import work_area_for_rect
        return work_area_for_rect((int(x), int(y), int(x), int(y)))
    except Exception:
        pass
    return None


def _primary_work_area():
    """主显示器工作区：纠错窗口定位的最终兜底，保证窗口一定落在可见区域。

    不能退回整个虚拟桌面——多显示器（尤其副屏在上方/左侧）时虚拟桌面含负坐标，
    旧实现正是把窗口放到了 y=-513 这种"合法但用户看不见"的位置。
    """
    work = _monitor_work_area_for_point(0, 0)
    if work:
        return work
    try:
        return 0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    except Exception:
        return None


def _clamp_rect_to_work(x, y, w, h, work):
    """把窗口左上角收进工作区（四周各留 8px）；工作区比窗口还小则贴左上角。"""
    l, t, r, b = work
    min_x, min_y = l + 8, t + 8
    max_x, max_y = r - w - 8, b - h - 8
    x = max(min_x, min(x, max_x) if max_x >= min_x else min_x)
    y = max(min_y, min(y, max_y) if max_y >= min_y else min_y)
    return int(x), int(y)


def _rect_intersects_work(x, y, w, h, work):
    """窗口矩形与该工作区是否有交集（至少有一部分能被看到）。

    用它而不是"中心点是否在屏内"来判断保存位置是否仍然有效：用户可以把
    窗口拖到屏幕下缘、标题栏还在屏内但中心点已经出屏，这种位置应当保留，
    只有像 y=-513 那样与所有显示器完全无重叠的位置才该被丢弃。

    注意 MonitorFromPoint 用的是 MONITOR_DEFAULTTONEAREST，屏幕外的坐标也会
    返回"最近"显示器的工作区，所以不能只看 work 是否为真。
    """
    l, t, r, b = work
    return x < r and y < b and x + w > l and y + h > t


def _clipboard_sequence():
    """剪贴板序列号（任何程序写入剪贴板都会自增）；不可用时返回 None。

    用来区分"Ctrl+C 真的复制到了选中文字"与"目标应用没响应、读到的是旧剪贴板"。
    """
    try:
        return int(_user32.GetClipboardSequenceNumber())
    except Exception:
        return None


from config import get_config
from hotkey import (MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, HotkeyListener,
                    format_hotkey, get_hotkey, hotkey_id, pynput_mod_bit, pynput_vk,
                    _vk_for)
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
TYPE_BATCH_CHARS = 32


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
        self._local_hotkey_active = False
        self._privileged_health_busy = False
        self._privileged_health_lock = threading.Lock()
        self._privileged_health_stop = threading.Event()
        self._last_helper_wake = 0.0
        # 纠错热键（默认 Alt + 反引号，可在设置里自定义）：首选 RegisterHotKey
        # 监听器，注册失败才回退下面的 pynput 钩子状态。
        self._correct_hotkey = None
        # pynput 纠错热键状态 —— 仅作回退路径
        self._kb = None
        self._kb_listener = None
        self._kb_mods = 0                 # 当前按住的修饰键（MOD_* 位掩码）
        self._kb_correct_fired = False
        # 纠错窗口单例：连续按热键只保留一个窗口，避免多窗口叠加、
        # 多个剪贴板事务互相覆盖（后开的窗口会把前一个的选区当成"原剪贴板"备份）。
        self._correction_win = None
        # 纠错窗口的剪贴板事务：armed=True 表示用户的剪贴板已被 Ctrl+C 覆盖，
        # 必须在任何退出路径（确认/取消/重建/异常）下还原。
        self._correction_clip_armed = False
        self._correction_clip_backup = None
        self._correction_clip_sequence = None
        # 代际计数：窗口被新窗口取代后，旧窗口已排队的回调（80ms 剪贴板读取、
        # 220ms 重新置顶）不得再执行，否则会误消费新窗口的剪贴板事务。
        self._correction_gen = 0
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

    def apply_hotkey_settings(self, key_name, trigger_mode, modifiers=0):
        """立即验证并应用新的主热键/触发方式；失败时自动恢复旧热键。

        modifiers 为 MOD_* 位掩码（0 = 裸键）。主热键在助手连着的时候由**提权
        助手**注册，所以修饰键必须一起通过 `reconfigure` 传过去，否则助手上仍然
        是旧的裸键，表现成"设置保存了但热键没变"。
        """
        modifiers = int(modifiers or 0)
        if self.privileged_bridge and self.privileged_bridge.connected:
            # 老版本助手只认裸键，会**静默忽略** modifiers。必须在这里拦住并说明，
            # 否则用户看到"设置已保存"，实际助手上还是旧组合，表现为热键没变。
            if modifiers and not self.privileged_bridge.supports("hotkey_modifiers"):
                log.warning("助手不支持 hotkey_modifiers，拒绝保存组合键主热键")
                return False, ("后台输入助手版本过旧，无法注册组合键热键。"
                               "请重新安装语润的高权限输入助手后重试。")
            reply = self.privileged_bridge.request(
                "reconfigure",
                {"hotkey": key_name, "trigger_mode": trigger_mode, "modifiers": modifiers},
                timeout=1.2)
            if reply and reply.get("ok"):
                log.info("高权限输入助手热键已更新: %s (%s)",
                         format_hotkey(modifiers, key_name), trigger_mode)
                return True, ""
            return False, "后台输入助手未能更新热键，设置未保存。"
        old_key = self.cfg.get("hotkey") or "`"
        old_mode = self.cfg.get("trigger_mode") or "hold"
        old_mods = int(self.cfg.get("hotkey_modifiers") or 0)
        self.hotkey.stop()
        if self.hotkey.start(key_name, trigger_mode, modifiers):
            log.info("主热键已即时更新: %s (%s)", format_hotkey(modifiers, key_name), trigger_mode)
            return True, ""

        # 新键不可用时，不让应用失去原先可用的录音入口。
        self.hotkey.stop()
        restored = self.hotkey.start(old_key, old_mode, old_mods)
        if restored:
            log.warning("新主热键不可用，已恢复: %s (%s)",
                        format_hotkey(old_mods, old_key), old_mode)
            return False, "该按键无法注册，已保留原来的热键。"
        log.error("新旧热键均无法注册: new=%s old=%s", key_name, old_key)
        return False, "该按键无法注册，原热键也未能恢复；请重启语润。"

    def apply_correction_hotkey_settings(self, key_name, modifiers):
        """立即应用新的纠错热键；失败时恢复旧组合，绝不留下"没有纠错入口"的状态。"""
        modifiers = int(modifiers or 0)
        old_key, old_mods = self._correction_combo()
        if self._correct_hotkey is not None:
            self._correct_hotkey.stop()
            self._correct_hotkey = None
        try:
            listener = HotkeyListener()
            listener.on_hold_start = self._on_correct_hotkey_fired
            if listener.start(key_name, "hold", modifiers=modifiers):
                self._correct_hotkey = listener
                log.info("纠错热键已即时更新: %s", format_hotkey(modifiers, key_name))
                return True, ""
        except Exception as exc:
            log.warning("纠错热键注册异常: %s", exc)

        # 回滚：用户宁可保留旧组合，也不能没有纠错入口。
        try:
            listener = HotkeyListener()
            listener.on_hold_start = self._on_correct_hotkey_fired
            if listener.start(old_key, "hold", modifiers=old_mods):
                self._correct_hotkey = listener
                log.warning("新纠错热键不可用，已恢复: %s", format_hotkey(old_mods, old_key))
                return False, "该组合无法注册（可能已被其他程序占用），已保留原来的纠错快捷键。"
        except Exception:
            pass
        log.error("纠错热键新旧组合均无法注册: new=%s old=%s",
                  format_hotkey(modifiers, key_name), format_hotkey(old_mods, old_key))
        return False, "该组合无法注册，原纠错快捷键也未能恢复；请重启语润。"

    def set_hotkeys_suspended(self, suspended):
        """录制快捷键期间临时停用两个热键。

        必须停用：否则用户在设置里按下**当前已生效的组合**时，会真的触发录音
        或弹出纠错窗口，看起来像"设置界面坏了"。主热键可能注册在**提权助手**
        里，因此除了本进程的监听器，还要通知助手一起让出。
        """
        if suspended:
            try:
                if self._correct_hotkey is not None:
                    self._correct_hotkey.stop()
                if self._kb_listener is not None:
                    self._kb_listener.stop()
                    self._kb_listener = None
            except Exception as exc:
                log.debug("暂停纠错热键监听失败: %s", exc)
            if self.privileged_bridge and self.privileged_bridge.connected:
                self.privileged_bridge.request("suspend", {"on": True}, timeout=0.8)
            else:
                try:
                    self.hotkey.stop()
                except Exception as exc:
                    log.debug("暂停主热键失败: %s", exc)
            log.info("快捷键录制开始，已临时停用热键")
            return

        if self.privileged_bridge and self.privileged_bridge.connected:
            self.privileged_bridge.request("suspend", {"on": False}, timeout=0.8)
        else:
            key = self.cfg.get("hotkey") or "`"
            mode = self.cfg.get("trigger_mode") or "hold"
            mods = int(self.cfg.get("hotkey_modifiers") or 0)
            try:
                self.hotkey.start(key, mode, mods)
            except Exception as exc:
                log.warning("恢复主热键失败: %s", exc)
        key, mods = self._correction_combo()
        try:
            listener = HotkeyListener()
            listener.on_hold_start = self._on_correct_hotkey_fired
            if listener.start(key, "hold", modifiers=mods):
                self._correct_hotkey = listener
        except Exception as exc:
            log.warning("恢复纠错热键失败: %s", exc)
        log.info("快捷键录制结束，热键已恢复")

    def _is_active_session(self, round_id):
        """只有最新语音轮次可以更新浮窗或执行输入；None 是非会话 UI 事件。"""
        return round_id is None or round_id == self._active_session_id

    def _event_round_id(self, evt):
        """提取会话事件携带的 round_id；兼容非会话 UI 事件。"""
        kind = evt[0]
        if kind in {"recording", "transcribing", "retrying", "refining", "done", "stream_insert_done"}:
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
            elif kind == "retrying":
                # 仅当已松手且完整最终结果丢失时触发一次；不把 partial 当作输入。
                self.indicator.show_retrying()
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
                log.info("纠错窗口 UI 事件开始")
                self._show_correction_dialog(evt[1] if len(evt) > 1 else None)
            elif kind == "bridge_hotkey":
                self._handle_privileged_hotkey(evt[1] if len(evt) > 1 else {})
            elif kind == "bridge_event":
                event = evt[1] if len(evt) > 1 else {}
                if event.get("event") == "connection_lost":
                    self._handle_privileged_bridge_lost(event.get("_bridge"))
            elif kind == "bridge_lost":
                self._handle_privileged_bridge_lost(evt[1] if len(evt) > 1 else None)
            elif kind == "bridge_connected":
                self._adopt_privileged_bridge(evt[1] if len(evt) > 1 else None)
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
        """桥接线程只投递事件，由 Tk 主线程处理热键和连接状态。"""
        if event.get("event") in {"hotkey_down", "hotkey_up"}:
            self.ui_q.put(("bridge_hotkey", event))
        else:
            self.ui_q.put(("bridge_event", event))

    def _handle_privileged_hotkey(self, event):
        if event.get("event") == "hotkey_down":
            self._on_hold_start(None, helper_event=event)
        elif event.get("event") == "hotkey_up":
            self._on_hold_end(None)

    def _start_local_hotkey(self):
        """Start the normal-permission fallback exactly once."""
        if self._local_hotkey_active or self._quit:
            return
        ok = self.hotkey.start(self.cfg.get("hotkey"),
                               self.cfg.get("trigger_mode", "hold"),
                               int(self.cfg.get("hotkey_modifiers") or 0))
        self._local_hotkey_active = bool(ok)
        if not ok:
            self.ui_q.put(("toast", "热键无效"))

    def _stop_local_hotkey(self):
        if not self._local_hotkey_active:
            return
        try:
            self.hotkey.stop()
        except Exception:
            log.exception("停止普通权限热键失败")
        finally:
            self._local_hotkey_active = False

    def _handle_privileged_bridge_lost(self, bridge=None):
        """Drop a dead helper and keep ordinary windows usable."""
        if bridge is not None and bridge is not self.privileged_bridge:
            bridge.close()
            return
        old = self.privileged_bridge
        self.privileged_bridge = None
        if old is not None:
            old.close()
        # A helper hotkey-up event cannot arrive after a disconnect. Stop the
        # active recording explicitly so the next fallback hotkey starts clean.
        active = self._sessions.get(self._recording_session_id)
        if active and active.helper_session_id:
            active.stop_event.set()
            self._recording_session_id = None
            self.ui_q.put(("error", "高权限输入助手已断开" , active.round_id))
        self._start_local_hotkey()
        self.ui_q.put(("toast", "高权限输入助手已断开，正在恢复"))

    def _adopt_privileged_bridge(self, bridge):
        if self._quit:
            bridge.close()
            return
        if self._recording_session_id is not None:
            # Do not change the input authority in the middle of a recording.
            bridge.close()
            return
        old = self.privileged_bridge
        self.privileged_bridge = bridge
        if old is not None and old is not bridge:
            old.close()
        self._stop_local_hotkey()
        log.info("高权限输入助手已恢复，已切回助手热键")
        self.ui_q.put(("toast", "高权限输入助手已恢复"))

    def _wake_privileged_helper(self):
        """Ask the installed scheduled task to start the helper, at most once per 30s."""
        now = time.monotonic()
        if now - self._last_helper_wake < 30.0:
            return
        self._last_helper_wake = now
        try:
            import subprocess
            from input_helper_setup import TASK_NAME
            result = subprocess.run(
                ["schtasks", "/Run", "/TN", TASK_NAME],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5,
            )
            if result.returncode != 0:
                log.debug("唤醒高权限输入助手失败: %s", result.stderr or result.stdout)
        except Exception as exc:
            log.debug("请求启动高权限输入助手失败: %s", exc)

    def _privileged_health_tick(self):
        if self._quit or self._privileged_health_stop.is_set():
            return
        self.root.after(5000, self._privileged_health_tick)
        with self._privileged_health_lock:
            if self._privileged_health_busy:
                return
            self._privileged_health_busy = True

        def check():
            try:
                bridge = self.privileged_bridge
                if bridge is not None and bridge.connected:
                    if bridge.request("ping", timeout=0.5) is None:
                        self.ui_q.put(("bridge_lost", bridge))
                    return
                self._wake_privileged_helper()
                # The task starts asynchronously; a short delay avoids racing
                # the helper's bind when reconnecting after a crash.
                time.sleep(0.5)
                candidate = PrivilegedBridge(on_event=self._on_privileged_bridge_event)
                if candidate.connect(timeout=0.6):
                    self.ui_q.put(("bridge_connected", candidate))
                else:
                    candidate.close()
            finally:
                with self._privileged_health_lock:
                    self._privileged_health_busy = False

        from privileged_ipc import PrivilegedBridge
        threading.Thread(target=check, daemon=True, name="yurun-helper-health").start()

    def _start_privileged_health_monitor(self):
        self.root.after(5000, self._privileged_health_tick)

    def _on_correct_key(self, _key):
        """纠错热键（左 Alt + 反引号）触发：转主线程弹「错误纠正」框。"""
        log.info("纠错热键触发")
        # 在后台热键线程立刻记住目标窗口；不要等 Tk 队列稍后处理时再取前台窗口，
        # 否则 Cindy 等应用可能已经短暂失焦，导致选区和定位对象不对。
        try:
            target_hwnd = _user32.GetForegroundWindow()
        except Exception:
            target_hwnd = None
        self.ui_q.put(("show_correction", target_hwnd))
        log.info("纠错窗口请求已进入 UI 队列")

    def _on_correct_hotkey_fired(self, _key=None):
        """RegisterHotKey 路径的纠错热键回调（热键消息线程）。

        **不再区分左右 Alt**（用户 2026-09-20 决策："不区分，是 Alt 就算"）。
        `RegisterHotKey` 的 `MOD_ALT` 本来就不区分左右，事后想区分只能靠
        `GetAsyncKeyState`，而它在**提权前台窗口**（Cindy）下对非提权进程一律
        返回 0 —— 旧实现正是在这里把按键静默丢弃（实测 Cindy 里连按 5 次全被
        忽略、WorkBuddy 下同一按键正常）。既然关键时刻读不到，就统一按 Alt
        处理，代价是右 Alt 也会触发。
        """
        self._on_correct_key(None)

    def _correction_combo(self):
        """返回配置里的纠错热键 (键名, 修饰位)。缺字段时回落 Alt+`。"""
        key = self.cfg.get("correction_hotkey") or "`"
        mods = self.cfg.get("correction_hotkey_modifiers")
        if mods is None:
            mods = MOD_ALT
        try:
            mods = int(mods)
        except (TypeError, ValueError):
            mods = MOD_ALT
        return key, mods

    def _start_correct_hotkey(self):
        """注册纠错热键（默认 Alt + 反引号；可在设置里自定义）。

        必须走 RegisterHotKey，不能用键盘钩子：非提权进程的低层键盘钩子
        （pynput / WH_KEYBOARD_LL）在**提权窗口**获得焦点时收不到按键。
        实测（Cindy 提权焦点下按左Alt+`）：
            RegisterHotKey  命中 9/9 次
            pynput 钩子     命中 0/9 次
        这正是主热键要交给提权助手接管、而纠错热键在 Cindy 里"没反应"的原因。
        RegisterHotKey 由系统在输入处理阶段匹配，与前台窗口的完整性级别无关，
        所以"抓住按键"不需要提权（把文字注入提权窗口才需要助手）。

        注：v0.1.17 记录过"第二热键注册失败"，当时注册的是**裸**反引号，
        与主热键是同一个组合，必然冲突；默认的 Alt+` 与裸反引号是两个不同
        组合，可以共存（用户自定义后由设置界面负责查重）。
        """
        key, mods = self._correction_combo()
        try:
            listener = HotkeyListener()
            listener.on_hold_start = self._on_correct_hotkey_fired
            if listener.start(key, "hold", modifiers=mods):
                self._correct_hotkey = listener
                log.info("纠错热键监听已启动: %s（RegisterHotKey，提权窗口下亦可捕获）",
                         format_hotkey(mods, key))
                return
            log.warning("纠错热键 RegisterHotKey 注册失败，回退 pynput 钩子")
        except Exception as exc:
            log.warning("纠错热键注册异常，回退 pynput 钩子: %s", exc)
        self._start_correct_hotkey_pynput()

    def _start_correct_hotkey_pynput(self):
        """回退路径：pynput 低层钩子。

        ⚠️ 仅在**非提权**前台窗口下有效。若前台是提权窗口（如 Cindy），
        钩子收不到按键，纠错热键会表现为"完全没反应"。
        """
        try:
            from pynput import keyboard as _kb
            self._kb = _kb
            self._kb_listener = _kb.Listener(
                on_press=self._kb_on_press, on_release=self._kb_on_release)
            self._kb_listener.daemon = True
            self._kb_listener.start()
            key, mods = self._correction_combo()
            log.info("纠错热键监听已启动: %s（pynput 回退；提权窗口下不可用）",
                     format_hotkey(mods, key))
        except Exception as e:
            log.warning("pynput 纠错热键监听启动失败: %s", e)

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
        """备份当前剪贴板的原生格式，失败返回 None。"""
        try:
            from clipboard_transaction import capture_clipboard
            snapshot = capture_clipboard()
            if snapshot is not None:
                return snapshot
        except Exception as exc:
            log.warning("原生剪贴板备份不可用: %s", exc)
        return None

    def _clipboard_restore(self, backup, expected_sequence=None):
        """Restore text only while the clipboard is still owned by this flow.

        A delayed restore must never overwrite content copied by the user after
        the correction action started.  Full-format clipboard preservation is a
        separate Windows integration task; this guard protects the more severe
        race in the current text fallback.
        """
        if backup is None:
            return False
        if expected_sequence is not None:
            current_sequence = _clipboard_sequence()
            if (current_sequence is not None
                    and current_sequence != expected_sequence):
                log.info("用户已更新剪贴板，跳过纠错内容还原")
                return False
        try:
            from clipboard_transaction import ClipboardSnapshot, restore_clipboard
            if isinstance(backup, ClipboardSnapshot):
                return restore_clipboard(backup)
            self.root.clipboard_clear()
            self.root.clipboard_append(backup)
            self.root.update()
            return True
        except Exception:
            return False

    def _send_ctrl_c(self):
        """向当前焦点窗口发 Ctrl+C，把选中文本送入剪贴板。"""
        try:
            import pyautogui
            pyautogui.hotkey("ctrl", "c")
        except Exception as e:
            log.warning("自动 Ctrl+C 失败: %s", e)

    def _privileged_copy_selection(self, hwnd) -> bool:
        """请提权助手把 hwnd 的选中文字复制到剪贴板。

        非提权进程做不到这一步，两个原因叠加：`SetForegroundWindow` 无法把
        提权窗口设为前台；即使焦点对，`SendInput` 也会被 UIPI 拦下。实测在
        Cindy 里表现为「纠错选中文本未取到（剪贴板序列号未变化）」，而
        WorkBuddy 等普通权限程序一直正常。

        助手提权执行「置前 + Ctrl+C」后，剪贴板内容仍由本进程读取 ——
        跨完整性级别**读**剪贴板是允许的。
        返回 True 表示 Ctrl+C 已由助手发出，调用方照原流程读剪贴板即可。
        """
        bridge = self.privileged_bridge
        if bridge is None or not bridge.connected:
            return False
        try:
            reply = bridge.copy_selection(int(hwnd))
        except Exception as exc:
            log.warning("高权限复制选区请求失败，回退普通路径: %s", exc)
            return False
        if not reply or not reply.get("ok"):
            log.info("高权限复制选区未成功，回退普通路径: %s", reply)
            return False
        log.info("高权限复制选区完成（Ctrl+C 由提权助手发出）")
        return True

    def _replace_correction_selection(self, correct, target_hwnd):
        """将弹窗打开前的选区替换为 correct，并恢复用户原有剪贴板文本。"""
        if not correct or not target_hwnd:
            return False
        backup = self._clipboard_backup()
        if backup is None:
            log.warning("无法安全备份剪贴板，取消纠错替换")
            return False
        owned_sequence = None
        try:
            # 这是纠正操作本身的必要聚焦，不属于录音/插入流程的焦点策略。
            if _user32.GetForegroundWindow() != target_hwnd:
                self._steal_focus(target_hwnd)
            self.root.clipboard_clear()
            self.root.clipboard_append(correct)
            self.root.update()
            owned_sequence = _clipboard_sequence()
            import pyautogui
            pyautogui.hotkey("ctrl", "v")
            # Ctrl+V 消费完内容后再恢复，既不污染剪贴板也不打断替换。
            self.root.after(180, lambda: self._clipboard_restore(
                backup, expected_sequence=owned_sequence))
            return True
        except Exception as exc:
            self._clipboard_restore(backup, expected_sequence=owned_sequence)
            log.warning("纠正替换当前选区失败: %s", exc)
            return False

    def _correction_clipboard_arm(self):
        """进入纠错窗口的剪贴板事务：先记下用户原有剪贴板内容。"""
        self._correction_clip_backup = self._clipboard_backup()
        self._correction_clip_sequence = None
        self._correction_clip_armed = self._correction_clip_backup is not None
        if not self._correction_clip_armed:
            log.warning("无法安全备份剪贴板，取消自动读取纠错选区")
        return self._correction_clip_armed

    def _correction_clipboard_restore(self, expected_sequence=None):
        """还原用户剪贴板；幂等，窗口确认/取消/重建/异常各路径都要调用。"""
        if not self._correction_clip_armed:
            return
        self._correction_clip_armed = False
        sequence = (expected_sequence if expected_sequence is not None
                    else self._correction_clip_sequence)
        self._clipboard_restore(self._correction_clip_backup,
                                expected_sequence=sequence)
        self._correction_clip_backup = None
        self._correction_clip_sequence = None

    def _correction_geometry(self, W2, H2, selection_hwnd):
        """决定纠错窗口左上角坐标，返回 (x, y, source, work_area)。

        降级顺序：上次拖动位置 → 热键触发时的目标窗口 → 主显示器中央。
        每一步都校验落点属于某个显示器工作区，最后再强制收进工作区，
        保证窗口不会像旧实现那样停在可见区域之外（曾出现 y=-513）。
        """
        # 1) 用户上次拖动的位置（仍落在某个显示器工作区内才恢复）
        try:
            saved = self.cfg.get("correction_window_position")
        except Exception:
            saved = None
        if isinstance(saved, dict):
            sx = sy = None
            try:
                sx, sy = int(saved["x"]), int(saved["y"])
            except (KeyError, TypeError, ValueError):
                pass
            if sx is not None:
                cx, cy = sx + W2 // 2, sy + H2 // 2
                work = _monitor_work_area_for_point(cx, cy)
                # 必须确认窗口与该显示器还有重叠。屏幕外的坐标也会拿到"最近"
                # 显示器的工作区，只判 work 真假会把 y=-513 当成有效位置，
                # 窗口被 clamp 后粘在屏幕边缘，用户很难看出它其实"没恢复到原位"。
                if work and _rect_intersects_work(sx, sy, W2, H2, work):
                    x, y = _clamp_rect_to_work(sx, sy, W2, H2, work)
                    return x, y, "saved", work
                if work:
                    log.info("纠错窗口保存位置已不在任何显示器内，改用当前目标窗口")

        # 2) 依次尝试 目标窗口 → 焦点窗口 → pill 锚点，取第一个有效矩形。
        #    注意 _focus_rect 在 GetWindowRect 失败时返回的是全 0 矩形（不是 None），
        #    必须显式跳过无效矩形，否则后面的 pill 锚点永远没有机会被用到。
        candidates = []
        try:
            target_rect = _window_rect(selection_hwnd)
        except Exception as exc:
            log.debug("纠错窗口读取目标窗口失败: %s", exc)
            target_rect = None
        if target_rect:
            candidates.append(("target_window", target_rect))
        try:
            from pill import _focus_rect
            focus_rect = _focus_rect(selection_hwnd)
        except Exception:
            focus_rect = None
        if focus_rect:
            candidates.append(("focus_rect", focus_rect))
        try:
            pill_rect = self._pill_anchor()
        except Exception:
            pill_rect = None
        if pill_rect:
            candidates.append(("anchor", pill_rect))

        for name, rect in candidates:
            pl, pt, pr, pb = rect
            if pr <= pl or pb <= pt:
                continue
            work = (_monitor_work_area_for_point((pl + pr) // 2, (pt + pb) // 2)
                    or _primary_work_area())
            if not work:
                continue
            x, y = _clamp_rect_to_work(
                (pl + pr) // 2 - W2 // 2, pt - H2 - 12, W2, H2, work)
            return x, y, name, work

        # 3) 兜底：主显示器工作区，保证窗口一定可见
        work = _primary_work_area() or (0, 0, W2, H2)
        x, y = _clamp_rect_to_work((work[0] + work[2] - W2) // 2,
                                   (work[1] + work[3] - H2) // 3,
                                   W2, H2, work)
        return x, y, "primary_fallback", work

    def _kb_on_press(self, key):
        """pynput 钩子（后台线程）：配置的纠错热键 → 纠错弹窗。防重复触发。

        用 vk（虚拟键码）判断主键：char 受键盘布局/输入法影响（中文输入法下
        反引号键的 char 可能是「·」而非「`」），vk 恒定可靠。
        修饰键要求**精确匹配**配置值：配了 Alt+` 时按 Ctrl+Alt+` 不会触发。
        """
        try:
            bit = pynput_mod_bit(key)
            if bit:
                self._kb_mods |= bit
                return
            if self._kb_correct_fired:
                return
            target_key, target_mods = self._correction_combo()
            if self._kb_mods == target_mods and pynput_vk(key) == _vk_for(target_key):
                self._kb_correct_fired = True
                self._on_correct_key(None)
        except Exception:
            pass

    def _kb_on_release(self, key):
        try:
            bit = pynput_mod_bit(key)
            if bit:
                self._kb_mods &= ~bit
                return
            target_key, _mods = self._correction_combo()
            if pynput_vk(key) == _vk_for(target_key):
                self._kb_correct_fired = False
        except Exception:
            pass

    # ================= 纠错弹窗（词库学习入口） =================
    def _show_correction_dialog(self, target_hwnd=None):
        """「错误纠正」弹窗：macOS/Apple 浅色风格（稳定 pack 布局版）。

        位置：贴 pill 气泡正上方 12px（水平居中于 pill）。B1 识别文本自动读
        选中文字（方案A：备份剪贴板 → Ctrl+C → 读选中 → 恢复剪贴板），打开时
        聚焦"正确写法"。
        """
        # 单例：连续按热键只保留一个纠错窗口。旧窗口先安全收尾（还原它持有的
        # 剪贴板备份），再建新窗口，避免多窗口叠加、多个剪贴板事务互相覆盖
        # （后开的窗口会把前一个窗口复制进剪贴板的选区当成"用户原剪贴板"）。
        prev = self._correction_win
        if prev is not None:
            try:
                if prev.winfo_exists():
                    log.info("纠错窗口已存在，先关闭旧窗口再重建")
                    prev.destroy()
            except Exception as exc:
                log.debug("关闭旧纠错窗口失败: %s", exc)
            self._correction_win = None
            self._correction_clipboard_restore()

        # 本轮窗口的代际号：旧窗口残留的回调靠它识别自己已经过期。
        self._correction_gen += 1
        my_gen = self._correction_gen

        from gui import TEXT, TEXT_DIM, ACCENT, FONT, PillButton
        from pill import TRANSPARENT, _round_rect_items, _focus_rect

        try:
            from dictionary import add_entry
        except Exception as e:
            log.error("词库模块加载失败: %s", e)
            self.ui_q.put(("error", "词库失败"))
            return

        # 在创建弹窗前保存原应用窗口；确认时只把已选中的原文字替换掉。
        try:
            selection_hwnd = target_hwnd or _user32.GetForegroundWindow()
        except Exception:
            selection_hwnd = target_hwnd
        try:
            title_buf = ctypes.create_unicode_buffer(256)
            _user32.GetWindowTextW(selection_hwnd, title_buf, len(title_buf))
            class_buf = ctypes.create_unicode_buffer(128)
            _user32.GetClassNameW(selection_hwnd, class_buf, len(class_buf))
            log.info("纠错目标窗口: hwnd=%s title=%r class=%r", selection_hwnd,
                     title_buf.value, class_buf.value)
        except Exception:
            pass

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
        selected_text = ""

        # B1+方案A：仍然自动复制选中内容，但不再让剪贴板读取决定窗口
        # 是否能显示。Cindy 等应用的剪贴板响应可能延迟或暂时无响应；窗口
        # 先出现，读取在窗口可见后进行，失败时只影响“识别文本”自动填充。
        def _finish_selection_capture(seq_before, refocus=False):
            nonlocal selected_text
            restore_sequence = None
            if my_gen != self._correction_gen:
                # 本窗口已被新窗口取代，剪贴板事务也已由新窗口接管，
                # 这里再动剪贴板会把新窗口的备份状态消费掉。
                return
            try:
                # 剪贴板序列号没变，说明 Ctrl+C 没真正写入剪贴板（目标未响应
                # 或焦点没切过去），此时读到的是旧内容，不能当成用户选中的文字。
                seq_after = _clipboard_sequence()
                if (seq_before is not None and seq_after is not None
                        and seq_before == seq_after):
                    log.info("纠错选中文本未取到（剪贴板序列号未变化）")
                else:
                    restore_sequence = seq_after
                    _sel = self.root.clipboard_get()
                    selected_text = (_sel or "").strip()
                    if selected_text:
                        wrong_var.set(selected_text[:120])
                        log.info("纠错选中文本读取完成: length=%s", len(selected_text))
                        # 文本变长会让「识别文本」标签换行、需求高度变大，
                        # 必须重新布局，否则底部按钮会被裁掉。
                        _reflow()
                    else:
                        log.info("纠错选中文本为空")
            except Exception as exc:
                log.warning("纠错选中文本读取失败，仍保留纠错窗口: %s", exc)
            finally:
                self._correction_clipboard_restore(restore_sequence)
            if refocus:
                # 走的是助手置前路径：目标窗口刚被拉到前台，得把纠错窗口拉回来，
                # 否则用户接着在「正确写法」里打字会打不进。与 220ms 的
                # _focus_correction_window 同效，只是补一次防止 IPC 慢时错过。
                try:
                    if win.winfo_exists():
                        win.lift()
                        win.attributes("-topmost", True)
                        win.focus_force()
                except Exception as exc:
                    log.debug("纠错窗口重新置前失败: %s", exc)

        def _capture_selection_after_show():
            if my_gen != self._correction_gen:
                return
            log.info("纠错窗口已显示，开始读取选中文本")
            seq_before = None
            copied_by_helper = False
            try:
                if not self._correction_clipboard_arm():
                    return
                seq_before = _clipboard_sequence()
                # 首选提权助手：目标若是提权程序（Cindy 等），只有助手能把它
                # 置前并发 Ctrl+C —— 非提权进程这两步都会被系统拦掉。
                if selection_hwnd:
                    copied_by_helper = self._privileged_copy_selection(selection_hwnd)
                if not copied_by_helper:
                    # 回退：普通权限路径（WorkBuddy / ChatGPT 等一直走这里）。
                    # 目标窗口仍由 selection_hwnd 标识；必要时短暂恢复其前台，
                    # 确保 Ctrl+C 不会复制到纠错窗口自身。
                    if selection_hwnd:
                        self._steal_focus(selection_hwnd)
                    self._send_ctrl_c()
            except Exception as exc:
                log.warning("纠错选区复制失败，仍保留空白识别文本: %s", exc)
            # 给目标应用一个很短的时间写入剪贴板；不在 Tk 主线程 sleep。
            win.after(80, lambda: _finish_selection_capture(seq_before, copied_by_helper))

        # 圆角白底：canvas 用 place 铺满窗口作背景画圆角白底，body 不透明白底内缩
        # 8px 浮在上层。中间完全不透明遮住背后内容，外圈 8px 露出 canvas 圆角白底
        # 形成圆角边框（对齐 pill.py 写法）。
        canvas = tk.Canvas(win, bg=TRANSPARENT, highlightthickness=0, bd=0)
        canvas.place(x=0, y=0, relwidth=1, relheight=1)

        body = tk.Frame(win, bg="#FFFFFF")
        # body 不透明白底、内缩 8px 浮在 canvas 圆角白底之上：
        # 中间完全不透、遮住背后文字；外圈 8px 露出 canvas 圆角白底形成圆角边框。
        body.place(x=8, y=8, width=W2 - 16, height=10)

        # 窗口尺寸定好后画圆角白底（铺满，四角透明）。
        # 显式接受尺寸：窗口在 deiconify 之前 winfo_width() 还是 1，而定位流程
        # 此时已知确切的 W2/H2，不必再靠轮询等待窗口映射。
        def _draw_bg(w=None, h=None):
            try:
                if w is None or h is None:
                    w, h = win.winfo_width(), win.winfo_height()
                if w <= 1 or h <= 1:
                    return
                canvas.configure(width=w, height=h)
                _round_rect_items(canvas, 0, 0, w, h, 18, "#FFFFFF")
                # 把 canvas 降到 body 之下。tk.Canvas.lower 是 tag_lower 别名（必须带
                # tagOrId），无参会 TclError；用 widget 级 lower 命令绕过别名。
                canvas.tk.call('lower', canvas._w)
            except Exception as exc:
                log.debug("纠错窗口背景绘制失败: %s", exc)

        H2_DEFAULT = 490   # 高度算不出来时的兜底值

        def _measure_height():
            """按当前内容求窗口总高（含上下各 8px 内缩的圆角边框）。"""
            try:
                win.update_idletasks()
                h = body.winfo_reqheight() + 16
                # 布局尚未完成时 winfo_reqheight 可能是异常小值，不能直接用
                return h if h > 120 else H2_DEFAULT
            except Exception as exc:
                log.warning("纠错窗口高度计算失败，使用默认高度: %s", exc)
                return H2_DEFAULT

        def _apply_size(px, py, h):
            """把窗口摆到 (px, py) 并设为 W2×h，同步 body 与圆角背景。"""
            try:
                win.geometry(f"{W2}x{int(h)}+{int(px)}+{int(py)}")
                body.place_configure(width=W2 - 16, height=int(h) - 16)
            except Exception as exc:
                log.error("纠错窗口几何设置失败: %s", exc)
            _draw_bg(W2, h)

        def _reflow():
            """内容变化后重算高度并重新摆放。

            识别文本是窗口显示之后才异步填入的：填入长文本会让「识别文本」
            标签换行、需求高度明显变大。body 用 place 且高度是写死的，不会
            自己撑高，所以不再算一次就会把底部按钮裁掉——这正是"复制的字
            太多时下面的按钮没了"的原因。
            """
            h = _measure_height()
            try:
                cur_x, cur_y = win.winfo_x(), win.winfo_y()
            except Exception:
                cur_x, cur_y = 0, 0
            try:
                # 高度没变就不重排，避免无谓的重绘和视觉跳动。
                if abs(win.winfo_height() - h) < 2:
                    log.debug("纠错窗口高度未变化（%s），跳过重排", h)
                    return h
            except Exception:
                pass
            work = (_monitor_work_area_for_point(cur_x + W2 // 2, cur_y + h // 2)
                    or _primary_work_area())
            if work:
                # 长文本可能把窗口撑得比工作区还高：先限高，再整体收进工作区，
                # 保证按钮那一行始终留在屏幕内。
                max_h = work[3] - work[1] - 16
                if 0 < max_h < h:
                    log.debug("纠错窗口高度 %s 超过工作区，限制为 %s", h, max_h)
                    h = max_h
                cur_x, cur_y = _clamp_rect_to_work(cur_x, cur_y, W2, h, work)
            _apply_size(cur_x, cur_y, h)
            return h

        # ===== Header（兼作拖动把手：overrideredirect 无标题栏，需手动绑拖拽）=====
        header = tk.Frame(body, bg="#FFFFFF", cursor="fleur")
        header.pack(fill="x", padx=PAD, pady=(PAD, 0))

        _position_save_after = None

        def _remember_position():
            """保存用户拖动后的坐标，供下次打开优先恢复。"""
            nonlocal _position_save_after
            _position_save_after = None
            try:
                x, y = win.winfo_x(), win.winfo_y()
                self.cfg.set("correction_window_position", {"x": int(x), "y": int(y)})
                log.info("纠错窗口位置已保存: x=%s y=%s", x, y)
            except Exception as exc:
                log.debug("保存纠错窗口位置失败: %s", exc)

        def _schedule_remember_position():
            nonlocal _position_save_after
            if _position_save_after:
                try:
                    win.after_cancel(_position_save_after)
                except Exception:
                    pass
            _position_save_after = win.after(220, _remember_position)

        def _close_correction_window():
            if my_gen != self._correction_gen:
                # 已被新窗口取代：不能再写共享状态（位置/剪贴板/单例引用），
                # 否则会用旧窗口的坐标和事务覆盖掉新窗口的。
                try:
                    if win.winfo_exists():
                        win.destroy()
                except Exception as exc:
                    log.debug("销毁过期纠错窗口失败: %s", exc)
                return
            _remember_position()
            # 窗口可能在任何时刻关闭（用户按 Esc / 点取消 / 被单例重建顶掉）。
            # 必须在这里还原用户剪贴板：排队中的读取回调会随窗口销毁而不再执行，
            # 否则用户剪贴板会被永久留在"被 Ctrl+C 覆盖过"的状态。
            self._correction_clipboard_restore()
            self._correction_win = None
            try:
                if win.winfo_exists():
                    win.destroy()
            except Exception as exc:
                log.debug("销毁纠错窗口失败: %s", exc)

        def _drag_start(e):
            win._drag_x = e.x_root - win.winfo_x()
            win._drag_y = e.y_root - win.winfo_y()

        def _drag_move(e):
            win.geometry(f"+{e.x_root - win._drag_x}+{e.y_root - win._drag_y}")
            _schedule_remember_position()

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
            win.after(1200, _close_correction_window)

        footer = tk.Frame(body, bg="#FFFFFF")
        footer.pack(fill="x", padx=PAD, pady=(0, PAD))
        status = tk.Label(footer, textvariable=status_var, bg="#FFFFFF", fg="#34C759",
                          font=FONT(16))
        status.pack(side="left")

        # 占位 spacer 把按钮推到右侧；pack 顺序：先 right 的会后出现，因此先 pack 存入词库（最右），再 pack 取消（左侧）
        tk.Frame(footer, bg="#FFFFFF").pack(side="left", fill="x", expand=True)
        PillButton(footer, "替换并存入词库", _confirm, primary=True, min_w=150).pack(side="right")
        PillButton(footer, "取消", _close_correction_window, primary=False,
                   weight="normal", min_w=90).pack(side="right", padx=(0, 12))

        # ===== 定位与显示：先在隐藏状态下算好尺寸和坐标，再一次性显示 =====
        # 旧实现先 win.deiconify()、30ms 后才跑 _place()：窗口会先用 Tk 默认
        # geometry 出现在未定位的（可能屏幕外）位置；一旦 _place 中途抛错，
        # 窗口就永远留在那里，表现成"按了热键却看不见窗口"（Cindy 场景下的
        # 主要故障形态）。现在改为：算高度 → 定坐标 → 设 geometry → 才显示。
        H2 = _measure_height()

        try:
            x, y, source, work = self._correction_geometry(W2, H2, selection_hwnd)
        except Exception as exc:
            # 定位兜底本身也失败时，至少把窗口放到主显示器左上角，绝不留在屏幕外。
            log.error("纠错窗口定位异常，回退主屏: %s", exc)
            work = _primary_work_area() or (0, 0, W2, H2)
            x, y, source = work[0] + 40, work[1] + 40, "error_fallback"

        log.info("纠错窗口定位: source=%s work_area=(%s,%s,%s,%s) x=%s y=%s w=%s h=%s",
                 source, work[0], work[1], work[2], work[3], x, y, W2, H2)

        _apply_size(x, y, H2)

        # 打开时聚焦正确写法；留出选区复制/读取时间，避免焦点过早回到弹窗。
        def _focus_correction_window():
            try:
                # 再执行一次显示与置顶：若上面首次 deiconify 因异常未生效，
                # 这里是最后一次补救机会（窗口已显示时 deiconify 是空操作）。
                win.deiconify()
                win.lift()
                win.attributes("-topmost", True)
                win.focus_force()
                e2.focus_set()
            except Exception as exc:
                log.debug("纠错窗口重新置顶失败: %s", exc)
        win.after(220, _focus_correction_window)

        e2.bind("<Return>", lambda _e: _confirm())
        win.bind("<Escape>", lambda _e: _close_correction_window())

        # 尺寸与位置都已确定，窗口第一次出现就落在正确位置。
        try:
            win.deiconify()
            win.lift()
            win.attributes("-topmost", True)
            win.focus_force()
        except Exception as exc:
            log.error("纠错窗口显示失败: %s", exc)
        log.info("纠错窗口已显示")
        self._correction_win = win
        # 先显示窗口，再做自动读取；即使 Cindy 的剪贴板卡住，用户仍能手动填写。
        win.after(10, _capture_selection_after_show)

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
                on_retry=lambda _reason: self.ui_q.put(("retrying", round_id)),
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

        # Send a bounded batch.  The old one-character path made normal input
        # wait on Tk scheduling and made elevated input perform one IPC request
        # per character.  A bound keeps target-window checks frequent without
        # sacrificing the fast direct-input path.
        batch = job["buffer"][:TYPE_BATCH_CHARS]
        job["buffer"] = job["buffer"][len(batch):]
        try:
            from typer import input_event_count, type_text
            session = self._sessions.get(job["round_id"])
            target_hwnd = session.target_hwnd if session else None
            if session and session.helper_session_id:
                # 高权限目标由助手输入。助手会二次确认原窗口仍在前台；不满足则安全取消，
                # 绝不写入用户后来切换到的新窗口。
                if not self.privileged_bridge or not self.privileged_bridge.connected:
                    self._abort_type_job(job, "privileged_helper_disconnected")
                    return
                sent = self.privileged_bridge.type_text(session.helper_session_id, batch)
            else:
                # 每个队列项只回到自己录音开始时的目标窗口，不能借用最新一轮的全局目标。
                if target_hwnd and _user32.GetForegroundWindow() != target_hwnd:
                    self._steal_focus(target_hwnd)
                sent = type_text(batch)
            expected = input_event_count(batch)
            if sent >= expected:
                self._record_first_insert(job["round_id"])
                job["sent_chars"] += len(batch)
                if self._is_active_session(job["round_id"]):
                    self._advance_preview_progress(job["sent_chars"])
            else:
                # A partial batch cannot be safely retried: some characters may
                # already be in the target, so retrying could duplicate text.
                self._abort_type_job(job, f"sent={sent}")
                return
        except Exception as e:
            log.warning("批量 SendInput 失败: %s", e)
            self._abort_type_job(job, "exception")
            return
        # 删除预览仍由独立的慢速定时器驱动，不拖慢文字提交。
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
        self._privileged_health_stop.set()
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
            if self._correct_hotkey:
                self._correct_hotkey.stop()
                self._correct_hotkey = None
        except Exception:
            pass
        try:
            if self._kb_listener:
                self._kb_listener.stop()
                self._kb_listener = None
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
            self._start_local_hotkey()
        self._start_privileged_health_monitor()
        # 纠错热键：默认 Alt + 反引号（可在设置里自定义）—— 首选 RegisterHotKey
        # （理由与实测数据见 _start_correct_hotkey 的注释：提权前台窗口下钩子
        # _start_correct_hotkey 的注释：提权前台窗口下钩子收不到按键）；
        # 注册被占用时才回退 pynput 钩子。
        self._start_correct_hotkey()
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
