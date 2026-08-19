"""Yurun refine enhancer (TEST copy, does not touch src): streaming first-token + record-time warmup.

Mirrors Cindy two latency optimizations on top of refiner.py batch request:
1) refine_stream(): OpenAI-compatible SSE /chat/completions, emits accumulated text via on_partial
   as soon as the first delta arrives (Cindy onTextSnapshot / onPartial).
2) warmup_refiner(): at recording start (not on release) sends a placeholder request with identical
   prefix but empty dictationText to heat the upstream prompt cache (Cindy buildWarmupRequest).

Runtime switches (controlled by test_gui_toggle.py checkboxes via env / module vars):
  STREAMING_ENABLED / WARMUP_ENABLED
  - streaming off -> refine_stream falls back to refiner.refine_text (batch, same as main flow).
  - warmup off -> start_warmup_thread is a no-op.
Stdlib only (urllib + json). Works with Volcengine/DeepSeek/any OpenAI-compatible endpoint.
Run `python test/refiner_stream.py` for offline self-test (SSE parse + payload shape), no network.
"""
import json
import os
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path

_THIS = Path(__file__).resolve()
_PROJECT_SRC = (_THIS.parent.parent / "src") if _THIS.parent.name == "test" else _THIS.parent
if str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))
_PROJECT_ROOT = _PROJECT_SRC.parent

STREAMING_ENABLED = os.environ.get("YURUN_REFINE_STREAMING", "1") not in ("0", "false", "False")
WARMUP_ENABLED = os.environ.get("YURUN_REFINE_WARMUP", "1") not in ("0", "false", "False")

# Ensure project src is importable whether run from src/ or test/.
if str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))
try:
    from logger import get_logger
except Exception:
    def get_logger(_n):
        import logging
        return logging.getLogger(_n)
from logger import get_logger
log = get_logger("yurun.refiner_stream")

PROMPT_VERSION = "dictation-refinement.zh.v17"


def _resource_path(*parts):
    try:
        base = Path(getattr(sys, "_MEIPASS", _PROJECT_ROOT))
    except Exception:
        base = _PROJECT_ROOT
    return base.joinpath(*parts)


PROMPT_ZH = _resource_path("prompts", "refine-dictation.zh.txt")
PROMPT_EN = _resource_path("prompts", "refine-dictation.en.txt")


def _load_prompt(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _build_user_payload(dictation_text, language, custom_instructions,
                        user_dictionary, selection_before, selection_after):
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
    return json.dumps({
        "promptVersion": PROMPT_VERSION,
        "context": context,
        "dictationText": dictation_text,
    }, ensure_ascii=False)


def _sse_parse_content(raw):
    deltas = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        try:
            delta = obj["choices"][0]["delta"].get("content")
        except Exception:
            delta = None
        if delta:
            deltas.append(delta)
    return deltas


def refine_stream(
    text,
    api_key,
    api_base="https://ark.cn-beijing.volces.com/api/v3",
    model="",
    custom_instructions="",
    user_dictionary="",
    selection_before="",
    selection_after="",
    language="zh",
    proxy="",
    timeout=30,
    disable_thinking=True,
    prompt_path=None,
    on_partial=None,
):
    if not STREAMING_ENABLED:
        try:
            from refiner import refine_text
            return refine_text(
                text=text, api_key=api_key, api_base=api_base, model=model,
                custom_instructions=custom_instructions, language=language,
                proxy=proxy, disable_thinking=disable_thinking, timeout=timeout,
            )
        except Exception as e:
            return {"ok": False, "text": text, "reason": "fallback_exception", "error": str(e)}
    from refiner import (
        normalize_text, normalize_output_text, is_diverged, extract_json,
    )

    source = normalize_text(text)
    if not source:
        return {"ok": False, "text": "", "reason": "empty_input"}

    system = _load_prompt(prompt_path or PROMPT_ZH)
    if not system:
        return {"ok": False, "text": source, "reason": "prompt_missing"}

    user = _build_user_payload(source, language, custom_instructions,
                               user_dictionary, selection_before, selection_after)

    url = api_base.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **({"thinking": {"type": "disabled"}} if disable_thinking else {}),
    }, ensure_ascii=False)

    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + api_key)
    req.add_header("Accept", "text/event-stream")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()

    accumulated = ""
    try:
        with opener.open(req, timeout=timeout) as resp:
            buf = ""
            while True:
                chunk = resp.read(1)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="ignore")
                if buf.endswith("\n\n"):
                    deltas = _sse_parse_content(buf)
                    buf = ""
                    for d in deltas:
                        accumulated += d
                        if on_partial:
                            partial = normalize_output_text(accumulated)
                            if partial:
                                on_partial(partial)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        return {"ok": False, "text": source, "reason": f"http_{e.code}", "error": detail}
    except Exception as e:
        return {"ok": False, "text": source, "reason": "network", "error": str(e)}

    parsed = extract_json(accumulated)
    refined = normalize_output_text(parsed.get("text") if isinstance(parsed, dict) else accumulated)
    if not refined:
        return {"ok": False, "text": source, "reason": "empty_output"}
    if normalize_output_text(source) == refined:
        return {"ok": False, "text": source, "reason": "no_change"}
    if is_diverged(source, refined):
        return {"ok": False, "text": source, "reason": "diverged_too_far"}
    return {"ok": True, "text": refined, "reason": "ok"}


def warmup_refiner(
    api_key,
    api_base="https://ark.cn-beijing.volces.com/api/v3",
    model="",
    language="zh",
    custom_instructions="",
    user_dictionary="",
    proxy="",
    prompt_path=None,
    timeout=10,
):
    try:
        system = _load_prompt(prompt_path or PROMPT_ZH)
        if not system:
            return
        user = _build_user_payload(
            dictation_text="",
            language=language,
            custom_instructions=custom_instructions,
            user_dictionary=user_dictionary,
            selection_before="",
            selection_after="",
        )
        url = api_base.rstrip("/") + "/chat/completions"
        body = json.dumps({
            "model": model,
            "stream": True,
            "max_tokens": 1,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **({"thinking": {"type": "disabled"}} if True else {}),
        }, ensure_ascii=False)
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + api_key)
        req.add_header("Accept", "text/event-stream")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=timeout) as resp:
            try:
                resp.read(256)
            except Exception:
                pass
    except Exception as e:
        log.debug("warmup failed (non-fatal): %s", e)


def start_warmup_thread(cfg=None):
    import config as _cfg_mod
    c = cfg if cfg is not None else _cfg_mod.get_config()
    if not WARMUP_ENABLED:
        return None
    if not c.get("refine_enabled", True) or not c.get("api_key"):
        return None
    t = threading.Thread(
        target=warmup_refiner,
        kwargs=dict(
            api_key=c.get("api_key"),
            api_base=c.get("api_base"),
            model=c.get("api_model"),
            language=c.get("language", "zh"),
            custom_instructions=c.get("custom_instructions", ""),
            user_dictionary=c.get("user_dictionary", ""),
            proxy=c.get("proxy", ""),
        ),
        daemon=True,
    )
    t.start()
    return t


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _P
    _src = str(_P(__file__).resolve().parent.parent / "src")
    if _src not in _sys.path:
        _sys.path.insert(0, _src)

    sample = 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: {"choices":[{"delta":{"content":"world"}}]}\n\ndata: [DONE]\n'
    print("SSE deltas:", _sse_parse_content(sample))
    print("PROMPT_VERSION:", PROMPT_VERSION)
    print("payload head:", _build_user_payload("test text", "zh", "keep", "Codex", "before", "after")[:90])
    print("STREAMING_ENABLED=", STREAMING_ENABLED, "WARMUP_ENABLED=", WARMUP_ENABLED)
