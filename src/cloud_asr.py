"""语润（Yurun）云端 ASR 模块：OpenAI 兼容 /v1/audio/transcriptions。

支持任意兼容端点：
- 火山方舟 doubao-seed-asr（Cindy 同款云端，OpenAI 兼容）
- 硅基流动 / 阿里 / 自建
返回文本；失败时抛异常，由调用方处理。
"""
import json
import urllib.request
import urllib.error


def cloud_transcribe(
    wav_path: str,
    api_key: str,
    api_base: str,
    model: str,
    language: str = "auto",
    proxy: str = "",
    timeout: int = 60,
) -> str:
    """把音频 POST 到云端 ASR，返回识别文本。"""
    if not api_key:
        raise ValueError("云端识别未配置 API Key")
    if not api_base:
        raise ValueError("云端识别未配置 Base URL")
    if not model:
        raise ValueError("云端识别未配置模型")

    # 读音频文件（multipart/form-data）
    with open(wav_path, "rb") as f:
        audio = f.read()

    boundary = "----YurunBoundary" + __import__("uuid").uuid4().hex
    parts = []
    fields = {
        "model": model,
        "response_format": "json",
        "language": language if language not in ("auto", "") else "auto",
    }
    for k, v in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
            f"{v}\r\n".encode("utf-8")
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode("utf-8")
        + audio
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    url = api_base.rstrip("/") + "/audio/transcriptions"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", "Bearer " + api_key)

    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = urllib.request.build_opener()

    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise RuntimeError(f"云端识别失败 HTTP {e.code}: {detail}") from e
    except Exception as e:
        raise RuntimeError(f"云端识别网络错误: {e}") from e

    # 解析：标准 OpenAI 返回 {"text": "..."}；火山可能返回 JSON 数组或带 result
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for key in ("text", "result", "transcript"):
                if data.get(key):
                    return str(data[key]).strip()
        elif isinstance(data, list) and data:
            return "".join(x.get("text", "") for x in data).strip()
    except Exception:
        pass
    # 兜底：直接返回原始内容（很多端点返回纯文本）
    return raw.strip()