"""语润（Yurun）转写模块：faster-whisper 常驻模型 + 转写。

- 模型缓存到 %APPDATA%\\Yurun\\models\\ 或 HuggingFace 缓存
- 离线加载本地模型（HF_HUB_OFFLINE），避免联网检查导致卡死
- beam_size + VAD + 语言自动检测，提高中英混合识别准确率
- 线程安全：模型只加载一次
"""
import os
import threading
from pathlib import Path

# 关键：离线加载本地模型，避免 HuggingFace 联网检查导致卡死
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

MODEL_CACHE = Path(os.environ.get("APPDATA", str(Path.home()))) / "Yurun" / "models"

# 中文口语提示词：帮助 Whisper 更懂中文口语、语气词、中英混合
ZH_PROMPT = ("以下是普通话日常口语语音转写，可能包含中英文混合（如 test、code、AI、GitHub 等）。"
             "请正确识别中文和英文单词，保留英文术语原样。")


def _resolve_model_path(model_name: str) -> str:
    """解析模型路径：优先本地缓存 / HF 快照，避免联网。"""
    # 1) Yurun 自己的模型目录
    local = MODEL_CACHE / model_name
    if local.exists():
        return str(local)
    # 2) HuggingFace 快照（已有完整 model.bin）
    hf = Path.home() / ".cache" / "huggingface" / "hub" / f"models--Systran--faster-whisper-{model_name}" / "snapshots"
    if hf.exists():
        snaps = [d for d in hf.iterdir() if d.is_dir()]
        if snaps:
            return str(snaps[0])
    # 3) 名字（让 faster-whisper 尝试下载）
    return model_name


class Transcriber:
    def __init__(self):
        self._model = None
        self._model_name = None
        self._lock = threading.Lock()
        self._loading = False
        self._load_error = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self, model_name: str = "base", device: str = "cpu",
             compute_type: str = "int8", progress_cb=None) -> bool:
        with self._lock:
            if self._model is not None and self._model_name == model_name:
                return True
            self._loading = True
            self._load_error = None
        try:
            try:
                # 用 importlib 动态导入：字符串形式对 PyInstaller 静态分析不可见，
                # 从而云端精简版 exe 不会强制打包本地引擎（体积砍半）。
                # 本地模式用户运行时若未安装则抛 ImportError，下方捕获并给出友好提示。
                import importlib
                _fw = importlib.import_module("faster_whisper")
                WhisperModel = getattr(_fw, "WhisperModel")
            except ImportError:
                self._load_error = ("本地识别引擎 faster-whisper 未安装。请使用包含本地引擎的"
                                    "完整版 exe，或自行 pip install faster-whisper 后运行源码版。")
                return False
            if progress_cb:
                progress_cb(0.2)
            model_path = _resolve_model_path(model_name)
            self._model = WhisperModel(model_path, device=device, compute_type=compute_type)
            if progress_cb:
                progress_cb(1.0)
            self._model_name = model_name
            return True
        except Exception as e:
            self._load_error = str(e)
            return False
        finally:
            self._loading = False

    def transcribe(self, wav_path: str, language: str = "auto",
                   beam_size: int = 3) -> str:
        """转写音频，返回文本。模型未加载时抛 RuntimeError。"""
        if self._model is None:
            raise RuntimeError("model not loaded: " + str(self._load_error or "unknown"))
        lang = None if language in ("auto", "") else language
        segments, _info = self._model.transcribe(
            wav_path,
            language=lang,
            beam_size=beam_size,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=300),
            initial_prompt=ZH_PROMPT,
        )
        return "".join(s.text for s in segments).strip()


# 全局单例
_transcriber = None
def get_transcriber() -> Transcriber:
    global _transcriber
    if _transcriber is None:
        _transcriber = Transcriber()
    return _transcriber