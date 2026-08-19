"""语润（Yurun）用户词库模块：渐进式"越用越聪明"词典。

存储：%APPDATA%\\Yurun\\user_dictionary.json

词条结构：
{
  "text": "changelog",                          # 正确写法（唯一键）
  "aliases": [{"text": "天气log", "count": 2}],  # 已知错误变体 + 累计出现次数
  "count": 3,                                   # 总纠正次数（热词权重）
  "source": "auto"                              # auto=快捷键纠错学习 / manual=手动添加
}

生效通道（调用方接线）：
1. ASR 热词：to_hotwords() -> 火山 SAUC request.context.hotwords（源头纠正）
2. bypass 本地替换：to_replace_map() -> 免润色短句贴出前替换错误变体（兜底）
3. LLM 润色词典：to_llm_text() -> context.userDictionary（长句润色参考）
"""
import json
import threading
from pathlib import Path

from config import app_data_dir
from logger import get_logger

log = get_logger("yurun.dictionary")

# 热词直传 token 预算（火山 SAUC 上限 200，留 10% 余量）
HOTWORDS_TOKEN_BUDGET = 180


def dict_path() -> Path:
    return app_data_dir() / "user_dictionary.json"


_lock = threading.Lock()
_cache = None  # list[dict] | None


def _load() -> list:
    global _cache
    if _cache is not None:
        return _cache
    p = dict_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _cache = _sanitize(data)
                return _cache
        except Exception:
            log.warning("词库文件解析失败，按空词库处理: %s", p)
    _cache = []
    return _cache


def _sanitize(data) -> list:
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        aliases = []
        for a in (item.get("aliases") or []):
            if isinstance(a, dict) and str(a.get("text") or "").strip():
                aliases.append({"text": str(a["text"]).strip(),
                                "count": int(a.get("count") or 1)})
        out.append({
            "text": text,
            "aliases": aliases,
            "count": int(item.get("count") or 1),
            "source": "manual" if item.get("source") == "manual" else "auto",
        })
    return out


def save(entries: list) -> None:
    global _cache
    _cache = entries
    p = dict_path()
    try:
        p.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.error("词库保存失败: %s", e)


def get_entries() -> list:
    with _lock:
        return [dict(e) for e in _load()]


def add_entry(correct: str, wrong_text: str = "", source: str = "auto") -> dict:
    """记录一次纠正：正确词唯一，错误变体累积去重，count 递增。

    - correct 为空则忽略（保护：正确写法必须有值）。
    - wrong_text 为空只累计 correct 的 count（如手动添加词条）。
    - 已存在的词条：aliases 命中则 count+1，未命中则追加；source 保持原有。
    """
    correct = (correct or "").strip()
    if not correct:
        return {}
    wrong = (wrong_text or "").strip()
    with _lock:
        entries = _load()
        for e in entries:
            if e["text"] == correct:
                e["count"] = int(e.get("count") or 0) + 1
                if wrong and wrong != correct:
                    hit = next((a for a in e["aliases"] if a["text"] == wrong), None)
                    if hit:
                        hit["count"] = int(hit.get("count") or 0) + 1
                    else:
                        e["aliases"].append({"text": wrong, "count": 1})
                save(entries)
                return e
        entry = {
            "text": correct,
            "aliases": [{"text": wrong, "count": 1}] if wrong and wrong != correct else [],
            "count": 1,
            "source": source,
        }
        entries.append(entry)
        save(entries)
        return entry


def delete_entry(text: str) -> bool:
    text = (text or "").strip()
    with _lock:
        entries = _load()
        before = len(entries)
        entries = [e for e in entries if e["text"] != text]
        if len(entries) != before:
            save(entries)
            return True
        return False


def to_hotwords(max_tokens: int = HOTWORDS_TOKEN_BUDGET) -> list:
    """按纠正次数降序取正确词，供火山 SAUC 热词直传（源头纠正）。

    token 粗估 = 字符数（中文 1 字约 1~2 token，英文 1 词约 1 token，留余量按
    字符数计更保守）。超出预算截断，火山端还会再兜底截断。
    """
    with _lock:
        entries = sorted(_load(), key=lambda e: int(e.get("count") or 0), reverse=True)
    words = []
    used = 0
    for e in entries:
        w = e["text"]
        cost = len(w)
        if used + cost > max_tokens:
            continue
        words.append(w)
        used += cost
    return words


def to_replace_map() -> dict:
    """错误变体 -> 正确词 映射（bypass 短句贴出前本地替换，兜底）。

    同一正确词的多个别名都映射到它；多个正确词共享同一别名时后者覆盖（罕见）。
    """
    with _lock:
        entries = _load()
    m = {}
    for e in entries:
        for a in e["aliases"]:
            at = a["text"]
            if not at or at == e["text"]:
                continue
            m[at] = e["text"]
    return m


def apply_local_replace(text: str) -> str:
    """对文本做词库本地替换：按别名长度降序，命中即换成正确词。

    只对免润色 bypass 路径做确定性兜底（LLM 路径有词典 + prompt 双重处理）。
    """
    if not text:
        return text
    m = to_replace_map()
    if not m:
        return text
    for alias in sorted(m, key=len, reverse=True):
        if alias in text:
            text = text.replace(alias, m[alias])
    return text


def to_llm_text() -> str:
    """userDictionary 文本：每行一个正确词（长句润色时 LLM 参考）。"""
    with _lock:
        entries = _load()
    return "\n".join(e["text"] for e in entries)
