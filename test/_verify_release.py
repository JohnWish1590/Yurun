"""临时验证：新「松手即用中间结果」逻辑，看返回文本完整度 + 松手后延迟。验证完即删。"""
import sys, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import soundfile as sf
import numpy as np
wav = str(Path(__file__).resolve().parent / "test_zh.wav")
audio, sr = sf.read(wav, dtype="float32")
pcm = (audio * 32767).astype("int16").tobytes()

cfg = json.loads(Path("C:/Users/user/AppData/Roaming/Yurun/config.json").read_text(encoding="utf-8"))
from sauc_asr import sauc_transcribe_stream

chunk = 16000 * 2 * 200 // 1000
release_at = [None]


def timed_chunks():
    for i in range(0, len(pcm), chunk):
        yield pcm[i:i + chunk]
        time.sleep(0.2)  # 模拟实时录音 200ms/块
    release_at[0] = time.time()  # 发完最后一块 = 松手


t0 = time.time()
text = sauc_transcribe_stream(
    timed_chunks(),
    api_key=cfg["asr_sauc_key"],
    resource_id=cfg["asr_sauc_resource_id"],
    endpoint=cfg["asr_sauc_endpoint"],
)
dt = time.time() - t0
rel = time.time() - release_at[0] if release_at[0] else -1

print("=== 返回文本: %r" % text)
print("=== 松手后延迟: %.2fs (总耗时 %.2fs)" % (rel, dt))
print("=== 参考最终结果: '今天天气很好，我们去公园散步，买了很多东西。'")
