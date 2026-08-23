# 语润（Yurun）语音识别引擎技术说明

> 适用版本：v1.0.1 ｜ 整理日期：2026-08-23
> 本文档描述语润**当前实际在用的识别（ASR）实现**，全部基于仓库源码，面向想了解技术细节的人。

---

## 1. 一句话总览

语润默认使用**火山引擎 SAUC 流式语音识别**（`wss://openspeech.bytedance.com/api/v3/sauc/bigmodel`），通过**自定义二进制 WebSocket 协议**做**双向流式**传输：录音线程边采集边把 16kHz PCM 音频块发给服务端，服务端边听边返回中间识别结果；用户松手后，语润发「最后一包」并等待服务端回**最终结果（FLAG_LAST）**，拿到完整文本后再交给润色模块。

识别全流程：
`按住热键 → 录音线程采集 PCM → 实时分包发往 SAUC → 服务端流式返回 → 松手发尾包 → 收最终结果 → 短句直通 / 长句润色 → SendInput 写入目标应用`

---

## 2. 架构与数据流向

```
┌────────────┐    PCM 块(16k mono int16)     ┌──────────────────────────┐
│  recorder  │ ───────────────────────────▶ │   SAUC WebSocket 服务端   │
│ (sounddevice)│  on_level(音量) ↑ 中间结果↓  │  (火山引擎 bigmodel 端点)  │
└────────────┘                               └──────────────────────────┘
      ▲                                                │ 最终结果(text)
      │  stop_event(松手)                              ▼
┌────────────┐                               ┌──────────────────────────┐
│ 热键/UI    │                               │  main._after_transcribe   │
│ (pynput)   │ ──── 启动/停止录音 ──────────▶│  → 短句bypass / 长句润色   │
└────────────┘                               └──────────────────────────┘
                                                          │ text
                                                          ▼
                                                 ┌──────────────────┐
                                                 │  typer.SendInput  │
                                                 │ (零剪贴板粘贴)    │
                                                 └──────────────────┘
```

涉及的核心源码文件：
- `src/sauc_asr.py` —— SAUC 流式客户端（协议编解码 + WebSocket 收发）
- `src/recorder.py` —— 录音采集（sounddevice，流式生成 PCM 块）
- `src/config.py` —— 识别相关配置项与默认值
- `src/main.py` —— 编排：`_on_hold_start` / `_record_job_sauc` / `_after_transcribe`
- `src/dictionary.py` —— 渐进式词库，向 SAUC 直传热词

---

## 3. 识别引擎：火山引擎 SAUC 双向流式

### 3.1 为什么是 SAUC

- **流式而非整段**：按住说话期间就能把音频源源不断发过去，松手即出结果，延迟低。
- **Cindy 同款技术路线**：语润参考了开源语音听写工具 Cindy 的架构，选用火山 SAUC（seed-asr 系列）作为识别后端。
- **自带标点 / 逆文本归一化（ITN）**：返回文本已带标点、数字已规整（例如「12345。」），不需要自己后处理。
- 配置里也能切换到「云端 OpenAI 兼容 /v1/audio/transcriptions」或「本地 Whisper」，但**默认且主推是 SAUC**。

### 3.2 通信协议（自定义二进制 WebSocket）

SAUC 不用纯 JSON 文本帧，而是「4 字节头 + 4 字节 payload 长度 + payload」的二进制帧，payload 为 gzip + JSON。

帧头构造（`sauc_asr.py:_make_header`）：

```python
b0 = (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE   # 0x10: 版本1 + 头长1(4字节)
b1 = (msg_type << 4) | flags                          # 消息类型 + 标志位
b2 = (serialization << 4) | compression               # 序列化方式 + 压缩方式
b3 = 0
```

协议常量：

| 常量 | 值 | 含义 |
|---|---|---|
| `PROTOCOL_VERSION` | `0b0001` | 协议版本 1 |
| `MSG_FULL_CLIENT_REQUEST` | `0b0001` | 建连首帧（携带音频参数 JSON） |
| `MSG_AUDIO_ONLY_REQUEST` | `0b0010` | 纯音频数据帧 |
| `MSG_FULL_SERVER_RESPONSE` | `0b1001` | 服务端响应 |
| `MSG_ERROR` | `0b1111` | 错误帧 |
| `FLAG_LAST` | `0b0010` | 最后一包（负包/尾包）标志 |
| `SER_JSON` / `SER_NONE` | `0b0001` / `0b0000` | JSON 序列化 / 无 |
| `COMPRESS_GZIP` | `0b0001` | gzip 压缩 |
| `SAMPLE_RATE` | `16000` | 采样率 |
| `CHUNK_MS` | `200` | 建议每包 200ms（整段模式用；流式模式实际按录音块 100ms 发） |

鉴权在建连时通过 HTTP 头传入：
```
X-Api-Key:          <火山语音技术 App Key>
X-Api-Resource-Id:  volc.seedasr.sauc.duration   # 2.0 小时版；1.0 版用 volc.bigasr.sauc.duration
X-Api-Request-Id:   <uuid4>
X-Api-Connect-Id:   <uuid4>
```

建连首帧 JSON（`_build_full_request`）关键字段：
```json
{
  "user":   {"uid": "yurun-pc", "platform": "Windows"},
  "audio":  {"format": "pcm", "rate": 16000, "bits": 16, "channel": 1},
  "request":{
    "model_name":   "bigmodel",
    "enable_itn":   true,   // 逆文本归一化：数字/量词规整
    "enable_ddc":   true,   // 顺滑/顺口处理
    "enable_punc":  true,   // 自动加标点
    "show_utterances": true // 返回逐句中间结果
  }
}
```
热词通过 `request.context = {"hotwords":[{"word":"..."}]}` 透传（≤200 tokens，见第 6 节）。

### 3.3 一次识别的完整时序

1. **建连**：`WebSocketApp` 连到 `wss://.../sauc/bigmodel`，带上述鉴权头。
2. **发首帧**（`on_open` 回调里）：发送 `MSG_FULL_CLIENT_REQUEST`（gzip JSON 音频参数）。
3. **边录边发**：录音线程通过 `record_chunks` 持续产出 int16 PCM 块，`ws.send(音频包, opcode=BINARY)` 实时发往服务端。
4. **边收结果**：`on_message` 回调在 WebSocket 线程里持续解析服务端帧，把 `result.text` 暂存（**只攒着不回显**，避免文字跳动）。
5. **松手**：录音线程结束 → 发送「最后一包」（`is_last=True` 的负包）。
6. **等最终结果**：`state["done"].wait(timeout)` 阻塞，直到收到 `FLAG_LAST` 标志帧 → 取出完整文本返回。
7. **收尾**：`ws.close(timeout=0.2)`（**必须带 timeout，否则偶发卡 2.7s**），文本交给 `_after_transcribe`。

> 模块注释实测：5 秒音频约 300–400ms 返回；松手后最终结果在真实场景约 **0.6–1s** 到达（比非流式 `bigmodel_nostream` 更快）。

### 3.4 中间结果 vs 最终结果（FLAG_LAST）

- SAUC 在录音期间会不断返回**中间结果**（句子还没说完时的识别），靠 `show_utterances:true` 开启。
- 语润**刻意不把中间结果显示/贴出**，原因（代码注释原话）：中间结果缺最后一段音频的识别，会漏掉松手前最后 1 秒的话，且边收边改会造成文字跳变。
- 因此语润**只信任 `FLAG_LAST` 的最终结果**：`flags & 0b0010` 为真才判定识别完成。这也是判断「收尾」的硬规则，与中间/ACK 帧（`0b0001`）区分开。

### 3.5 关键识别开关（来自 SAUC）

| 开关 | 作用 | 对结果的影响 |
|---|---|---|
| `enable_punc` | 自动标点 | 返回文本自带逗号/句号 |
| `enable_itn` | 逆文本归一化 | 「二零二四」→「2024」，数字、日期规整 |
| `enable_ddc` | 顺滑处理 | 顺口、去口吃式冗余 |
| `show_utterances` | 逐句中间结果 | 流式体验所需，但语润仅内部暂存 |

---

## 4. 录音采集（`src/recorder.py`）

- 用 `sounddevice.InputStream`，**16kHz / 单声道 / float32** 实时读取。
- 流式模式走 `record_chunks(stop_event, on_level, chunk_ms=100, max_seconds=90)`：
  - 一个生成器（generator），每 ~100ms `yield` 一块 `int16` PCM（`data * 32767`）。
  - 调用方（SAUC 线程）边收边 `ws.send`，天然实现「边录边发」。
  - `on_level` 回调把当前音量（RMS 归一化到 0~1）抛给 UI，驱动「正在录音」气泡的音量跳动。
  - `max_seconds=90`：按住超过 90 秒自动停止（配合气泡「还剩 10 秒」提示）。
- 重依赖（sounddevice / soundfile / numpy）**延迟 import**，只有真正录音时才加载，避免启动就加载 PortAudio。

---

## 5. 实时性工程要点

- **双向流式 + WebSocketApp 回调**：录音线程只负责 `send`，所有 `recv` 在 `run_forever` 内部线程的 `on_message` 里处理。这规避了早期「同步 WebSocket 跨线程 send/recv 竞争」导致 UI 卡死的坑。
- **连接建立等待**：`state["opened"].wait(timeout=15)`，15s 连不上直接报错，不无限挂起。
- **识别超时**：`sauc_transcribe_stream` 的 `timeout=90`，超时标记 `state["error"]="识别超时"`。
- **流式首字即上屏的「润色」层**：识别结果出来后，若句子较长且开启了润色，润色也走流式（`refine_stream`），首字即贴入目标应用；识别文本本身（短句 bypass 或润色前）是识别引擎直接给的纯文本。

---

## 6. 热词 / 渐进式词库（`src/dictionary.py`）

语润有「渐进式词库」功能（Ctrl+反引号弹「错误纠正」框录入正确写法），其中**第一通道**就是直接作用到 SAUC 识别：

- 用户词库（`%APPDATA%\Yurun\user_dictionary.json`）里的词条，经 `dictionary.to_hotwords()` 转成 `[{"word": "..."}, ...]`。
- 在 `main._record_job_sauc` 里作为 `hotwords=to_hotwords()` 传给 `sauc_transcribe_stream`。
- `sauc_asr._build_hotwords_context` 把它塞进请求的 `request.context.hotwords`（≤200 tokens）。
- 效果：提高专业术语、人名、品牌名的识别准确率。
- 另外两通道（本地别名替换、LLM 词典参考）不直接影响识别引擎，这里不展开。

---

## 7. 相关配置项（`src/config.py`）

| 配置键 | 默认值 | 说明 |
|---|---|---|
| `asr_provider` | `"sauc"` | 识别引擎：`sauc` / `cloud` / `local` |
| `asr_sauc_key` | `""` | 火山语音技术 App Key（SAUC 用） |
| `asr_sauc_resource_id` | `volc.seedasr.sauc.duration` | SAUC 资源 ID（2.0 小时版） |
| `asr_sauc_endpoint` | `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel` | SAUC WebSocket 地址 |
| `language` | `"auto"` | `auto` / `zh` / `en` |
| `proxy` | `""` | 可选 HTTP 代理 |
| `asr_base_url` | `https://ark.cn-beijing.volces.com/api/v3` | 火山方舟（cloud 模式用） |
| `asr_model` | `doubao-seed-asr-250429` | 火山方舟 ASR 模型（cloud 模式用） |

配置存于 `%APPDATA%\Yurun\config.json`，改完即时生效。

---

## 8. 三种识别模式（供了解，默认 SAUC）

| 模式 `asr_provider` | 后端 | 特点 |
|---|---|---|
| `sauc`（默认） | 火山 SAUC 流式 `bigmodel` | 双向流式、延迟最低、自带标点/ITN |
| `cloud` | OpenAI 兼容 `/v1/audio/transcriptions`（火山方舟等） | 整段音频 HTTP 上传，非流式 |
| `local` | 本地 Whisper（`whisper_model: small/base`） | 离线、隐私好但需本机算力 |

源码中 `_record_job_sauc`（流式）+ `_record_job`/`_transcribe`（整段）两条路径分别对应流式与非流式后端。

---

## 9. 工程坑与已解决项（识别相关）

1. **websocket-client 0.57 + Python 3.12 的 `isAlive` 兼容**：`WebSocketApp.run_forever` 内部调 `threading.Thread.isAlive()`，Python 3.12 已移除该方法。在 `sauc_asr.py` 顶部 monkey-patch：
   ```python
   if not hasattr(threading.Thread, "isAlive"):
       threading.Thread.isAlive = threading.Thread.is_alive
   ```
2. **`ws.close()` 默认 3s 握手超时**：会导致松手后偶发卡 2.7s，必须 `ws.close(timeout=0.2)`。
3. **中间结果不能提前贴**：只用 `FLAG_LAST` 最终结果，避免漏最后 1 秒、避免文字跳变。
4. **跨线程竞争**：改为 WebSocketApp 回调模式（收在 on_message 线程、发在录音线程），不再用同步 `create_connection` 跨线程 send/recv。

---

## 10. 给朋友看的对比：语润 vs「言出法随」类输入法

| 维度 | 语润（独立工具） | 网页/输入法内嵌的「言出法随」 |
|---|---|---|
| 识别 | 火山 SAUC 双向流式，松手出最终结果 | 多为流式 ASR，边说边出中间字幕 |
| 文字落点 | `SendInput` 把文本当键盘事件打进任意第三方应用光标处 | 自己拥有输入框 DOM，直接写进去 |
| 中间结果 | 内部暂存、不回显（防跳变/防漏字） | 直接显示在输入框里（天然 inline） |
| 实时感 | 松手后 ~0.6–1s 出完整文本 | 边说边出（但需要「自己拥有输入框」为前提） |
| 润色 | 独立 LLM 模块（可关思考、可清洗/完整两档） | 通常 LLM 润色后替换 |

**结论**：语润是**独立 Windows 工具**，要把文字塞进 Notion/Word/微信等第三方应用，因此走 `SendInput`，不可能像网页那样做 DOM 级 inline preedit；但 SAUC 流式 + 松手即出最终结果的延迟已经很低，配合润色流式首字上屏，体验接近「言出法随」。
