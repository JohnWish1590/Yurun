"""语润（Yurun）SAUC 流式语音识别客户端（火山引擎语音技术，Cindy 同款）。

协议：WebSocket 二进制，自定义 4 字节头 + payload size + payload。
流程（流式输入模式 bigmodel_nostream）：
1. 建连时带鉴权头（X-Api-Key / X-Api-Resource-Id / X-Api-Request-Id / X-Api-Connect-Id）
2. 发 full client request（JSON，含音频参数）
3. 分包发送音频（每包 ~100-200ms，gzip 压缩）
4. 发最后一包（负包，flags=0b0010）
5. 收最终识别结果

速度：5 秒音频约 300-400ms 返回。
"""
import gzip
import json
import os
import struct
import threading
import time
import uuid

import websocket
from logger import get_logger
log = get_logger("yurun.sauc")

# ---- 协议常量 ----
PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001  # 4 字节
MSG_FULL_CLIENT_REQUEST = 0b0001
MSG_AUDIO_ONLY_REQUEST = 0b0010
MSG_FULL_SERVER_RESPONSE = 0b1001
MSG_ERROR = 0b1111
FLAG_NONE = 0b0000
FLAG_LAST = 0b0010  # 最后一包（负包）
SER_JSON = 0b0001
SER_NONE = 0b0000
COMPRESS_GZIP = 0b0001
COMPRESS_NONE = 0b0000

SAMPLE_RATE = 16000
CHUNK_MS = 200  # 每包 200ms（文档推荐性能最优）
CHUNK_BYTES = int(SAMPLE_RATE * 2 * CHUNK_MS / 1000)  # 16bit mono


def _make_header(msg_type, flags, serialization, compression):
    b0 = (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE
    b1 = (msg_type << 4) | flags
    b2 = (serialization << 4) | compression
    b3 = 0
    return bytes([b0, b1, b2, b3])


def _build_full_request(language: str = "auto") -> bytes:
    """构造 full client request（JSON + gzip）。"""
    req = {
        "user": {"uid": "yurun-pc", "platform": "Windows"},
        "audio": {
            "format": "pcm",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_ddc": True,
            "enable_punc": True,
        },
    }
    if language not in ("auto", ""):
        req["audio"]["language"] = language
    payload = gzip.compress(json.dumps(req, ensure_ascii=False).encode("utf-8"))
    header = _make_header(MSG_FULL_CLIENT_REQUEST, FLAG_NONE, SER_JSON, COMPRESS_GZIP)
    return header + struct.pack(">I", len(payload)) + payload


def _build_audio_packet(audio_bytes: bytes, is_last: bool = False) -> bytes:
    flags = FLAG_LAST if is_last else FLAG_NONE
    header = _make_header(MSG_AUDIO_ONLY_REQUEST, flags, SER_NONE, COMPRESS_GZIP)
    payload = gzip.compress(audio_bytes)
    return header + struct.pack(">I", len(payload)) + payload


def _parse_response(data: bytes) -> dict:
    """解析服务端响应帧：header(4) + sequence(4) + payload_size(4) + payload。"""
    if len(data) < 12:
        return {"text": "", "raw": data.decode("utf-8", "ignore")}
    msg_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    payload_size = struct.unpack(">I", data[8:12])[0]
    payload = data[12:12 + payload_size]
    try:
        payload = gzip.decompress(payload)
    except Exception:
        pass
    try:
        obj = json.loads(payload.decode("utf-8"))
    except Exception:
        return {"text": "", "raw": payload.decode("utf-8", "ignore")}
    if msg_type == MSG_ERROR:
        return {"error": True, "code": obj.get("code"), "message": obj.get("message") or obj.get("error")}
    text = ""
    try:
        text = obj["result"]["text"]
    except Exception:
        try:
            text = obj.get("text", "")
        except Exception:
            pass
    return {"text": text, "raw": payload.decode("utf-8", "ignore")}


def sauc_transcribe(
    wav_path: str,
    api_key: str,
    resource_id: str = "volc.bigasr.sauc.duration",
    endpoint: str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream",
    language: str = "auto",
    proxy: str = "",
    timeout: int = 60,
) -> str:
    """整段 wav 流式识别，返回最终文本。"""
    if not api_key:
        raise ValueError("SAUC 识别未配置 API Key")

    # 读取 wav，转成 16bit PCM
    import soundfile as sf
    import numpy as np
    audio, sr = sf.read(wav_path, dtype="float32")
    if sr != 16000:
        raise ValueError(f"SAUC 需要 16kHz 音频，当前 {sr}Hz")
    pcm = (audio * 32767).astype("int16").tobytes()
    if len(pcm) == 0:
        raise ValueError("空音频")

    request_id = str(uuid.uuid4())
    connect_id = str(uuid.uuid4())
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
        "X-Api-Connect-Id": connect_id,
    }
    # 代理支持
    proxy_opts = {}
    if proxy:
        proxy_opts["http_proxy_host"], proxy_opts["http_proxy_port"] = _parse_proxy(proxy)
        proxy_opts["proxy_type"] = "http"

    ws = websocket.create_connection(
        endpoint,
        header=headers,
        timeout=timeout,
        **proxy_opts,
    )
    try:
        # 1. full client request
        ws.send_binary(_build_full_request(language))
        # 2. 分包发送音频
        total = len(pcm)
        offset = 0
        seq = 1
        while offset < total:
            chunk = pcm[offset:offset + CHUNK_BYTES]
            ws.send_binary(_build_audio_packet(chunk, is_last=False))
            offset += CHUNK_BYTES
            seq += 1
        # 3. 最后一包（负包，可为空）
        ws.send_binary(_build_audio_packet(b"", is_last=True))
        # 4. 收结果
        last_text = ""
        while True:
            data = ws.recv()
            if not data:
                continue
            parsed = _parse_response(data)
            if parsed.get("error"):
                raise RuntimeError(f"SAUC 错误: {parsed.get('code')} {parsed.get('message')}")
            if parsed.get("text"):
                last_text = parsed["text"]
            flags = (data[1] & 0x0F) if len(data) > 1 else 0
            if flags & 0b0010:  # 仅当 FLAG_LAST(0b0010) 时收尾；0b0001 是带 seq 的中间/ACK 帧，不能 break
                break
        return last_text.strip()
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _parse_proxy(proxy: str):
    """解析 http://host:port 或 host:port。"""
    s = proxy.strip()
    if s.startswith("http://"):
        s = s[7:]
    elif s.startswith("https://"):
        s = s[8:]
    host, _, port = s.partition(":")
    return host, int(port or 0)


def sauc_transcribe_stream(chunk_iter, api_key: str,
                           resource_id: str = "volc.bigasr.sauc.duration",
                           endpoint: str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream",
                           language: str = "auto", proxy: str = "", timeout: int = 90) -> str:
    """真流式识别（Cindy 同款）：边消费音频块边发送，并发接收最终文本。

    chunk_iter: 迭代产生 int16 PCM bytes（如 recorder.record_chunks）。
    返回最终识别文本。松开热键后迭代结束 → 自动发尾包 → 等待最终结果。
    """
    if not api_key:
        raise ValueError("SAUC 识别未配置 API Key")

    request_id = str(uuid.uuid4())
    connect_id = str(uuid.uuid4())
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
        "X-Api-Connect-Id": connect_id,
    }
    proxy_opts = {}
    if proxy:
        proxy_opts["http_proxy_host"], proxy_opts["http_proxy_port"] = _parse_proxy(proxy)
        proxy_opts["proxy_type"] = "http"

    ws = websocket.create_connection(endpoint, header=headers, timeout=timeout, **proxy_opts)
    ws.send_binary(_build_full_request(language))

    # 并发接收线程：服务端边收边增量返回，最后一包带 FLAG_LAST
    result = {"text": "", "error": None, "done": threading.Event()}

    def _recv_loop():
        try:
            while True:
                data = ws.recv()
                if not data:
                    continue
                parsed = _parse_response(data)
                if parsed.get("error"):
                    result["error"] = f"{parsed.get('code')} {parsed.get('message')}"
                    result["done"].set()
                    return
                if parsed.get("text"):
                    result["text"] = parsed["text"]
                flags = (data[1] & 0x0F) if len(data) > 1 else 0
                if flags & 0b0010:  # 仅当 FLAG_LAST(0b0010) 收尾
                    result["done"].set()
                    return
        except Exception as e:
            if result["error"] is None:
                result["error"] = str(e)
            result["done"].set()

    t = threading.Thread(target=_recv_loop, daemon=True)
    t.start()

    try:
        for chunk in chunk_iter:
            ws.send_binary(_build_audio_packet(chunk, is_last=False))
        ws.send_binary(_build_audio_packet(b"", is_last=True))
    except Exception as e:
        if result["error"] is None:
            result["error"] = str(e)

    if not result["done"].wait(timeout=timeout):
        if result["error"] is None:
            result["error"] = "SAUC 超时未返回最终结果"
    try:
        ws.close()
    except Exception:
        pass
    if result["error"]:
        raise RuntimeError(f"SAUC 错误: {result['error']}")
    return result["text"].strip()