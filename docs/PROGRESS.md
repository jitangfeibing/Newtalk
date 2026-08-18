# Newtalk 开发进度

本文档记录 Newtalk 实际已经落地并验证过的能力。规划中的功能只有在代码完成并通过验证后，才会进入“已完成”清单。

## 更新规则

每完成一个 Part，至少记录以下内容：

- 本阶段目标与完成状态。
- 新增的用户可见能力。
- 主要代码入口和调用链。
- 自动测试与手工验证结果。
- 明确尚未实现的边界。
- 下一阶段准备处理的内容。

## 当前状态

| 项目 | 当前值 |
| --- | --- |
| 当前阶段 | P5-B 豆包双向流式 ASR |
| 阶段状态 | 已完成并通过真实浏览器语音验收 |
| 开发分支 | `codex/p5-real-asr` |
| 项目版本 | `0.5.1` |
| Python | 3.11.5 |
| 环境 | 项目内标准 `.venv`，由 Anaconda Base Python 创建 |
| 后端 | FastAPI + Uvicorn |
| 前端 | 原生 HTML + CSS + JavaScript |
| 自动测试 | 76 项通过，2 项 live 默认跳过；真实 ASR 不进入普通 CI |
| CI | P1-P5-A 已合并；P5-B 尚未提交 PR |
| 最后更新 | 2026-08-18 |

## P1：基础运行骨架

### 目标

建立一个不依赖旧小智运行时的独立 Newtalk 服务，验证浏览器、HTTP 和 WebSocket 可以形成最小闭环。

### 已完成

- 建立 `src/` Python 包结构和 `newtalk` 命令行入口。
- 建立 FastAPI 应用工厂 `create_app()`。
- 实现 `GET /health` 健康检查。
- 实现 `GET /` 以及 Web 静态资源托管。
- 实现 `WS /ws` WebSocket 端点。
- 服务端连接后发送 `hello`，包含协议版本和唯一 `session_id`。
- 浏览器可以发送 `ping`，服务端返回关联相同 `event_id` 的 `pong`。
- 浏览器通过 `close` 请求正常关闭，服务端返回 `closing` 后使用代码 `1000` 关闭连接。
- 非法 JSON、非法事件、二进制帧和未知事件返回结构化错误。
- 从 `.env` 或进程环境加载并验证主机、端口、日志级别和 Web 根目录。
- 建立 Newtalk 命名空间日志，记录服务和 WebSocket 生命周期。
- 建立 HTTP、WebSocket 单元测试和真实 Uvicorn 子进程集成测试。
- 建立 P1 架构边界和消息协议文档。

### 当前调用链

```text
浏览器访问 /
-> newtalk.app:create_app
-> FastAPI StaticFiles
-> web/index.html + web/styles.css + web/app.js

浏览器连接 /ws
-> newtalk.transport.websocket:websocket_endpoint
-> accept
-> hello(session_id)
-> 接收 JSON 事件
-> ping 事件处理
-> pong(session_id, event_id)

浏览器主动断开
-> close(event_id)
-> closing(session_id, event_id)
-> WebSocket close(code=1000)
```

### 主要文件

- `src/newtalk/app.py`：应用创建、健康检查和静态页面挂载。
- `src/newtalk/config.py`：P1 环境变量加载和校验。
- `src/newtalk/logging_config.py`：Newtalk 应用日志配置。
- `src/newtalk/transport/websocket.py`：WebSocket 生命周期和 P1 消息协议。
- `web/index.html`：连接测试页面。
- `web/app.js`：浏览器 WebSocket 客户端。
- `web/styles.css`：页面样式。
- `tests/test_app.py`：HTTP 与静态页面测试。
- `tests/test_websocket.py`：WebSocket 协议测试。
- `tests/test_config.py`：配置和日志初始化测试。
- `tests/integration/test_runtime.py`：真实服务进程、HTTP 和 WebSocket 集成测试。
- `docs/protocol.md`：当前协议说明。
- `docs/architecture.md`：当前架构边界。

### 验证结果

- `GET /health` 返回 `status=ok`、`service=newtalk`、`version=0.1.0`。
- `GET /` 返回 `200` 和 `text/html`。
- 真实网络 WebSocket 握手成功。
- `hello -> ping -> pong` 消息往返成功。
- `close -> closing -> code 1000` 正常关闭成功。
- 集成测试自动启动和清理真实 Uvicorn 服务。
- `pytest` 执行结果为 `15 passed`，无警告。

### P1 结束时尚未实现

- 文本聊天和对话 Turn。
- LLM 及其 Provider 接口。
- 麦克风采集、VAD 和 ASR。
- TTS、音频流发送和浏览器播放。
- 用户打断。
- 摄像头、图片上传和 Vision/VLLM。
- Identity、声纹、Memory 和 User Profile。
- Tool Calling。
- 持久化 Session、性能指标、Docker 和 Provider 配置系统。

这些能力不是 P1 的缺陷，而是尚未开始对应 Part。当前不会提前定义完整 Provider 或 Manager 体系。

## P2：文本聊天闭环

### 目标

在不接入真实模型的前提下，完成浏览器文本输入到流式回复显示的第一个纵向聊天闭环，并验证一次用户事件只创建一个 Turn。

### 已完成

- Web 提供文本输入、发送和流式消息显示。
- `text_input` 使用必填 `event_id` 关联一个用户行为。
- 每个有效事件创建包含唯一 `turn_id` 的 `Turn`。
- Fake LLM 通过异步生成器稳定返回两个文本增量。
- 服务端依次发送 `turn_started`、`text_delta` 和 `turn_completed`。
- 相同 `event_id` 重发返回 `duplicate_event`，不创建第二个 Turn。
- 相同文本使用不同 `event_id` 时正常创建两个独立 Turn。
- 空文本、超长文本、生成失败和断开连接有明确边界。
- Turn 失败后 WebSocket 保持可用。
- WebSocket 生命周期、协议错误、文本事件处理和聊天核心分离。

### 当前调用链

```text
Web 文本输入
-> web/app.js 发送 text_input(event_id, text)
-> transport/websocket.py 路由事件
-> transport/text_chat.py 校验输入和幂等键
-> ChatService.create_turn(session_id, user_text)
-> FakeLLM.stream(user_text)
-> text_delta(turn_id, sequence, delta)
-> web/app.js 增量更新助手消息
-> turn_completed(turn_id, text)
```

### 主要文件

- `src/newtalk/chat/models.py`：P2 Turn 数据边界。
- `src/newtalk/chat/service.py`：Turn 创建和回复流组织。
- `src/newtalk/chat/fake_llm.py`：确定性异步文本流。
- `src/newtalk/transport/protocol.py`：协议版本和公共错误事件。
- `src/newtalk/transport/text_chat.py`：文本事件校验和协议输出。
- `src/newtalk/transport/websocket.py`：连接生命周期和事件路由。
- `web/index.html`、`web/app.js`、`web/styles.css`：P2 聊天页面。
- `tests/test_chat.py`：聊天核心测试。
- `tests/test_websocket.py`：Turn、重复输入和失败协议测试。
- `tests/integration/test_runtime.py`：真实进程文本聊天集成测试。

### 验证结果

- `pytest` 执行结果为 `23 passed`。
- JavaScript 语法检查通过。
- 真实浏览器自动连接并收到 `hello 0.2`。
- 真实页面发送文本后收到两个 delta 和完成事件。
- 桌面和 390px 移动视口布局通过检查。
- 浏览器控制台没有 JavaScript warning 或 error。

### 当前边界

- Fake LLM 不是 Provider 接口，只是 P2 测试实现。
- 每个 Turn 当前独立，不保存 Dialogue Context。
- 同一连接内 Turn 目前顺序处理，不实现并发打断。
- 不包含真实 LLM、Memory、ASR、TTS、Vision 或 Tool。

## P3：第一个真实流式 LLM

### 目标

基于一次真实 OpenAI-compatible 流式调用定义聊天核心当前真正需要的最小模型契约，同时保持 WebSocket 和 Turn 不感知厂商协议。

### 已完成

- 定义 `ChatModel.stream(user_text)` 和 `ChatModel.aclose()` 最小契约。
- Fake LLM 和 OpenAI-compatible LLM 由同一个 `ChatService` 调用。
- 使用 `AsyncOpenAI` 和异步 SSE 迭代，不在事件循环中执行同步网络请求。
- 支持 API Key、Base URL、模型名、静态 System Prompt 和超时配置。
- 默认使用 Fake；只有显式配置 `NEWTALK_LLM_BACKEND=openai` 才创建真实客户端。
- API Key 不进入 `AppConfig` 的字符串表示，`.env` 保持 Git 忽略。
- `ChatService` 记录首 Token、流完成总耗时和失败日志。
- 空文本流视为模型失败，并沿用 `turn_failed` 协议，不关闭 WebSocket。
- 浏览器提前断开或停止消费时显式关闭异步生成器和底层 HTTP 流。
- 应用关闭时通过生命周期钩子关闭模型客户端。
- 增加默认跳过的 `live` 冒烟测试，避免 CI 或普通 pytest 产生 API 费用。

### 当前调用链

```text
Web text_input
-> transport/websocket.py
-> transport/text_chat.py
-> ChatService.create_turn()
-> ChatService.stream_reply()
-> ChatModel.stream(user_text)
   |-> FakeLLM.stream()                    默认测试路径
   `-> OpenAICompatibleChatModel.stream()  真实异步 SSE 路径
-> text_delta
-> turn_completed / turn_failed
```

### 主要文件

- `src/newtalk/chat/model.py`：P3 最小模型契约。
- `src/newtalk/chat/openai_compatible.py`：第一个真实异步流式实现。
- `src/newtalk/chat/service.py`：统一调用模型并记录 LLM 耗时。
- `src/newtalk/config.py`：LLM 环境变量校验和 Secret 边界。
- `src/newtalk/app.py`：Fake/真实模型装配和资源生命周期。
- `src/newtalk/transport/text_chat.py`：流式消费和提前关闭资源释放。
- `tests/test_openai_compatible.py`：模拟 SSE、消息参数和资源释放测试。
- `tests/live/test_llm.py`：显式开启的真实 Provider 冒烟测试。

### 验证结果

- `pytest` 执行结果为 `33 passed, 1 skipped`。
- 默认跳过项是会产生真实外部调用的 `live` 测试。
- 模拟流验证 System/User 消息、增量过滤、流上下文退出和客户端关闭。
- Fake 路径继续通过真实 Uvicorn 子进程和 WebSocket 集成测试。
- DeepSeek `deepseek-v4-pro` 真实流式冒烟测试通过，收到 6 个文本增量。
- 本次真实调用首 Token 为 `2564.0ms`，流完成总耗时为 `2609.9ms`。
- 用户通过真实 Web 页面完成文本发送，页面能够逐步显示 DeepSeek 流式回复。

### 当前边界

- 每个 Turn 仍然独立，只发送当前用户文本，不包含 Dialogue Context。
- System Prompt 是可选静态配置，不是 Dynamic Prompt。
- 只有一个真实 OpenAI-compatible 实现，没有 Registry、动态 Provider 加载或第二个 LLM 对比。
- 不包含 Tool Calling、Token 统计、重试策略、ASR、TTS、Vision 或 Memory。
- 同一连接仍然顺序处理 Turn，不包含打断和并发生成。

## P4：TTS 播放闭环

### 目标

让 LLM 流式文本进入真实 TTS，并让浏览器在完整回复结束前开始播放音频。

### 已完成

- 根据真实豆包 V3 双向流式调用定义最小 `TextToSpeech` 契约。
- 实现 Fake TTS 和豆包 TTS，不建立动态 Provider 注册中心。
- LLM 文本增量经过独立分段器进入 TTS 文本队列。
- LLM 和 TTS 是同一 Turn 内的两个异步生产者，共用有序输出队列。
- WebSocket 使用 JSON 传输音频元数据，使用二进制帧传输 PCM。
- 浏览器使用 24kHz、16-bit、单声道 AudioWorklet 队列播放。
- 浏览器提供本地停止播放，并在实际取出首批样本时上报耗时。
- 服务端记录 `llm_first_token`、`tts_first_audio` 和 `browser_playback_started`。
- TTS 失败产生 `audio_failed`，不影响文本继续形成 `turn_completed`。
- Provider 会话或浏览器断开时，异步生成器负责取消任务和关闭连接。
- 豆包 App ID 和 Access Token 不进入配置对象的 `repr`。
- 豆包 WebSocket 默认禁用 Windows 系统代理，避免本机代理为实时链路增加约 4 秒延迟；需要时可显式开启。

### 当前调用链

```text
text_input
-> ChatService.stream_turn
   |-> ChatModel.stream -> text_delta
   |-> StreamingTextSegmenter -> TTS text queue
   `-> TextToSpeech.stream -> PCM frames
-> transport/text_chat.py
-> audio_start -> WebSocket binary frames -> audio_end
-> web/app.js -> pcm-player-worklet.js -> system output
-> playback_started -> service log
```

### 验证结果

- 普通测试 `50 passed, 2 skipped`，live 测试不会在 CI 中产生费用。
- 豆包真实 V3 冒烟测试成功返回 PCM，配置、鉴权、Resource ID 和音色有效。
- 真实 Uvicorn 子进程完成 HTTP、JSON WebSocket 和二进制音频集成测试。
- 浏览器实际收到并消费 Fake PCM，状态从缓冲进入播放完成。
- 浏览器测得本次 Fake 链路播放开始时间为 `347.3ms`。
- 代理隔离测试中，完整真实管线首字由 `5.76–6.07s` 恢复为 `1.42–2.08s`，豆包首音频增量约 `253ms`。
- JavaScript 语法检查通过，浏览器控制台无 warning/error。

### 当前边界

- 浏览器停止播放只清空本地播放队列，不取消服务端 LLM/TTS；服务端取消属于 P5。
- 同一连接仍顺序处理 Turn，不支持麦克风输入和用户语音打断。
- 只支持 PCM；Opus 与音频格式协商尚未实现。
- 豆包每个 Turn 新建一条 Provider WebSocket，尚未实现连接复用。
- 真实豆包音频与真实 LLM 的浏览器听感需要用户手工确认。
- Dialogue Context、Memory、ASR、Vision 和 Tool 仍未实现。

## P5-A：麦克风、VAD、Fake ASR 与打断

### 已完成

- 浏览器麦克风重采样为 16kHz、单声道、PCM S16LE，并按 20ms 发送。
- 固定 Silero VAD v6.2.1 ONNX 模型，双阈值、滑动窗口、pre-roll 和静音结束均在服务端执行。
- 定义最小 `SpeechRecognizer` 契约和 Fake ASR，ASR Final 进入现有 ChatService。
- 每条连接持有独立 VAD/采集状态，只允许一个活动 capture 和一个活动 Turn。
- WebSocket 接收循环不再阻塞于聊天生成，当前 Turn 可被新文本或 `speech_start` 取消。
- 单一发送队列按 `turn_id` 丢弃旧 LLM/TTS 的迟到结果。
- 浏览器收到 `audio_stop` 时清空播放队列，系统“停止播放”按钮仍只处理本地播放。

### 验证结果

- `63 passed, 2 skipped`，覆盖 VAD 边界、静音、音频会话、语音唯一 Turn 和 barge-in。
- 真实 Silero ONNX 对 1 秒全零 PCM 推理完成，未产生误触发。
- JavaScript 语法检查、`git diff --check` 和真实 Uvicorn HTTP/WebSocket 集成测试通过。
- 用户通过 Chrome 完成真实麦克风测试，VAD 能检测声音并在静音后形成唯一 Fake ASR Turn。

### 当前边界

- Fake ASR 只返回 `.env` 中的固定测试文本，不代表语音内容识别正确。
- 豆包真实流式 ASR 尚未接入，用户暂时不需要提供 App ID、Access Token 或 Resource ID。
- Fake ASR 阶段不验证识别准确率；扬声器回声和真实流式识别下的打断听感留到 P5-B 验收。

## P5-B：豆包双向流式 ASR

### 已实现

- 根据官方 V3 协议实现鉴权头、gzip JSON 请求、PCM 音频包、序列号和服务响应解析。
- 浏览器 20ms PCM 在 Provider 内聚合为 100ms 包；最后一包使用负序列结束 utterance。
- 豆包 partial/final 映射到现有 `SpeechRecognizer` 契约，只有 final 创建 Turn。
- 默认 `enable_nonstream=false`，继续由本地 Silero 决定打断和静音结束。
- 记录 `asr_first_result` 和 `asr_stream_completed` 耗时日志。
- 鉴权、协议或超时失败通过 `asr_failed` 返回浏览器，WebSocket 保持可用。
- Fake ASR 保留为 CI 默认路径，普通测试不调用外部服务或消耗额度。

### 人工验收

- 用户在火山控制台开通正确项目的 ASR 资源后，Chrome 已能返回真实中文识别文本。
- 握手失败时已验证火山错误正文和 `logid` 会进入服务日志，便于区分代码错误与资源未授权。
- 耳机/扬声器回声场景和完整 ASR→LLM→TTS 延迟仍作为后续持续观测项，不阻塞 P5-B 完成。

## 下一阶段

P5-B 合并后，再讨论 Dialogue Context、Identity/Memory 或 Vision 的优先顺序。

## 变更记录

| 日期 | Part | 内容 | 验证 |
| --- | --- | --- | --- |
| 2026-08-14 | P1 | 建立独立仓库、配置、日志、FastAPI、静态 Web 和 WebSocket 生命周期 | 15 项测试通过，包含真实服务 HTTP/WS 集成测试 |
| 2026-08-14 | P1 工程流程 | 加入 GitHub Actions CI 和可调整的默认开发流程 | PR #1 CI 通过并合并到 main |
| 2026-08-15 | P2 | 建立唯一 Turn、Fake LLM 流式回复和 Web 文本聊天闭环 | 23 项测试及真实浏览器桌面/移动验证通过 |
| 2026-08-15 | P3 | 增加最小 ChatModel 契约、异步 OpenAI-compatible 流和 LLM 耗时日志 | 33 项通过；DeepSeek live 测试和真实 Web 流式聊天通过 |
| 2026-08-16 | P4 | 增加豆包双向流式 TTS、PCM 二进制协议和 AudioWorklet 播放 | 50 项通过；豆包 live 与浏览器播放验证通过 |
| 2026-08-18 | P5-A | 增加麦克风、Silero VAD、Fake ASR、可取消 Turn 和服务端 barge-in | 63 项通过；Silero 静音推理与协议集成测试通过 |
| 2026-08-18 | P5-B | 增加豆包双向流式 ASR 协议、partial/final、失败反馈和耗时日志 | 76 项通过，2 项 live 跳过；真实 Chrome 中文识别通过 |
