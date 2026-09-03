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
from hotkey import HotkeyListener
from logger import get_logger, install_crash_handler, log_startup_banner
from privileged_ipc import HOST, PORT, PROTOCOL_VERSION, _JsonSocket, _proof, helper_secret
from typer import type_text

log = get_logger("yurun.input_helper")
user32 = ctypes.windll.user32


class InputHelper:
    def __init__(self):
        self._listener = None
        self._conn = None
        self._send_lock = threading.Lock()
        self._running = True
        self._hotkey = HotkeyListener()
        self._sessions: dict[str, int] = {}
        self._sequence = 0

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
        if not self._hotkey.start(cfg.get("hotkey"), cfg.get("trigger_mode", "hold")):
            raise RuntimeError("输入助手热键启动失败")
        log.info("高权限输入助手已启动")
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
                self._reply(request_id, {"ok": True, "protocol": PROTOCOL_VERSION})
            elif kind == "ping":
                self._reply(request_id, {"ok": True})
            elif kind == "reconfigure":
                self._reply(request_id, self._reconfigure(payload))
            elif kind == "type":
                self._reply(request_id, self._type(payload))
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
        key_name = payload.get("hotkey")
        trigger_mode = payload.get("trigger_mode")
        if not isinstance(key_name, str) or trigger_mode not in ("hold", "toggle"):
            return {"ok": False, "reason": "invalid_configuration"}
        old_key = self._hotkey._key_name
        old_mode = self._hotkey.trigger_mode
        self._hotkey.stop()
        if self._hotkey.start(key_name, trigger_mode):
            log.info("输入助手热键已更新: %s (%s)", key_name, trigger_mode)
            return {"ok": True}
        # 配置失败时恢复旧入口，不能让用户失去语音输入。
        self._hotkey.stop()
        self._hotkey.start(old_key, old_mode)
        return {"ok": False, "reason": "hotkey_unavailable"}

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


def main():
    install_crash_handler()
    log_startup_banner()
    helper = InputHelper()
    signal.signal(signal.SIGTERM, helper.stop)
    signal.signal(signal.SIGINT, helper.stop)
    helper.run()


if __name__ == "__main__":
    main()
