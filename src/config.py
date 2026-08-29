"""语润（Yurun）配置模块：读取/保存用户设置。
配置文件：%APPDATA%\\Yurun\\config.json
"""
import json
import os
import sys
from pathlib import Path

from logger import get_logger
log = get_logger("yurun.config")

APP_NAME = "Yurun"

def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d

def config_path() -> Path:
    return app_data_dir() / "config.json"

DEFAULTS = {
    # 润色 API（OpenAI 兼容）
    "api_key": "",
    "api_base": "https://api.deepseek.com/v1",
    "api_model": "deepseek-chat",
    "refine_enabled": True,
    # Phase 0：Direct 是新配置的默认输入模式；旧字段保留给迁移使用。
    "input_mode": "direct",
    "custom_instructions": "",
    # 语音识别：local=本地 Whisper / cloud=云端 ASR（OpenAI 兼容 /v1/audio/transcriptions）
    "asr_provider": "sauc",           # sauc=火山SAUC流式(Cindy同款,默认) / cloud=OpenAI兼容 / local=本地
    "asr_base_url": "https://ark.cn-beijing.volces.com/api/v3",  # 火山方舟 OpenAI 兼容
    "asr_key": "",                     # 火山方舟 / 硅基流动 / 任何兼容 key
    "asr_model": "doubao-seed-asr-250429",  # 火山 doubao-seed-asr（可改）
    "asr_sauc_key": "",                  # 火山语音技术 App Key（SAUC 流式）
    "asr_sauc_resource_id": "volc.seedasr.sauc.duration",  # 2.0 小时版；1.0 用 volc.bigasr.sauc.duration
    "asr_sauc_endpoint": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
    "whisper_model": "small",          # 本地模型：base / small（推荐 small）
    "language": "auto",                # zh / en / auto
    # 热键
    "hotkey": "`",                     # 默认反引号
    "trigger_mode": "hold",            # hold=按住说话 / toggle=单击开关
    "correction_hotkey": "`",          # 纠错热键（Ctrl+此键 弹「错误纠正」框）
    # 其他
    "auto_start": False,
    "proxy": "",                       # 留空=不走代理，如 http://127.0.0.1:7897
    "mirror": "https://hf-mirror.com",  # 模型下载镜像
    "insert_method": "type",           # type=SendInput逐字(零剪贴板污染,默认) / paste=剪贴板+Ctrl+V(兜底)
    "refine_streaming": True,          # 流式首字上屏（边润边贴，默认开）；关闭则回退整段润色
}

class Config:
    def __init__(self):
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        p = config_path()
        if p.exists():
            try:
                saved = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    self.data.update({k: v for k, v in saved.items() if k in DEFAULTS})
                    # 最小兼容迁移：已有 input_mode 优先；旧配置保留既有行为。
                    if "input_mode" not in saved:
                        self.data["input_mode"] = (
                            "refine" if bool(saved.get("refine_enabled", True)) else "direct"
                        )
                    elif self.data.get("input_mode") not in ("direct", "refine"):
                        log.warning("无效 input_mode=%r，回退 direct", self.data.get("input_mode"))
                        self.data["input_mode"] = "direct"
                    if "input_mode" not in saved:
                        self.save()
            except Exception:
                pass

    def save(self):
        p = config_path()
        p.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, key, default=None):
        return self.data.get(key, default if default is not None else DEFAULTS.get(key))

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def __getitem__(self, key):
        return self.get(key)

    def __setitem__(self, key, value):
        self.set(key, value)


# 全局单例
_config = None
def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
