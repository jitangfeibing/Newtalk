# Newtalk

Newtalk 是一个以 Web 为主要客户端的多模态家庭陪伴机器人。

项目采用按 Part 逐步构建的方式，每个阶段都必须可运行、可测试、可演示。原小智项目仅作为只读参考，不作为 Newtalk 的运行时依赖。

详细规划见 [PROJECT_PLAN.md](PROJECT_PLAN.md)，实际开发进度见 [docs/PROGRESS.md](docs/PROGRESS.md)，默认协作方式见 [docs/DEVELOPMENT_WORKFLOW.md](docs/DEVELOPMENT_WORKFLOW.md)。

## 当前阶段：P3

P3 已在文本聊天闭环上加入第一个真实流式 LLM 实现：

- `GET /health` 健康检查。
- `WS /ws` WebSocket 握手、文本聊天和正常关闭。
- 每个 `text_input` 创建唯一 `turn_id`。
- `ChatModel` 定义当前聊天核心实际需要的最小流式契约。
- OpenAI-compatible 模型通过异步 SSE 流返回 `text_delta`。
- Fake LLM 继续用于本地开发、自动测试和 CI。
- 记录首 Token、总耗时和失败日志。
- `web/` 展示用户消息和流式助手回复。
- 环境变量配置和 Newtalk 应用日志。
- HTTP、WebSocket 与真实服务进程自动测试。

当前阶段不包含 Dialogue Context、ASR、TTS、Vision、Memory 和 Provider Registry。

## 本地启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
newtalk
```

如需覆盖默认运行参数，先复制 `.env.example` 为 `.env`。默认
`NEWTALK_LLM_BACKEND=fake`，不需要 API Key。

使用智谱或其他 OpenAI-compatible 服务时，在本地 `.env` 配置：

```dotenv
NEWTALK_LLM_BACKEND=openai
NEWTALK_LLM_API_KEY=replace-with-local-secret
NEWTALK_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
NEWTALK_LLM_MODEL=replace-with-enabled-model
NEWTALK_LLM_SYSTEM_PROMPT=你是 Newtalk，一个简洁、友善的家庭陪伴助手。
NEWTALK_LLM_TIMEOUT_SECONDS=30
```

`.env` 已被 Git 忽略。不要把真实 API Key 写入 `.env.example` 或提交到仓库。

打开 <http://127.0.0.1:8006/>。不要使用 `file://` 直接打开 `web/index.html`。

运行测试：

```powershell
pytest
```

普通测试不会调用真实 LLM。显式执行真实 Provider 冒烟测试：

```powershell
$env:NEWTALK_RUN_LIVE_LLM="1"
pytest -m live tests/live/test_llm.py
```

架构和协议说明见 [docs/architecture.md](docs/architecture.md) 与 [docs/protocol.md](docs/protocol.md)。
