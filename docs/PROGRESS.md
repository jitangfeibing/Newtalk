# Newtalk 项目进度

本文记录当前可运行、可验收的真实状态。历史实现细节由 Git 提交和 Pull Request 保存，避免在仓库中维护多份过期交接材料。

## 当前状态

| 项目 | 状态 |
| --- | --- |
| 当前阶段 | P7.8 已交付 |
| 项目版本 | 0.13.0 |
| 更新时间 | 2026-10-20 |
| 主服务 | FastAPI / Uvicorn，默认 8006 |
| VoicePrint | 独立 FastAPI 服务，默认 8010 |
| 数据库 | PostgreSQL + SQLAlchemy + Alembic |
| Web | 原生 HTML / CSS / JavaScript |
| CI | GitHub Actions |
| 最近交付 | PR #21，合并提交 `2d61f68` |
| 自动测试基线 | 主项目 165 passed / 9 skipped；VoicePrint 9 passed / 1 skipped |

## 里程碑

| 阶段 | 已交付内容 |
| --- | --- |
| P1 | 独立仓库、配置、日志、`/health`、WebSocket hello 和最小 Web |
| P2 | 文本 Turn、Fake LLM、流式事件、取消与基础 Dialogue |
| P3 | OpenAI-compatible 真实流式 LLM |
| P4 | 豆包流式 TTS、浏览器 PCM 边收边播、首字/首包时延日志 |
| P5 | 浏览器 PCM 上行、VAD、语音段管理和双向流式 ASR |
| P6 | Dialogue 上下文与会话内多轮对话 |
| P7.1 | PostgreSQL、Device、Family、Member 和数据隔离 |
| P7.2 | 独立 3D-Speaker CAM++ VoicePrint 服务 |
| P7.3 | ASR 与声纹并行汇合、成员映射与 Guest 降级 |
| P7.4 | Profile 与 MemOS Provider 边界 |
| P7.5 | `memory_search`、长期记忆读取和异步写入 |
| P7.6 | Memory Center，查看、搜索、编辑、删除和锁定 |
| P7.7 | Session/Dialogue 持久化与页面刷新恢复 |
| P7.8 | 产品界面整理、新对话清理、真实语音打断和逐 Turn 时延日志 |

## 当前运行链路

语音输入主链路：

```text
Browser PCM
  -> WebSocket
  -> VAD
  -> Doubao streaming ASR
  -> VoicePrint identity resolution
  -> Dialogue + Profile
  -> OpenAI-compatible LLM
  -> Doubao streaming TTS
  -> Browser playback
```

长期记忆链路：

```text
Member Turn completed
  -> Memory Job outbox
  -> MemOS Add Message
  -> remote task polling
  -> completed / failed
```

主 LLM 可按需调用 `memory_search`。普通聊天不强制等待 MemOS，Guest 不加载或写入成员长期记忆。

## 已验收能力

- 文本与语音均可完成流式问答。
- 豆包 ASR/TTS 和 DeepSeek/OpenAI-compatible LLM 可真实调用。
- 页面刷新后恢复当前设备的短期 Dialogue。
- “新对话”清除当前 Session 的后端 Dialogue 与前端消息，不删除成员、声纹、Profile 和长期记忆。
- AI 播放期间用户开口会触发 interruption，取消当前 TTS/Turn，并开始识别新语音。
- 声纹服务以 CAM++ 启动时可以录入模板并识别成员。
- Memory Center 可以管理 Profile 和 MemOS 记忆。
- 日志可以查看 ASR 首结果、身份解析、LLM 首字、TTS 首包和播放完成时延。
- 不同 Device/Family 的持久化数据按数据库关系隔离。
- 同一 Device 内 Member/Guest 共用短期 Dialogue；声纹降级不会切断最近上下文，Profile/Memory 仍按成员隔离。

## 已知边界

### 被打断 Turn 不写入上下文

当前只有完整完成的 Turn 才提交 Dialogue。取消、失败和播放中被打断的 Turn 不恢复、不进入后续 Prompt。这保证了状态简单，但可能使“英文呢”“继续”等下一轮追问缺少指代对象。

是否保存用户问题及已播放回复片段仍需确定，见后续规划 B2。

### 音频工程化仍有限

- 浏览器具备基础回声消除约束，但没有工程级 AEC 与复杂环境稳定性保证。
- VAD 阈值当前为固定配置，没有自动噪声校准。
- 短语音可能因有效音频不足收到 VoicePrint 422，需要更好的质量提示与补帧策略。
- VoicePrint 单模型实例的并发容量尚未完成压测。

### 外部服务边界

- MemOS、豆包和 LLM 均为外部依赖，需要正确配置和网络可用。
- Memory Job 已跟踪远端任务状态；若进程恰好在 MemOS 接收请求后、保存 `task_id` 前崩溃，因官方接口没有明确幂等键，仍存在极小概率重复投递。
- 豆包 ASR 每个 utterance 建立独立 WebSocket 连接，当前没有连接池复用。

## 前端状态

P7.8 已完成第一版可用界面：

- 首页
- 对话
- 家庭成员与声纹录入
- Profile 与 Memory Center
- 服务状态、空状态与错误提示
- 新对话、播放控制和说话人选择

当前前端仍为单页原生 Web，实现以现有 API 为准。后续界面调整应优先服务于新的后端状态，不在没有明确收益时迁移框架。

## 下一步

优先讨论并确认：

1. 被打断 Turn 应保存哪些内容。
2. 音频质量诊断、动态 VAD 与 AEC 的投入范围。
3. VoicePrint 云端容量目标。

Vision、生产 CD 和更复杂的意图路由均不是当前自动进入开发的阶段。
