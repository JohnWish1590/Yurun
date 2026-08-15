# Changelog

本项目开发过程中的关键里程碑与工程修复记录。所有改动均围绕「复刻 Cindy 丝滑语音润色体验 + 修复真机踩坑」展开。

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

## 交接说明（2026-08-16 · 由 Cindy AI 整理）

> 给后续接手者（其他 agent / 同事）的总览：已做、已验证、未解决。本仓库无 .git（仅 .gitignore），改动以本地提交存在（git log 可见），尚未推远程。

### 一、已完成改动（按版本）

| 版本 | 日期 | 内容 | 涉及文件 |
|------|------|------|----------|
| 0.1.2 | 08-14 | 润色后台异步：原文先贴、后台润色再 replace_paste；_round_seq 轮次守卫；SAUC 识别耗时日志；去 _do_paste 的 sleep | main.py |
| 0.1.3 | 08-14 | 三态标签时序(录音→识别→润色)；润色收尾(短句/未改动立即收尾)；replace_paste 用 Ctrl+Z 撤销再粘贴(防重复)；提示框锚定焦点窗口(不再跟鼠标)；录音药丸回退原版红点+转圈；托盘换绘制麦克风；修 caret None 日志崩溃 | main.py, pill.py, tray.py |
| 0.1.4 | 08-15 | 统一图标：程序/exe/任务栏/设置窗 全用 favicon.ico(16/32/48/64+透明)；Yurun.spec 打包 assets；托盘 _MEIPASS 容错 | assets/icon.ico, Yurun.spec, tray.py, gui.py |
| 0.1.5 | 08-15 | 移除启动引导气泡；修复任务栏图标不显示(根因 @staticmethod 内 __file__ 作用域歧义致 _icon_path 返回 None，改为模块加载时解析 ASSETS_ICON 常量) | main.py, tray.py |

### 二、已验证

- 异步润色三态(短句/长句ok/长句no_change)单测通过。
- 焦点窗口定位函数可调用。
- 打包后 _MEIPASS/assets/icon.ico 运行时存在；托盘路径可解析(开发模式 ASSETS_ICON 指向 assets/icon.ico 且 exists)。
- dist/Yurun.exe 可生成(约68MB onefile)。

### 三、当前未解决 / 用户最新反馈(08-16，需接手)

1. 程序启动即失败：用户报告一允许就跳 注册热键失败，且启动气泡还是显示不全(应已移除，可能旧 exe 缓存或打包未生效)。
2. 任务栏图标仍不显示：尽管 0.1.5 修了路径解析，实测感觉没运行成功，托盘图标依旧缺失。怀疑 pystray 打包环境仍有静默异常(原 except 吞错)或 explorer 缓存。
3. 以上两点均未在代码中定位确定根因，只按现象改过；需接手者实际跑 dist/Yurun.exe 看 %APPDATA%\Yurun\logs\yurun.log 真实报错。

### 四、关键排查入口

- 热键注册失败：看 src/hotkey.py 的 RegisterHotKey 分支(错误码/是否被占用) 及 main.py 里 hotkey.start() 返回值。
- 任务栏图标：src/tray.py 的 ASSETS_ICON 常量、start() 里 pystray.Icon(icon=ico_path)；建议临时把 start() 的 except 打印到日志确认 pystray 是否抛错。
- 启动气泡：main.py 已删 after(600, guide)，若仍出现必是运行了旧 exe；确认 dist/Yurun.exe 是最新打包(py -3 -m PyInstaller Yurun.spec --clean --noconfirm)。
- 日志位置：%APPDATA%\Yurun\logs\yurun.log（崩溃/异常都写这里，首要排查源）。

### 五、打包命令

    cd D:\SynologyDrive\CODING\yurun-stream
    py -3 -m PyInstaller Yurun.spec --clean --noconfirm
    # 产物 dist/Yurun.exe (onefile，内联 prompts/ 与 assets/)

依赖：Python 3.12 + PyInstaller 6.x；requirements.txt 列出全部运行时依赖。

### 六、紧急待办

- [ ] 修复启动 注册热键失败(最高优先级，程序目前打不开)。
- [ ] 确认/修复任务栏托盘图标在打包环境下真实显示。
- [ ] 确认启动气泡彻底移除(排除旧 exe 缓存)。
- [ ] 把仓库推到 GitHub(当前无远程，本地提交未同步)。
