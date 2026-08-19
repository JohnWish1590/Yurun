"""语润润色增强 —— 开关演示 + 接线示例（TEST 用，不修改正式 src/gui.py）。

本文件演示「流式润色 / 预热」两个勾选项如何落地：
- 用 tkinter 画两个 Checkbutton（流式润色、录音期预热）；
- 勾选状态写入 config.json 的 refine_streaming / refine_warmup；
- 启动时把这些开关映射成 refiner_stream 的模块级变量与环境变量，
  供主流程（或本演示的「模拟一次润色」按钮）读取。

如果你的正式 GUI 想接这两个开关，只需把下面 _apply_flags() 的逻辑搬进
src/gui.py 的设置窗口：勾选 -> 写 config.json + 设 refiner_stream.STREAMING_ENABLED /
WARMUP_ENABLED（或环境变量 YURUN_REFINE_STREAMING / YURUN_REFINE_WARMUP）。

运行：python test/test_gui_toggle.py
依赖：tkinter（标准库）。不需要联网即可看 UI 与开关逻辑。
"""
import os
import sys
import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
_PROJECT_SRC = _PROJECT_ROOT / "src"
if str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))

import config as _cfg  # 正式项目的配置单例

# 把 test/refiner_stream.py 也纳入导入路径（它不在 src/ 下）
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import refiner_stream as rs


CONFIG_PATH = _cfg.config_path()


def _read_flags():
    cfg = _cfg.get_config()
    return {
        "refine_streaming": bool(cfg.get("refine_streaming", True)),
        "refine_warmup": bool(cfg.get("refine_warmup", True)),
    }


def _write_flags(streaming, warmup):
    cfg = _cfg.get_config()
    cfg.set("refine_streaming", bool(streaming))
    cfg.set("refine_warmup", bool(warmup))


def _apply_flags(streaming, warmup):
    """把勾选状态同时映射到：config.json + 模块变量 + 环境变量。"""
    _write_flags(streaming, warmup)
    rs.STREAMING_ENABLED = bool(streaming)
    rs.WARMUP_ENABLED = bool(warmup)
    os.environ["YURUN_REFINE_STREAMING"] = "1" if streaming else "0"
    os.environ["YURUN_REFINE_WARMUP"] = "1" if warmup else "0"


class ToggleDemo(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("语润 · 润色增强开关（TEST 演示）")
        self.geometry("460x320")
        flags = _read_flags()

        tk.Label(self, text="润色增强选项", font=("Microsoft YaHei", 13, "bold")).pack(pady=10)

        self.var_stream = tk.BooleanVar(value=flags["refine_streaming"])
        self.var_warm = tk.BooleanVar(value=flags["refine_warmup"])

        tk.Checkbutton(
            self, text="流式首字上屏（边生成边预览，更快首字）",
            variable=self.var_stream, command=self._on_change,
        ).pack(anchor="w", padx=20, pady=4)
        tk.Checkbutton(
            self, text="录音期预热（打热 prompt cache，降低 TTFT）",
            variable=self.var_warm, command=self._on_change,
        ).pack(anchor="w", padx=20, pady=4)

        tk.Button(self, text="模拟一次润色（演示开关生效）", command=self._demo_refine).pack(pady=14)
        self.log = tk.Text(self, height=8, width=56)
        self.log.pack(padx=12, pady=6)
        self._log("当前：streaming=%s warmup=%s" % (rs.STREAMING_ENABLED, rs.WARMUP_ENABLED))

    def _on_change(self):
        _apply_flags(self.var_stream.get(), self.var_warm.get())
        self._log("已保存：streaming=%s warmup=%s" % (rs.STREAMING_ENABLED, rs.WARMUP_ENABLED))

    def _demo_refine(self):
        # 仅演示开关是否生效：不连网，调用本地归一化 + 发散护栏判断分支。
        cfg = _cfg.get_config()
        text = "今天我们用 refine 的 prompt 来润色一段语音识别文本"
        self._log("输入: %s" % text)
        self._log("streaming=%s -> 走%s" % (
            rs.STREAMING_ENABLED,
            "refiner_stream.refine_stream（需联网）" if rs.STREAMING_ENABLED
            else "refiner.refine_text（整段，与原主流程一致）"))
        self._log("warmup=%s -> 录音开始时%s" % (
            rs.WARMUP_ENABLED, "会预热" if rs.WARMUP_ENABLED else "不预热"))
        self._log("（真实润色需要 api_key；此处仅展示开关路由，未发起请求）")

    def _log(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")


if __name__ == "__main__":
    ToggleDemo().mainloop()
