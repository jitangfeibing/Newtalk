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
| 当前阶段 | P2 文本聊天闭环 |
| 阶段状态 | 已完成并验证 |
| 开发分支 | `codex/p2-text-chat` |
| 项目版本 | `0.2.0` |
| Python | 3.11.5 |
| 环境 | 项目内标准 `.venv`，由 Anaconda Base Python 创建 |
| 后端 | FastAPI + Uvicorn |
| 前端 | 原生 HTML + CSS + JavaScript |
| 自动测试 | 23 项通过，包括真实服务进程文本聊天测试 |
| CI | P1 已在 PR #1 通过；P2 等待 PR 验证 |
| 最后更新 | 2026-08-15 |

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

## 下一阶段

P3 目标是接入第一个真实流式 LLM，并根据真实调用需要定义最小模型契约。Fake LLM 将继续保留用于自动测试。

## 变更记录

| 日期 | Part | 内容 | 验证 |
| --- | --- | --- | --- |
| 2026-08-14 | P1 | 建立独立仓库、配置、日志、FastAPI、静态 Web 和 WebSocket 生命周期 | 15 项测试通过，包含真实服务 HTTP/WS 集成测试 |
| 2026-08-14 | P1 工程流程 | 加入 GitHub Actions CI 和可调整的默认开发流程 | PR #1 CI 通过并合并到 main |
| 2026-08-15 | P2 | 建立唯一 Turn、Fake LLM 流式回复和 Web 文本聊天闭环 | 23 项测试及真实浏览器桌面/移动验证通过 |
