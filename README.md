# Newtalk

Newtalk 是一个面向家庭陪伴场景的实时语音对话系统。项目沿用小智服务端的实时语音思路，但以独立 Python 工程重新实现，当前重点是 Web 端演示、设备级家庭隔离、声纹身份、短期对话、长期记忆与可观测性。

当前交付基线为 **P7.8**，项目版本为 **0.13.0**。

![Newtalk 当前系统架构](diagrams/newtalk-current-architecture.png)

- 交互式架构图：[diagrams/newtalk-current-architecture.html](diagrams/newtalk-current-architecture.html)
- 当前架构说明：[docs/architecture.md](docs/architecture.md)
- WebSocket 协议：[docs/protocol.md](docs/protocol.md)
- 实际进度：[docs/PROGRESS.md](docs/PROGRESS.md)
- 后续规划：[PROJECT_PLAN.md](PROJECT_PLAN.md)
- 开发流程：[docs/DEVELOPMENT_WORKFLOW.md](docs/DEVELOPMENT_WORKFLOW.md)

## 已有能力

- FastAPI HTTP 与 WebSocket 服务，提供 `/health`、静态 Web 页面和实时事件协议。
- 浏览器 PCM 麦克风采集、VAD、豆包双向流式 ASR、流式 LLM、豆包流式 TTS。
- AI 播放期间检测用户开口并取消当前 Turn，支持真实语音打断。
- PostgreSQL 持久化 Device、Family、Member、Session、Dialogue 与 Memory Job。
- 独立 VoicePrint 服务，支持 3D-Speaker CAM++ 声纹录入和识别。
- Member Profile、MemOS 长期记忆、Memory Center 与后台异步写入。
- 页面刷新恢复短期 Dialogue，并可通过“新对话”清除当前 Session 上下文。
- 逐 Turn 时延日志，覆盖 ASR、身份解析、LLM 首字、TTS 首包与播放完成。

## 技术栈

- Python 3.11、FastAPI、Uvicorn、websockets
- PostgreSQL、SQLAlchemy、Alembic
- 原生 HTML、CSS、JavaScript、Web Audio / AudioWorklet
- 豆包 ASR/TTS、OpenAI-compatible LLM
- ModelScope 3D-Speaker CAM++ VoicePrint
- MemOS HTTP API
- pytest、GitHub Actions

## 本地启动

先复制 `.env.example` 为 `.env`，填写实际使用的 LLM、ASR、TTS、数据库、VoicePrint 和 MemOS 配置。密钥只保存在本地 `.env`，不要提交。

```powershell
cd D:\Desktop\Newtalk
.\.venv\Scripts\Activate.ps1
docker compose up -d postgres
alembic upgrade head
newtalk
```

主服务默认地址：

- Web：<http://127.0.0.1:8006/>
- Health：<http://127.0.0.1:8006/health>

真实声纹服务需要在第二个终端启动：

```powershell
cd D:\Desktop\Newtalk
.\.venv\Scripts\Activate.ps1
$env:VOICEPRINT_BACKEND = "campplus"
newtalk-voiceprint
```

声纹服务会读取项目根目录的 `.env`。独立的 `VOICEPRINT_API_TOKEN` / `VOICEPRINT_DATABASE_URL` 优先；未设置时分别复用 `NEWTALK_VOICEPRINT_API_TOKEN` / `NEWTALK_DATABASE_URL`，避免主服务与声纹服务使用不同鉴权或数据库配置。

VoicePrint 默认地址：

- API 文档：<http://127.0.0.1:8010/docs>
- Health：<http://127.0.0.1:8010/health>

如果日志显示 `backend=deterministic`，当前运行的是自动测试后端，不是真实 CAM++ 模型。

## 验证

```powershell
python -m pytest
python -m pytest services/voiceprint/tests -q
```

真实供应商测试可能产生费用，默认不会随普通测试执行。GitHub Actions 在 Pull Request 和 `main` push 时运行自动测试；当前尚未配置生产 CD。

## 日志

开发时可将终端日志同时写入文件：

```powershell
newtalk 2>&1 | Tee-Object -FilePath .\logs\newtalk.log
```

重点事件包括：

- `vad_speech_start`、`asr_first_result`、`asr_final`
- `speaker_resolved`
- `llm_first_token`、`tts_first_audio`
- `browser_playback_started`、`browser_playback_drained`
- `turn_completed`、`memory_job_completed`

## 当前边界

当前运行时按 `device_id` 维护一条家庭共享的短期 Dialogue。声纹结果只标记当前说话人并决定是否可以使用该成员的 Profile/Memory；声纹超时或未命中会降级为 Guest，但不会再切断同一设备的最近对话。数据库暂时保留 `family/guest` lane 作为兼容元数据和分 lane 裁剪依据。

工程级 AEC、声纹横向扩容、完整云端部署与 P8 Vision 也尚未进入当前交付基线。
