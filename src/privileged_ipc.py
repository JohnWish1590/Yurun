"""语润高权限输入助手的本机通信。

Windows 会阻止普通权限进程连接由高权限进程创建的默认命名管道。这里改用
仅绑定 127.0.0.1 的回环套接字，并以随机 32 字节密钥进行挑战握手；没有密钥
的本机进程不能伪装助手或发送输入请求。

注意：助手密钥是**机器级**的（见 `_helper_secret_dir`）。它必须与已安装的
唯一助手共用同一个命名空间，不能按版本（Stable / Pre）隔离，否则连不上助手，
提权程序里的语音输入会整个失效。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import secrets
import socket
import threading
from pathlib import Path

from logger import get_logger

log = get_logger("yurun.privileged_ipc")

PROTOCOL_VERSION = 1
HOST = "127.0.0.1"
PORT = 47689
_SECRET_FILE = "input-helper.secret"


def _helper_secret_dir() -> Path:
    """输入助手密钥所在目录 —— **机器级，不按版本隔离**。

    助手是"一台机器一个"的常驻组件：登录计划任务始终启动已安装的
    ``C:\\Program Files\\语润\\YurunInputHelper.exe``（提权、无 YURUN_PRE
    环境变量），因此它读到的密钥只会是 ``%APPDATA%\\Yurun``。

    早期版本把密钥也按版本隔离（Pre 读 ``Yurun-Pre``），结果 Pre 拿着自己的
    密钥去和"已安装助手"握手，HMAC 校验失败、连接被关闭，日志只留下
    ``高权限输入助手暂不可用: connection_closed``。失去提权通路后，
    Pre 无法向提权程序（如 Cindy）注入文字 —— 表现为"在 Cindy 里连录音都
    启动不了"。密钥必须与那个唯一助手保持同一命名空间，只有配置/词库/日志
    才按版本隔离。
    """
    base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    path = base / "Yurun"
    path.mkdir(parents=True, exist_ok=True)
    return path


def helper_secret(create: bool = False) -> bytes | None:
    """读取助手密钥；仅初始化/安装时允许创建。"""
    path = _helper_secret_dir() / _SECRET_FILE
    try:
        value = path.read_bytes()
        if len(value) == 32:
            return value
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("读取输入助手密钥失败: %s", exc)
        return None
    if not create:
        return None
    value = secrets.token_bytes(32)
    try:
        with path.open("xb") as f:
            f.write(value)
        return value
    except FileExistsError:
        return helper_secret(create=False)
    except Exception as exc:
        log.warning("创建输入助手密钥失败: %s", exc)
        return None


def _proof(secret: bytes, nonce: str) -> str:
    return hmac.new(secret, nonce.encode("ascii"), hashlib.sha256).hexdigest()


class _JsonSocket:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buffer = bytearray()
        self._send_lock = threading.Lock()

    def send(self, message: dict):
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._send_lock:
            self.sock.sendall(raw)

    def recv(self) -> dict:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("invalid_message")
                return value
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection_closed")
            self._buffer.extend(chunk)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class PrivilegedBridge:
    """主程序侧客户端。连接失败时调用方走现有普通权限路径。"""

    def __init__(self, on_event=None):
        self._conn: _JsonSocket | None = None
        self._reader = None
        self._running = False
        self._waiters: dict[str, queue.Queue] = {}
        self._waiters_lock = threading.Lock()
        self._sequence = 0
        self._on_event = on_event
        # 助手自报的能力集合。老版本助手不带这个字段，于是这里是空集——调用方
        # 据此判断"新命令发过去也会被拒"，避免出现"设置保存了但实际没生效"。
        self.capabilities: set[str] = set()

    @property
    def connected(self) -> bool:
        return self._running and self._conn is not None

    def supports(self, capability: str) -> bool:
        """助手是否支持某条增量命令；未连接或老版本助手都返回 False。"""
        return capability in self.capabilities

    def connect(self, timeout: float = 0.6) -> bool:
        secret = helper_secret(create=False)
        if secret is None:
            return False
        raw = None
        try:
            raw = socket.create_connection((HOST, PORT), timeout=timeout)
            raw.settimeout(timeout)
            conn = _JsonSocket(raw)
            nonce = secrets.token_hex(24)
            conn.send({"kind": "hello", "nonce": nonce, "proof": _proof(secret, nonce)})
            reply = conn.recv()
            if (reply.get("kind") != "hello_ok" or not isinstance(reply.get("nonce"), str)
                    or not hmac.compare_digest(reply.get("proof", ""), _proof(secret, reply["nonce"]))):
                raise ConnectionError("helper_authentication_failed")
            raw.settimeout(None)
            self._conn = conn
            self._running = True
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
            hello = self.request("hello", {"protocol": PROTOCOL_VERSION}, timeout=timeout)
            if not hello or hello.get("protocol") != PROTOCOL_VERSION:
                self.close()
                return False
            caps = hello.get("capabilities")
            self.capabilities = set(caps) if isinstance(caps, (list, tuple)) else set()
            log.info("已连接高权限输入助手（能力: %s）",
                     ", ".join(sorted(self.capabilities)) or "基础")
            return True
        except Exception as exc:
            log.debug("高权限输入助手暂不可用: %s", exc)
            if raw is not None and self._conn is None:
                try:
                    raw.close()
                except OSError:
                    pass
            self.close()
            return False

    def close(self):
        self._running = False
        self.capabilities = set()
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()
        with self._waiters_lock:
            for waiter in self._waiters.values():
                try:
                    waiter.put_nowait(None)
                except queue.Full:
                    pass
            self._waiters.clear()

    def request(self, kind: str, payload: dict | None = None, timeout: float = 0.8) -> dict | None:
        if not self.connected:
            return None
        with self._waiters_lock:
            self._sequence += 1
            request_id = str(self._sequence)
            waiter: queue.Queue = queue.Queue(maxsize=1)
            self._waiters[request_id] = waiter
        try:
            self._conn.send({"kind": kind, "request_id": request_id, "payload": payload or {}})
            return waiter.get(timeout=timeout)
        except Exception as exc:
            log.warning("输入助手请求失败: kind=%s error=%s", kind, exc)
            self.close()
            return None
        finally:
            with self._waiters_lock:
                self._waiters.pop(request_id, None)

    def type_character(self, helper_session_id: str, text: str) -> int:
        reply = self.request("type", {"session_id": helper_session_id, "text": text}, timeout=0.8)
        if not reply or not reply.get("ok"):
            return 0
        return int(reply.get("sent") or 0)

    def copy_selection(self, hwnd: int, timeout: float = 1.5) -> dict | None:
        """请助手把 hwnd 的选中文字复制进剪贴板（提权执行）。

        非提权进程做不到：`SetForegroundWindow` 无法把提权窗口设为前台，且
        `SendInput` 会被 UIPI 拦下。助手提权执行后，剪贴板内容仍由本进程读取
        （跨完整性级别读剪贴板是允许的）。
        返回 {"ok": bool, "sent": int, "foreground_ok": bool} 或 None。
        """
        return self.request("copy_selection", {"hwnd": int(hwnd)}, timeout=timeout)

    def _read_loop(self):
        try:
            while self._running and self._conn is not None:
                message = self._conn.recv()
                request_id = message.get("request_id")
                if request_id:
                    with self._waiters_lock:
                        waiter = self._waiters.get(str(request_id))
                    if waiter is not None:
                        waiter.put(message.get("payload"))
                elif message.get("kind") == "event" and self._on_event:
                    self._on_event(message.get("payload") or {})
        except Exception as exc:
            if self._running:
                log.warning("高权限输入助手连接已断开: %s", exc)
        finally:
            self.close()
