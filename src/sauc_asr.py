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

# websocket-client 0.57 内部用 threading.Thread.isAlive()，Python 3.12 已移除该方法（改为 is_alive），
# 这里补兼容别名，否则 WebSocketApp.run_forever 会抛 AttributeError。
if not hasattr(threading.Thread, "isAlive"):
    threading.Thread.isAlive = threading.Thread.is_alive

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


def _build_hotwords_context(hotwords) -> str:
    """火山热词直传 JSON：request.context = {"hotwords":[{"word":...}]}（≤200 tokens）。"""
    if not hotwords:
        return ""
    items = [{"word": w} for w in hotwords if w and w.strip()]
    if not items:
        return ""
    return json.dumps({"hotwords": items}, ensure_ascii=False)


def _build_full_request(language: str = "auto", hotwords=None) -> bytes:
    """构造 full client request（JSON + gzip）。hotwords: 正确词列表，用于提高术语识别率。"""
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
            "show_utterances": True,
        },
    }
    if language not in ("auto", ""):
        req["audio"]["language"] = language
    hw = _build_hotwords_context(hotwords)
    if hw:
        req["request"]["context"] = hw
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
    endpoint: str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
    language: str = "auto",
    proxy: str = "",
    timeout: int = 60,
    hotwords=None,
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
        ws.send_binary(_build_full_request(language, hotwords))
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
            ws.close(timeout=0.2)
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
                           endpoint: str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
                           language: str = "auto", proxy: str = "", timeout: int = 90,
                           hotwords=None, on_partial=None, on_timeline=None) -> str:
    """双向流式识别（bigmodel 端点）：边录边发，边收中间结果。

    chunk_iter: 迭代产生 int16 PCM bytes（如 recorder.record_chunks）。
    录音期间服务端边听边返回中间结果（这里只攒着、不回显，避免文字跳变），
    松手后发尾包 → 等 FLAG_LAST 最终结果 → 返回最终文本。

    用 WebSocketApp 回调模式：on_message 在 run_forever 线程，send 从录音线程
    调用，天然规避同步 WebSocket「跨线程 send/recv」竞争（之前导致 indicator 卡死的坑）。

    诊断回调（Phase 0/1，不影响主路径）：
    - on_partial(text): 每次收到服务端中间结果/最终结果时回调（主线程请自行投到 UI 队列）。
    - on_timeline(mark, t): 打点 T0..T7 时间戳（见 docs/架构诊断与重构方案.md §4 Phase 0）。
    """
    if not api_key:
        raise ValueError("SAUC 识别未配置 API Key")

    def _t(mark):
        if on_timeline:
            try:
                on_timeline(mark, time.time())
            except Exception:
                pass

    _t("T0")  # 建连前

    request_id = str(uuid.uuid4())
    connect_id = str(uuid.uuid4())
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
        "X-Api-Connect-Id": connect_id,
    }

    state = {
        "text": "",
        "error": None,
        "opened": threading.Event(),
        "done": threading.Event(),
        "first_partial": False,
        # 只有服务端明确带 FLAG_LAST 的结果才是可安全输入的完整文本。
        "final_received": False,
    }

    def _on_open(ws):
        ws.send(_build_full_request(language, hotwords), opcode=0x2)  # BINARY 帧
        state["opened"].set()
        _t("T1")  # 连接建立

    def _on_message(ws, message):
        if isinstance(message, str):
            message = message.encode("latin-1")
        try:
            parsed = _parse_response(message)
        except Exception as e:
            state["error"] = f"解析失败: {e}"
            state["done"].set()
            return
        if parsed.get("error"):
            state["error"] = f"{parsed.get('code')} {parsed.get('message')}"
            state["done"].set()
            return
        if parsed.get("text"):
            state["text"] = parsed["text"]
            if not state["first_partial"]:
                state["first_partial"] = True
                _t("T3")  # 首个 Partial
            if on_partial:
                try:
                    on_partial(state["text"])
                except Exception:
                    pass
        flags = (message[1] & 0x0F) if len(message) > 1 else 0
        if flags & 0b0010:  # FLAG_LAST 最终结果
            state["final_received"] = True
            _t("T6")  # Final 结果
            state["done"].set()

    def _on_error(ws, error):
        state["error"] = str(error)
        state["done"].set()

    def _on_close(ws, *args):
        # 网络中断时可能已经收到 partial；它不能伪装成完整识别结果并被输入。
        if not state["final_received"] and not state["error"]:
            state["error"] = "连接在收到最终结果前关闭"
        state["done"].set()

    ws = websocket.WebSocketApp(
        endpoint, header=headers,
        on_open=_on_open, on_message=_on_message, on_error=_on_error, on_close=_on_close,
    )

    run_kwargs = {"ping_interval": 20, "ping_timeout": 10}
    if proxy:
        run_kwargs["http_proxy_host"], run_kwargs["http_proxy_port"] = _parse_proxy(proxy)
        run_kwargs["proxy_type"] = "http"

    threading.Thread(target=ws.run_forever, kwargs=run_kwargs, daemon=True).start()

    # 等连接建立（on_open 触发）
    if not state["opened"].wait(timeout=15):
        try:
            ws.close(timeout=0.2)
        except Exception:
            pass
        raise RuntimeError("SAUC 连接超时")

    try:
        # 边录边发：录音线程持续发音频块，on_message 线程持续收中间结果（防缓冲区积压）
        first_chunk = True
        for chunk in chunk_iter:
            if first_chunk:
                _t("T2")  # 第一包 PCM
                first_chunk = False
            ws.send(_build_audio_packet(chunk, is_last=False), opcode=0x2)
        _t("T4")  # 最后包 PCM（松手）
        ws.send(_build_audio_packet(b"", is_last=True), opcode=0x2)
        _t("T5")  # 发 FLAG_LAST
        # 松手后等最终结果（FLAG_LAST）：完整不漏字。bigmodel 端点边听边识别，
        # 松手后最终结果已经很快（真实场景约 0.6~1s），无需冒险用中间结果
        # （中间结果缺最后一段音频的识别，会漏掉松手前最后 1 秒的话）。
        if not state["done"].wait(timeout=timeout):
            state["error"] = "识别超时"
    finally:
        try:
            ws.close(timeout=0.2)
        except Exception:
            pass
    _t("T7")  # 关闭

    if state["error"]:
        raise RuntimeError(f"SAUC 错误: {state['error']}")
    if not state["final_received"]:
        raise RuntimeError("SAUC 错误: 未收到最终识别结果")
    return state["text"].strip()
