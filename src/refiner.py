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


def strip_numeric_trailing_punct(s) -> str:
    """数字串/编号类文本去掉末尾标点（ASR 引擎会为纯数字预测句号）。

    背景：火山 SAUC 等 ASR 引擎自带标点预测，纯数字/手机号/订单号常被补末尾
    句号（如「12345。」），免润色 bypass 直接贴原文时句号跟着进来，改润色
    prompt 无效（根本没走 LLM）。故在代码层兜底：
    - 末尾是标点 且 文本数字占比 ≥ 60%（数字为主）→ 去掉末尾标点；
    - 正常句子（数字占比低）保留标点，不受影响。
    """
    t = str(s or "").strip()
    if not t:
        return str(s or "")
    digits = sum(1 for ch in t if ch.isdigit())
    alnum = sum(1 for ch in t if ch.isalnum())
    if alnum and digits / alnum >= 0.6:
        cleaned = t.rstrip("。．.！!？?；;，,、")
        if cleaned:
            return cleaned
    return t


def is_diverged(source: str, refined: str) -> bool:
    a = content_length(source)
    b = content_length(refined)
    if a == 0 or b < DIVERGENCE_MIN_OUTPUT_CONTENT_CHARS:
        return False
    return b >= a * DIVERGENCE_MIN_LENGTH_RATIO


BYPASS_MAX_LENGTH = 15  # 免润色阈值：有效字符 ≤ 此值直接贴原文，不调 LLM（短句 ASR 已够清楚）


def _should_bypass_llm(source: str, custom_instructions: str) -> bool:
    """短句且无自定义指令时跳过 LLM，直接用 ASR 原文，省一次网络往返。

    仅对较短、且无用户规则引导的输入生效；有自定义指令时必须走 LLM。
    用 content_length 统计有效字符（忽略标点/空白），阈值 15 字以内视为短句跳过。
    """
    if custom_instructions and custom_instructions.strip():
        return False
    return content_length(source) <= BYPASS_MAX_LENGTH


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


PROMPT_VERSION = "dictation-refinement.zh.v17"


def _build_user_payload(dictation_text, language, custom_instructions,
                        user_dictionary, selection_before, selection_after):
    """构造润色请求的 user payload。字段顺序即缓存前缀：稳定字段在前，易变字段在后。"""
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


STREAM_OUTPUT_INSTRUCTION = (
    "输出要求：\n"
    "- 直接返回最终要插入的文本本身，不要 JSON、不要引号、不要任何解释、标题、Markdown 代码块或前后缀说明。\n"
    "- 如果文本已经清楚，原样返回。"
)


def _load_stream_prompt(path=None):
    """流式用 system prompt：沿用 Cindy 原 prompt 精华，仅把「输出要求」段替换为纯文本直出。"""
    base = _load_prompt(path or PROMPT_ZH)
    if not base:
        return ""
    idx = base.find("输出要求")
    if idx != -1:
        base = base[:idx].rstrip()
    return base + "\n\n" + STREAM_OUTPUT_INSTRUCTION


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
    disable_thinking: bool = True,
    prompt_path: Path = None,
) -> dict:
    """润色听写文本。返回 {"ok": bool, "text": str, "reason": str}"""
    source = normalize_text(text)
    if not source:
        return {"ok": False, "text": "", "reason": "empty_input"}

    # 短句智能跳过：无自定义指令且 ≤4 字，直接回原文，省一次 LLM 网络往返
    if _should_bypass_llm(source, custom_instructions):
        return {"ok": False, "text": source, "reason": "bypass_short"}

    system = _load_prompt(prompt_path or PROMPT_ZH)
    if not system:
        return {"ok": False, "text": source, "reason": "prompt_missing"}

    user = _build_user_payload(source, language, custom_instructions,
                               user_dictionary, selection_before, selection_after)

    url = api_base.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # 关闭思考模式：DeepSeek-V4-Flash 等推理模型默认会先 reasoning_content 再出正文，
        # 对 Cindy 这套指令明确的润色任务是冗余，关掉可提速 1~2s。
        **({"thinking": {"type": "disabled"}} if disable_thinking else {}),
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


def refine_stream(
    text: str,
    api_key: str,
    api_base: str = "https://ark.cn-beijing.volces.com/api/v3",
    model: str = "",
    custom_instructions: str = "",
    user_dictionary: str = "",
    selection_before: str = "",
    selection_after: str = "",
    language: str = "zh",
    proxy: str = "",
    timeout: int = 30,
    disable_thinking: bool = True,
    prompt_path: Path = None,
    on_delta=None,
) -> dict:
    """流式润色：纯文本直出，边收边 on_delta(delta) 回调，用于首字即上屏。

    返回 {"ok": bool, "text": str, "reason": str}：
    - ok=True：text 为完整/部分润色文本（已通过 on_delta 逐段回显）。
    - ok=False：text 为原文，表示首字前失败（连接/HTTP/网络），调用方应回退整段 refine_text。
    """
    source = normalize_text(text)
    if not source:
        return {"ok": False, "text": "", "reason": "empty_input"}

    system = _load_stream_prompt(prompt_path or PROMPT_ZH)
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
    req.add_header("Accept", "text/event-stream")
    req.add_header("Accept-Encoding", "identity")
    req.add_header("Authorization", "Bearer " + api_key)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()

    accumulated = ""
    started = False
    try:
        with opener.open(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                try:
                    delta = obj["choices"][0]["delta"].get("content")
                except Exception:
                    delta = None
                if delta:
                    accumulated += delta
                    started = True
                    if on_delta:
                        on_delta(delta)
    except urllib.error.HTTPError as e:
        if started:
            return {"ok": True, "text": accumulated, "reason": "partial"}
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        return {"ok": False, "text": source, "reason": f"http_{e.code}", "error": detail}
    except Exception as e:
        if started:
            return {"ok": True, "text": accumulated, "reason": "partial"}
        return {"ok": False, "text": source, "reason": "network", "error": str(e)}

    refined = normalize_output_text(accumulated)
    if not refined:
        return {"ok": False, "text": source, "reason": "empty_output"}
    return {"ok": True, "text": refined, "reason": "ok"}


# ===================== 轻清洗（润色关模式，纯本地规则、秒出） =====================
# 设计取舍（与用户对齐，方案 X）：
# - 只删「几乎无实义的低风险语气词」：呃/嗯/哦/额/唉（口语叹息/停顿，删了不伤语义）。
# - 不删任何双字/多字填充词（这个/那个/就是/其实/然后/的话/对吧/反正/就是说 等），
#   因为它们常作实义（「这个方案不错」「然后我们走」），一刀切会误伤。
# - 不删「吧/哎/呀/啊」（保留「算了吧」「哎呀」「你好呀」「我的天啊」等）。
# - 不做重复词合并（避免误伤「让我想想」→「让我想」）。
# - 末尾标点规则：短句（≤5 字）或纯数字串剥「句末标点」（连续末尾全剥）；
#   正常长句（>5 字且非纯数字）保留末尾标点。句末标点不含逗号/顿号/波浪号。

_LIGHT_SINGLE_FILLERS = ["呃", "嗯", "哦", "额", "唉"]
_LIGHT_TRAILING_PUNCT = "。．.！!？?；;：:"
# 仅含数字（可含小数点与空格）视为「纯数字串」，如 12345 / 3.14；带单位（如「128元」）不算。
_RE_PURE_DIGITS = re.compile(r"^[0-9][0-9\s.]*$")

# 预编译：单字语气词（后接任意标点/空白也一并吃掉，避免留下空格）
_RE_SINGLE = re.compile("[" + re.escape("".join(_LIGHT_SINGLE_FILLERS)) + "][\\s,。.!?;:，、~～…]*")


def _is_short_or_numeric(t: str) -> bool:
    """短句（≤5 字符）或纯数字串 → 需要剥句末标点；正常长句保留标点。"""
    if not t:
        return False
    core = t.rstrip(_LIGHT_TRAILING_PUNCT).rstrip()
    if _RE_PURE_DIGITS.match(core):       # 剥掉末尾标点后是纯数字（可含小数点/空格）
        return True
    if len(t) <= 5:                       # 短句
        return True
    return False


def light_clean(text: str) -> str:
    """轻清洗：去口语 filler + 按规则剥句末标点，纯正则、毫秒级、不调 LLM。

    用于「润色关」模式：用户要的是「去口水字 + 秒出」，而非 LLM 大改写。
    返回清洗后的文本；若输入为空原样返回。

    末尾标点规则（用户明确）：
    - 短句（≤5 字）或纯数字串 → 剥「句末标点」（连续末尾全剥）；
    - 正常长句（>5 字且非纯数字）→ 末尾标点保留。
    """
    t = str(text or "").strip()
    if not t:
        return t
    # 1) 删单字语气词（连同紧跟的标点/空白）
    t = _RE_SINGLE.sub("", t)
    # 2) 合并被拆出的多余空白（filler 删除可能留下空格）
    t = re.sub(r"\\s+", " ", t).strip()
    # 4) 末尾标点规则：短句/纯数字剥「句末标点」（连续末尾全剥）；正常长句保留。
    if _is_short_or_numeric(t):
        t = t.rstrip(_LIGHT_TRAILING_PUNCT).rstrip()
    return t