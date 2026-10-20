# Newtalk 后续规划

本文只记录当前交付基线之后仍有效的规划。已完成阶段的详细过程由 Git、Pull Request 和 [docs/PROGRESS.md](docs/PROGRESS.md) 保留，不再在仓库中维护重复设计稿。

## 产品目标

Newtalk 面向“一台设备对应一个家庭空间”的家庭陪伴场景：

- 同一设备中的对话上下文保持连续。
- 成员通过声纹映射身份，使用自己的 Profile 和长期 Memory。
- Guest 或身份不确定者可以参与当前家庭对话，但不能读取成员私有长期数据。
- 语音主链路优先保证实时性、可打断、可降级和可观测。
- Web 是当前主要演示与验收载体，后端协议保留未来接入 ESP32 或其他设备的空间。

## 当前基线

P1-P7.8 已交付：

- 独立仓库、测试、CI、HTTP/WebSocket 与 Web 页面。
- 文本流式 LLM、PCM 流式 TTS、VAD 和豆包双向流式 ASR。
- PostgreSQL Device/Family/Member/Session/Dialogue 数据模型。
- 独立 CAM++ VoicePrint 服务与成员声纹映射。
- Profile、MemOS 长期记忆、Memory Center 和异步 Memory Job。
- Session 刷新恢复、新对话清理、真实语音打断和逐 Turn 时延日志。
- 首页、对话、家庭成员与记忆中心的第一版产品界面。

当前实现细节以 [docs/architecture.md](docs/architecture.md) 和 [docs/protocol.md](docs/protocol.md) 为准。

在代码审查整改中已经补充：设备共享短期 Dialogue、同设备多连接上下文同步、恢复码数据库限流、真实 VoicePrint 默认后端和 CI 静态检查/覆盖率报告。数据库仍保留 `family/guest` lane 作为兼容元数据，不再用它分隔模型看到的短期上下文。

## 下一阶段候选

以下项目必须经过需求确认后再进入开发，不代表已经实现。

### B2：被打断 Turn 的上下文语义

问题：当前只有完整完成的 Turn 才写入 Dialogue。被打断的用户问题和已生成回复全部丢弃，下一轮可能失去指代对象。

候选方案：

- 保存用户问题。
- 保存已实际播放的助手文本片段，而不是全部已生成文本。
- 为 Turn 增加 `interrupted` 状态和播放截止位置。
- Prompt 中明确该回复被打断，避免模型误以为已经完整表达。

在实现前需要先确定“已生成”“已发送”“已播放”三种边界采用哪一种。

### B3：音频稳定性与诊断

- 记录输入音量、有效语音时长、VAD 判定和 ASR/VoicePrint 音频质量。
- 评估动态 VAD 阈值、短语音补帧和重录提示。
- 评估浏览器 AEC、耳机/扬声器模式和服务端回声处理。
- 建立真实环境回归样本，不只依赖单元测试。

### B4：VoicePrint 容量与准确率

- 测试单进程 CAM++ 的吞吐与队列等待时间。
- 将模型推理进程与 API 层解耦，评估多 Worker 或多实例。
- 增加模板质量、阈值分布和误识别/漏识别统计。
- 云端部署时根据压测结果决定 CPU/GPU 规格和副本数。

### 可选：P8 Vision

Vision 暂不在当前交付范围。若启用，应复用统一 Turn、身份、日志和 Memory 边界，而不是把图片描述伪装成普通用户文本。

### 可选：部署与 CD

当前只有 CI。确认云服务器、域名、HTTPS、密钥管理、数据库备份和回滚策略后，再增加容器化生产部署与 CD。

## 固定边界

- 不为 Intent 单独增加一个前置 LLM；优先使用主 LLM Tool Calling。
- Memory 是可关闭 Provider，不得成为聊天主链路的硬依赖。
- 普通聊天不强制前置查询 MemOS。
- Guest 不读写 Member 的 Profile 与长期 Memory。
- 浏览器当前继续使用 24 kHz 单声道 PCM；是否引入 Opus 由设备与带宽需求决定。
- 不在未确认需求前自动增加 Vision、知识图谱或复杂 Memory Router。

## 文档分工

- [README.md](README.md)：启动、能力和日常使用入口。
- [docs/architecture.md](docs/architecture.md)：当前真实运行架构。
- [docs/protocol.md](docs/protocol.md)：WebSocket 消息契约。
- [docs/PROGRESS.md](docs/PROGRESS.md)：交付状态与已知边界。
- [docs/DEVELOPMENT_WORKFLOW.md](docs/DEVELOPMENT_WORKFLOW.md)：分支、测试、PR 和 CI 流程。
- 本文：尚未实现的后续规划。
