"""语润（Yurun）润色模块：调用 OpenAI 兼容端点，复刻 Cindy v17 提示词 + 安全护栏。
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

from logger import get_logger
log = get_logger("yurun.refiner")

def _resource_path(*parts) -> Path:
    """兼容源码运行与 PyInstaller 打包：_MEIPASS 优先。"""
    try:
        import sys
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    except Exception:
        base = Path(__file__).resolve().parent.parent
    return base.joinpath(*parts)


PROMPT_ZH = _resource_path("prompts", "refine-dictation.zh.txt")
PROMPT_EN = _resource_path("prompts", "refine-dictation.en.txt")

DIVERGENCE_MIN_OUTPUT_CONTENT_CHARS = 48
DIVERGENCE_MIN_LENGTH_RATIO = 3


def _load_prompt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def normalize_text(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def normalize_output_text(s) -> str:
    t = str(s or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in t.split("\n")]
    t = "\n".join(lines)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def content_length(s) -> int:
    import unicodedata
    n = 0
    for ch in str(s):
        try:
            cat = unicodedata.category(ch)
            if cat.startswith("L") or cat.startswith("N"):
                n += 1
        except Exception:
            pass
    return n


def is_diverged(source: str, refined: str) -> bool:
    a = content_length(source)
    b = content_length(refined)
    if a == 0 or b < DIVERGENCE_MIN_OUTPUT_CONTENT_CHARS:
        return False
    return b >= a * DIVERGENCE_MIN_LENGTH_RATIO


def extract_json(text: str):
    """解析模型输出为 JSON：支持纯 JSON、```json 包裹、前后杂音。"""
    s = str(text or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            pass
    return None


def refine_text(
    text: str,
    api_key: str,
    api_base: str = "https://api.deepseek.com/v1",
    model: str = "deepseek-chat",
    custom_instructions: str = "",
    user_dictionary: str = "",
    selection_before: str = "",
    selection_after: str = "",
    language: str = "zh",
    proxy: str = "",
    timeout: int = 30,
    prompt_path: Path = None,
) -> dict:
    """润色听写文本。返回 {"ok": bool, "text": str, "reason": str}"""
    source = normalize_text(text)
    if not source:
        return {"ok": False, "text": "", "reason": "empty_input"}

    system = _load_prompt(prompt_path or PROMPT_ZH)
    if not system:
        return {"ok": False, "text": source, "reason": "prompt_missing"}

    context = {}
    if language and language != "auto":
        context["sourceLanguage"] = language
    if custom_instructions:
        context["userRefinementInstructions"] = custom_instructions
    if user_dictionary:
        context["userDictionary"] = user_dictionary
    if selection_before:
        context["selectionBefore"] = selection_before[-1200:]
    if selection_after:
        context["selectionAfter"] = selection_after[:1200]

    user = json.dumps({
        "promptVersion": "dictation-refinement.zh.v17",
        "context": context,
        "dictationText": source,
    }, ensure_ascii=False)

    url = api_base.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + api_key)
    if proxy:
        proxy_handler = urllib.request.ProxyHandler({
            "http": proxy, "https": proxy,
        })
        opener = urllib.request.build_opener(proxy_handler)
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
        return {"ok": False, "text": source, "reason": f"http_{e.code}", "error": detail}
    except Exception as e:
        return {"ok": False, "text": source, "reason": "network", "error": str(e)}

    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return {"ok": False, "text": source, "reason": "bad_response"}

    parsed = extract_json(content)
    refined = normalize_output_text(parsed.get("text") if isinstance(parsed, dict) else "")

    if not refined:
        return {"ok": False, "text": source, "reason": "empty_output"}
    if normalize_output_text(source) == refined:
        return {"ok": False, "text": source, "reason": "no_change"}
    if is_diverged(source, refined):
        return {"ok": False, "text": source, "reason": "diverged_too_far"}
    return {"ok": True, "text": refined, "reason": "ok"}