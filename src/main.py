"""语润（Yurun）主程序：迷你浮窗 + 全局热键 + 录音→转写→润色→插入。

架构（修 RuntimeError: Calling Tcl from different apartment）：
- 主线程 = Tk root + after 循环：驱动 indicator / loading 动画、执行粘贴
- 托盘 pystray 在后台线程运行（set_icon 线程安全）
- 热键/录音/转写/润色在各自线程，通过队列把 UI 事件发给主线程

日志：%APPDATA%\\Yurun\\logs\\yurun.log
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import get_config
from hotkey import get_hotkey
from logger import (
    get_logger,
    install_crash_handler,
    register_tk_error,
    log_startup_banner,
)
from pill import PillBubble
from gui import SettingsWindow  # 预导入：让 PyInstaller 在启动时即解开依赖，避免首次打开设置卡顿
import tray as tray_mod

# 以下重依赖（recorder/sounddevice、refiner/requests、sauc_asr/cloud_asr/websocket、
# transcriber/faster_whisper）全部改为"用到才 import"，云端模式启动零负担。

log = get_logger("yurun")

# 尽早安装全局崩溃捕获：后续任何 import 失败 / 子线程崩 / Tk 报错都写进日志，
# 用户把日志文件发回即可反馈问题。
install_crash_handler()
log_startup_banner()

APP_TITLE = "语润 Yurun"


class App:
    def __init__(self):
        self.cfg = get_config()
        self.ui_q = queue.Queue()          # 子线程 → 主线程的 UI 事件
        self._quit = False
        self._rec_stop = None              # 当前录音 stop_event
        self._rec_thread = None
        self._paste_cb = None              # 等待粘贴的回调

        self.root = tk.Tk()
        self.root.withdraw()
        # Tk 回调异常也写进日志（崩溃可反馈）
        register_tk_error(self.root)
        self.indicator = PillBubble(self.root)

        self.tray = tray_mod.Tray(
            on_quit=self._on_quit,
            on_open_settings=self._open_settings,
        )
        tray_mod._tray_instance = self.tray

        # 热键
        self.hotkey = get_hotkey()
        self.hotkey.on_hold_start = self._on_hold_start
        self.hotkey.on_hold_end = self._on_hold_end
        self.hotkey.on_error = lambda m: self.ui_q.put(("toast", m))
        self.hotkey.on_toggle = self._on_toggle

    # ================= UI 事件泵 =================
    def pump(self):
        """主线程每 40ms：处理队列 + 驱动动画。"""
        try:
            while True:
                evt = self.ui_q.get_nowait()
                self._handle_ui(evt)
        except queue.Empty:
            pass
        self.indicator._tick()
        if not self._quit:
            self.root.after(40, self.pump)

    def _handle_ui(self, evt):
        kind = evt[0]
        try:
            if kind == "guide":
                hk = (self.cfg.get("hotkey") or "`").strip()
                label = "` 键" if hk in ("`", "~") else hk
                self.indicator.show_guide(f"按住 {label} 说话")
            elif kind == "recording":
                # 按下热键：显示「正在录音」+ 红点呼吸
                self.indicator.start_recording()
            elif kind == "transcribing":
                # ASR 识别中：保持「正在录音」视觉，连贯不跳变
                self.indicator.start_recording()
            elif kind == "refining":
                self.indicator.show_refining()
            elif kind in ("done", "fallback"):
                # 完成：直接隐藏 + 粘贴，pill 不显示预览文字
                self.indicator.force_idle()
            elif kind == "error":
                self.indicator.show_error(evt[1] if len(evt) > 1 else "出错了")
            elif kind == "toast":
                self.indicator.show_error(evt[1])
            elif kind == "model_loading":
                pass  # 仅本地离线模式触发，不打扰
            elif kind in ("model_ready", "model_error"):
                # 模型加载完成/失败都直接隐藏，不在 pill 里显示文字
                self.indicator.force_idle()
            elif kind == "paste":
                self._do_paste(evt[1])
        except Exception as e:
            log.exception("UI 事件处理失败: %s", e)

    # ================= 加载模型（仅本地离线模式） =================
    # 云端 SAUC 模式不会走到这里；本地模式用迷你浮窗提示，不再弹独立加载窗。
    def _load_model_async(self):
        def job():
            from transcriber import get_transcriber
            cfg = get_config()
            tr = get_transcriber()
            self.ui_q.put(("model_loading", None))
            ok = tr.load(cfg.get("whisper_model", "base"))
            if ok:
                log.info("模型加载完成: %s", cfg.get("whisper_model"))
                self.ui_q.put(("model_ready", None))
            else:
                log.error("模型加载失败: %s", tr._load_error)
                self.ui_q.put(("model_error", tr._load_error or "未知错误"))
        threading.Thread(target=job, daemon=True).start()

    # ================= 热键 =================
    def _on_hold_start(self, _key):
        log.info("热键按下，开始录音")
        # 不再拦截"已有录音进行中"：允许前一句还在识别/润色时按下热键录下一句
        # （重叠录音）。hold 模式物理上同一键按住中不会再触发 WM_HOTKEY，
        # 所以这里每次按下都是独立的一次录音，配独立临时文件互不干扰。
        stop = threading.Event()
        self._rec_stop = stop
        self._rec_thread = threading.Thread(target=self._record_job, args=(stop,), daemon=True)
        self._rec_thread.start()
        self.ui_q.put(("recording", None))

    def _on_hold_end(self, _key):
        log.info("热键松开，停止录音")
        if self._rec_stop is not None:
            self._rec_stop.set()
            self._rec_stop = None

    def _on_toggle(self, _key, pressed):
        if pressed:
            self._on_hold_start(_key)
        else:
            self._on_hold_end(_key)

    # ================= 录音→转写→润色→粘贴 =================
    def _record_job(self, stop):
        cfg = get_config()
        # 统一路径：先录 wav 文件，再调 _transcribe 转写。
        # sauc/cloud/local 都走这条（_transcribe 内部按 provider 分派），
        # 避免真流式双线程 ws race 导致 indicator 卡死。
        # 每次录音用独立临时文件，支持"前句润色中按热键录下一句"的重叠场景，
        # 多个录音线程互不抢同一个 wav。
        import tempfile
        fd, tmp_wav = tempfile.mkstemp(suffix=".wav", prefix="yurun_")
        os.close(fd)
        try:
            from recorder import record_to_file
            log.info("录音开始 provider=%s tmp=%s", cfg.get("asr_provider", "sauc"), tmp_wav)
            ok, dur, err = record_to_file(
                tmp_wav, stop_event=stop,
                max_seconds=30, silence_timeout=0.0,
                on_level=self._on_level,
            )
            if not ok or dur < 0.3:
                log.warning("录音无效: ok=%s dur=%.2f err=%s", ok, dur, err)
                self.ui_q.put(("error", "没听到声音，再试一次"))
                return
            log.info("录音完成: %.2fs", dur)

            self.ui_q.put(("transcribing", None))
            try:
                log.info("进入 _transcribe provider=%s wav=%s", cfg.get("asr_provider", "sauc"), tmp_wav)
                t_asr0 = time.time()
                text = self._transcribe(tmp_wav, cfg)
                log.info("_transcribe 返回 text=%r 识别耗时=%.2fs", text, time.time() - t_asr0)
            except Exception as e:
                log.error("识别失败: %s", e)
                self.ui_q.put(("error", "识别失败"))
                return
            self._after_transcribe(text)
        except Exception as e:
            log.error("录音异常: %s", e)
            self.ui_q.put(("error", "录音失败"))
            return
        finally:
            try:
                os.remove(tmp_wav)
            except Exception:
                pass

    # _record_job_sauc / sauc_transcribe_stream / record_chunks 是已写好的"真流式"实现，
    # 因双线程 ws race 暂不启用，等修好 race 条件后改回 _record_job 第一行调用 _record_job_sauc 即可。

    def _record_job_sauc(self, stop, cfg):
        """SAUC 真流式分支：录音生成器边产出 PCM 边发往 WebSocket，并发收结果。"""
        try:
            from recorder import record_chunks
            from sauc_asr import sauc_transcribe_stream
        except Exception as e:
            log.error("导入 SAUC 流式模块失败: %s", e)
            self.ui_q.put(("error", "SAUC 模块加载失败"))
            return
        self.ui_q.put(("transcribing", None))
        try:
            text = sauc_transcribe_stream(
                record_chunks(stop, on_level=self._on_level),
                api_key=cfg.get("asr_sauc_key"),
                resource_id=cfg.get("asr_sauc_resource_id"),
                endpoint=cfg.get("asr_sauc_endpoint"),
                language=cfg.get("language", "auto"),
                proxy=cfg.get("proxy", ""),
            )
        except Exception as e:
            log.error("识别失败: %s", e)
            self.ui_q.put(("error", "识别失败"))
            return
        self._after_transcribe(text)

    def _after_transcribe(self, text):
        """识别成功后的共用收尾：日志 + 润色 + 回主线程粘贴。"""
        log.info("识别结果: %s", text)
        if not text or not text.strip():
            self.ui_q.put(("error", "没识别到内容"))
            return

        self.ui_q.put(("refining", None))
        t_rf0 = time.time()
        result = self._refine(text)
        log.info("润色耗时=%.2fs ok=%s reason=%s", time.time() - t_rf0, result["ok"], result.get("reason"))
        if result["ok"]:
            final = result["text"]
        else:
            # no_change / 未配置 → 用原文
            final = text
            if result.get("reason") not in ("no_change", "no_api_key"):
                log.warning("润色失败: %s，用原文", result.get("reason"))
        # 回主线程粘贴
        self.ui_q.put(("paste", final))

    def _transcribe(self, wav_path, cfg):
        """按配置选择识别引擎：cloud=云端 ASR / local=本地 Whisper。"""
        provider = cfg.get("asr_provider", "cloud")
        if provider == "sauc":
            from sauc_asr import sauc_transcribe
            return sauc_transcribe(
                wav_path,
                api_key=cfg.get("asr_sauc_key"),
                resource_id=cfg.get("asr_sauc_resource_id"),
                endpoint=cfg.get("asr_sauc_endpoint"),
                language=cfg.get("language", "auto"),
                proxy=cfg.get("proxy", ""),
            )
        if provider == "cloud":
            from cloud_asr import cloud_transcribe
            return cloud_transcribe(
                wav_path,
                api_key=cfg.get("asr_key"),
                api_base=cfg.get("asr_base_url"),
                model=cfg.get("asr_model"),
                language=cfg.get("language", "auto"),
                proxy=cfg.get("proxy", ""),
            )
        # 本地
        from transcriber import get_transcriber
        return get_transcriber().transcribe(
            wav_path,
            language=cfg.get("language", "auto"),
            beam_size=3,
        )

    def _on_level(self, level):
        self.indicator.set_level(level)

    def _refine(self, text):
        cfg = get_config()
        if not cfg.get("refine_enabled", True) or not cfg.get("api_key"):
            return {"ok": False, "text": text, "reason": "no_api_key"}
        try:
            from refiner import refine_text
            return refine_text(
                text=text,
                api_key=cfg.get("api_key"),
                api_base=cfg.get("api_base"),
                model=cfg.get("api_model"),
                custom_instructions=cfg.get("custom_instructions", ""),
                language=cfg.get("language", "zh"),
                proxy=cfg.get("proxy", ""),
            )
        except Exception as e:
            log.error("润色调用异常: %s", e)
            return {"ok": False, "text": text, "reason": "exception"}

    def _do_paste(self, text):
        """主线程：写剪贴板 + 模拟 Ctrl+V。"""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
            log.info("剪贴板已写入，准备粘贴")
        except Exception as e:
            log.error("剪贴板写入失败: %s", e)
        time.sleep(0.05)
        try:
            import pyautogui
            pyautogui.hotkey("ctrl", "v")
            log.info("Ctrl+V 已发送")
            self.ui_q.put(("done", text))
        except Exception as e:
            log.warning("pyautogui 失败: %s", e)
            try:
                import subprocess
                subprocess.Popen(["powershell", "-Command",
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "[System.Windows.Forms.SendKeys]::SendWait('^v')"])
                self.ui_q.put(("done", text))
            except Exception as e2:
                log.error("备用粘贴失败: %s", e2)
                self.ui_q.put(("error", "粘贴失败"))

    # ================= 托盘 =================
    def _on_quit(self):
        log.info("用户退出")
        self._quit = True
        try:
            self.hotkey.stop()
        except Exception:
            pass
        try:
            self.tray.stop()
        except Exception:
            pass
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            os._exit(0)

    def _open_settings(self):
        # Tkinter 非线程安全：托盘运行在后台线程，必须用 after(0) 切回主线程操作 Tk
        try:
            self.root.after(0, self._open_settings_ui)
        except Exception as e:
            log.error("打开设置失败: %s", e)

    def _open_settings_ui(self):
        try:
            SettingsWindow(master=self.root)
        except Exception as e:
            log.error("打开设置失败: %s", e)

    # ================= 启动 =================
    def _warmup(self):
        """后台预热重依赖，消灭首次按键的冷加载卡顿。"""
        try:
            import numpy, sounddevice, soundfile  # noqa
            import websocket  # noqa
            import pyautogui  # noqa
            from recorder import record_to_file, record_chunks  # noqa
            from sauc_asr import sauc_transcribe, sauc_transcribe_stream  # noqa
            from cloud_asr import cloud_transcribe  # noqa
            from refiner import refine_text  # noqa
            log.info("依赖预热完成（首次按键不再冷加载）")
        except Exception as e:
            log.warning("预热部分依赖失败，将在首次使用时按需加载: %s", e)

    def run(self):
        log.info("语润启动（开发版）")
        # 仅本地离线模式才在启动时加载 Whisper 模型；云端 SAUC 用户无需等待/下载
        if self.cfg.get("asr_provider") == "local":
            self._load_model_async()
        else:
            log.info("识别引擎为 %s，跳过本地模型加载", self.cfg.get("asr_provider"))
        ok = self.hotkey.start(self.cfg.get("hotkey"), self.cfg.get("trigger_mode", "hold"))
        if not ok:
            self.ui_q.put(("toast", "热键注册失败，请检查是否被占用"))
        # 启动托盘（后台线程）
        threading.Thread(target=self.tray.start, daemon=True).start()
        # 后台预热重依赖，避免首次按下热键才现场加载 numpy/sounddevice/websocket/pyautogui
        threading.Thread(target=self._warmup, daemon=True).start()
        # 主循环
        # 首次启动引导一次（窄气泡提示快捷键），3.5s 后自动淡出
        self.root.after(600, lambda: self.ui_q.put(("guide", None)))
        self.root.after(40, self.pump)
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()