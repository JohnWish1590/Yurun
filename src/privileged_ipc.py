"""语润高权限输入助手的本机通信。

Windows 会阻止普通权限进程连接由高权限进程创建的默认命名管道。这里改用
仅绑定 127.0.0.1 的回环套接字，并以随机 32 字节密钥进行挑战握手；没有密钥
的本机进程不能伪装助手或发送输入请求。
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


def _app_data_dir() -> Path:
    name = "Yurun-Pre" if os.environ.get("YURUN_PRE") == "1" else "Yurun"
    base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def helper_secret(create: bool = False) -> bytes | None:
    """读取助手密钥；仅初始化/安装时允许创建。"""
    path = _app_data_dir() / _SECRET_FILE
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

    @property
    def connected(self) -> bool:
        return self._running and self._conn is not None

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
            log.info("已连接高权限输入助手")
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
