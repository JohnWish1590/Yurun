"""语润高权限输入助手。

此进程没有窗口、没有网络访问、不读取或保存键盘文本。它只做两件事：
1. 捕获已配置的语润主热键；2. 向该次热键锁定且仍在前台的窗口发送最终文字。
"""
from __future__ import annotations

import ctypes
import hmac
import signal
import secrets
import socket
import threading
import time

from config import get_config
from hotkey import HotkeyListener, format_hotkey
from logger import get_logger, install_crash_handler, log_startup_banner
from privileged_ipc import HOST, PORT, PROTOCOL_VERSION, _JsonSocket, _proof, helper_secret
from typer import send_ctrl_c, type_text

log = get_logger("yurun.input_helper")
user32 = ctypes.windll.user32

VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

# 助手自报的增量能力。老版本客户端不认识这个字段（直接忽略），新客户端用
# `PrivilegedBridge.supports()` 判断某条新命令能不能用 —— 否则会出现"设置保存了
# 但实际没生效"（例如老助手会静默丢掉 reconfigure 里的 modifiers）。
CAPABILITIES = ("copy_selection", "suspend", "hotkey_modifiers")


class InputHelper:
    def __init__(self):
        self._listener = None
        self._conn = None
        self._send_lock = threading.Lock()
        self._running = True
        self._hotkey = HotkeyListener()
        self._sessions: dict[str, int] = {}
        self._sequence = 0
        # 当前生效的主热键组合，供暂停/恢复使用。
        self._suspended = False
        self._active_hotkey = "`"
        self._active_mode = "hold"
        self._active_modifiers = 0

    def run(self):
        secret = helper_secret(create=True)
        if secret is None:
            raise RuntimeError("无法初始化输入助手密钥")
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((HOST, PORT))
        self._listener.listen(1)
        cfg = get_config()
        self._hotkey.on_hold_start = self._on_hotkey_down
        self._hotkey.on_hold_end = self._on_hotkey_up
        self._hotkey.on_toggle = self._on_hotkey_toggle
        key_name = cfg.get("hotkey")
        mode = cfg.get("trigger_mode", "hold")
        try:
            modifiers = int(cfg.get("hotkey_modifiers") or 0)
        except (TypeError, ValueError):
            modifiers = 0
        if not self._hotkey.start(key_name, mode, modifiers):
            raise RuntimeError("输入助手热键启动失败")
        self._active_hotkey, self._active_mode, self._active_modifiers = (
            key_name, mode, modifiers)
        log.info("高权限输入助手已启动，主热键 %s", format_hotkey(modifiers, key_name))
        while self._running:
            raw, _address = self._listener.accept()
            conn = _JsonSocket(raw)
            try:
                if self._authenticate(conn, secret):
                    self._conn = conn
                    self._serve_connection(conn)
            except Exception as exc:
                log.info("输入助手客户端已断开: %s", exc)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
                self._conn = None
                # 客户端（主程序）消失时必须把热键还回来，否则用户会永久失去
                # 语音输入 —— 暂停状态是主程序在录制快捷键时设的，它一走就再也
                # 没人来恢复了。
                if self._suspended:
                    self._resume_hotkey()
        self._shutdown()

    def stop(self, *_args):
        self._running = False
        try:
            if self._listener:
                self._listener.close()
        except Exception:
            pass

    def _shutdown(self):
        self._hotkey.stop()
        try:
            self._listener.close()
        except Exception:
            pass

    def _send(self, message: dict):
        conn = self._conn
        if conn is None:
            return
        try:
            with self._send_lock:
                conn.send(message)
        except Exception:
            pass

    def _reply(self, request_id, payload):
        self._send({"kind": "reply", "request_id": str(request_id), "payload": payload})

    def _serve_connection(self, conn):
        while self._running:
            message = conn.recv()
            if not isinstance(message, dict):
                continue
            request_id = message.get("request_id")
            payload = message.get("payload") or {}
            kind = message.get("kind")
            if kind == "hello":
                self._reply(request_id, {"ok": True, "protocol": PROTOCOL_VERSION,
                                         "capabilities": list(CAPABILITIES)})
            elif kind == "ping":
                self._reply(request_id, {"ok": True})
            elif kind == "reconfigure":
                self._reply(request_id, self._reconfigure(payload))
            elif kind == "suspend":
                self._reply(request_id, self._suspend(payload))
            elif kind == "type":
                self._reply(request_id, self._type(payload))
            elif kind == "copy_selection":
                self._reply(request_id, self._copy_selection(payload))
            else:
                self._reply(request_id, {"ok": False, "reason": "unknown_command"})

    @staticmethod
    def _authenticate(conn, secret: bytes) -> bool:
        try:
            hello = conn.recv()
            nonce = hello.get("nonce")
            if hello.get("kind") != "hello" or not isinstance(nonce, str):
                return False
            if not hmac.compare_digest(hello.get("proof", ""), _proof(secret, nonce)):
                return False
            server_nonce = secrets.token_hex(24)
            conn.send({"kind": "hello_ok", "nonce": server_nonce, "proof": _proof(secret, server_nonce)})
            return True
        except Exception:
            return False

    def _on_hotkey_down(self, _key):
        target = int(user32.GetForegroundWindow() or 0)
        if not target:
            return
        self._sequence += 1
        session_id = str(self._sequence)
        self._sessions[session_id] = target
        self._send({"kind": "event", "payload": {
            "event": "hotkey_down", "session_id": session_id, "target_hwnd": target,
        }})

    def _on_hotkey_up(self, _key):
        self._send({"kind": "event", "payload": {"event": "hotkey_up"}})

    def _on_hotkey_toggle(self, _key, pressed):
        self._on_hotkey_down(_key) if pressed else self._on_hotkey_up(_key)

    def _reconfigure(self, payload: dict) -> dict:
        """更新主热键（支持修饰键组合）。

        `modifiers` 是本轮新增字段：老客户端不带它，按裸键处理以保持既有行为；
        新客户端会带 MOD_* 位掩码。热键在助手进程里注册，所以**修饰键必须由
        客户端传过来**，否则助手上还是旧的裸键。
        """
        key_name = payload.get("hotkey")
        trigger_mode = payload.get("trigger_mode")
        modifiers = payload.get("modifiers", 0)
        if not isinstance(key_name, str) or trigger_mode not in ("hold", "toggle"):
            return {"ok": False, "reason": "invalid_configuration"}
        try:
            modifiers = int(modifiers or 0)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid_configuration"}
        old = (self._active_hotkey, self._active_mode, self._active_modifiers)
        self._hotkey.stop()
        if self._hotkey.start(key_name, trigger_mode, modifiers):
            self._active_hotkey = key_name
            self._active_mode = trigger_mode
            self._active_modifiers = modifiers
            self._suspended = False
            log.info("输入助手热键已更新: %s (%s)",
                     format_hotkey(modifiers, key_name), trigger_mode)
            return {"ok": True}
        # 配置失败时恢复旧入口，不能让用户失去语音输入。
        self._hotkey.stop()
        self._hotkey.start(*old)
        self._suspended = False
        log.warning("输入助手热键更新失败，已恢复: %s", format_hotkey(old[2], old[0]))
        return {"ok": False, "reason": "hotkey_unavailable"}

    def _suspend(self, payload: dict) -> dict:
        """录制快捷键期间临时让出主热键。

        主程序在设置界面录制组合键时必须先暂停热键，否则用户按下**当前已生效**
        的组合会真的触发录音。主热键注册在助手进程里，所以只能由客户端请求助手
        让出。只停主热键，`type` / `copy_selection` 等能力保持不变。
        """
        want = bool(payload.get("on"))
        if want == self._suspended:
            return {"ok": True, "suspended": self._suspended}
        self._hotkey.stop()
        self._suspended = want
        if want:
            log.info("主热键已暂停（客户端正在录制快捷键）")
            return {"ok": True, "suspended": True}
        if self._resume_hotkey():
            return {"ok": True, "suspended": False}
        return {"ok": False, "reason": "hotkey_restore_failed", "suspended": True}

    def _resume_hotkey(self) -> bool:
        """按记下的组合恢复主热键；失败则保持暂停并记错误日志。"""
        if not self._hotkey.start(self._active_hotkey, self._active_mode,
                                  self._active_modifiers):
            log.error("恢复主热键失败: %s",
                      format_hotkey(self._active_modifiers, self._active_hotkey))
            return False
        self._suspended = False
        log.info("主热键已恢复: %s",
                 format_hotkey(self._active_modifiers, self._active_hotkey))
        return True

    def _type(self, payload: dict) -> dict:
        session_id = str(payload.get("session_id") or "")
        text = payload.get("text")
        target = self._sessions.get(session_id)
        if not target or not isinstance(text, str) or not text:
            return {"ok": False, "reason": "invalid_session_or_text"}
        # 安全底线：用户主动切走窗口时不把文字写进新窗口，也不强行抢回焦点。
        if int(user32.GetForegroundWindow() or 0) != target:
            log.warning("高权限输入取消：目标窗口已不在前台 session=%s", session_id)
            return {"ok": False, "reason": "target_not_foreground"}
        sent = type_text(text)
        log.info("高权限输入: session=%s text_length=%s sent=%s", session_id, len(text), sent)
        return {"ok": sent >= 2, "sent": sent}

    def _copy_selection(self, payload: dict) -> dict:
        """把指定窗口的选中文字复制进剪贴板（提权执行）。

        主程序是非提权进程，既无法把提权窗口拉到前台（`SetForegroundWindow`
        被系统拒绝），也无法向它 `SendInput`（被 UIPI 拦下）—— 实测在 Cindy
        里表现为「纠错选中文本未取到（剪贴板序列号未变化）」。助手是提权的，
        因此可以代做这一步；剪贴板写入后由主程序读取（跨完整性级别**读**剪贴板
        是允许的）。
        """
        try:
            hwnd = int(payload.get("hwnd") or 0)
        except (TypeError, ValueError):
            hwnd = 0
        if not hwnd or not user32.IsWindow(hwnd):
            return {"ok": False, "reason": "invalid_window", "sent": 0}

        # 安全底线：目标窗口没能置前就**不发** Ctrl+C。否则复制到的是别的窗口
        # 的内容，会把无关文字当成"识别文本"填进纠错窗口。
        if not self._force_foreground(hwnd):
            log.warning("高权限复制选区取消：目标窗口未能置于前台 hwnd=%s", hwnd)
            return {"ok": False, "reason": "target_not_foreground",
                    "sent": 0, "foreground_ok": False}

        time.sleep(0.06)          # 让焦点切换生效，避免 Ctrl+C 打到旧焦点窗口
        sent = send_ctrl_c()
        log.info("高权限复制选区: hwnd=%s sent=%s", hwnd, sent)
        return {"ok": sent >= 2, "sent": sent, "foreground_ok": True}

    @staticmethod
    def _force_foreground(hwnd: int) -> bool:
        """把 hwnd 拉到前台；返回最终是否确实在前台。

        前台锁会让 `SetForegroundWindow` 直接失败，所以先制造一次 Alt 按下/抬起，
        使本进程成为"最近有输入"的进程再调用（与主程序 `_steal_focus` 同一手法）。
        助手已提权，因此对提权窗口同样生效。
        """
        try:
            if int(user32.GetForegroundWindow() or 0) == hwnd:
                return True
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.SetForegroundWindow(hwnd)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        except Exception as exc:
            log.warning("目标窗口置前失败: %s", exc)
            return False
        time.sleep(0.05)
        return int(user32.GetForegroundWindow() or 0) == hwnd


def main():
    install_crash_handler()
    log_startup_banner()
    helper = InputHelper()
    signal.signal(signal.SIGTERM, helper.stop)
    signal.signal(signal.SIGINT, helper.stop)
    helper.run()


if __name__ == "__main__":
    main()
