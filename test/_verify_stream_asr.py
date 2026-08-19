"""临时验证：bigmodel 流式端点 + WebSocketApp 边发边收。验证完即删。"""
import sys, time, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

cfg = json.loads(Path("C:/Users/user/AppData/Roaming/Yurun/config.json").read_text(encoding="utf-8"))

from sauc_asr import sauc_transcribe_stream


def chunks():
    # 3 秒静音 PCM（16kHz 16bit mono），按 200ms 分块，模拟实时录音
    chunk = 16000 * 2 * 200 // 1000  # 200ms = 6400 bytes
    silence = b"\x00\x00" * (16000 * 3)
    for i in range(0, len(silence), chunk):
        yield silence[i:i + chunk]
        time.sleep(0.05)  # 模拟实时（比真实快，便于快速验证）


t0 = time.time()
try:
    text = sauc_transcribe_stream(
        chunks(),
        api_key=cfg["asr_sauc_key"],
        resource_id=cfg["asr_sauc_resource_id"],
        endpoint=cfg["asr_sauc_endpoint"],
        language=cfg.get("language", "auto"),
    )
    print("OK 识别结果=%r 耗时=%.2fs" % (text, time.time() - t0))
except Exception as e:
    print("FAIL: %s 耗时=%.2fs" % (e, time.time() - t0))
