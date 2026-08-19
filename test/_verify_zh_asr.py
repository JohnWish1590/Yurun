"""临时：用 pyttsx3 合成中文语音，验证 bigmodel 流式识别 + 中间结果。验证完即删。"""
import sys, time, json, struct
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 1. 合成中文语音
import pyttsx3
engine = pyttsx3.init()
voices = engine.getProperty("voices")
zh = None
for v in voices:
    langs = str(getattr(v, "languages", ""))
    if "zh" in langs.lower() or "chinese" in v.name.lower() or "huihui" in v.name.lower() or "kangkang" in v.name.lower():
        zh = v
        break
print("中文语音:", (zh.name if zh else "未找到，用默认"), flush=True)
engine.setProperty("voice", zh.id if zh else voices[0].id)
engine.setProperty("rate", 160)
wav = str(Path(__file__).resolve().parent / "test_zh.wav")
engine.save_to_file("今天天气很好我们去公园散步买了很多东西", wav)
engine.runAndWait()
print("已合成:", wav, flush=True)

# 2. 读 wav 转 PCM
import soundfile as sf
import numpy as np
audio, sr = sf.read(wav, dtype="float32")
pcm = (audio * 32767).astype("int16").tobytes()
print("wav 时长 %.2fs 采样率 %d" % (len(pcm) / 2 / sr, sr), flush=True)

# 3. 用 sauc 底层函数 + WebSocketApp，模拟 sauc_transcribe_stream 并记录中间结果
import gzip, uuid, threading, websocket
import sauc_asr as S

cfg = json.loads(Path("C:/Users/user/AppData/Roaming/Yurun/config.json").read_text(encoding="utf-8"))

state = {"text": "", "intermediates": [], "done": threading.Event(), "opened": threading.Event(), "error": None}

def on_open(ws):
    ws.send(S._build_full_request("auto"), opcode=0x2)
    state["opened"].set()

def on_message(ws, message):
    if isinstance(message, str):
        message = message.encode("latin-1")
    parsed = S._parse_response(message)
    if parsed.get("error"):
        state["error"] = f"{parsed.get('code')} {parsed.get('message')}"
        state["done"].set()
        return
    if parsed.get("text"):
        state["text"] = parsed["text"]
        state["intermediates"].append(parsed["text"])
    flags = (message[1] & 0x0F) if len(message) > 1 else 0
    if flags & 0b0010:
        state["done"].set()

def on_error(ws, e):
    state["error"] = str(e); state["done"].set()

def on_close(ws, *a):
    state["done"].set()

headers = {
    "X-Api-Key": cfg["asr_sauc_key"],
    "X-Api-Resource-Id": cfg["asr_sauc_resource_id"],
    "X-Api-Request-Id": str(uuid.uuid4()),
    "X-Api-Connect-Id": str(uuid.uuid4()),
}
ws = websocket.WebSocketApp(cfg["asr_sauc_endpoint"], header=headers, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 20, "ping_timeout": 10}, daemon=True).start()
state["opened"].wait(timeout=15)

chunk = 16000 * 2 * 200 // 1000
for i in range(0, len(pcm), chunk):
    ws.send(S._build_audio_packet(pcm[i:i+chunk], is_last=False), opcode=0x2)
    time.sleep(0.05)

# 松手时刻：记录此刻的中间结果
mid_at_release = state["text"]
print("=== 松手时中间结果: %r" % mid_at_release, flush=True)

ws.send(S._build_audio_packet(b"", is_last=True), opcode=0x2)
state["done"].wait(timeout=30)
ws.close()

print("=== 最终结果: %r" % state["text"], flush=True)
print("=== 中间结果历史(%d条):" % len(state["intermediates"]), flush=True)
for t in state["intermediates"]:
    print("   ", repr(t), flush=True)
