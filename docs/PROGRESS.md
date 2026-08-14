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
| 当前阶段 | P1 基础运行骨架 |
| 阶段状态 | 已完成并验证 |
| 开发分支 | `codex/p1-bootstrap` |
| Python | 3.11.5 |
| 环境 | 项目内标准 `.venv`，由 Anaconda Base Python 创建 |
| 后端 | FastAPI + Uvicorn |
| 前端 | 原生 HTML + CSS + JavaScript |
| 自动测试 | 15 项通过，包括真实服务进程集成测试 |
| 最后更新 | 2026-08-14 |

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

### 尚未实现

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

## 下一阶段

P2 尚未开始。预定目标是建立最小文本对话闭环：

```text
Web 文本输入
-> WebSocket text_input 事件
-> 创建唯一 Turn
-> Fake LLM 生成回复
-> WebSocket 返回文本结果
-> 页面显示对话
```

P2 首先验证 Turn 和聊天核心的职责边界，不接入真实 LLM、Memory、Vision、ASR 或 TTS。

## 变更记录

| 日期 | Part | 内容 | 验证 |
| --- | --- | --- | --- |
| 2026-08-14 | P1 | 建立独立仓库、配置、日志、FastAPI、静态 Web 和 WebSocket 生命周期 | 15 项测试通过，包含真实服务 HTTP/WS 集成测试 |
