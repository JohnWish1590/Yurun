"""语润（Yurun）录音模块：按住热键期间录音（或 toggle 模式由外部控制启停）。

- sounddevice / soundfile / numpy 等重依赖延迟到真正录音时才 import，
  避免程序启动（尤其云端模式）就被迫加载 PortAudio / numpy。
"""
import sys
import time

SAMPLE_RATE = 16000


def record_to_file(out_path: str, stop_event=None, max_seconds: float = 60.0,
                   silence_timeout: float = 0.0, silence_threshold: float = 0.01,
                   on_level=None):
    """录音直到 stop_event 被设置 或 达到 max_seconds 或 静音超时。
    stop_event: threading.Event，外部设置后停止。
    on_level: 可选回调，接收当前音量（0~1），用于界面显示。
    返回 (成功, 时长秒, 错误信息)
    """
    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    chunks = []
    start = time.time()
    silent_secs = 0.0
    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if time.time() - start >= max_seconds:
                    break
                data, _ = stream.read(int(SAMPLE_RATE * 0.1))
                chunks.append(data.copy())
                rms = float(np.sqrt(np.mean(np.square(data)))) if len(data) else 0.0
                if on_level:
                    on_level(min(1.0, rms / 0.3))
                if silence_timeout > 0:
                    if rms < silence_threshold:
                        silent_secs += 0.1
                        if silent_secs >= silence_timeout:
                            break
                    else:
                        silent_secs = 0.0
    except Exception as e:
        return False, 0.0, str(e)

    if not chunks:
        return False, 0.0, "empty"
    audio = np.concatenate(chunks)
    dur = len(audio) / SAMPLE_RATE
    try:
        sf.write(out_path, audio, SAMPLE_RATE)
        return True, dur, ""
    except Exception as e:
        return False, dur, str(e)


def record_chunks(stop_event=None, on_level=None, chunk_ms: int = 100):
    """流式录音生成器：边录边 yield int16 PCM 块（bytes），供 SAUC 真流式使用。

    不写文件；调用方负责把每块实时发往 ASR 或自行累积。
    用法：
        for pcm in record_chunks(stop, on_level=...):
            ws.send(pcm)
    """
    import numpy as np
    import sounddevice as sd

    frames = int(SAMPLE_RATE * chunk_ms / 1000)
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            data, _ = stream.read(frames)
            rms = float(np.sqrt(np.mean(np.square(data)))) if len(data) else 0.0
            if on_level:
                on_level(min(1.0, rms / 0.3))
            yield (data * 32767).astype("int16").tobytes()