# 语润 · 润色增强 TEST 目录

本目录是 Cindy 润色机制调研后，给语润（Yurun）补的两项「延迟优化」的**实验副本**，
**不修改 `src/` 下的正式代码**。经你确认后再决定如何并入正式工程。

## 它解决什么

你自建在线润色比 Cindy 慢，根因不是模型也不是那套 prompt，而是缺两件事（Cindy 的工程层）：

1. **流式首字上屏**：边生成边把累积文本推给 UI，首字即上屏，不等整段 JSON。
2. **录音期预热**：录音一开始（不是松口才）发一次「前缀相同、dictationText 为空」的
   占位请求，把上游 prompt cache 打热，松口时 TTFT 大幅降低。

## 文件

- `refiner_stream.py` —— 核心模块，复刻 Cindy 的 `DictationRefiner` 思路：
  - `refine_stream(...)`：OpenAI 兼容 SSE 流式润色，`on_partial` 回调用于实时预览。
  - `warmup_refiner(...)` / `start_warmup_thread(cfg)`：录音期预热（fire-and-forget）。
  - 复用 `src/refiner.py` 的归一化、发散护栏、prompt 加载，行为与原整段润色一致。
  - 运行时开关 `STREAMING_ENABLED` / `WARMUP_ENABLED`（见下）。
  - `python refiner_stream.py` 可做**离线自测**（SSE 解析 + payload 结构），不连网。
- `test_gui_toggle.py` —— tkinter 演示「流式润色 / 预热」两个勾选项：
  - 勾选写入 `config.json` 的 `refine_streaming` / `refine_warmup`；
  - 同时映射成 `refiner_stream` 的模块变量与环境变量（`YURUN_REFINE_STREAMING` / `YURUN_REFINE_WARMUP`）。
  - `python test_gui_toggle.py` 直接看 UI 与开关路由（不联网）。

## 两个开关（默认值：都开）

| 开关 | 环境变量 | 关闭时行为 |
|------|----------|------------|
| 流式首字上屏 | `YURUN_REFINE_STREAMING=0` | `refine_stream` 回退到 `refiner.refine_text`（整段，与原主流程一致） |
| 录音期预热 | `YURUN_REFINE_WARMUP=0` | `start_warmup_thread` 直接 no-op，不发预热请求 |

config.json 字段：`refine_streaming`（默认 true）、`refine_warmup`（默认 true）。

## 快速验证

```powershell
cd D:\SynologyDrive\CODING\yurun\test
python refiner_stream.py        # 离线：SSE 解析 + payload 结构 + 开关状态
python test_gui_toggle.py       # 图形：两个勾选项 + 开关路由演示
```

真连网测延迟（需要 `src/config.py` 里配好 `api_key` / `api_base` / `api_model`）：
在 `refiner_stream.refine_stream(...)` 里填好 key 后调用，观察 `on_partial` 首字到达时刻。

## 想并入正式工程时

1. 把 `refiner_stream.py` 移到 `src/`（它已能兼容 `src/` 直接跑：路径解析会在 `src/` 下
   自动指向 `_THIS.parent` 而非 `parent.parent/src`）。
2. 在 `src/main.py` 的录音开始处（`_on_hold_start`）调 `start_warmup_thread(cfg)`；
   在 `_refine()` 里优先走 `refine_stream(..., on_partial=...)`，失败回退 `refine_text`。
3. 在 `src/gui.py` 设置窗口加两个 Checkbutton，逻辑同 `test_gui_toggle.py` 的 `_apply_flags()`。

> 注：预热要真正生效，你的端点需支持 prompt cache（火山方舟 / DeepSeek 均支持 cache token）。
> 若接入点不支持缓存前缀，预热仍会发一次 `max_tokens:1` 的短请求（成本极低），但省延迟
> 的效果取决于端点缓存能力。
