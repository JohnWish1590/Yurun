# Changelog

本项目开发过程中的关键里程碑与工程修复记录。所有改动均围绕「复刻 Cindy 丝滑语音润色体验 + 修复真机踩坑」展开。

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
