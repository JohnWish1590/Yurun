# 语润 Yurun · 全局语音润色输入悬浮工具

一个 **Windows 全局** 的语音转写 + AI 润色输入工具：按住热键说话，松手后自动转写、用大模型润色，再把润色后的文本**直接填进当前光标所在的输入框**（WorkBuddy、微信、浏览器、编辑器等任意软件）。

> **致敬声明 (Tribute)**
> 本项目高度致敬并源自 **心动网络 (makecindy)** 的开源 AI 语音润色算法框架 [Cindy](https://github.com/makecindy)，以及 **dash (dashhuang)** 极简、高效、即插即用的语音交互设计思想 [dash](https://github.com/dashhuang)。
> 项目的润色提示词复刻自 Cindy v17，语音识别默认走与 Cindy 同款的火山 SAUC 流式协议。感谢这些优秀作品提供的深厚技术积淀与交互灵感。

> **当前版本**：v0.1.8 — 单实例锁、托盘修复、Plan B 润色（不再误吞前文）、短句直出。

---

## 一、它解决什么问题

大多数 AI Agent（例如各类桌面客户端的“麦克风”功能）属于**接收端 LLM**，在“点击麦克风 → 录音 → 发送”的链条里没有“发送前”的钩子。你只能在发送后才看到结果，无法在输入框里直接编辑润色后的文字。

**语润** 是一个独立的**外部前置全局工具**：

- 用全局热键触发录音（默认反引号 `` ` ``，Tab 键上方；支持 **按住说话** / **单击切换**）
- 录音结束自动高精度转写
- 把原始转写发给大模型做语义去噪、口语润色、中英混杂校正
- 最终**写回剪贴板并模拟 Ctrl+V**，直接填充并替换当前输入框里的光标位置

---

## 二、核心特性

- **悬浮药丸 (Pill)**：录音/润色时，在 I 形光标（或鼠标）正下方浮现一个 132×50 的极简胶囊气泡，显示「● 正在录音」/「⟳ 正在润色」，不弹大窗、不打断工作流。
- **智能跟背景**：采样光标上方像素亮度，亮底界面用浅色 pill，暗底界面用深色 pill。
- **双轨语音识别**：
  - **云端火山 SAUC（默认，Cindy 同款）**：WebSocket 二进制流式，自带 Gzip 实时压缩 + 大端 4 字节头封装，中英混杂与多方言识别强。
  - **本地离线 Whisper**：基于 `Faster-Whisper`，`small`（推荐，约 460MB）/ `base`（极速，约 140MB），首次使用自动下载。
  - **云端 OpenAI 兼容**：任意 `/v1/audio/transcriptions` 端点。
- **大模型润色**：OpenAI 兼容接口，默认 DeepSeek-Chat，复刻 Cindy v17 提示词，带**发散护栏**（输出过长/偏离原文则回退原文）。
- **苹果风设置界面**：圆角 Segmented 选项卡 + 白色圆角卡片，一屏展示，无滚动条。
- **热键即时反馈**：按下瞬间即出「正在录音」，不吞事件、不抖动；前一句润色途中也能立刻录下一句（重叠录音）。
- **系统托盘常驻**：开机自启可配，退出/设置都从托盘走。
- **单实例锁（0.1.8）**：启动时自动清理同名 / 同目录的旧进程（含无托盘图标的僵尸进程），永远只跑一个 Yurun，避免旧进程占用全局热键导致注册失败。
- **润色不再误吞前文（0.1.7 / Plan B）**：长句润色完成后，只把最终文本一次性粘贴到光标处，不再触发撤销 / 选区替换，彻底解决「润色后把前面已输入内容覆盖掉」的问题。
- **短句直出（0.1.8）**：转写结果 ≤ 8 个有效中文字（含标点）时跳过润色，直接填入，省去等待。
- **热键冲突提示（0.1.8）**：若全局热键已被其它程序占用，悬浮 pill 直接提示「热键被占」并在日志记录具体错误码，不再弹大窗。

---

## 三、架构

纯 Python 3.12，采用 **主线程 UI 渲染 + 子线程后台逻辑（Queue 消息泵）** 的经典高内聚、低耦合架构，100% 线程安全。

```
                      [ 全局键盘钩子 (hotkey.py) ]
                                    │  触发开始 / 结束录音
                                    ▼
   [ 托盘常驻 (tray.py) ] ─► [ 应用主循环 (main.py) ] ─► [ 浮空胶囊 (pill.py) ]
                                    │
                  ┌─────────────────┴─────────────────┐
                  ▼                                   ▼
     [ 云端火山 ASR (sauc_asr.py) ]        [ 本地离线 Whisper (transcriber.py) ]
     [ 云端 OpenAI 兼容 (cloud_asr.py) ]          [ 麦克风采集 (recorder.py) ]
                  └─────────────────┬─────────────────┘
                                    ▼
                       [ 语义润色 (refiner.py) + prompts/ ]
                       - Cindy v17 Prompt，OpenAI 兼容端点
                       - 发散护栏、JSON 容错解析
                                    ▼
                       [ 光标焦点写入 (main.py) ]
                       - 写入系统剪贴板 + 模拟 Ctrl+V / PowerShell SendKeys 兜底
```

| 模块 | 职责 |
|------|------|
| `main.py` | 主循环、UI 消息泵、录音→识别→润色→粘贴全流程编排 |
| `hotkey.py` | 低级键盘钩子，按住说话 / 单击切换，即时触发与去抖 |
| `pill.py` | 无边框半透明悬浮胶囊，跟随 I 形光标 / 鼠标，智能跟背景 |
| `tray.py` | 系统托盘图标与菜单 |
| `gui.py` | macOS/iOS 风格的配置窗口（Segmented、卡片） |
| `sauc_asr.py` | 火山 SAUC WebSocket 二进制流式协议（Gzip + 大端 4B 头） |
| `cloud_asr.py` | OpenAI 兼容 `/v1/audio/transcriptions` |
| `transcriber.py` | 本地 Faster-Whisper 推理 |
| `recorder.py` | 麦克风采集（sounddevice） |
| `refiner.py` | 大模型润色（Cindy v17 提示词 + 发散护栏） |
| `config.py` | 配置读写（`%APPDATA%\Yurun\config.json`） |
| `logger.py` | 日志（`%APPDATA%\Yurun\logs\yurun.log`） |
| `prompts/` | `refine-dictation.zh.txt` / `.en.txt`（Cindy v17 润色提示词） |

---

## 四、安装与运行

### 环境要求

- **Windows 10/11**（依赖 Win32 API 抓取光标、全局热键）
- **Python 3.10+**（建议 3.12）

### 1. 安装依赖

```powershell
pip install -r requirements.txt
```

### 2. 运行（开发模式）

```powershell
# 方式 A：直接运行
python src/main.py

# 方式 B：双击启动器（自动定位 python）
run_yurun.bat
```

首次运行会在 `%APPDATA%\Yurun\` 生成 `config.json`，并在系统托盘出现语润图标。

---

## 五、配置

右键托盘图标 → **设置**，只需填两项 Key（其余已预置）：

| 配置项 | 说明 | 获取 |
|--------|------|------|
| **火山 SAUC Key**（语音识别，默认） | 火山引擎「语音技术」产品的 App Key | [火山语音技术控制台](https://console.volcengine.com/speech) |
| **DeepSeek API Key**（润色，默认） | DeepSeek 开放平台 Key（`sk-` 开头） | [platform.deepseek.com](https://platform.deepseek.com) |

> ⚠️ **两个 Key 不是一回事**：
> - 做**语音识别**的是「火山语音技术」的 SAUC Key。
> - 做**润色**的是大模型 Key（DeepSeek / 火山方舟 ARK / 阿里百炼 等 OpenAI 兼容端点）。
> - 想给润色换更快的端点（如火山方舟、阿里百炼），在设置里改 `api_base` / `api_model` 即可，代码无需改动。

其他可选项：本地模型（`small`/`base`）、语言（自动/中文/英语）、热键、触发方式（按住/切换）、代理、模型下载镜像等。

---

## 六、使用

1. 把光标放到任意软件的输入框（如微信聊天框、浏览器搜索栏）。
2. **按住**反引号键 `` ` ``（Tab 上方那个键）说话，松手即开始转写 + 润色。
3. 稍候，润色后的文字自动填入当前输入框。
4. 也可在设置里改成「单击切换」模式。

---

## 七、打包为独立 EXE（可选）

源码运行即可，若想分发给无 Python 环境的用户：

### 1. PyInstaller 单文件

```powershell
pyinstaller --clean Yurun.spec
# 产物：dist/语润.exe
```

### 2. Inno Setup 安装包（含中文、桌面快捷方式、干净卸载）

```powershell
& "C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer/yurun_setup.iss
# 产物：dist/语润-Setup-0.1.8.exe
```

> 构建顺序：先 PyInstaller 生成 `dist/语润.exe`，再跑 Inno Setup（脚本会读取 `dist/语润.exe`）。

---

## 八、已知限制

- **润色延迟取决于所选大模型端点**。实测 DeepSeek 付费 API 首 token 约 5.3s（服务端生成耗时，非网络/非本机）。若想更快，切换到火山方舟 / 阿里百炼等端点通常可压到 1–1.5s。
- **本地模型润色不推荐**：本项目无 GPU 依赖，纯 CPU 跑 7B 模型反而更慢且质量不如云端大模型。
- 仅支持 Windows（依赖 Win32 光标抓取与全局热键）。

---

## 九、日志与问题反馈

程序会把**完整运行日志**写到：

```
%APPDATA%\Yurun\logs\yurun.log
```

- 日志自动轮转（单文件 ≤ 1MB，保留最近 3 份）。
- **任何未捕获异常**（主线程 / 子线程 / 界面回调）都会**自动写入该文件**，并附上 `请将以上日志发给开发者反馈问题` 提示——你无需截图黑框。
- 启动时会写入版本 / Python / 平台信息（banner），便于定位环境。
- **遇到 bug 时**：右键托盘图标 → **「打开日志目录」**，把 `yurun.log` 发给我即可。

> 日志目录即 `%APPDATA%\(程序名)`，程序名固定为 `Yurun`，故路径恒为
> `C:\Users\<你>\AppData\Roaming\Yurun\logs\yurun.log`。

---

## 十、许可证

[MIT](LICENSE) © 2026 JohnWish1590 — 基于 Cindy / dash 的开源成果派生，保留其署名与致谢。

---

📦 源码 / Issues / Releases：https://github.com/JohnWish1590/Yurun

🐛 问题反馈：GitHub Issues

📝 变更历史：CHANGELOG.md

🚀 发布说明规范：RELEASE.md

⚡ 快速开始（部署细节）：docs/QUICKSTART.md

🔒 隐私政策：PRIVACY.md

🏪 上架清单（未来）：STORE_GUIDE.md

Socials: @下一站澳门. DM for inquiries.
