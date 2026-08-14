"""语润自测 v2：调用修复后的真实模块，跑通 SAUC→润色 完整链路。"""
import json, os, sys, traceback
sys.path.insert(0, r"D:/SynologyDrive/CODING/yurun/src")

import numpy as np
import soundfile as sf
import sauc_asr
from refiner import refine_text

CFG_PATH = r"C:/Users/user/AppData/Roaming/Yurun/config.json"
WAV_16K = r"D:/SynologyDrive/CODING/yurun/_test_speech_16k.wav"


def load_cfg():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    cfg = load_cfg()
    print("=== 配置 ===")
    print("asr_provider:", cfg.get("asr_provider"))
    print("refine api_key:", (cfg.get("api_key") or "")[:8], "...")
    print("refine api_base:", cfg.get("api_base"), "model:", cfg.get("api_model"))
    print("speech wav:", WAV_16K, "exists:", os.path.exists(WAV_16K))

    print("\n=== [TEST 1] SAUC 真实识别（修复后）===")
    text = ""
    try:
        import time as _t
        _t0 = _t.time()
        text = sauc_asr.sauc_transcribe(
            WAV_16K,
            api_key=cfg.get("asr_sauc_key"),
            resource_id=cfg.get("asr_sauc_resource_id"),
            endpoint=cfg.get("asr_sauc_endpoint"),
            language=cfg.get("language", "auto"),
        )
        print(f"[T1 耗时] {_t.time()-_t0:.2f}s")
        print("[T1 结果] text =", repr(text))
        print("[T1]", "PASS" if text else "FAIL 空文本")
    except Exception as e:
        print("[T1 EXCEPTION]", repr(e))
        traceback.print_exc()

    if text:
        print("\n=== [TEST 2] 润色（DeepSeek 真实链路）===")
        try:
            import time as _t
            _t0 = _t.time()
            r = refine_text(
                text=text,
                api_key=cfg.get("api_key"),
                api_base=cfg.get("api_base"),
                model=cfg.get("api_model"),
                language=cfg.get("language", "zh"),
            )
            print(f"[T2 耗时] {_t.time()-_t0:.2f}s")
            print("[T2 结果]", r)
            if r["ok"]:
                print("[T2] PASS 润色后:", repr(r["text"]))
            else:
                print(f"[T2] 注意 reason={r['reason']}（非致命，会用原文）")
        except Exception as e:
            print("[T2 EXCEPTION]", repr(e))
            traceback.print_exc()

    print("\n=== [TEST 3] 静音 wav 应返回空（验证空路径兜底）===")
    silence = np.zeros(int(16000 * 1.0), dtype="float32")
    sf.write(r"D:/SynologyDrive/CODING/yurun/_test_silence.wav", silence, 16000)
    sres = sauc_asr.sauc_transcribe(
        r"D:/SynologyDrive/CODING/yurun/_test_silence.wav",
        api_key=cfg.get("asr_sauc_key"),
        resource_id=cfg.get("asr_sauc_resource_id"),
        endpoint=cfg.get("asr_sauc_endpoint"),
    )
    print("[T3] 静音返回 =", repr(sres), "->", "符合预期(空)" if not sres else "异常(非空)")

    print("\n=== [TEST 4] App 流水线集成（成功路径事件映射）===")
    try:
        from main import App
        import queue as _q
        app = App()
        captured = []
        app._do_paste = lambda t: captured.append(("paste", t))  # 不真发 Ctrl+V
        # 走真实 _transcribe + _after_transcribe
        t4_text = app._transcribe(WAV_16K, app.cfg)
        app._after_transcribe(t4_text)
        events = []
        try:
            while True:
                events.append(app.ui_q.get_nowait())
        except _q.Empty:
            pass
        # 模拟主线程 pump：把事件喂给 _handle_ui（paste 事件会触发 _do_paste mock）
        for ev in events:
            app._handle_ui(ev)
        print("  流水线事件序列:", [e[0] for e in events])
        print("  粘贴捕获:", captured)
        kinds = [e[0] for e in events]
        ok4 = ("refining" in kinds) and captured and captured[0][0] == "paste"
        print("[T4]", "PASS" if ok4 else "FAIL 事件序列不对")
    except Exception as e:
        print("[T4 EXCEPTION]", repr(e))
        traceback.print_exc()

    print("\n=== [TEST 5] App 空结果兜底（应 emit error）===")
    try:
        from main import App as _App
        import queue as _q2
        app5 = _App()
        app5._after_transcribe("")
        ev5 = []
        try:
            while True:
                ev5.append(app5.ui_q.get_nowait())
        except _q2.Empty:
            pass
        print("  空结果事件:", [e[0] for e in ev5], "msg=", ev5[0][1] if ev5 else None)
        ok5 = ev5 and ev5[0][0] == "error"
        print("[T5]", "PASS" if ok5 else "FAIL 空结果未走 error")
    except Exception as e:
        print("[T5 EXCEPTION]", repr(e))
        traceback.print_exc()


if __name__ == "__main__":
    main()
