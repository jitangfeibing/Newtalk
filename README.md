# Newtalk

Newtalk 是一个以 Web 为主要客户端的多模态家庭陪伴机器人。

项目采用按 Part 逐步构建的方式，每个阶段都必须可运行、可测试、可演示。原小智项目仅作为只读参考，不作为 Newtalk 的运行时依赖。

详细规划见 [PROJECT_PLAN.md](PROJECT_PLAN.md)，实际开发进度见 [docs/PROGRESS.md](docs/PROGRESS.md)。

## 当前阶段

P1 提供最小可运行骨架：

- `GET /health` 健康检查。
- `WS /ws` WebSocket 握手、`ping/pong` 和正常关闭。
- `web/` 基础连接验证页面。
- 环境变量配置和 Newtalk 应用日志。
- HTTP、WebSocket 与真实服务进程自动测试。

当前阶段不包含 ASR、LLM、TTS、Vision、Memory 和 Provider 抽象。

## 本地启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
newtalk
```

如需覆盖默认运行参数，先复制 `.env.example` 为 `.env`。支持
`NEWTALK_HOST`、`NEWTALK_PORT`、`NEWTALK_LOG_LEVEL` 和 `NEWTALK_WEB_ROOT`。

打开 <http://127.0.0.1:8006/>。不要使用 `file://` 直接打开 `web/index.html`。

运行测试：

```powershell
pytest
```

架构和协议说明见 [docs/architecture.md](docs/architecture.md) 与 [docs/protocol.md](docs/protocol.md)。
