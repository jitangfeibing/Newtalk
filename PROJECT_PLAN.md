# Newtalk 项目规划

## 1. 项目定位

Newtalk 是一个以 Web 为主要客户端的多模态家庭陪伴机器人。

核心能力包括：

- 文字聊天。
- 浏览器麦克风实时语音输入。
- VAD 语音活动检测，以及流式 ASR、LLM 和 TTS。
- 浏览器音频播放和用户打断。
- 摄像头、图片理解和多模态输入。
- 基于声纹的家庭成员身份识别。
- 每个家庭成员独立的长期 Memory 和 User Profile。
- 少量适合陪伴场景的工具，例如时间、天气和提醒。

Newtalk 参考小智项目中已经验证过的协议、音频处理方式和 Provider 实现，但不继承其过度集中的 `ConnectionHandler` 架构。

## 2. 仓库与 Legacy 边界

### 2.1 独立仓库

`D:\Desktop\Newtalk` 应从第一天开始作为独立 Git 仓库：

```text
D:\Desktop\Newtalk\
├── .git\
├── README.md
├── PROJECT_PLAN.md
├── pyproject.toml
├── src\
├── web\
├── tests\
├── docs\
├── config\
└── docker\
```

GitHub Remote 必须绑定用户自己的仓库，不使用临时或代建仓库：

```bash
git init -b main
git remote add origin <用户提供的 GitHub 仓库 URL>
git remote -v
```

在用户提供真实 URL 前，不创建虚假的 Remote。

### 2.2 Legacy 完全只读

当前目录中的 `xiaozhi-esp32-server-main` 只作为 Legacy Reference：

- 不修复。
- 不重构。
- 不继续增加功能。
- 不作为 Newtalk 的运行时依赖。
- 不提交到 Newtalk Git 仓库。
- 只用于核对 WebSocket 协议、音频格式、Provider 参数和历史行为。

如果 Legacy 能通过环境和配置运行，则记录基线；如果因为源码问题无法运行，只记录阻塞点，不修改 Legacy。

## 3. 已确定的产品边界

### 3.1 必须实现

- Web 是主要客户端。
- 文本、语音和图片最终进入同一套聊天核心。
- 一个用户行为只创建一个对话 Turn。
- 麦克风音频必须经过明确的语音起止检测；静音和环境噪声不能直接创建 Turn。
- 声纹识别结果必须映射到家庭成员 Identity。
- Memory 和 User Profile 按家庭成员隔离。
- Memory 查询不能无限增加首字延迟。
- Vision 不再通过 `abort -> 拼接文本 -> 再次聊天` 实现。
- Tool 只保留少量明确有用的能力。
- 自动测试、集成测试、日志、指标、Health Check、Docker 和文档属于正式交付内容。

### 3.2 暂不实现

- 复杂文本、语音、视觉情绪融合。
- 完整迁移 IoT、Device MCP 和大量 Server Plugin。
- 一次性重写所有 Provider。
- 一次性拆分出大量 Manager。
- 为了架构完整而提前设计尚未使用的接口。

## 4. 核心设计原则

### 4.1 一个 Part 一个 Part 写

每个 Part 都必须形成可以运行、可以测试、可以演示的纵向闭环。

不采用以下方式：

```text
先设计所有接口
-> 再创建所有 Manager
-> 再搬迁所有 Provider
-> 最后统一联调
```

采用以下方式：

```text
选择当前最小目标
-> 写最少代码跑通
-> 增加测试和耗时指标
-> 发现真实重复后再抽象
-> 提交一个可回退的 Git 版本
```

### 4.2 Provider 延迟抽象

Provider 思想提前确定，但不在 P2 预先设计完整 Provider 世界。

具体规则：

1. 接入第一个能力时，先围绕真实调用写最小实现。
2. 接入第一个真实 Provider 时，只定义当前链路实际需要的方法和数据。
3. 第二个同类 Provider 出现后，再比较差异并提取最小公共契约。
4. 只有确实需要通过配置切换时，才增加 Registry、Factory 或动态加载。
5. 不把 Legacy Provider 的全部方法直接定义成 Newtalk 标准。
6. Provider 内部协议细节不得泄漏到 Turn 和 Conversation 核心。

示例：

```text
P2 Fake LLM：直接满足文本闭环，不建立通用 Provider 框架。
P3 第一个真实 LLM：根据真实流式输出定义最小 ChatModel 契约。
以后接入第二个 LLM：确认公共行为后再增加配置切换。
P4 接入 TTS：此时才定义 TTS 所需的最小音频流契约。
P5 接入 VAD 和 ASR：此时才根据真实麦克风、音频格式和 ASR 行为定义最小契约。
```

接口不是越早越好，而是在有真实调用者和至少一个真实实现时才有意义。

### 4.3 实时路径与后台路径分离

实时路径只保留生成当前回答必须完成的工作：

```text
用户输入
-> 创建 Turn
-> 有时限的 Context 准备
-> LLM 流式输出
-> TTS 流式播放
```

后台路径处理不应阻塞当前回答的工作：

```text
保存长期记忆
更新用户画像
生成摘要
整理视觉观察
非关键日志和统计
```

## 5. 目标调用链

```text
Web 文本 ---------------------------------------+
浏览器麦克风 -> 音频帧 -> VAD -> ASR 最终文本 --+--> ChatInput
Web 图片/摄像头帧 ------------------------------+       |
                                            v
                                      TurnContext
                                            |
                    +-----------------------+-----------------------+
                    |                       |                       |
               Identity                Memory 查询            Vision 理解
                    |                  有数量和超时限制         按需或使用缓存
                    +-----------------------+-----------------------+
                                            |
                                      Context 组装
                                            |
                                      LLM 流式输出
                                            |
                              +-------------+-------------+
                              |                           |
                         Web 流式文本                 TTS 流式音频
                                                          |
                                                    浏览器实时播放

回答完成 -> 后台保存 Memory -> 后台更新 User Profile
```

## 6. 关键数据边界

### 6.1 ChatInput

一次用户行为对应一个 `ChatInput`：

```text
turn_id
source: text | voice | image | multimodal
user_text
speaker_id / user_id
images
vision_observation
created_at
```

图片和视觉描述是输入字段，不伪装成第二条用户消息。

### 6.2 TurnContext

一次对话轮次独立保存：

```text
turn_id
session_id
user_id
cancelled
input
retrieved_memory
profile_snapshot
provider_tasks
timing
```

所有 VAD、ASR、Vision、LLM、Tool、TTS 返回都必须能归属到当前输入或 `turn_id`。旧 Turn 的迟到结果不得写入新 Turn。

### 6.3 四类上下文

| 类型 | 示例 | 生命周期 |
|---|---|---|
| Dialogue | 用户和助手当前会话消息 | 当前 Session |
| Vision Observation | 桌面上有水杯 | 短期缓存，默认不进入长期记忆 |
| Long-term Memory | 用户每周一加班 | 跨 Session |
| User Profile | 姓名、家庭关系、稳定偏好 | 长期结构化数据 |

## 7. 分阶段实施计划

### P0：Legacy 调研和可运行性验证

目标：获得参考基线，不修改 Legacy。

工作内容：

- 记录启动环境、依赖和配置。
- 尝试运行原服务端和 `test_page.html`。
- 记录 WebSocket 消息、音频格式和 Provider 配置。
- 记录 ASR、LLM、TTS 的关键耗时。
- 无法运行时记录准确阻塞点。

完成标准：形成 Legacy 基线文档，不要求修复 Legacy。

### P1：Newtalk 仓库和最小骨架

目标：建立完全独立、可测试的新项目。

工作内容：

- 初始化独立 Git 仓库并绑定用户 GitHub。
- 建立 Python 项目、配置加载、日志和测试框架。
- 提供 `/health`。
- 建立 WebSocket 服务和最小 Web 页面。
- 完成 `hello` 和连接关闭。

完成标准：自动测试启动服务，Web 能连接并收到 `hello`。

### P2：文本聊天闭环

目标：完成第一个可演示的纵向 Part。

工作内容：

- Web 输入文本。
- 创建唯一 `turn_id`。
- 使用 Fake LLM 流式返回确定性内容。
- 浏览器显示流式文本。
- 测试重复输入、断开连接和异常。

本阶段不设计完整 Provider Registry。

完成标准：一次文本输入只产生一个 Turn，测试可稳定复现。

### P3：第一个真实 LLM

目标：根据真实需求形成第一个最小 Provider 契约。

工作内容：

- 接入一个真实 LLM。
- 定义当前需要的最小流式输出类型。
- 增加首 Token 耗时和错误日志。
- Fake LLM 继续用于测试。

完成标准：Fake 和真实 LLM 使用同一聊天核心，业务代码不知道厂商协议。

### P4：TTS 播放闭环

目标：完成文本输入到浏览器语音播放。

工作内容：

- 根据第一个真实 TTS 定义最小 TTS 契约。
- LLM 文本分段进入 TTS。
- 音频帧发送到 Web。
- 浏览器解码和播放。
- 支持播放停止。

完成标准：记录 LLM 首 Token、TTS 首帧和浏览器开始播放时间。

### P5：麦克风、VAD、ASR 和打断

目标：完成包含语音起止检测的实时语音聊天闭环。

工作内容：

- 浏览器采集麦克风。
- 明确浏览器到服务端的音频格式。
- 定义音频帧的顺序、时间和连接归属，避免不同输入的音频串流。
- 接入一个真实 VAD，检测 `speech_start` 和 `speech_end`。
- 使用 VAD 的 `speech_start` 触发低延迟打断候选，使用 `speech_end` 推进 ASR 最终结果。
- 接入一个 ASR，按实际实现定义最小契约。
- ASR 最终文本转换为 `ChatInput`。
- 新 Turn 能取消旧 Turn。
- 旧 LLM/TTS 的迟到结果被丢弃。
- 区分“浏览器本地停止播放”和“服务端取消当前 Turn”。

VAD 放在浏览器、服务端或两端配合，在 P5 根据真实音频格式、ASR Provider 的 endpointing 能力和实测延迟决定；本阶段之前不预设完整 VAD Provider Registry。

完成标准：连续麦克风音频能稳定识别语音起止，静音不创建 Turn，一段有效语音只创建一个 Turn；语音输入、回答、播放和 barge-in 有自动化集成测试。

### P6：Session 和 Context

目标：建立稳定的多轮聊天能力。

工作内容：

- 明确 Connection、Session、Turn 的生命周期。
- 建立有限 Dialogue Window。
- 区分用户消息、助手消息和 Tool 消息。
- 禁止连接级共享标记控制多个并发 Turn。

完成标准：连续对话和快速打断不会出现消息串轮。

### P7：Identity、Memory 和 User Profile

目标：建立家庭陪伴核心能力。

P7 的 Session、Device、Identity、Dialogue、Guest 和长期数据边界已进入独立讨论文档；已确认内容和待决策项见 [`docs/P7_DESIGN.md`](docs/P7_DESIGN.md)。该文档描述设计状态，不表示功能已经实现。

工作内容：

- 声纹结果映射到稳定 `user_id`。
- 未识别成员使用 Guest 身份。
- Memory Retrieval 设置超时、Top-K 和字符上限。
- Memory 查询使用真正异步 I/O，不阻塞主事件循环。
- 当前回答结束后后台保存长期记忆。
- 从对话中后台提取稳定画像，不把所有聊天直接当画像。
- Profile 保存字段来源、置信度和更新时间。

完成标准：不同家庭成员的 Context、Memory 和 Profile 不串数据；关闭 Memory 不影响聊天主链。

### P8：统一 Vision 输入

目标：实现一次输入、一个 Turn 的多模态聊天。

工作内容：

- Web 图片直接进入当前 `ChatInput.images`。
- 删除 `abort -> 拼接 listen/detect -> 再次聊天` 行为。
- 普通聊天可读取带时间戳的最近视觉缓存。
- 明确视觉问题才等待 VLLM。
- 视觉结果默认不写入长期 Memory。

完成标准：一句话和一张图片只产生一个 Turn，Memos 中不再自动出现临时画面描述。

### P9：少量 Tool

目标：增加真正有陪伴价值的能力。

首批范围：

- 当前时间和日期。
- 天气。
- 提醒。

要求：

- Tool 白名单。
- 每个 Tool 有超时。
- Tool 失败不破坏当前 Turn。
- Tool 结果是否进入 Memory 必须显式决定。

### P10：工程化和发布

目标：形成可重复部署的项目。

工作内容：

- Unit Test 和 Integration Test。
- Provider Contract Test。
- 配置和 Secret 分离。
- Health Check 和 Readiness Check。
- 结构化日志和性能指标。
- Docker 和启动文档。
- GitHub CI。
- 架构决策记录。

## 8. 测试策略

测试顺序固定为：

```text
Fake Provider 单元测试
-> 单 Part 集成测试
-> WebSocket 协议测试
-> 一个真实 Provider 冒烟测试
-> 浏览器端到端测试
```

必须覆盖：

- 一次输入只创建一个 Turn。
- 静音和短暂环境噪声不会创建 Turn。
- 一段有效语音只产生一次 `speech_start`、`speech_end` 和 ASR 最终输入。
- 打断后旧结果不能继续输出。
- Memory 超时不会阻止 LLM。
- Vision 失败能降级为纯文本聊天。
- Provider 失败不会导致连接资源泄漏。
- 不同用户的 Memory/Profile 不串数据。

## 9. 性能指标

每轮至少记录：

```text
input_received
audio_first_frame
vad_speech_start
vad_speech_end
asr_final
context_ready
memory_ready_or_timeout
llm_first_token
tts_first_frame
browser_play_start
turn_complete
```

文本输入不产生音频、VAD 和 ASR 指标；语音输入必须记录这些阶段，便于区分采集、端点检测、识别、模型和合成各自的延迟。

总延迟必须能拆解到具体阶段，不能只记录“这一轮用了 3 秒”。

普通对话中，Vision、Memory 保存、摘要和画像更新不得串行叠加在首包路径上。

## 10. Git 交付规则

- `main` 始终保持可运行。
- 每个 Part 使用独立分支。
- 一个 Part 对应一个可验证的 Pull Request。
- 每个 PR 必须写明行为变化、测试结果和性能变化。
- 不把真实 API Key、Token、声纹数据和用户画像提交到 Git。
- Legacy 目录必须加入 `.gitignore`。
- 未完成的抽象不提前合并到 `main`。

建议分支示例：

```text
codex/p1-bootstrap
codex/p2-text-turn
codex/p3-real-llm
codex/p4-tts-stream
codex/p5-asr-barge-in
codex/p7-identity-memory
codex/p8-vision-input
```

## 11. 第一执行批次

第一批只做：

1. 确认用户 GitHub 仓库 URL。
2. 初始化 `D:\Desktop\Newtalk` 独立 Git 仓库。
3. 排除 `.idea/` 和 `xiaozhi-esp32-server-main/`。
4. 建立最小 Python 项目和测试框架。
5. 完成 `/health`。
6. 完成 WebSocket `hello`。
7. 完成 Fake LLM 文本流式闭环。

第一批明确不接入 ASR、TTS、Vision、Memory、声纹和 Tool。

只有 P2 文本 Turn 稳定后，才进入第一个真实 LLM 的接口定义。
