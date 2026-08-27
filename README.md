# Newtalk

Newtalk 是一个以 Web 为主要客户端的多模态家庭陪伴机器人。

项目采用按 Part 逐步构建的方式，每个阶段都必须可运行、可测试、可演示。原小智项目仅作为只读参考，不作为 Newtalk 的运行时依赖。

详细规划见 [PROJECT_PLAN.md](PROJECT_PLAN.md)，实际开发进度见 [docs/PROGRESS.md](docs/PROGRESS.md)，默认协作方式见 [docs/DEVELOPMENT_WORKFLOW.md](docs/DEVELOPMENT_WORKFLOW.md)。

## 当前阶段：P6

P6 在完整语音闭环上增加连接级 Session 和有限多轮 Context：

- `GET /health` 健康检查。
- `WS /ws` WebSocket 握手、文本聊天和正常关闭。
- 每个 `text_input` 创建唯一 `turn_id`。
- `ChatModel` 定义当前聊天核心实际需要的最小流式契约。
- OpenAI-compatible 模型通过异步 SSE 流返回 `text_delta`。
- Fake LLM 继续用于本地开发、自动测试和 CI。
- `TextToSpeech` 定义当前真实调用需要的最小音频流契约。
- 豆包 V3 双向 WebSocket 接收分段文本并流式返回 PCM。
- Fake TTS 继续用于自动测试和不产生费用的本地验证。
- WebSocket 通过 JSON 发送音频元数据，通过二进制帧发送 PCM。
- 浏览器通过 AudioWorklet 缓冲、播放和停止 PCM。
- 记录 LLM 首 Token、TTS 首音频帧和浏览器开始播放时间。
- TTS 失败不会丢失已经生成的文本回复。
- 浏览器通过 `getUserMedia` 和 AudioWorklet 采集麦克风，重采样为 16kHz 单声道 PCM。
- 服务端通过 Silero VAD v6.2.1 检测语音开始和静音结束。
- `speech_start` 取消旧 LLM/TTS Turn，并要求浏览器立即停止旧音频。
- Fake ASR 继续用于自动测试；豆包 ASR 使用官方 V3 二进制 WebSocket 协议。
- 豆包 ASR 按 100ms 聚合 PCM，实时返回 partial，并在 final 时只创建一个 Turn。
- ASR 记录首个识别结果和完整识别耗时；失败会返回 `asr_failed`，不会关闭 WebSocket。
- 每条 WebSocket 连接持有独立 `DialogueSession`，不同连接不共享历史。
- 只有成功完成的用户/助手轮次进入 Dialogue History；取消或失败的 Turn 不写入历史。
- 上下文按最近轮次和总字符数双重限制，默认最多 8 轮、12000 字符。
- Fake 和 OpenAI-compatible LLM 使用同一个多轮消息契约。
- WebSocket 接收、当前 Turn 和单一发送队列并发运行，旧 Turn 的迟到结果会被丢弃。
- 环境变量配置和 Newtalk 应用日志。
- HTTP、WebSocket 与真实服务进程自动测试。

当前阶段仍不包含跨连接 Session 恢复、长期 Memory、Vision 和 Provider Registry。

## 本地启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
newtalk
```

如需覆盖默认运行参数，先复制 `.env.example` 为 `.env`。默认
`NEWTALK_LLM_BACKEND=fake`，不需要 API Key。

P6 对话窗口配置：

```dotenv
NEWTALK_DIALOGUE_MAX_TURNS=8
NEWTALK_DIALOGUE_MAX_CHARS=12000
```

窗口只保存当前 WebSocket 连接内成功完成的对话；刷新或断开页面后历史会清空。

使用智谱或其他 OpenAI-compatible 服务时，在本地 `.env` 配置：

```dotenv
NEWTALK_LLM_BACKEND=openai
NEWTALK_LLM_API_KEY=replace-with-local-secret
NEWTALK_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
NEWTALK_LLM_MODEL=replace-with-enabled-model
NEWTALK_LLM_SYSTEM_PROMPT=你是 Newtalk，一个简洁、友善的家庭陪伴助手。
NEWTALK_LLM_TIMEOUT_SECONDS=30
```

使用豆包 V3 双向流式 TTS 时，在同一个本地 `.env` 配置：

```dotenv
NEWTALK_TTS_BACKEND=doubao
NEWTALK_TTS_APP_ID=replace-with-local-app-id
NEWTALK_TTS_ACCESS_TOKEN=replace-with-local-secret
NEWTALK_TTS_RESOURCE_ID=seed-tts-2.0
NEWTALK_TTS_VOICE_TYPE=replace-with-enabled-voice
NEWTALK_TTS_AUDIO_FORMAT=pcm
NEWTALK_TTS_SAMPLE_RATE=24000
NEWTALK_TTS_TIMEOUT_SECONDS=30
NEWTALK_TTS_USE_SYSTEM_PROXY=false
```

豆包默认直连，避免 `websockets` 自动读取 Windows 系统代理并增加实时链路延迟。只有网络环境明确要求豆包经过系统代理时，才将 `NEWTALK_TTS_USE_SYSTEM_PROXY` 改为 `true`。

使用豆包 2.0 双向流式 ASR 时，在本地 `.env` 配置：

```dotenv
NEWTALK_ASR_BACKEND=doubao
NEWTALK_ASR_API_KEY=replace-with-local-secret
NEWTALK_ASR_RESOURCE_ID=volc.seedasr.sauc.duration
NEWTALK_ASR_WS_URL=wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async
NEWTALK_ASR_PACKET_DURATION_MS=100
NEWTALK_ASR_TIMEOUT_SECONDS=30
NEWTALK_ASR_USE_SYSTEM_PROXY=false
```

小时版资源使用 `volc.seedasr.sauc.duration`，并发版使用 `volc.seedasr.sauc.concurrent`。ASR 同样默认直连；只有网络明确要求时才启用系统代理。

`.env` 已被 Git 忽略。不要把真实 API Key 写入 `.env.example` 或提交到仓库。

打开 <http://127.0.0.1:8006/>。不要使用 `file://` 直接打开 `web/index.html`。

运行测试：

```powershell
pytest
```

普通测试不会调用真实 Provider。显式执行真实 Provider 冒烟测试：

```powershell
$env:NEWTALK_RUN_LIVE_LLM="1"
pytest -m live tests/live/test_llm.py

$env:NEWTALK_RUN_LIVE_TTS="1"
pytest -m live tests/live/test_tts.py
```

架构和协议说明见 [docs/architecture.md](docs/architecture.md) 与 [docs/protocol.md](docs/protocol.md)。
