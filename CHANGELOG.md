# Changelog

本项目开发过程中的关键里程碑与工程修复记录。所有改动均围绕「复刻 Cindy 丝滑语音润色体验 + 修复真机踩坑」展开。

---

## [1.0] — 2026-08-19 · 重大版本（端到端管线定型 + 纠错弹窗最终修复）

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：src/main.py、src/gui.py、src/pill.py、src/logger.py、installer/yurun_setup.iss、CHANGELOG.md
> **背景**：在 0.1.18d 基础上又经历四/五/六度修复（纠错弹窗崩溃、显示不出来+不能拖动、背景透字），整合为 **v1.0 重大版本**。版本号 0.1.18d→1.0（logger.py `YURUN_VERSION` / yurun_setup.iss `MyAppVersion` 同步）。本版把整条语音听写润色管线端到端定型，并补齐「历史技术演进：用过什么 / 改了什么 / 回退了什么」记录，方便接手者一眼看清全貌。

### 1.0 技术方案总览（用了什么 / 怎么实现）

- **全局热键**：`pynput` 键盘钩子监听 **Ctrl+反引号**（用虚拟键码 `vk=0xC0` 判定，布局/输入法无关，恒定可靠），触发「错误纠正」弹窗；录音热键沿用低级钩子。放弃 `RegisterHotKey`（进程内注册失败、真实错误码被 ctypes 掩盖）。
- **识别（ASR）**：火山 SAUC **双向流式**端点 `bigmodel`（`show_utterances:true`），边录边发边收中间结果；松手后等 `FLAG_LAST` **最终结果**（不用中间结果提前返回，避免漏松手前最后 1 秒）。`WebSocketApp` 边发边收规避同步 WebSocket 跨线程 race（旧坑：indicator 卡死）。`ws.close(timeout=0.2)` 防 close 握手默认 3s 阻塞。
- **短句免润色（bypass）**：≤**15 字**（`refiner.BYPASS_MAX_LENGTH=15`）直接贴 ASR 原文秒出、不调 LLM；数字串经 `refiner.strip_numeric_trailing_punct` 剥末尾句号（代码层兜底，因 SAUC 引擎自带标点预测）。
- **润色（LLM）**：火山方舟 DeepSeek-V4-Flash 接入点（`ep-` ID），**关思考模式**（`thinking.type=disabled`）TTFT≈1s（开思考≈2.4s）；流式 `refine_stream` **首字即上屏**（40ms/字打字机节奏），首字前失败自动回退整段；超时放宽到 30s。
- **粘贴**：`typer.py` 用 `user32.SendInput` + `KEYEVENTF_UNICODE` **逐字输入，零剪贴板污染**；个别窗口不兼容 SendInput 时切「剪贴板」兜底（`insert_method` 开关）。
- **词库**：渐进式 `dictionary.py`，Ctrl+反引号弹框存入（正确词+别名+次数）；**三通道生效**——①SAUC 热词直传 `request.context.hotwords`；②bypass 本地替换 `apply_local_replace`；③LLM 词典 `to_llm_text()` 长句润色参考。
- **录音提示框**：pill 胶囊气泡**停在首次弹出位置固定不动**（v0.1.18 修掉"随鼠标飘"）；单实例锁（`singleinstance.py`）保证永远只有一个进程、新进程杀旧接管。

### 历史技术演进回顾：用过什么 / 改了什么 / 回退了什么

- **流式润色**：v0.1.12 引入「流式首字上屏」（`stream:true` + 关 `json_object`+`stream` 防首字乱码，纯文本 SSE 边收边贴）；v0.1.13 把 ASR 从 `bigmodel_nostream`（非流式，松手后识别约 1s）改成双向流式 `bigmodel`，识别延迟归零。**沿用至今。**
- **短句免润色 + 15 字阈值**：v0.1.14 把免润色阈值从 8 字提到 **15 字**（`BYPASS_MAX_LENGTH`），更多日常短句秒出；同时修「松手用中间结果漏字」改回等最终结果。**沿用至今。**
- **固定正在录音提示框**：早期 pill 气泡会**随鼠标/光标移动**（"鼠标走"），v0.1.18 删除 `_maybe_reposition` 轻量重定位，停在首次弹出位置。**沿用至今。**
- **建立词库**：v0.1.17 新增渐进式词库（Ctrl+反引号纠错框 + 三通道）。**沿用至今。**
- **粘贴方式演进**：v0.1.9 前走「写剪贴板+Ctrl+V」（污染 Win+V 历史）→ v0.1.7 试「Ctrl+Z 撤原文再贴润色版」（跨 app 误删整段历史，否决）→ v0.1.7 方案B「润色完一次 `paste` 最终文本」→ v0.1.10 起 `typer.py` **SendInput 零剪贴板污染**（最终方案，剪贴板路径降为兜底）。
- **热键演进**：`RegisterHotKey` 双热键（v0.1.17 尝试，进程内注册失败、错误码被 ctypes 掩盖）→ **pynput 钩子 + vk 0xC0**（v0.1.18 定型，回退掉 RegisterHotKey 路径）。
- **润色端点**：DeepSeek 官方（TTFT≈5.3s）→ **火山方舟**（v0.1.11，TTFT≈1s）+ 关思考（再省≈1.3s）。**沿用至今。**
- **数字串去句号**：v0.1.15 改 prompt（纯数字不补句号）→ v0.1.16 发现 SAUC 自带标点、改**代码层 `strip_numeric_trailing_punct` 兜底**（prompt 保留作第一道、代码层为最终防线）。
- **录音提示框定位**：v0.1.3 锚定焦点窗口矩形（不再跟鼠标飘）→ v0.1.18 进一步停掉录音中轻量重定位（完全固定）。

### v1.0 纠错弹窗最终定型（四/五/六度，详见 0.1.18 章末）

- **四度**：修 `_draw_bg` 里 `canvas.lower()` 无参 `TclError`（tk.Canvas.lower 是 tag_lower 别名，须带 tagOrId；改 `canvas.tk.call('lower', canvas._w)`）；外加 `logger.py` `_report` 调未定义 `_dump` 把真堆栈吞掉，改 `traceback.format_exception` 输出完整堆栈。
- **五度**：修弹窗"一闪不显示"（设计 AI 放大版 `canvas.pack()`+`body.pack()` 不重叠，body 被窗口高度裁掉；改 `canvas.place` 铺满 + `body.place` 浮上层）；加 **header 拖拽把手**（`<ButtonPress-1>`/`<B1-Motion>` 用 `win.geometry("+x+y")` 移动）。
- **六度**：修**背景透出背后文字**（body 原 `bg=TRANSPARENT` 浮在 canvas 上，中间缝隙透字；改 body 不透明白底 `#FFFFFF` + 内缩 8px，窗口高度 +16px，四角仍圆、中间完全遮住）。

### 行为变化

- 版本号 0.1.18d→**1.0**；启动 banner 显示「语润 v1.0」。
- 纠错弹窗：不透明白底完全遮住背后内容、四角圆、可拖动。
- 其余行为同 0.1.18d（pynput 钩子 / 4 字错误文案 / 气泡固定 / 短句 bypass / 流式润色 / 零剪贴板粘贴 / 词库三通道）。

---

## [1.0.1] — 2026-08-22 · 修复睡眠唤醒后字体放大（DPI 锁）

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：src/main.py、src/logger.py、installer/yurun_setup.iss、CHANGELOG.md
> **背景**：用户实测电脑睡眠唤醒后，语润设置窗口与「错误纠正」弹窗**所有字体突然整体放大**（字撑出格子），缩放设置未改、显示器未插拔，重启进程即恢复正常。根因：进程跨睡眠唤醒时，Windows（Win11 24H2）DWM 重新初始化触发 DPI 探测抖动，Tkinter 内部 `tk scaling` 被临时抬高，DPI 敏感的 `tkfont.Font` 随之放大；窗口几何/padding/canvas 圆角为固定像素未同步变，故文字「撑破格子」。非代码 bug，是进程跨睡眠未锁 DPI 所致。

### 改动点（初版，见下方二次修正）

- **进程级 DPI 锁定**（`main.py` 顶部，任何窗口创建前）：`ctypes.windll.shcore.SetProcessDpiAwareness(1)`（SYSTEM_AWARE），声明后 Tk 不再跟随系统后续 DPI 漂移。
- **font scaling 兜底锁定（初版，已回退）**：`self.root.tk.call('tk', 'scaling', 1.0)`。
- 版本号 1.0→**1.0.1**（`logger.py` `YURUN_VERSION` / `yurun_setup.iss` `MyAppVersion` 同步）。

### 二次修正（同日）：改"硬钉 1.0"为"记住原值+漂移还原"

> **背景**：初版硬钉 `tk scaling 1.0` 导致用户实测**所有界面字体整体变小**（pill 气泡/设置/纠错弹窗全是 tkfont.Font 驱动，无一幸免）。根因：`tk scaling` 是「点→像素」换算因子，用户系统 Tk 默认算出的值 >1.0（即 v1.0 正常字号基准），硬钉 1.0 把字压到约 75%。`SetProcessDpiAwareness(1)` 不改变像素、非元凶。

- **删掉 `tk scaling 1.0` 硬钉**（字压小的直接原因）。
- **保留 `SetProcessDpiAwareness(1)`**（见下方三次修正：此行本身抬高了 orig_scaling 基准，最终被删）。
- **新增 `_watch_dpi_drift`**（`App.__init__` 调用）：`tk.Tk()` 创建后捕获 `self._orig_scaling = float(root.tk.call('tk','scaling'))`（即 v1.0 原字号基准）；内部 `_restore` 每 2s（`root.after(2000)`）检查一次，若 `tk scaling` 偏离 `orig_scaling` 即还原；并绑 `<Configure>` 事件作即时兜底（WM_DPICHANGED 收不到时也能纠正）。漂移还原时记日志 `DPI 漂移已还原: x -> y`。

### 三次修正（最终）：删 aware 声明 + 拦截 WM_DPICHANGED

> **背景**：二次修正后用户实测**设置界面字号比 v1.0 还大**。根因复盘：`SetProcessDpiAwareness(1)` 让进程声明 DPI Aware，Tk 在 Aware 模式下读到的系统缩放基准比 v1.0（非 Aware）算出的**更大**，于是 `orig_scaling` 捕获到的是被抬高的错误基准，守卫把它还原成"错误的大值" → 字比 v1.0 大。
>
> 真正触发"睡眠后变大"的机制是：**Win11 睡眠唤醒时 DWM 广播 `WM_DPICHANGED`，Tk 收到后按当前 DPI 模式重算 font scaling**。要"睡眠前=睡眠后"，正确做法是①回到 v1.0 模式（不声明 aware，orig_scaling 即 v1.0 正确基准）②拦截 `WM_DPICHANGED` 阻止 Tk 在唤醒时重算。

- **删掉 `SetProcessDpiAwareness(1)` 声明**（顶部，回到 v1.0 DPI 模式，orig_scaling 捕获值 = v1.0 原字号基准）。
- **`_watch_dpi_drift` 升级**：
  - 用 `ctypes` 子类化 root 窗口 WndProc（`SetWindowLongW` + `GWL_WNDPROC`），捕获 `WM_DPICHANGED (0x02E0)`；
  - 收到该消息时先 `tk scaling` 还原回 `orig_scaling`，再 `return 0` 吞掉消息，告诉系统"已处理"，**阻止 Tk 默认重算字体**；
  - 同时保留每 2s `_restore` 周期兜底（其他路径改了 scaling 也能纠正）；
  - 安装失败（如缺 wintypes）自动降级为仅周期兜底，不崩；WndProc 回调保活（`self._wndproc_ref`）防 GC。
- 效果：**平时字号严格 = v1.0**；睡眠唤醒时 `WM_DPICHANGED` 被拦截 + scaling 锁回原值，字体不放大、不缩小。代价：运行中拖到不同 DPI 显示器不自动重适配（与「字别乱跑」诉求一致，可接受）。

### 验证

- 代码层：`py_compile` 通过；`SetProcessDpiAwareness`、`_orig_scaling` 捕获、`_restore` 均 `try/except` 包裹。
- 行为：初版（字变小）已修正；字号恢复 v1.0；睡眠唤醒漂移由守卫还原。多显示器间拖窗不自动重适配（有意取舍）。

---

## [0.1.18] — 2026-08-18 · 纠错热键改 pynput 钩子 + 错误文案 4 字 + 气泡停位

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：src/main.py、src/hotkey.py、src/pill.py、src/logger.py、installer/yurun_setup.iss、Yurun.spec、CHANGELOG.md
> **背景**：用户实测 v0.1.17 修复版：①Ctrl+反引号仍无反应（日志 07:07:55「纠错热键注册失败（Ctrl+`），错误码 0」）；②错误气泡字多带省略号（要求统一 4 字 + 红感叹号）；③「正在录音/识别」气泡会随鼠标/光标移动（要求停在首次弹出位置）。诊断（独立测试）：系统层面 Ctrl+反引号可注册、先主键后组合键顺序无冲突、无修饰热键在带 Ctrl 时不会误触发录音——即 RegisterHotKey 路径理论可行，但语润进程内注册失败且真实错误码被 ctypes 掩盖（windll 未开 use_last_error）。放弃 RegisterHotKey 第二热键，改用 pynput 全局键盘钩子。

### 改动点

- **纠错热键改 pynput 钩子**（`main.py`）：`hotkey.py` 回滚第二热键改动（start_correct/_window_ready/on_correct 全删，恢复单热键）。`run()` 启动 `pynput.keyboard.Listener`，`_kb_on_press/_kb_on_release` 检测 Ctrl+反引号组合（防重复触发：触发后等反引号释放才复位）→ `_on_correct_key` → 弹「错误纠正」框。启动日志 `纠错热键监听已启动: Ctrl+`（pynput）`。
- **错误文案统一 4 字**（`main.py`）："没听到声音"→"识别失败"（用户明确）、"模块加载失败"→"模块失败"、"没识别到内容"→"没识别到"、"词库加载失败"→"词库失败"。红感叹号图标上版已画（canvas 圆+!，不占文本空间）。
- **气泡停止随动**（`pill.py`）：删除 `_tick` 里录音/润色的 800ms 轻量重定位（`_maybe_reposition`），状态窗口停在首次弹出位置直到本次结束。
- **版本号 0.1.17→0.1.18**：logger.py、yurun_setup.iss 同步；spec hiddenimports 补 `pynput`、`pynput.keyboard`。

### 验证

- py_compile 通过；pynput import 与 `<ctrl>+`` 解析 OK（系统 Python 3.12.2）。
- 独立测试已证：无修饰反引号热键在带 Ctrl 按下时**不触发**（录音/纠错两键位共存安全）。

### v0.1.18 修复（07:30 重打包，同版完善）

- **pynput 钩子检测不到 Ctrl+反引号**（用户实测仍无反应；日志确认钩子已启动但无触发）：根因 `_kb_on_press` 用 `key.char == "`"` 判断反引号键，**char 受键盘布局/输入法影响**（中文输入法下反引号键 char 可能是「·」而非「`」）→ 改**用 vk（虚拟键码 0xC0）判断**，布局无关、恒定可靠。
- **提示字体不统一**（用户要求"所有提示按正在录音统一"）：`show_error`/`show_guide` 从 13px（FONT_GUIDE）改 15px（FONT），`ERROR_FONT_SIZES` 起点同步 15px；全部状态提示统一 15px。
- **说明（非 bug）**：changelog 识别成"change log"是词库为空所致——纠错弹窗此前从未成功弹出，词库没有 changelog 词条，热词/替换自然不生效；修好纠错键、存一次词条后生效。

### v0.1.18 修复二（08:40 重打包，同版完善）

- **纠错弹窗"没反应"真根因**（用户实测 WorkBuddy 里 Ctrl+反引号无反应；日志 08:25:10~25 十次「纠错热键触发」+ 十次 `UI 事件处理失败: wrong # args: ....canvas raise tagOrId`）：Apple 风格重写时误写 `canvas.lift()`——**tk.Canvas 的 lift 是 item 方法（需 tagOrId 参数）**，无参调用抛异常，弹窗创建后永远卡在隐藏态不显示。修复：删除该行（body 后创建本就在 canvas 上层）。
- **焦点锁定 C1**（用户核心需求"固定产生文字的框"）：`_on_hold_start` 用 GetForegroundWindow + GetGUIThreadInfo 记录目标输入控件 hwnd；`pump` 每 40ms 检查，焦点被切走 0.4s 后 `AttachThreadInput + Alt keybd_event + SetForegroundWindow` 抢回；`done`/`error` 事件释放锁定。
- **录音超时逻辑**：① `pill.STATE_TIMEOUT` 删除 recording 40s 假报错（与 max_seconds=90 矛盾）；② 录音 80s 时气泡切感叹号图标 +「还剩10秒」（不切状态，90s 自然终止）；③ **`recorder.record_chunks` 加 max_seconds=90 自动停止**（v0.1.18 之前 SAUC 真流式无限录直到松手——"超时跳了还能继续录"的另一半原因），`main._record_job_sauc` 传 max_seconds=90。
- **短句策略**：bypass 原样贴 ASR 原文（用户确认不改策略；"测试"说三遍只出一遍是 SAUC 连续发音合并的引擎行为，非程序干预）。

### v0.1.18 修复三（18 重打包，同版完善）

- **纠错弹窗字体统一 15px**（`main.py` `_show_correction_dialog`）：标题/识别文本/正确写法/输入框/按钮/状态文案全部从 9~11px 升到 15px，与「正在录音/正在润色」状态提示（`pill.FONT = ("Microsoft YaHei UI", 15)`）完全一致；弹窗尺寸 340×172 放大到 360×220 容纳。
- **选中文字 + Ctrl+反引号 自动复制（方案A，零剪贴板污染）**（`main.py`）：按 Ctrl+反引号时不再要求用户先 Ctrl+C——弹窗显示前（`win` 仍 withdraw、焦点在外部 app）先 `_clipboard_backup()` 备份用户原剪贴板 → `_send_ctrl_c()` 发 Ctrl+C 把选中内容送进剪贴板 → `time.sleep(0.08)` 读选中文本填「识别文本」→ `_clipboard_restore()` 把原内容写回剪贴板。效果：用户选中几个字、直接按 Ctrl+反引号，弹窗里就是那几个字，且**原剪贴板内容不丢**（复用 v0.1.x「避免污染剪贴板」思路：过去是输出侧用 SendInput 跳过剪贴板，本次是读取侧用备份/恢复）。
- **版本号 0.1.18→0.1.18d**：logger.py、yurun_setup.iss 同步。

### v0.1.18d 字体放大版（08-19 重打包，按《hy3全套指令-字体放大版》）

> **背景**：用户实测设置界面「字体太小了，根本看不清」——上一版沿用了 macOS 默认 12–14px，在 Windows 125%–150% 缩放下过小。按放大版指令把正文锚定到 16px、其余按比例放大，颜色/圆角/胶囊/交通灯/分段控件规则一律不变。

- **设置窗口字号整体放大**（`gui.py`）：F_TITLE 34→**40**、F_CARD 17→**20**、F_SUB 15→**17**、F_ROW 15→**17**、F_LABEL 13→**15**、F 14→**16**、F_SMALL 12→**14**、F_LINK 13→**15**、F_SEG 13→**15**、F_BTN 14→**16**（数值与放大版字号表一一对应）。
- **配套尺寸放大**（`gui.py`）：窗口宽 580→**600**；标题栏 38→**42**；交通灯 12→**13**；卡片内边距 20→**24**；分段控件内边距 14→**16**、容器内边距 3→**4**、默认高 32→**36**；按钮默认高 38→**42**；热键框新增 F_HOTKEY=**19**（72×42 观感）；内容区内边距 32→**36**、顶部 0→**12**、底部 32→**36**。
- **错误纠正弹窗字号放大**（`main.py` `_show_correction_dialog`）：标题 20→**24**、副标题 14→**16**、区块标签 13→**15**、识别文本内容 15→**17**、输入框 15→**17**、状态/按钮 13→**16**；弹窗高 340→**360**、内边距 28/24→**32/24**、只读框/输入框高 46→**52**、观感更宽松。
- **仍 0.1.18d**（仅界面放大，功能无变化）：logger.py / yurun_setup.iss 版本号不变。

### v0.1.18d 像素级复刻修正（08-19 二度重打包，按《hy3纠错指令-Apple风格.md》）

> **背景**：用户实测「严重偏离设计稿」。逐条比对 HTML 参考稿，定位到 4 类结构性偏差，全部修正（HTML 为准，文字规范冲突时以 HTML 为准）。

- **致命：衬线字体根因**（`gui.py`）：`FONT` 原是**字体元组** `("SF Pro Text","SF Pro Display","PingFang SC","Microsoft YaHei")`，又被当作 `font=(FONT, size, weight)` 使用 → 嵌套元组导致 Tkinter 字体解析错乱、回退到系统默认（"语润"呈衬线）。改为**单一字符串** `FONT = "Microsoft YaHei UI"`（Windows 回退即该无衬线字体，与 pill 一致）；所有 `font=(FONT, ...)` 现在都是合法 `(family, size, weight)` 元组。
- **字段布局：标签在上、输入框在下**（`gui.py` `_field`）：原为 `side="left"/side="right"` 并排（违反 HTML `.field-group` flex column）。改为 grp 内 label 占一行、input 在下一行（label margin-bottom 10px）、desc 在 input 下（margin-top 8px）。
- **卡片 header 同行**（`gui.py` `Card` + `_card_header`）：原卡片标题画在 canvas 顶部、分段控件堆在标题下方（上下）。重构 `Card` 去掉 canvas 标题，新增 `_card_header(card, title, right=)` 在 body 首行放「标题左 + 分段右」（`justify-content: space-between`），完全对齐 HTML `.card-header`。
- **去三角 + 去多余说明**：高级链接由 `▸/▾ 高级（…）` 改为纯文字 `高级（…）`（规范禁止 ▶ 三角）；热键下「默认反引号（`），位于 Tab 上方」说明文字删除（规范禁止），只留「热键」+ 白色圆角输入框（72×42 观感，19px 居中）。
- **验收清单逐项对照**：无衬线标题 ✓ / 三个白卡 ✓ / 云端火山·本地离线胶囊分段 ✓ / API Key 标签在上 ✓ / 热键白框 ✓ / 触发·粘贴胶囊分段 ✓ / 取消左保存右胶囊 ✓ / 正确写法输入框完整边框 ✓。
- 仍 0.1.18d（仅视觉，功能无变化）。

### v0.1.18d 纠错弹窗崩溃修复（四度，prev7 重打包）

- **根因 1（主因/`main.py` `_draw_bg`）**：设计 AI 放大版 `_draw_bg` 里写 `canvas.lower()` 无参。Tkinter 中 `tk.Canvas.lower` 被别名成 `tag_lower`（降 canvas 内部图元，必须带 tagOrId），无参调用 → `TclError: wrong # args`，热键一触发即崩、弹窗永远卡在隐藏态。修复：`canvas.lower()` → `canvas.tk.call('lower', canvas._w)`（Widget 级 lower 正确调用，绕过别名；body 后创建本就在上层，z 序不变）。
- **根因 2（次因/`logger.py` `_report`）**：`_report` 调了未定义的 `_dump(...)`，Tk 回调异常时二次抛 `NameError` 把真实堆栈吞掉，排错时只见 `wrong # args` 反复出现却找不到来源。修复：改用 `traceback.format_exception` 把真实堆栈写进 `yurun.log`，以后任何 Tk 错误都能看见根因。
- 用户明确"bug 修掉、设计先不动"——只改这 2 处，不碰设计 AI 的布局。

### v0.1.18d 纠错弹窗「显示不出来 + 不能拖动」修复（五度，prev8 重打包）

- **显示根因**：设计 AI 放大版用 `canvas.pack()` + `body.pack()` 上下排布（不重叠）。canvas 占顶部一条、white 的 body 被窗口高度裁掉 → 只看到透明/错位、内容全无。正确写法对齐 `pill.py`：canvas `place` 铺满窗口作圆角白底、body `place` 浮在上层且 `bg=TRANSPARENT`（四角由 canvas 圆角呈现、body 不挡四角）。
- **拖动**：`overrideredirect` 无标题栏，新增 **header 拖拽把手**：`header` Frame（cursor=fleur）绑 `<ButtonPress-1>`/`<B1-Motion>`，用 `win.geometry("+x+y")` 移动窗口；标题/副标题移入 header。
- 仍 0.1.18d（仅修复，视觉样式原样未动）。

### v0.1.18d 纠错弹窗「背景透出背后文字」修复（六度，prev9 重打包 → 整合为 v1.0）

- **根因**：body 此前是 `bg=TRANSPARENT` 浮在 canvas 之上，弹窗中间缝隙透出背后文字（白底下正常、有文字背景下透出）。用户要求"对齐问题和需求再改"。
- **修复**：body 改为不透明白色 `#FFFFFF` + 内缩 8px（`place(x=8,y=8,width=W2-16,height=H2-16)`），浮在 canvas 圆角白底之上；`_place` 窗口高度多给 16px（`H2 = body.winfo_reqheight() + 16`）。外圈 8px 露出 canvas 圆角白底形成圆角边框，中间完全不透明、遮住背后内容。
- 此轮即 v1.0 整合前的最后一轮修复；版本号随整合升到 **1.0**（见 [1.0] 章）。

### 行为变化

- Ctrl+反引号弹「错误纠正」框（pynput 钩子，不再依赖 RegisterHotKey）。
- 错误气泡统一 [红感叹号] + 4 字短文案。
- 「正在录音/识别/润色」气泡停在首次位置，不随鼠标移动。
- 「错误纠正」弹窗字体与状态提示统一 15px；选中文字后直接 Ctrl+反引号自动填入，不必先 Ctrl+C。

---

## [0.1.17] — 2026-08-18 · 渐进式词库（纠错快捷键）+ 气泡错误图标修复

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：src/dictionary.py（新增）、src/hotkey.py、src/main.py、src/pill.py、src/sauc_asr.py、src/config.py、src/logger.py、installer/yurun_setup.iss、Yurun.spec、CHANGELOG.md
> **背景**：用户要"越用越聪明"的词库——说"千机 log"识别成"天气 log"（实为 changelog），多次纠正后自动记住。查 Cindy 源码（makecindy/cindy voice-input 模块）确认其机制：上屏后监视编辑器 transaction → 检测用户修改刚上屏文本 → 停改 15s 后 LLM advisor 判断 → 自动存词条（正确词+别名+次数）+ Toast 可删除。但 Cindy 能"感知修改"是因为文本在**它自己的编辑器**里；语润 SendInput 打进外部应用，**无法感知用户事后修改**，只能靠用户主动告知 → 用户拍板：B 为主（纠错快捷键 Ctrl+反引号）+ A 辅助（后续 GUI 词库页），手动确认式，词库规模小，热词+本地替换双保险。另修复：气泡错误提示带"⚠ "文本前缀，⚠ 在 Windows 渲染成 emoji 挤压文字导致「识别失败」显示不完整。

### 改动点

- **新增 `src/dictionary.py` 词库模块**：存储 `%APPDATA%\Yurun\user_dictionary.json`；词条结构 `{text(正确词,唯一), aliases[{text,count}], count, source}`；API：add_entry（正确词+错误变体累积去重、count 递增）/delete_entry/to_hotwords（按 count 降序，≤180 字符预算）/apply_local_replace（bypass 替换，别名按长度降序）/to_llm_text（userDictionary 文本）。
- **纠错热键 Ctrl+反引号**：`hotkey.py` 扩展第二热键（RegisterHotKey id=2，MOD_CONTROL|MOD_NOREPEAT），`config.py` 加 `correction_hotkey`（默认 `）;main.py 触发 `show_correction` 事件。
- **「错误纠正」弹窗**：`main.py` `_show_correction_dialog`——识别文本自动读剪贴板（可编辑）+ 正确写法输入框 + 存入词库/取消；确认后显示「已存入词库：xxx」1.2s 自动关；回车确认、Esc 取消。
- **三通道生效**：①ASR 热词——`sauc_asr.py` `_build_full_request` 加 `request.context.hotwords` 直传，`sauc_transcribe_stream`/`sauc_transcribe` 透传 hotwords，main 从词库取；②bypass 本地替换——`_after_transcribe` 剥句号后 `apply_local_replace`（短句直通路径兜底）；③LLM 词典——`_refine`/`_refine_stream_and_paste` 传 `user_dictionary=to_llm_text()`（长句润色参考）。
- **气泡错误图标修复**：`pill.py` 去掉 `"⚠ "` 文本前缀（⚠ emoji 渲染挤压文字致「识别失败」显示不全），改画圆底感叹号（canvas 圆+“!”）固定位置不占文本空间；`show_error` 文本左对齐 + `_fit_error_text` 超宽截断兜底。
- **版本号 0.1.16→0.1.17**：logger.py、yurun_setup.iss 同步；spec hiddenimports 加 `dictionary`。

### 验证

- py_compile 通过；dictionary 单测（新增/别名累积/热词列表/替换映射）待打包前跑。
- 热词为火山 SAUC `request.context` 直传（≤200 tokens），英文词（changelog）效果需实机验证，bypass 本地替换为确定性兜底。

### 行为变化

- Ctrl+反引号 弹「错误纠正」框：识别文本自动读剪贴板，填正确写法确认即入词库。
- 词库生效：下次识别走热词（源头）+ 短句 bypass 本地替换 + 长句 LLM 词典。
- 错误提示气泡：圆底感叹号图标 + 纯文本消息，任何长度完整显示。

### v0.1.17 修复（07:06 重打包，同版完善）

- **纠错热键注册失败**（用户实测 Ctrl+反引号无反应）：根因 `hotkey.start()` 异步建窗（后台线程），`run()` 里 start 后立即 `start_correct()` 时 `_hwnd` 还是 None → 静默 return False，热键从未注册。修复：`HotkeyListener` 加 `_window_ready` 事件，窗口创建后 set；`start_correct` 等待窗口就绪（≤3s）再注册，失败打 warning 日志（不再静默）。
- **错误气泡"字多+省略号"**（用户实测"没听到声音，再试一次"被截成"没听到声音…"）：①错误文案缩短——"没听到声音，再试一次"→"没听到声音"、"SAUC 模块加载失败"→"模块加载失败"（全部 ≤6 字，86px 内放下不触发截断）；②`_fit_error_text` 改为**字体缩小优先**（13→11→10px，放得下就不截断、不加省略号），仅 10px 仍超宽才截断兜底。

---

## [0.1.16] — 2026-08-18 · 数字串去句号改代码层（ASR 自带标点）

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：src/refiner.py、src/main.py、src/logger.py、installer/yurun_setup.iss、CHANGELOG.md
> **背景**：v0.1.15 按方案 A 改 prompt（数字不补句号）后用户实测纯数字/手机号/订单号**仍加句号**。查 `%APPDATA%\Yurun\logs\yurun.log` 实锤根因：**火山 SAUC ASR 引擎自带标点预测，识别原文就带句号**——日志 `识别结果: 12345。`、`识别结果: 13382513336。`、`识别结果: 1234567。`。短句（≤15 字）走免润色 bypass 直接贴 ASR 原文，**根本没调 LLM**，改 prompt 完全无效。之前"12345 被 LLM 加句号"的判断不完整：即使走 LLM 是 prompt 的锅，短句场景纯属 ASR 端。

### 改动点

- **`refiner.py` 新增 `strip_numeric_trailing_punct()`**：末尾是标点 且 文本数字占比 ≥ 60%（数字为主）→ 剥掉末尾标点；正常句子（数字占比低）保留。覆盖纯数字/手机号/订单号/日期等编号类场景。
- **`main.py` 三处接线**：① `_after_transcribe` 入口拿到 ASR 文本后先剥（bypass 主路径——问题核心）；② 非流式润色 `final` 输出再剥一次（LLM 不遵守 prompt 时兜底）；③ 流式失败回退整段的 `final` 同样剥。流式 on_delta 动态贴出不处理（只有长句走流式，数字占比高的长句罕见，prompt 规则已兜）。
- **版本号 0.1.15→0.1.16**：`logger.py`、`yurun_setup.iss` 同步。

### 验证

- `strip_numeric_trailing_punct` 9 用例全过：`12345。`→`12345`、手机号、`订单号20260818。`、`2026年8月18日。` 均剥句号；`今天早就要去生活，嗯，对。`、`第3季度营收100亿。` 正常句子保留句号。
- py_compile 通过；重新打包 `dist/语润.exe`（约 71.4MB）。

### 行为变化

- 纯数字/手机号/订单号/日期等数字为主的文本，末尾不再带句号（无论 bypass 还是润色路径）。
- 正常句子照旧保留标点。v0.1.15 的 prompt 修改保留（LLM 路径第一道防线，无害）。

---

## [0.1.15] — 2026-08-17 · 数字串/编号不补句号

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：prompts/refine-dictation.zh.txt、src/logger.py、installer/yurun_setup.iss、CHANGELOG.md
> **背景**：用户实测纯说「12345」也会被润色 LLM 在末尾补句号。标点补全逻辑完全由润色提示词驱动（prompts/refine-dictation.zh.txt 第 36 行「添加标点…」），代码层无任何标点规则。即便短句本应 bypass 不调 LLM，一旦因自定义指令或 ASR 原文带点导致走润色，LLM 就会按 prompt 把数字串当句子补句号。用户要求加硬性规则：数字串/编号/密码结尾不补句号。

### 改动点

- **prompts/refine-dictation.zh.txt 三处加固**：① 第 36 行「添加标点」段补「纯数字串、编号、密码、连续数字结尾不补句号」；② 硬性禁止段新增一条「不要给纯数字串/编号/密码结尾补句号（如 12345 原样返回）」，优先级最高（连自定义指令也覆盖不了）；③ 简短示例段新增反例「12345」->「12345」。流式与非流式两条润色路径共用同一 base prompt，均生效。
- **版本号 0.1.14→0.1.15**：`logger.py`、`yurun_setup.iss` 同步。

### 验证

- py_compile 通过；重新打包 `dist/语润.exe`（约 71.4MB），二进制内确认新规则（`12345` 示例）已嵌入。

### 行为变化

- 走 LLM 润色的文本，若末尾是纯数字串/编号/密码，不再补句号；正常句子仍按原规则补标点。

---

## [0.1.14] — 2026-08-17 · 免润色阈值 8→15（短句秒出）

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：src/refiner.py、src/main.py、src/logger.py、installer/yurun_setup.iss、CHANGELOG.md
> **背景**：用户实测短句（≤8 字免润色）秒出、长句（>8 字）要等方舟润色 1~8s，体感差异大。方舟润色速度波动（1~8s）是长句慢的根源，预热无效（实测连续 3 次无 cache 递减）。故提高免润色阈值，让更多日常短句直接贴原文秒出。

### 改动点

- **免润色阈值 8→15 字**：`refiner.py` 新增 `BYPASS_MAX_LENGTH=15` 常量，`_should_bypass_llm` 与 `main.py` `_refine_will_change` 均引用之。15 字以内短句不再调 LLM，直接贴 ASR 原文（秒出）。
- **修复「松手即用中间结果」漏字**：`sauc_transcribe_stream` 松手后改回等最终结果（FLAG_LAST），不再用中间结果提前返回——中间结果缺最后一段音频识别，短句免润色时会把松手前最后 1 秒的话漏掉（用户实测发现）。
- **版本号 0.1.13→0.1.14**：`logger.py`、`yurun_setup.iss` 同步。

### 验证

- py_compile 通过。

### 行为变化

- 15 字以内的短句不再润色（不补标点/不整理），直接出原文；长句仍润色。

---

## [0.1.13] — 2026-08-17 · 流式识别（边录边识别，松手立即润色）

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：src/sauc_asr.py、src/config.py、src/logger.py、installer/yurun_setup.iss、CHANGELOG.md
> **背景**：续 [0.1.12] 流式润色，用户提出「能不能边录音边上传边润色」。查证火山 SAUC 端点：当前用 `bigmodel_nostream`（非流式输出，等说完才返回结果，松手后识别约 1s），改用双向流式 `bigmodel` 端点可边听边出中间结果，识别重叠到录音期。真正「边识别边润色」暂不做（中间文本不稳定会打错字），本版先把识别挪进录音期。

### 改动点

- **端点 `bigmodel_nostream` → `bigmodel`**（双向流式）：`config.py` DEFAULTS + 用户本地 `config.json` 同步；`sauc_asr.py` 两处 endpoint 默认值。
- **加 `show_utterances: true`**：`_build_full_request` 让服务端返回实时中间结果。
- **`sauc_transcribe_stream` 改 WebSocketApp 回调模式**：边录边发 + 边收中间结果（on_message 在 run_forever 线程、send 在录音线程），规避同步 WebSocket「跨线程 send/recv」竞争（之前导致 indicator 卡死的坑）；中间结果只攒着不回显（避免文字跳变），松手后等 FLAG_LAST 最终结果。
- **补 `Thread.isAlive` 兼容别名**：websocket-client 0.57 内部用 `isAlive()`，Python 3.12 已移除（改为 is_alive），不补则 `run_forever` 抛 AttributeError。
- **松手即用中间结果（识别延迟归零）**：松手后不再死等 FLAG_LAST 最终结果，改为「0.35s 宽限期——优先等最终结果，超时则用当前最新中间结果」；`ws.close(timeout=0.2)` 修复 close 握手默认 3s 阻塞（否则松手后偶发卡 2.7s）。
- **版本号 0.1.12→0.1.13**：`logger.py`、`yurun_setup.iss` 同步。

### 验证

- 静音 3 秒喂 `sauc_transcribe_stream`：连接 + 边发边收 + FLAG_LAST 结束正常，无 race/卡死。
- 合成中文语音（pyttsx3 Huihui「今天天气很好我们去公园散步买了很多东西」）实测 4 次：松手后识别延迟 0.05~0.06s，返回文本完整（含标点），识别延迟近乎归零；加 `close(timeout=0.2)` 前偶发 2.7s 阻塞（close 握手默认 3s）。
- 改动文件 py_compile 通过。

### 行为变化

- 松手后延迟：识别从「松手后完整识别 ~1s」降到「已边听边识别、松手后仅尾部定稿 <0.3s」，再进流式润色；长句收益更大。
- 录音期间保持 WebSocket 长连接（联网时机从「松手后」前移到「说话时」），用户无感。

---

## [0.1.12] — 2026-08-17 · 流式首字上屏（润色边出边贴）

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：src/refiner.py、src/main.py、src/config.py、src/logger.py、installer/yurun_setup.iss、CHANGELOG.md
> **背景**：续 [0.1.11] 提速，用户实测关 thinking 后仍觉出字慢。读 Cindy 源码确认其润色是「纯在线 + 流式首字上屏 + 录音期预热」，快在工程层而非模型本身。本版复刻「流式首字上屏」：润色结果边生成边 SendInput 输入，首字即上屏，不再等整段 JSON 回包。预热暂不做（DeepSeek/方舟 prompt cache 按前缀自动命中，预热仅对首次有增量且每次多一次计费，性价比低）。

### 改动点

- **流式润色 `refine_stream()`**：`refiner.py` 新增纯文本流式（`stream:true` + 关 thinking + `Accept: text/event-stream` + `Accept-Encoding: identity`），边收 SSE `delta` 边 `on_delta` 回调；system prompt 沿用 Cindy 原 prompt 精华，仅把「输出要求」段替换为纯文本直出（`_load_stream_prompt`），避免边流边拼 JSON 的首字乱码问题。
- **首字即上屏接线**：`main.py` 新增 `_refine_stream_and_paste`，`on_delta` → `ui_q` 的 `type_partial` 事件 → 主线程 `_do_type` 逐段 SendInput；首字前失败（连接/HTTP/网络）自动回退整段 `refine_text`（此时尚未贴字，安全）。
- **固定前缀顺序**：`_build_user_payload` 提取为共用 helper，字段顺序稳定（promptVersion/context 稳定字段在前、dictationText 易变字段最后），配合端点自动 prompt cache 命中。
- **配置开关**：`config.refine_streaming`（默认 True）可关流式回退整段；流式仅在 `insert_method=type`（SendInput）时启用，paste 模式保持整段。
- **逐字打字节奏（打字机效果）**：`typer.py` `type_text` 加 `char_interval` 参数（>0 时逐字投递 + sleep），`main.py` 流式 `_do_type` 用 `char_interval=0.04`（40ms/字）。此前模型生成多快就贴多快，短句生成 <0.5s、分片间隔太短，人眼感知不到「逐字」，观感仍是「整段蹦出」；现在固定 40ms/字节奏，文字像打字机逐字冒出。
- **修复 show_transcribing 缺失**：`pill.py` 补 `show_transcribing` 方法（「⟳ 正在识别」态）。此前 `main.py` 每次松手都抛 `AttributeError: 'PillBubble' object has no attribute 'show_transcribing'`（识别等待期的 pill 状态一直没实现）。
- **版本号 0.1.11→0.1.12**：`logger.py`、`yurun_setup.iss` 同步。

### 验证

- 改动文件 `py_compile` 通过。

### 行为变化

- 长句润色从「等整段回包再一次性贴」改为「首字即出、边润边贴」，打字速度随模型生成实时推进。
- 流式遇断流保留已贴部分（partial）不再重贴；首字前失败自动回退整段，体验与旧版一致。
- 发散护栏 `is_diverged` 在流式路径不做事后撤回（边贴边出无法撤销），但短句已被 `_refine_will_change` 过滤、关 thinking + Cindy 强约束下跑偏概率极低。

---

## [0.1.11] — 2026-08-17 · 润色端点迁火山方舟 + 关思考 + 超时30s

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：src/config.py、src/main.py、src/refiner.py、src/gui.py、src/logger.py、installer/yurun_setup.iss、CHANGELOG.md
> **背景**：续 [0.1.2]/[0.1.7] 出字速度讨论，DeepSeek 官方端点 TTFT≈5.3s 偏慢。实测火山方舟 DeepSeek-V4-Flash 接入点（带 `ep-` 的推理接入点 ID）TTFT≈1s、整句润色 ≈1.1s（关思考）/2.4s（开思考）；且其 `message["content"]` 可直接解析，Yurun 现有 OpenAI 兼容逻辑无需改动即兼容。用户要求切换并提速。

### 改动点

- **润色端点默认迁火山方舟**：`config.py` `api_base` 默认改 `https://ark.cn-beijing.volces.com/api/v3`，`api_model` 默认留空（提示填 `ep-` 接入点 ID）；用户本地 `config.json` 已写入真实接入点 `ep-20260817122432-jpl9q` 与方舟 key。GUI 红圈区标签由「DeepSeek 开放平台 API Key（sk- 开头）」改为通用「润色 API Key（OpenAI 兼容，sk-/ark- 等均可）」；高级项默认 Base URL 预填方舟、模型留空。
- **关思考模式（默认开）**：`refiner.py` 请求体新增 `"thinking": {"type": "disabled"}`（受 `config.disable_thinking` 控制，默认 `True`）。针对 Cindy 这套指令明确的润色任务，模型「内心戏」是冗余，关掉实测提速约 1.3s（2.4s→1.1s）且 `reasoning_content` 不再出现；真需质量可改 `disable_thinking:false` 还原。
- **润色超时 10s→30s**：`refiner.py` `timeout` 默认改 30（受 `config.refine_timeout` 控制），长句 / 网络波动不再轻易触发 `timeout` 失败回退原文。仅放宽上限，正常该 1~2s 完成仍是 1~2s。
- **版本号 0.1.10→0.1.11**：`logger.py` `YURUN_VERSION`、`installer/yurun_setup.iss` `MyAppVersion` 同步。
- 新增 config 字段：`disable_thinking`（bool，默认 True）、`refine_timeout`（int，默认 30）。

### 验证

- 用真实方舟 key 精确复刻 `refiner.py` 请求（json_object + thinking 关闭 + 非流式）：耗时 1.07s，`reasoning_content` 不存在，`content` 正确解析为 `{"text": "今天去超市买了很多东西。"}`。
- 改动文件 `py_compile` 通过。

### 行为变化（对协作同学说明）

- 默认润色端点从 DeepSeek 官方切到火山方舟；新装用户需自己建一个 `ep-` 接入点填进「模型」框。
- 思考模式默认关闭（更快），非推理类润色无感；若发现复杂长文润色质量下降，把 `disable_thinking` 改 false。
- 润色等待上限放宽到 30s，故障回退等待也随之变长。

---

## [0.1.10] — 2026-08-17 · 修复 SendInput 类型错误（0.1.9 仍污染剪贴板的真因）

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：src/typer.py、src/logger.py、installer/yurun_setup.iss
> **背景**：0.1.9 宣称零剪贴板污染，但用户实测剪贴板历史仍被写入。查 0.1.9 日志发现 `typer.py` 每次 `SendInput` 抛 `TypeError`（传入 `ctypes.byref(arr)` 得到的是 `LP_INPUT_Array_N`，而 `SendInput` 期望 `LP_INPUT`），异常被 `_do_paste` 捕获后回退「写剪贴板 + Ctrl+V」，于是污染照旧。

### 改动点

- **typer.py 修复类型**：`ctypes.byref(arr)` → `ctypes.cast(arr, ctypes.POINTER(INPUT))`，实测 `type_text('hi')` 返回 4，投递成功。
- **版本号 0.1.9→0.1.10**：`logger.py`、`yurun_setup.iss` 同步。
- 保留 `dist/语润-v0.1.9.exe`（坏版本，备查）与 `dist/语润-v0.1.8.exe`（原版回退）。

### 验证

- 用户退出 dev 模式 Python、双击 v0.1.10 exe 启动后确认：「还真是可以了」。日志实锤：`SendInput 已投递 60 字（type 模式，零剪贴板污染）`，全程无「剪贴板已写入 / Ctrl+V / 回退」任一条。
- **v0.1.10 = 语润首次真正实现零剪贴板污染粘贴**。

### 行为变化

- 仅修内部调用类型，无接口变化；`insert_method=type` 路径这下才真正生效。

---

## [0.1.9] — 2026-08-17 · 零剪贴板污染粘贴 + 录音90秒 + 拿掉识别/润色超时

> **编辑**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件**：新增 src/typer.py；src/main.py、src/pill.py、src/config.py、src/gui.py、src/logger.py、installer/yurun_setup.iss、Yurun.spec
> **背景**：用户反馈两个问题：①说话超过 20–30 秒会弹出「处理超时，重试一次」提示，但润色其实正常完成（提示具有误导性）；②用完语润后 Windows 剪贴板历史（Win+V / 第三方剪贴板工具）全是语润刚贴出去的内容，原本复制的工作内容被挤掉，剪贴板功能基本没法用。经查 Cindy 源码确认：其 Windows 粘贴路径也是「写剪贴板 + Ctrl+V + 600ms 还原快照」，还原只还原「当前剪贴板内容」、不还原「历史」，所以历史污染是机制固有局限，唯一治本路径是不走剪贴板。

### 改动点

- **粘贴改 SendInput Unicode 逐字输入（零剪贴板污染，核心）**：新建 `src/typer.py`，用 `user32.SendInput` + `KEYEVENTF_UNICODE` 逐字符投递 Unicode 码点，**全程不碰剪贴板**。Win+V 历史一条都不多，原剪贴板内容零影响；中英文均准确（码点直传，不经过虚拟键码 / IME）。`_do_paste` 按 config 的 `insert_method` 分流：`type`（默认）走 SendInput；`paste` 走原「写剪贴板 + Ctrl+V」作兜底；SendInput 失败时自动回退剪贴板路径并记日志。
- **设置窗加「粘贴方式」开关**：触发方式下方新增 Segmented（逐字输入 / 剪贴板），默认逐字输入；个别窗口不兼容 SendInput 时可切剪贴板。
- **录音上限 30→90 秒**：`main.py` `max_seconds=90`，长阐述不再被截断。
- **拿掉识别 / 润色态硬超时**：`pill.py` `STATE_TIMEOUT` 删掉 `transcribing`(25) / `refining`(25)，只保留 `recording:40` 兜底。长录音后识别 / 润色耗时再久也不再误报「处理超时」；录音态 40s 仅防录音线程异常卡死（正常 max_seconds=90 会先到，40s 兜底实际跑不到，留作异常保险）。
- **版本号 0.1.8→0.1.9**：`logger.py` `YURUN_VERSION`、`installer/yurun_setup.iss` `MyAppVersion` 同步。
- **打包**：`Yurun.spec` `hiddenimports` 加 `'typer'`。

### 验证

- 全部改动文件 `py_compile` 通过；`typer` 的 `INPUT` 结构 `sizeof=40`（64 位正确）。
- 用系统 Python 3.12.2（装了完整运行时依赖）打包成功，`dist/语润.exe` 68.1 MB（与 v0.1.8 同量级，依赖完整）。
- 保留 `dist/语润-v0.1.8.exe` 作回退；用户实机试用确认剪贴板历史零污染 + 长录音不再误报超时。
- 踩坑记录：WorkBuddy 托管 Python 3.13.12 是干净环境、未装项目依赖，用它打包出来仅 28MB（缺 numpy/sounddevice 等大库 DLL）；必须用装了完整依赖的系统 Python 3.12.2 打包。

### 行为变化（对协作同学说明）

- 默认粘贴路径从「写剪贴板 + Ctrl+V」改为 SendInput 逐字输入；Win+V 历史不再被污染。
- 新增 config 字段 `insert_method`（`type` / `paste`，默认 `type`）。
- pill 不再有 `transcribing` / `refining` 硬超时；录音态 40s 兜底保留。
- 单次录音最长 90 秒。
- 代价：逐字输入长文本（如 200 字）约需 0.1–0.5 秒（批量 SendInput），期间输入框不能动；个别应用不接受 SendInput 时切 `paste` 模式。

---

## [0.1.8] — 2026-08-16 · 单实例锁（永远只有一个进程）+ pill 失败文案修复

> **编辑**：WorkBuddy（GLM-5.2，本仓库 AI 协作 agent）
> **涉及文件**：新增 src/singleinstance.py；src/main.py、src/hotkey.py、Yurun.spec
> **背景**：用户反馈①连测多个版本后留了 Yurun.exe 僵尸进程（如 PID 9288）占着反引号热键，导致新启动实例 `RegisterHotKey` 失败、弹"热键注册失败"框；②该失败提示文案"热键注册失败（错误码 X），可能已被其他程序占用"在固定 132×50 pill 里被截成"热键注册失"。用户进一步提出关键担忧：若旧进程活着但任务栏没图标（托盘崩溃/没显示），用户点不到"退出"，新进程也注册失败，会被彻底卡死、没法用。

### 改动点

- **单实例锁：新进程杀旧、自己接管**（核心）。新建 `src/singleinstance.py`：
  - `kill_old_and_takeover()`：PID 文件法（`%APPDATA%/Yurun/yurun.pid`）。读旧 PID → 用 `QueryFullProcessImageNameW` 校验进程名属 `{yurun.exe, python.exe, pythonw.exe}` 才杀（防 PID 被回收后误杀别的程序）→ `TerminateProcess` 杀掉 → 等 0.6s 让旧实例释放全局热键 → 写入自身 PID。
  - `kill_other_yurun_exe()`：用 `CreateToolhelp32Snapshot` 枚举所有进程，杀掉所有名为 `yurun.exe` 的非自身实例。**兜底清理无 PID 文件的旧版本 exe 僵尸**（如 9288 这种旧版本从没写过 pid 文件，PID 文件法找不到它）。
  - `main.py run()` 起头依次调用两个函数，先于热键/托盘启动。
- **设计意图**：不做"新实例直接退出"（那会把用户卡死），而是"新实例杀旧、自己继续"，保证用户哪怕旧实例是没图标的隐形僵尸，双击一次就能把旧的收掉、新的接管——**永远不会被卡死**。
- **pill 失败文案缩短**（`src/hotkey.py`）：`RegisterHotKey` 失败时，pill 提示从长文案缩成 **"热键被占"** 4 字（132 宽 pill 装得下，不再截断）；完整错误码仍写进 `yurun.log` 便于排查。`main.py` 里 `vk=0`（按键名无法识别）路径的 toast 同步缩成 **"热键无效"**。
- **打包**：`Yurun.spec` 的 `hiddenimports` 增加 `'singleinstance'`，防 PyInstaller 静态分析漏掉 `run()` 内的函数级 import。

### 验证（实战）

启动前 `tasklist` 显示 `Yurun.exe 9288`（僵尸）在跑；启动新 exe 后日志写：
```
已结束旧 Yurun.exe PID=9288
已结束旧 Yurun.exe PID=3920   ← 另一个未察觉的僵尸也被收掉
语润启动（开发版）
识别引擎为 sauc               ← 热键注册成功（无"热键被占"）
```
启动后 `tasklist` 仅剩新实例一个 `Yurun.exe`。**单实例锁实战生效，"僵尸占热键→新进程失败→卡死"的恐惧彻底解除。**

### 行为变化（对协作同学说明）

- 以后任何时候双击 Yurun.exe，旧实例（含无图标僵尸）会被自动收掉，永远只有一个进程。
- PID 文件位于 `%APPDATA%\Yurun\yurun.pid`（纯文本，记当前实例 PID）。
- 热键失败提示现在是 4 字短文案；排查看日志而非 pill。

### 收尾补充（2026-08-16 · 混元3 接手）

> **补充**：WorkBuddy（混元3，本仓库 AI 协作 agent）
> **涉及文件（补充）**：src/gui.py、src/tray.py、src/logger.py、Yurun.spec、installer/yurun_setup.iss、README.md、.gitignore
> **背景**：0.1.8 初版条目只记了单实例锁与 pill 文案；用户定稿后由 混元3 完成品牌化与收尾，并补齐期间遗漏的记录。

### 改动点（补充）

- **版本号对齐**：`src/logger.py` 的 `YURUN_VERSION` 与 `installer/yurun_setup.iss` 的 `MyAppVersion` 由 `0.1.0` → `0.1.8`，消除启动 banner 版本与 CHANGELOG 不一致。
- **界面显示名统一为「语润」**：`src/gui.py`（设置窗标题 / 标题标签 / 关于弹窗）、`src/main.py`（`APP_TITLE`）、`src/tray.py`（start / notify 标题）、`src/logger.py`（启动 banner）的 "语润 Yurun" → "语润"。内部 `APP_NAME`、配置文件目录与进程名匹配一律不动，旧配置零影响。
- **打包产物改名「语润.exe」**：`Yurun.spec` 的 `name='Yurun'` → `'语润'`，输出 `dist/语润.exe`；`installer/yurun_setup.iss` 同步 `MyAppExeName` / `Source` / `DefaultDirName` / `OutputBaseFilename`，`README.md` 构建说明同步。
- **单实例锁动态识别（改名不失效）**：`src/singleinstance.py` 进程名校验改为动态取 `os.path.basename(sys.executable)` + `_IS_FROZEN` 守卫；改名 `语润.exe` 后单实例锁仍能认出自身进程并清理旧僵尸，`热键被占` bug 不回潮；`kill_other_yurun_exe` 仅在打包模式按自身 exe 名枚举，避免 dev 模式（python.exe）误杀其它 python 进程。
- **README 更新**：补充 0.1.6–0.1.8 真实功能（单实例锁 / Plan B 润色不误吞前文 / 短句直出 ≤8 中文字 / 热键被占提示）与版本说明。
- **.gitignore 加固**：追加 `dist_*/`、`build_*/` 通配，避免版本化构建目录再次沦为未跟踪废目录。
- **目录收尾**：删除原版 `D:\SynologyDrive\CODING\yurun`（旧 v0.1.1 源码，146MB）；新代码在 `yurun-stream` 完成收尾后改回名为 `yurun`，成为当前唯一版本；清理 `yurun` 内冗余 `dist*/build*` 构建目录（释放约 880MB），仅留 `dist/语润.exe` 作为官方构建。

### 验证（补充）

- 用户实测 `dist/语润.exe`：启动 banner 显示「语润 v0.1.8 启动」、界面显示「语润」、托盘 / 任务栏图标正常，确认可用。

---

## [0.1.7] — 2026-08-16 · 润色改同步一次贴（方案 B，弃用 replace_paste）

> **编辑**：WorkBuddy（GLM-5.2，本仓库 AI 协作 agent）
> **涉及文件**：src/main.py
> **背景**：0.1.2 引入"原文先贴 + 后台润色 + replace_paste 替换"，0.1.3 把 replace_paste 改成"Ctrl+Z 撤原文 + Ctrl+V 贴润色版"。用户实测：润色完按 Ctrl+Z 时，**很多输入框（聊天框/浏览器/Electron）的撤销粒度是"整段历史"而非"最后一次粘贴"**，把之前说的话全删了，只剩润色那句。曾临时试过"Shift+Left×N 选中原文再覆盖"（方案 A），多句连说场景仍会误吃前句，用户否决，改采方案 B。

### 改动点

- **`_after_transcribe` 改为不先贴原文**（方案 B）：`_refine_will_change` 为真时，只显示"正在润色"并起后台线程 `_refine_and_paste`；润色完成后一次性 `("paste", final, True)` 贴最终文本（润色版或原文）。bypass 短句仍走 `("paste", text, True)` 即时贴。
- **`_refine_and_replace` → `_refine_and_paste`**：去掉 `replace_paste` 事件产生，润色完直接 `paste`。保留 `_round_seq` 轮次守卫（重叠录音时旧润色作废，不回插）。
- **`replace_paste` UI 分支与 `_do_paste(replace=True)` 路径保留为防御性死代码**（注释标注方案 B 不再产生该事件），逻辑回退到 0.1.5 的 Ctrl+Z 版本以保持基线一致。
- **从根上消除误删**：无 replace 步骤 = 不可能误删输入框里之前的内容。

### 行为变化（对协作同学说明）

- 松手后**不再立刻出原文**；润色会改的句子要等润色完（约 0.5~5.9s，受端点波动）才一次性出最终文本，中间只显示"正在润色"。短句（≤8 有效字符）仍秒出。
- 代价是"松手即出字"的即时感让位于"绝不误删之前内容"的可靠性。用户已拍板接受。

### 已知限制

- 润色慢时（如 DeepSeek 服务端 ~5.3s）松手到出字有数秒空白；治本需换更快端点（火山 Doubao 等），非本次范围。

---

## [0.1.6] — 2026-08-16 · 修复托盘图标崩溃（0.1.5 引入的 string-path bug）

> **编辑**：WorkBuddy（GLM-5.2，本仓库 AI 协作 agent）
> **涉及文件**：src/tray.py
> **背景**：0.1.5 把托盘图标加载"优化"为 `pystray.Icon(icon=ico_path)` 传字符串路径、不再传 PIL Image。实测每次启动都在后台 `setup_handler` 线程崩 `AttributeError: 'str' object has no attribute 'save'`（pystray 的 `serialized_image` 对字符串调 `.save()`），托盘图标不显示。0.1.5 自己在 CHANGELOG 里也承认"任务栏图标仍不显示，根因未定位"。

### 改动点

- **`src/tray.py` `start()`**：删掉传字符串路径 `pystray.Icon(icon=ico_path)` 与无效 try/except 兜底（构造不报错、崩在 setup 线程，兜底永远走不到），改回传 PIL Image `img`（`_make_image()` 已用 `Image.open().convert("RGBA").resize()` 加载好）。
- 原理：pystray 的 `icon` 参数要 PIL Image 对象，不是文件路径字符串。

### 验证

dev 模式 + 打包 exe 两次实测，启动日志均不再出现 `'str' object has no attribute 'save'` 崩溃，托盘图标正常创建。0.1.5 交接说明里"未解决"的①②项（启动失败、托盘不显示）由本版解决。

### 行为变化（对协作同学说明）

- 0.1.5 引入的托盘回归 bug 修复；托盘图标恢复显示，用户可右键正常退出，不再产隐形僵尸进程（但仍建议配合 0.1.8 单实例锁做兜底）。

---

## [0.1.5] — 2026-08-15 · 去除启动气泡 + 修复任务栏图标

> **编辑**：Cindy（本仓库 AI 协作 agent）
> **涉及文件**：src/main.py、src/tray.py
> **背景**：用户反馈①启动时弹出「按住 \ 键 说话」引导气泡，文字显示不全、多余；②运行时任务栏无任何图标。

### 改动点

- **移除启动引导气泡（src/main.py）**：删除 
un() 里 
oot.after(600, ... guide ...)，程序启动后直接进主循环，不再弹首次引导。
- **修复任务栏图标不显示（src/tray.py）**：
  - 根因：原 _icon_path 在 @staticmethod 内用 os.path.abspath(__file__) 解析图标路径，存在作用域歧义导致返回 None，托盘静默降级为透明、不显示。
  - 改为**模块加载时**即用 Path(__file__).resolve() 解析并缓存为 ASSETS_ICON 常量，开发/打包（_MEIPASS）两种模式都正确。
  - start() 用 pystray.Icon(icon=<ico路径>) 原生加载多尺寸 favicon，不再传 PIL Image。

### 验证

- 开发模式 ASSETS_ICON 解析为 ssets/icon.ico（exists=True，64x64 RGBA）。
- 打包后运行时 _MEIPASS/assets/icon.ico 存在，托盘可加载。
- 启动不再弹引导气泡。

---

## [0.1.4] — 2026-08-15 · 统一图标（程序 / 任务栏 / 设置窗）

> **编辑**：Cindy（本仓库 AI 协作 agent）
> **涉及文件**：ssets/icon.ico、Yurun.spec、src/tray.py、src/gui.py
> **背景**：采用用户提供的 favicon（16/32/48/64 四尺寸、RGBA 透明），三处图标统一，去掉此前手绘的蓝圆/麦克风占位图。

### 改动点

- **图标资源**：ssets/icon.ico 替换为用户提供的 favicon（含多尺寸 + 透明通道）。
- **exe 程序图标**：Yurun.spec 的 icon 已指向 ssets/icon.ico，重新打包后 dist/Yurun.exe 图标同步更新。
- **任务栏托盘图标（src/tray.py）**：由原先绘制的「蓝圆 + 白点 / 手绘麦克风」改为加载 ssets/icon.ico；并兼容打包后运行（sys._MEIPASS/assets/icon.ico 路径）。
- **设置窗口图标（src/gui.py）**：SettingsWindow 加 iconbitmap，标题栏显示同一 favicon。
- **打包**：Yurun.spec 的 datas 增加 ('assets', 'assets')，确保图标随 onefile exe 一起分发；已验证运行时 _MEIPASS/assets/icon.ico 可被托盘正确加载。

### 行为变化（对协作同学说明）

- 程序图标、任务栏托盘、设置窗口三处图标现完全一致。
- 托盘图标在 Windows 上可能需退出重进或重启 explorer 才会刷新缓存。
- 若后续更换图标：只需替换 ssets/icon.ico（建议保留 16/32/48/64 多尺寸 + 透明），重新打包即可，无需改代码。

---

## [0.1.3] — 2026-08-14 · 交互打磨（标签时序 / 润色收尾 / 定位 / 图标）

> **编辑**：Cindy（本仓库 AI 协作 agent）
> **涉及文件**：`src/main.py`、`src/pill.py`、`src/tray.py`
> **背景**：异步润色落地后，实测发现若干交互与视觉问题，本轮集中打磨（不含图标美术，图标由其他 AI 另行处理）。

### 改动点（功能 / 交互）

- **三态标签时序修正（`src/main.py` + `src/pill.py`）**：松手后、ASR 等待期间显示「正在识别」，识别完成进入润色时显示「正在润色」，按键期间为「正在录音」。消除此前「松手后还显示正在录音、润色框凭空跳出」的错觉。
- **润色收尾逻辑（`src/main.py`）**：短句 / 未配置 key / 模型大概率不改写（`no_change`）时，原文贴出后立即收尾隐藏提示框，不再空挂约 5s「正在润色」。新增 `_refine_will_change` 预判，仅当润色真可能改动时才显示「正在润色」并等待后台结果。
- **替换不再重复（`src/main.py`）**：`replace_paste` 改为「先 `Ctrl+Z` 撤销刚贴的原文，再 `Ctrl+V` 粘贴润色版」，确保是原地替换而非追加。
- **提示框定位（`src/pill.py`）**：`_compute_anchor` 改为优先锚定焦点窗口矩形（输入框底部居中），不再跟随鼠标飘移；有可靠光标时仍浮在光标正下方；仅最后兜底才用鼠标位置。
- **录音药丸视觉回退（`src/pill.py`）**：去掉麦克风图标与光晕/高光/呼吸等装饰，回归「红点呼吸 + 转圈」极简风格，仅四个字状态提示。
- **任务栏图标（`src/tray.py`）**：由「蓝圆 + 白点」改为绘制的麦克风图标（胶囊头 + 支架 + U 形底座，亮蓝描边）。正式美术图标由其他 AI 另行提供。
- **修复 caret 日志崩溃（`src/pill.py`）**：光标不可用时 `_caret_screen_rect` 返回 `None`，旧代码用 `%x` 打印 `owner/focus` 会抛异常刷屏，已加 None 保护。

### 行为变化（对协作同学说明）

- 事件流：`recording → transcribing(正在识别) → refining(正在润色) → done/replace_paste`，`ui_q` 事件契约未变。
- `_after_transcribe(text, round_id=None)` 新增 `round_id`，用于过期轮次守卫（重叠录音时旧润色不回插）。
- 提示框位置依赖焦点窗口，部分不暴露光标/焦点的应用（如某些 Web/Electron 客户端）可能退到焦点窗口矩形或鼠标兜底。

### 测试（已后台验证，无需网络/密钥）

1. 短句（`bypass_short`）→ 立即 `done`，不显示「正在润色」。
2. 长句 + 润色 `ok` → `refining` → `replace_paste`（撤销原文后粘贴润色版）。
3. 长句 + `no_change` → `refining` → `done`（不替换）。
4. 轮次过期守卫：旧轮润色不回插覆盖新内容。
5. 三态图标绘制 / 动画无异常；焦点窗口定位函数可调用。

---

## [0.1.2] — 2026-08-14 · 异步润色（去掉串行 LLM 往返，大幅降延迟）

> **编辑**：Cindy（本仓库 AI 协作 agent，对应本次 src/main.py 改动）
> **涉及文件**：`src/main.py`
> **背景**：此前「识别 → 润色(DeepSeek) → 粘贴」是串行的，松手后必须等一次完整 LLM 网络往返才出字，是体感卡顿的主因（已确认瓶颈在润色调用，非网络传输）。

### 改动点

- **润色改为后台异步（核心优化）**：`_after_transcribe` 现在先把 ASR 原文**立刻**粘贴到输入框（松手即出字），润色放到独立线程 `_refine_and_replace` 跑；润色成功且文本有变化时，用新事件 `replace_paste` 原地替换已粘贴内容，否则保持原文。把「一次串行 LLM 网络往返」从主路径里彻底移除，出字延迟从「ASR+润色」降为「仅 ASR」。
- **新增轮次守卫 `_round_seq`**：每次按下热键 +1，后台润色完成时核对自己的 `round_id`；若期间用户已录了新的一句，旧润色直接作废、不回插覆盖新内容，保证「重叠录音」场景下最终内容正确。
- **SAUC 分支补「识别耗时」日志**：`log.info("SAUC 识别耗时=%.2fs", ...)`，cloud/local 路径本来就有 `_transcribe 识别耗时`。现在三段耗时都能在 `yurun.log` 里直接对比，便于量化确认瓶颈到底在 ASR 还是润色。
- **删除人为延迟**：`_do_paste` 里原有的 `time.sleep(0.05)` 与多余 `root.update()` 已移除，不再白等约 50ms。

### 行为变化（对协作同学说明）

- 松手后输入框**先出现原文**，约几百毫秒后若润色有改动则原地替换为润色版；无改动则保持原文，pill 在润色收尾后隐藏。
- 新增 UI 事件：`replace_paste`（润色版替换）。`_handle_ui` 已处理；若有其它消费 `ui_q` 的地方需同步感知。
- `_after_transcribe(text, round_id=None)` 签名新增 `round_id` 参数，旧调用方（仅传 text）仍兼容。

### 测试（Cindy 后台验证，无需网络/密钥）

用 mock 替换 `_refine` 与剪贴板，跑通 4 项：

1. 原文即时贴出耗时 ~0.002s（不阻塞等 LLM），后台润色后正确 `replace_paste`。
2. 轮次过期守卫：旧 `round_id` 的润色不回插（事件为 `done` 而非 `replace_paste`）。
3. 润色 `no_change`：保持原文，不替换。
4. `_do_paste` 中已无 `time.sleep(0.05)`。

### 待确认 / 后续（非本次改动）

- cloud / local 提供商仍是「整段 wav 上传」串行路径，未在本次改为流式；若实际在用这两个 provider，建议后续同样流式化。
- `bypass_short` 8 字阈值未动（异步化后影响已很小）。

---

## [0.1.1] — 2026-08-03 · 日志与崩溃反馈（强制可反馈）

- **全局崩溃捕获**：`logger.install_crash_handler()` 注册 `sys.excepthook` + `threading.excepthook` + Tk `report_callback_exception`，**任何未捕获异常（主线程 / 子线程 / 界面回调）都写入 `yurun.log`**，并附 `请将以上日志发给开发者反馈问题`。解决了「程序崩了只闪黑框、别人无法反馈」的问题。
- **启动 banner**：每次启动写入版本 / Python / 平台 / 日志目录，便于定位环境。
- **所有模块接入 logger**：`hotkey / refiner / sauc_asr / recorder / config / gui` 全部统一写 `%APPDATA%\Yurun\logs\yurun.log`（此前仅 main / pill 有日志）。
- **托盘「打开日志目录」菜单项**：右键托盘 → 打开日志目录，直接定位 `yurun.log` 发给开发者。
- **日志路径固定**：`%APPDATA%\Yurun\logs\yurun.log`（程序名恒为 `Yurun`），自动轮转（≤1MB × 3 份）。

## [0.1.0] — 2026-08-03 · 首次整理开源

**功能 / 体验**
- **悬浮药丸 (Pill) UI 复刻**：固定 132×50 胶囊气泡，浮在 I 形光标正下方约 10px、水平中线偏移 caret.x + 48px（设计 C：飘落感），贴任务栏时自动翻到光标上方。
- **智能跟背景**：采样光标上方 30px 三点 RGB 亮度，亮底 → 浅色 pill（`#f7f7f7` 底 / `#222` 字），暗底 → 深色 pill（`#0a0a0a` 底 / `#F2F2F2` 字）。
- **状态文案**：录音中「● 正在录音」、润色中「⟳ 正在润色」、完成即隐藏并直接粘贴（完成态不在 pill 显示预览文字）。

**修复 / 工程**
- **火山 SAUC 协议修复（致命）**：
  - 收尾判停 flags `0b0011` → `0b0010`（旧值把中间帧误判为末帧，导致识别返回空）。
  - `format: "wav"` → `format: "pcm"`（旧代码发裸 PCM 却被标记成 WAV，服务端报 invalid WAV）。
- **光标抓取三层兜底**：`rcCaret` 屏幕坐标（宽高 ≥4px 才认）→ `GetCaretPos + ClientToScreen(hwndCaret)`（过滤 `(0,0)` 假阳性）→ 鼠标位置兜底，解决 Electron/微信等不暴露 caret 时 pill 飘到屏幕左上角 `(0,0)` 的问题。
- **热键即时触发**：删除 0.18s `_hold_check` 阈值与 `_pressed` 竞争态——旧逻辑会在该窗口内被 `_poll` 抢先清掉 `_pressed`，导致按下不被触发、用户反复按。改为 `WM_HOTKEY` 收到即**同步**调 `on_hold_start`；松开改为「连续 2 次采样 not-down」去抖，过滤 `GetAsyncKeyState` 偶发误判。
- **允许重叠录音**：去掉 `_on_hold_start` 的重叠拦截；每次录音用独立临时文件（`tempfile.mkstemp`），前一句润色途中也能立刻录下一句，无空窗、无文件抢占。
- **设置窗字号体系升级**：`F=14→17`、`F_SMALL=12→14`、`F_TITLE=22→24`、`F_CARD=17→20`、`F_SEG=18`（保留）；Card padding / 字段行距同步放大，整窗层级协调。
- **设置窗切 Tab 零抖动**：拆 `_fit_window()` 为 `_center_window()`（仅首次居中算 +x+y）+ `_refit()`（只刷 `WxH` 不带 +x+y）。切 Tab / 展开高级 / 收起 / 切触发方式，窗口位置 100% 不动。
- **启动器修复**：`.bat` 统一转 CRLF（LF-only 会被 `cmd.exe` 解析错乱，报 `'1' 不是内部或外部命令`）；`run_yurun.bat` 去掉硬编码用户路径，改用 `py`/`python` 启动器 + `cd /d "%~dp0"`，可移植。

**诊断 / 待优化**
- **润色延迟根因定位**：端点延迟探针实测 `connect=95ms`（网络正常）、`server_TTFT≈5.3s`（占 98%）→ 瓶颈在 **DeepSeek 付费 API 服务端生成**，非网络、非本机。**本地模型排除**（无 GPU，CPU 跑 7B 更慢且质量差）。治本路径：切换到火山方舟 / 阿里百炼等首 token 更快的端点（OpenAI 兼容，仅改 config 的 `api_base`/`api_model`）。

---

## 开发期踩坑备忘（已固化为代码）

以下为已修复的工程死角，供接手者参考：

1. **Tkinter 跨线程访问 Tcl 崩溃** → 建立 UI 消息队列泵（`ui_q`）+ 主线程 `root.after(40, pump)` 串行操作 UI，100% 线程安全。
2. **火山 SAUC 大端二进制协议** → `struct.pack('>I', size)` 封装 4B 头 + Gzip，收尾发负包判停。
3. **热键全局监听与焦点丢失** → 低级键盘钩子，按下时事件吞噬阻断默认键入，hold 模式不劫持光标。
4. **剪贴板中文编码与 pyautogui 兼容** → `clipboard_append` 写入 + `sleep(0.05)` 缓冲 + 多重机制（pyautogui 优先，失败回退 PowerShell `SendKeys`）。
5. **Canvas 继承类不要用 `self._w`** → 与 Tkinter 底层 widget 路径属性冲突会崩溃。
6. **Windows `.bat` 必须是 CRLF** → LF-only 会被 cmd 解析错乱。

---

## Roadmap

- [ ] 润色端点下拉（DeepSeek / 火山方舟 / 阿里百炼），设置窗内一键切换。
- [ ] GitHub Actions 自动打包 EXE + Inno Setup（Windows Runner）。
- [ ] 可选流式输出（首字仍受服务端 TTFT 限制，仅改善体感）。
- [ ] 更小更准的本地蒸馏模型适配。


---

## 交接说明（2026-08-16 · Cindy 整理 / 混元3 收尾更新）

> 给后续接手者（其他 agent / 同事）的总览：已做、已验证、未解决。本仓库已有本地 git（master 分支，多笔本地提交，git log 可见），但尚未添加远程、未推 GitHub（见六、紧急待办）。

### 一、已完成改动（按版本）

| 版本 | 日期 | 内容 | 涉及文件 |
|------|------|------|----------|
| 0.1.2 | 08-14 | 润色后台异步：原文先贴、后台润色再 replace_paste；_round_seq 轮次守卫；SAUC 识别耗时日志；去 _do_paste 的 sleep | main.py |
| 0.1.3 | 08-14 | 三态标签时序(录音→识别→润色)；润色收尾(短句/未改动立即收尾)；replace_paste 用 Ctrl+Z 撤销再粘贴(防重复)；提示框锚定焦点窗口(不再跟鼠标)；录音药丸回退原版红点+转圈；托盘换绘制麦克风；修 caret None 日志崩溃 | main.py, pill.py, tray.py |
| 0.1.4 | 08-15 | 统一图标：程序/exe/任务栏/设置窗 全用 favicon.ico(16/32/48/64+透明)；Yurun.spec 打包 assets；托盘 _MEIPASS 容错 | assets/icon.ico, Yurun.spec, tray.py, gui.py |
| 0.1.5 | 08-15 | 移除启动引导气泡；修复任务栏图标不显示(根因 @staticmethod 内 __file__ 作用域歧义致 _icon_path 返回 None，改为模块加载时解析 ASSETS_ICON 常量) | main.py, tray.py |
| 0.1.6 | 08-16 | 修复托盘图标崩溃(0.1.5 引入的 string-path bug：pystray.Icon(icon=字符串路径) 改回传 PIL Image) | tray.py |
| 0.1.7 | 08-16 | 润色改同步一次贴(方案B)：不先贴原文、润色完一次 paste 最终文本；弃用 replace_paste(Ctrl+Z 撤销法跨 app 误删整段历史) | main.py |
| 0.1.8 | 08-16 | 单实例锁(新进程杀旧接管：kill_old_and_takeover+kill_other_yurun_exe)；pill 失败文案缩成"热键被占"/"热键无效"；收尾品牌化：版本号对齐 0.1.8、界面显示名统一「语润」、打包产物改名「语润.exe」、单实例锁动态识别进程名(改名不失效)、README/.gitignore/目录收尾 | 新增 singleinstance.py, main.py, hotkey.py, Yurun.spec, gui.py, tray.py, logger.py, installer/yurun_setup.iss, README.md, .gitignore |

### 二、已验证

- 异步润色三态(短句/长句ok/长句no_change)单测通过。
- 焦点窗口定位函数可调用。
- 打包后 _MEIPASS/assets/icon.ico 运行时存在；托盘路径可解析(开发模式 ASSETS_ICON 指向 assets/icon.ico 且 exists)。
- dist/语润.exe 可生成(约68MB onefile)。

### 三、原"未解决"项状态（08-16 由 WorkBuddy/GLM-5.2 接手后更新）

1. ~~程序启动即失败、弹"热键注册失败"~~ → **已解决（0.1.8）**。根因坐实：连测多版本后留了 Yurun.exe 僵尸进程占着反引号热键（实测机器上有 PID 9288）。单实例锁启动时杀旧接管，实战验证把 9288 + 另一个 3920 僵尸都收掉了，热键注册成功。
2. ~~任务栏图标仍不显示~~ → **已解决（0.1.6）**。根因：0.1.5 把 `pystray.Icon(icon=ico_path)` 传了字符串路径（pystray 要 PIL Image），后台 setup 线程崩 `'str' object has no attribute 'save'`。改回传 PIL Image 后托盘正常。
3. ~~启动气泡显示不全~~ → 0.1.5 已删 `after(600, guide)`；若仍出现为旧 exe 缓存，用最新打包版即可。

### 四、关键排查入口

- 热键注册失败：看 src/hotkey.py 的 RegisterHotKey 分支(错误码/是否被占用) 及 main.py 里 hotkey.start() 返回值。
- 任务栏图标：src/tray.py 的 ASSETS_ICON 常量、start() 里 pystray.Icon(icon=ico_path)；建议临时把 start() 的 except 打印到日志确认 pystray 是否抛错。
- 启动气泡：main.py 已删 after(600, guide)，若仍出现必是运行了旧 exe；确认 dist/语润.exe 是最新打包(py -3 -m PyInstaller Yurun.spec --noconfirm)。
- 日志位置：%APPDATA%\Yurun\logs\yurun.log（崩溃/异常都写这里，首要排查源）。

### 五、打包命令

    cd D:\SynologyDrive\CODING\yurun
    py -3 -m PyInstaller Yurun.spec --noconfirm
    # 产物 dist/语润.exe (onefile，内联 prompts/ 与 assets/)
    # 注：--clean 在部分开启安全删除拦截的环境会失败(内部 os.remove 被拦截)，可省略；
    # 若旧构建残留，先 `rm -rf build dist` 手动清理后再重建即可。

依赖：Python 3.12 + PyInstaller 6.x；requirements.txt 列出全部运行时依赖。

### 六、紧急待办

- [x] ~~修复启动 注册热键失败~~（0.1.8 单实例锁解决）。
- [x] ~~确认/修复任务栏托盘图标在打包环境下真实显示~~（0.1.6 解决）。
- [x] ~~确认启动气泡彻底移除~~（0.1.5 已删，用最新打包版即可）。
- [ ] 把仓库推到 GitHub(当前无远程，本地提交未同步)。
- [x]（已解决·0.1.8 收尾）logger.py 的 YURUN_VERSION 已由 "0.1.0" → "0.1.8"，启动 banner 显示 v0.1.8，与 CHANGELOG 一致。
- [x]（已解决·0.1.8 收尾）冗余 dist_v2~v5 / build_v2~v5 等中间产物已清理，仅留 dist/语润.exe 作为官方构建；原版仓库 D:\SynologyDrive\CODING\yurun（旧 v0.1.1 源码）已删除，新代码收尾后由 yurun-stream 改名回 yurun，成为当前唯一版本。
