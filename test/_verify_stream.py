"""临时验证：真实方舟端点跑 refine_stream，确认纯文本流式 + on_delta 正常。验证完即删。"""
import sys, json, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

cfg_path = Path("C:/Users/user/AppData/Roaming/Yurun/config.json")
cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

from refiner import refine_stream

pieces = []

def on_delta(seg):
    pieces.append(seg)
    print("delta: %r" % seg, flush=True)

t0 = time.time()
result = refine_stream(
    text="今天去超市买了很多东西然后回家做饭",
    api_key=cfg.get("api_key"),
    api_base=cfg.get("api_base"),
    model=cfg.get("api_model"),
    language="zh",
    on_delta=on_delta,
)
dt = time.time() - t0
print("=== 耗时 %.2fs ok=%s reason=%s" % (dt, result["ok"], result.get("reason")))
print("=== 完整文本: %r" % result["text"])
print("=== 分片数: %d" % len(pieces))
print("=== 首字到达(第1片): %r" % (pieces[0] if pieces else None))
