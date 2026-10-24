# Newtalk 独立代码审查（CODE_AUDIT）

> 归档说明：以下保留审查时的原文、行号与测试基线，不代表当前代码状态。审查后已实现 ASR 队列防阻塞、删除/记忆重试调整、Alembic 元数据补全、Runtime Task 注释及删除与记忆写回协调；P0-2 中的 Dialogue 追加竞态等剩余工作仍需处理。当前限制以 [架构](../architecture.md) 为准，待办以 [路线图](../roadmap.md) 为准。文内旧路径均为审查时的仓库根目录相对路径，旧报告现位于 [同目录](2026-10-20-code-review.md)。

- 审查日期：2026-10-22
- 审查基线：`main` @ `b2c69a0`（工作区另有未提交的前端改动 `web/app.js`、`web/index.html`、`web/styles.css`、`docs/PROGRESS.md`，本报告不评价这些未提交改动）
- 范围：`src/newtalk`、`services/voiceprint`、`migrations`、`tests`，前端只做概要评估
- 方式：逐文件阅读核心链路，所有结论以当前代码为准；旧 `CODE_REVIEW_REPORT.md` 只作线索。关键 P0 用临时脚本实际复现（脚本放在仓库外，见附录 A）
- 测试基线：`.venv` 下 `pytest tests services/voiceprint/tests`：**174 passed, 10 skipped, 0 failed**，`src/newtalk` 语句覆盖率 **84%**（跳过项为未配置 PostgreSQL 的集成测试与 live Provider 测试）
- 本轮只审查，未修改任何代码

**一句话结论**：这不是“屎山”。整体分层清楚、命名一致、关键并发点有意识地加了锁和测试，比典型的 AI 长期堆叠项目好很多。真正的问题集中在少数几处：**WebSocket 接收循环里内联执行了可能长时间阻塞的 await**、**Turn 提交（持久化）发生在可被打断的任务里**、**成员删除与后台记忆写入之间缺少互斥**，以及 `ConnectionRuntime` 正在逼近“看不清哪个方法在哪个 Task 里执行”的临界点。

---

## 1. 当前架构摘要

### 1.1 启动入口

- `python -m newtalk` → `newtalk/__main__.py` → `newtalk.app.main()` → `uvicorn.run(app)`
- `newtalk.app` 在**模块导入时**执行 `load_config()` + `create_app()`（`app.py:377-381`），这是组合根（composition root）
- `create_app()` 按配置构造所有 Provider/Store，挂到 `app.state`，`lifespan` 负责 `start()`/`close()`
- 独立服务：`services/voiceprint`（CAM++ 声纹，独立 FastAPI，独立 Python 环境），通过 HTTP + Bearer Token 被主服务调用，并**直接读写主库 `identities.voiceprint_*` 列**

### 1.2 核心模块与职责

| 模块 | 职责 | 持有的状态 |
|---|---|---|
| `transport/websocket.py` | 设备鉴权、打开 Dialogue Session、发送 `hello`、接收循环、事件分发 | 无（每连接一个 `ConnectionRuntime`） |
| `transport/runtime.py` `ConnectionRuntime` | 单连接内的一切编排：发送队列、Turn 生命周期、打断、音频采集、声纹汇合、Turn 提交 | **连接级全部可变状态**（见 1.5） |
| `audio/session.py` `AudioInputSession` | PCM → VAD 切分 utterance → 每个 utterance 一个 ASR 任务；收集整段 PCM 交给声纹 | 当前 utterance 的队列、pre-roll、识别任务集合 |
| `audio/vad.py` | Silero ONNX 流式 VAD | 每条采集流的 recurrent state |
| `asr/doubao.py` / `tts/doubao.py` | 豆包二进制 WebSocket 协议；每 utterance / 每 Turn 一条 Provider 连接 | 无跨请求状态 |
| `chat/service.py` `ChatService` | LLM 流（含一次 `memory_search` 工具轮）+ 文本分句 + TTS 并行流，输出 `TurnOutput` 事件流 | 每 Turn 内的两个 producer task 与队列 |
| `chat/openai_compatible.py` | OpenAI 兼容流式客户端、工具调用聚合 | 无 |
| `chat/session.py` | `DialogueSession`（短期窗口）、`DialogueCacheCoordinator`（同进程同设备的窗口同步） | 内存窗口 |
| `chat/persistence.py` | `dialogue_sessions` / `dialogue_exchanges` 持久化（每设备一个 session，family/guest 两个 lane） | PostgreSQL |
| `identity/*` | 设备注册/鉴权/恢复、成员 CRUD、删除作业（outbox + worker）、恢复码限流 | PostgreSQL；删除 worker task |
| `memory/jobs.py` | 长期记忆写入 outbox + 单 worker（MemOS 异步任务轮询） | PostgreSQL；worker task |
| `memory/api.py` / `profile/memos.py` | Memory Center HTTP API；MemOS HTTP 协议 | 无 |
| `profile/session.py` | 每连接 Profile 快照缓存（后台预取）+ 进程内协调器 | 内存快照、加载 task |
| `voiceprint/client.py` | 主服务调用声纹服务的 HTTP 客户端 | httpx client |
| `config.py` | 环境变量解析与校验 | 冻结 dataclass |

### 1.3 依赖方向（基本健康）

```text
app ──> transport ──> runtime ──> chat.service ──> (LLM, TTS, memory.provider)
                         │──> audio.session ──> (VAD, ASR)
                         │──> voiceprint.client
                         │──> identity.service
                         │──> chat.session / chat.persistence
                         └──> memory.jobs (enqueue) , profile.session
identity.deletion ──> voiceprint.client, memory.provider, chat.persistence, chat.session(coordinator), profile.session(coordinator)
```

没有发现循环依赖。唯一值得注意的耦合：`chat.persistence`、`memory.jobs`、`identity.deletion`、`identity.recovery_limit` 都从 `identity.sqlalchemy_store` 借用 `Base`，各自 `create_async_engine`（共 5 个连接池，见 P2-5）。

### 1.4 边界

| 边界 | 位置 | 说明 |
|---|---|---|
| WebSocket（浏览器） | `transport/websocket.py` + `runtime.py` | JSON 控制帧 + 二进制 PCM（上行 16k/20ms，下行 TTS PCM）；协议见 `docs/protocol.md` |
| HTTP（浏览器） | `identity/api.py`、`voiceprint/api.py`、`memory/api.py` | HttpOnly 设备 Cookie 鉴权 |
| 数据库 | 5 个 SQLAlchemy Store | 7 张表，Alembic 迁移建表 |
| 外部 Provider | 豆包 ASR/TTS（WebSocket）、OpenAI 兼容 LLM（HTTP 流）、MemOS（HTTP）、VoicePrint 服务（HTTP） | 各自有超时，但**没有 Turn 级总时限** |

### 1.5 主要状态由谁管理

| 状态 | 所有者 | 生命周期 |
|---|---|---|
| 活动 Turn / 播放中的 stream | `runtime._TurnState`（受 `_turn_transition_lock` 保护） | 连接内 |
| 发送顺序与旧 Turn 帧过滤 | `runtime._OutboundSender`（读取 `_TurnState.turn_id`） | 连接内 |
| 当前采集 / utterance | `runtime._audio_session` + `AudioInputSession` | 采集内 |
| 声纹汇合 | `runtime._voice_joins: dict[utterance_id, _VoiceJoinState]` | utterance 内 |
| 短期对话窗口 | `DialogueSession`（内存）+ `dialogue_exchanges`（DB）+ `DialogueCacheCoordinator`（同进程广播） | 设备级，跨连接 |
| Profile 快照 | `SessionProfileCache` + `ProfileCacheCoordinator` | 连接内 |
| 长期记忆写入 | `memory_jobs` 表 + `MemoryWriteService` worker | 进程级 |
| 成员删除 | `identities.status` + `identity_deletion_jobs` + `IdentityDeletionService` worker | 进程级 |

---

## 2. 核心调用链

### 2.1 一次语音交互（最重要）

```text
[接收循环 Task]  websocket._receive_messages
  └─ runtime.push_audio(pcm)
      └─ AudioInputSession.push
          ├─ await active_queue.put(pcm)          ← 有界队列(128帧≈2.56s)，见 P0-1
          └─ VAD.process → speech_start
              ├─ create_task(_recognize)  ───────────────┐ [识别 Task]
              └─ await runtime._on_speech_boundary       │
                  ├─ send vad_speech_start               │
                  ├─ _voice_joins[uid] = _VoiceJoinState  │
                  └─ await cancel_turn("barge_in")        │  ← 在接收循环里等待旧 Turn 完全退出
          ... speech_end:                                │
              ├─ send vad_speech_end                      │
              ├─ queue.put(_AUDIO_END)                    │
              └─ runtime._on_utterance_audio              │
                  └─ create_task(voiceprint.identify) ─── [声纹 Task]
                                                          │
  [识别 Task]  DoubaoStreamingASR.stream ── partial ──> runtime._on_asr_event → send asr_partial
                                          ── final ───> runtime._on_asr_event
                                                          ├─ send asr_final
                                                          ├─ await _resolve_voice_speaker (≤ join_timeout 1.5s)
                                                          ├─ send speaker_resolved
                                                          └─ await start_turn(...)     ← 在识别 Task 里执行
                                                               (lock) cancel 旧 Turn → 构造上下文 → send turn_started
                                                               └─ create_task(_stream_turn) ─ [Turn Task]

[Turn Task]  runtime._stream_turn
  └─ ChatService.stream_turn
       ├─ [produce_text Task]  stream_reply → LLM（可能 1 次 memory_search 工具轮）→ TextDelta → 分句 → text_queue
       └─ [produce_audio Task] TTS.stream(text_queue) → AudioStarted / AudioFrame / AudioCompleted|AudioFailed
       └─ 两个 producer 都结束后才 yield TurnCompleted
  └─ TurnCompleted:
       ├─ await _can_commit_turn（DB 查 identity 是否仍 active）
       ├─ DialogueCacheCoordinator.commit（同步写入所有同设备连接的内存窗口）
       ├─ await dialogue_store.append（DB 事务）
       ├─ await memory_writer.enqueue_completed_turn（DB）
       └─ send turn_completed                                   ← 以上全部在可被打断的 Turn Task 内，见 P1-2

[发送 Task]  _OutboundSender._run：串行 send_json/send_bytes；带 turn_id 的帧若不属于当前活动 Turn 则丢弃
```

一条连接上同时存在 **接收循环、识别 Task、声纹 Task、Turn Task（及其两个 producer）、发送 Task、Profile 预取 Task**，而它们都会调用同一个 `ConnectionRuntime` 对象的方法。这是理解本项目的关键，也是可读性下降的根源（见第 7 节）。

### 2.2 文本交互

`text_input` → `text_chat.handle_text_input`（校验、去重）→ `runtime.start_text_turn`（DB 查成员）→ `start_turn` → 与语音相同的 Turn Task。

### 2.3 后台链路

- 记忆写入：`memory_jobs`（pending → processing → submitted(task_id) → completed/failed），`MemoryWriteService._run` 单 worker 轮询
- 成员删除：HTTP DELETE → `schedule()`（同一事务：identity 标记 `deletion_pending` + 插入 job）→ worker：声纹删除 → MemOS 记忆/Profile 删除 → Dialogue 删除 → 物理删除 identity（级联删 memory_jobs）

---

## 3. 当前做得好的地方

这些是真实优点，后续重构时应当**保留**：

1. **Turn 替换是串行的**：`start_turn` / `cancel_turn` 共用 `_turn_transition_lock`，`_TurnState.take_for_cancellation()` 先原子取走状态再 await，避免了孤儿 Task（`runtime.py:440-589`），并有并发测试。
2. **旧 Turn 输出过滤在发送点完成**：`_OutboundSender._run` 在真正写 socket 前比较 `turn_id`（`runtime.py:141-147`），这是正确位置，比在生产端检查更可靠。
3. **播放状态与生成状态分离**：`playback_turn_id/stream_id` 独立于活动 Turn，Turn 完成后浏览器仍在播放时，打断仍能发 `audio_stop`；浏览器 `playback_drained` 后清除。设计思路正确且有测试。
4. **Provider 协议层干净**：ASR/TTS 都是“收发分离 + finally 取消 sender + gather”模式，`aclosing` 用得一致，CancelledError 普遍正确地重新抛出。
5. **持久化细节扎实**：`dialogue_exchanges.turn_id` 唯一 + `ON CONFLICT DO NOTHING` 保证幂等；`append` 先 `FOR UPDATE` 锁 session 行再裁剪；删除调度在一个事务里完成；任务领取用 `FOR UPDATE SKIP LOCKED` + 租约；MemOS task_id 先持久化再轮询，避免重启重复提交。
6. **隐私边界清晰**：所有成员/记忆/声纹查询都以认证后的 `device_id` 限定；Turn 创建时冻结 speaker，迟到的声纹结果不能改变身份；提交前重新校验成员仍为 active，且异常时 fail-closed（`runtime.py:919-947`）。
7. **生命周期关闭链**：`_close_lifecycle_components` 逐个关闭并最终重抛首个异常（`app.py:61-80`）。
8. **注释质量**：中文注释大多解释“为什么”（如 `runtime.py:302-314`、`447-448`），不是复述代码。

旧报告中已经修复、本次确认不再存在的问题：Turn 替换竞态（旧 P0-1）、发送队列故障传播（旧 P0-2）、提交前身份校验 fail-open（旧 P1-5）、声纹决定 Dialogue 通道（旧 P1-3）、持久化失败仍写 Memory（旧 P1-4 的一半）、事件去重集合无上限（旧 P2-1）。

---

## 4. P0 问题

### P0-1：ASR 中途失败后，`AudioInputSession.push` 永久阻塞，整条 WebSocket 连接“假死”

- **置信度：HIGH（已复现）**
- 位置：`src/newtalk/audio/session.py:49-68`（`push`）、`118-128`（`_start_utterance`）、`162-191`（`_recognize`）；`src/newtalk/asr/doubao.py:90-132`
- 问题：
  1. `push()` 先 `await queue_at_start.put(pcm)`，**之后**才跑 VAD（`session.py:57-68`）。队列 `maxsize=128`（20ms 帧，约 2.56s）。
  2. 唯一的消费者是 ASR Provider 内部的 sender（`doubao.py:97, 143`）。ASR 任意失败（握手失败、服务端错误帧、30s 接收超时）时，`stream()` 的 finally 会取消 sender，`_recognize` 走到 `_on_asr_error`，**但 `AudioInputSession._active_queue` 仍指向这个已无人消费的队列**。
  3. 用户继续说话，队列填满后 `put()` 永久挂起。因为 VAD 在 put 之后，`speech_end` 永远不会被检测到，`_active_queue` 永远不会被清空。
- 后果：`push_audio` 运行在**接收循环**里（`websocket.py:155-158`），接收循环随之卡死：
  - 不再处理 `text_input`、`ping`、`audio_input_stop`、`close`
  - 不再调用 `websocket.receive()`，所以**连客户端断开都感知不到**，`runtime.close()` 永不执行；连接对象、Profile/Dialogue 协调器注册、声纹任务全部泄漏
  - 这与 `docs/architecture.md` 中“接收循环不再等待……连接可继续接收麦克风帧、ping、新文本和关闭事件”的承诺相反
- 触发条件：默认参数下（pre-roll 300ms、静音判定 600ms），ASR 不消费时用户说话超过约 1.6s 即触发。**网络抖动/豆包鉴权失败/配额耗尽**时几乎必然出现。另外，即使 ASR 最终成功，握手慢于约 1.6s 时接收循环也会被临时阻塞（非永久）。
- 复现：附录 A 脚本，输出 `push() blocked forever at frame 131 (~2620 ms of audio)`。
- 现有测试为什么没发现：`test_audio_session_reports_recognizer_failure` 和 `test_asr_failure_is_reported_without_closing_websocket` 的音频都很短（脚本化 VAD 只有两帧），没有在失败后继续推送音频。
- 是否值得修改：**必须修**。
- 修改方向（低风险）：
  - `_recognize` 结束（无论成功失败）时，如果 `self._active_queue is queue`，把当前 utterance 从 session 上摘除（置空 `_active_queue`/`_active_utterance_id`），后续帧只进 pre-roll 和 VAD；
  - 并且 `push()` 不应 `await` 一个可能永不被消费的队列：改为 `put_nowait`，`QueueFull` 时放弃本 utterance 并上报 `asr_failed`（实时语音场景丢帧优于阻塞整个连接）。
- 修改风险：低～中。需注意 `_finish_utterance` 仍要能对已摘除的 utterance 正确发送 `vad_speech_end` 和声纹音频。
- 需要的测试：ASR 在第 N 帧抛错后，继续推送 ≥ 300 帧，断言 `push` 不阻塞、之后能正常处理 `ping`/`audio_input_stop`/断开；握手慢（sender 延迟 3s 才开始消费）时接收循环仍响应 `ping`。

### P0-2：成员删除与后台记忆写入没有互斥，已删除成员的记忆可能被重新写回 MemOS

- **置信度：机制 HIGH，发生频率 MEDIUM**
- 位置：
  - `src/newtalk/memory/jobs.py:186-220` `SqlAlchemyMemoryJobStore.claim`：只按 `status`/`available_at` 领取，不关心成员状态
  - `src/newtalk/memory/jobs.py:401-461` `MemoryWriteService._run/_process/_submit_to_provider`：提交 MemOS 前不校验成员是否仍 active
  - `src/newtalk/identity/deletion.py:354-368` `IdentityDeletionService._process`：删除 MemOS 记忆前不处理该成员的 `memory_jobs`；这些行只在最后物理删除 identity 时被外键级联删掉
- 问题场景（任一即可）：
  1. 成员说完一句话，Turn 已入队 `memory_jobs`；用户立刻在界面删除该成员。删除 worker 先执行 `delete_all_memories`，随后记忆 worker 领取该 job 并 `add_completed_turn` → 记忆写回 MemOS。
  2. 记忆 job 处于失败退避（`fail()` 把状态改回 `pending` 并设 `retry_at`），期间删除完成清理 → 退避结束后写回。
  3. job 已是 `submitted`（MemOS 异步任务仍在运行），删除 worker 清理后，MemOS 任务才落库。
- 后果：删除完成后，MemOS 中 `newtalk_<device>_<identity>` 下仍有该成员的私人记忆，而本地已经没有任何记录指向它（job 行被级联删除），**无法再被发现或清理**。这是用户明确请求删除后的数据残留，属于隐私/数据正确性问题。
- 同类但更小的窗口：`runtime._can_commit_turn`（`runtime.py:919`）与随后 `dialogue_store.append`（`runtime.py:845`）之间是 check-then-act，删除 worker 恰好在两者之间执行 `delete_identity` 时，会插回一条已删除成员的 `dialogue_exchanges`（`speaker_identity_id` 无外键，`persistence.py:108`），永远不会再被清理。
- 是否值得修改：**值得**，且改动范围可控。
- 修改方向：
  - `claim()` 增加 `JOIN identities ... status='active'` 条件（或 `schedule()` 在同一事务里把该成员的未完成 `memory_jobs` 标记为 cancelled）；
  - 删除 worker 在清理 MemOS 前，等待/取消该成员 `submitted` 状态的 job，或在它们终结后再做一次 `delete_all_memories`；
  - Dialogue 追加可在 `append` 的同一事务里校验 identity 仍 active（两者在同一库，用一个 `EXISTS` 子查询即可）。
- 修改风险：中（涉及两个 worker 的交互，需要集成测试）。
- 需要的测试：入队 job → 请求删除 → 先跑删除 worker、再跑记忆 worker，断言 provider 在 `delete_all_memories` 之后**没有**收到该 scope 的 `add_completed_turn`；`submitted` 状态版本同理；PostgreSQL 集成测试版本。

> 未列入 P0 的候选：多进程部署下的协调器问题、恢复码限流按 IP 等，均缺少“当前部署形态下一定出错”的证据，放入 P2。

---

## 5. P1 技术债（建议优先处理）

### P1-1：接收循环内联执行“可能很慢的 await”，打断与停止录音会冻结整条连接

- **置信度：HIGH（stop 路径）/ MEDIUM（打断路径的实际时长）**
- 这是 P0-1 的根因类别，P0-1 修掉后仍然存在以下两处：
  1. `stop_audio_input`（`runtime.py:662-687`）→ `AudioInputSession.close()`（`session.py:87-95`）会**等待所有识别任务结束**。识别任务内部还会执行 `_on_asr_event` → `_resolve_voice_speaker`（≤1.5s）→ `start_turn`（取消旧 Turn、等待发送）。ASR 的接收超时是 30s（`config.py:33`），也就是说点“停止录音”后，接收循环最长可被阻塞 30s+，期间无法 ping、无法发文本、无法 close。旧报告 P1-2 指出过，**目前仍未修复**（只修了断开时的 `abort`）。
  2. 语音打断：`push` → `_on_speech_boundary` → `await cancel_turn("barge_in")`（`runtime.py:977`）在接收循环里等待旧 Turn Task 完全退出。旧 Turn 的退出包括 TTS Provider 的 finally：发送 `CANCEL_SESSION` 并退出 `async with connect(...)`（`tts/doubao.py:97-161`），后者会做 WebSocket 关闭握手（websockets 默认 close_timeout 10s）。Provider 无响应时，打断本身会让麦克风上行停顿数秒。
- 为什么是问题：实时语音系统的接收循环应当只做“快操作”；任何依赖外部服务的 await 放在这里，都会把外部故障放大成整条连接的故障。
- 修改方向：`stop_audio_input` 先回 `audio_input_stopped`，把“等待最终识别结果”交给后台并设总预算（超时后 `abort`）；打断时只需同步完成“状态切换 + 通知浏览器”，旧 Task 的清理可以放到后台 await（`_TurnState` 已经把旧 Task 摘出来了，这一步很自然）。
- 修改风险：中。需要保证“旧 Turn 不会再发帧”这一性质仍由 `_OutboundSender` 的 turn_id 过滤保证（它已经是这样）。
- 需要的测试：识别任务挂起时调用 `audio_input_stop`，断言紧随其后的 `ping` 在 100ms 内收到 `pong`；TTS 关闭握手挂起时打断，断言 `turn_cancelled` 及时送达且后续音频帧仍被处理。

### P1-2：Turn 提交（持久化 + 记忆入队）发生在可被打断的 Turn Task 内，打断会造成内存/DB/记忆三方不一致

- **置信度：机制 HIGH，频率 MEDIUM**
- 位置：`runtime.py:823-889`
- 时序：`TurnCompleted` 只有在 TTS **生成**完全部音频后才产生（`chat/service.py:354-365`），此时浏览器通常才播放了开头。正是用户最容易开口打断的时刻，Turn Task 却正在执行：
  1. `await _can_commit_turn`（DB 查询）
  2. `DialogueCacheCoordinator.commit`（同步，已写入所有同设备内存窗口）
  3. `await dialogue_store.append`（DB 事务）
  4. `await memory_writer.enqueue_completed_turn`（DB）
  5. `send turn_completed`
- 此时 speech_start → `cancel_turn("barge_in")` 会取消这个 Task：
  - 若取消发生在 (3)：**内存窗口已有这轮对话，DB 没有**，重连后上下文“消失”；
  - 若取消发生在 (3) 之后、(4) 之前：Dialogue 已持久化，但**长期记忆漏写**；
  - 客户端收到 `turn_cancelled`，但它已收到全部文本、正在播放全部音频，UI 状态与事实不符。
- 另外，`turn_completed` 必须等 2～3 次 DB 往返后才发出，给客户端的“完成”信号增加了不必要的延迟。
- 修改方向：收到 `TurnCompleted` 后，先把 Turn 从“生成中”转为“仅播放中”（`clear_active`，之后的打断只会产生 `audio_stop`），再把提交工作交给一个**不受打断影响**的提交步骤（独立的连接级提交任务，`close()` 时带预算等待）。这也是把提交逻辑从 `_stream_turn` 中提取出来的自然切口（见 7.1）。
- 修改风险：中。会改变“打断后是否提交”的可观察行为，需要和产品语义（`docs/architecture.md` “被打断 Turn 当前完全不提交”）一起确认：这里讨论的是**生成已完成**的 Turn，不是生成中被打断的 Turn。
- 需要的测试：用可阻塞的 fake `DialogueStore.append`，在其阻塞时触发 speech_start，断言内存窗口、DB、memory job 三者一致（全有或全无），且客户端事件序列明确。

### P1-3：成员删除作业失败后进入不可恢复状态

- **置信度：HIGH**
- 位置：`identity/deletion.py:372-382`（重试耗尽 → `failed`）、`deletion.py:130-160`（`schedule` 要求 `ACTIVE`）、`identity_deletion_jobs.identity_id` 唯一约束
- 问题：重试次数复用 `memory_job_max_attempts`（默认 3），退避 `min(2**attempts, 60)`，总预算只有约 6 秒。声纹服务或 MemOS 中断超过约 10 秒，删除作业即永久 `failed`；成员停留在 `deletion_pending`（列表与 API 都看不到它，`get_identity` 返回 404），用户无法重新发起删除，MemOS 数据与声纹模板永久残留。
- 修改方向：删除作业使用独立、更长的重试策略（或无限重试 + 上限退避 + 告警日志），并允许重新调度 `failed` 作业。`IdentityDeletionService` 构造参数目前复用 `memory_job_poll_seconds/max_attempts`（`app.py:290-291`），应拆开。
- 需要的测试：Provider 连续失败 4 次后恢复，断言删除最终完成。

### P1-4：记忆写入的重试策略：短故障丢数据、`submitted` 可能无限轮询

- **置信度：HIGH**
- 位置：`memory/jobs.py:215-216`（只有未提交时才计 attempts）、`463-494`（`_poll_provider`）、`510-520`（`_fail_submission`）
- 问题：
  1. 与 P1-3 相同的 3 次 / 约 6 秒预算：MemOS 抖动十几秒，这期间完成的成员对话**永久不进长期记忆**。
  2. `submitted` 状态下，状态查询异常或 MemOS 一直返回 `running`，每次都 `submit(check_at=now+poll)`，没有次数或时长上限。单 worker 串行处理，这类 job 会持续占用轮询并越积越多。
- 修改方向：区分“提交重试预算”和“状态轮询预算”（按 `created_at` 设最大存活时长）；退避参数可注入，顺便消除测试里 2 秒的真实等待。
- 需要的测试：`get_write_status` 永远 `running` / 永远抛错时，job 最终进入终态。

### P1-5：Alembic `env.py` 只能看到 2/7 张表，`--autogenerate` 会生成删表迁移

- **置信度：HIGH（已验证）**
- 位置：`migrations/env.py:10, 21` 只导入 `newtalk.identity.sqlalchemy_store.Base`。实测该导入后 `Base.metadata.tables == ['devices', 'identities']`；`memory_jobs`、`identity_deletion_jobs`、`dialogue_*`、`recovery_rate_limits` 的模型在其他模块注册，未被导入。
- 后果：任何人（包括 AI 助手）下次执行 `alembic revision --autogenerate` 都会得到 5 个 `drop_table`。若未仔细审阅即执行，会删掉生产数据。
- 另有模型与迁移的小漂移：3 个只存在于迁移的索引（`ix_memory_jobs_pending` 等）、2 处重复的唯一约束 + 唯一索引、若干状态列只有 Python 默认值没有 `server_default`。修好 env.py 后 autogenerate 会提议删除这些索引。
- 修改方向：`env.py` 显式导入所有模型模块（或建立统一的 `newtalk.db.models`）；把仅迁移中存在的索引写回 `__table_args__`；CI 中对迁移后的数据库执行 `alembic check`。
- 修改风险：低。

### P1-6：`ConnectionRuntime._stream_turn` 中的提交逻辑应先行提取

- **置信度：HIGH（可维护性）**
- 位置：`runtime.py:713-917`（约 205 行），其中 `TurnCompleted` 分支（`823-889`）嵌套 5 层，混合了：身份校验、多连接内存同步、lane 选择、DB 持久化、记忆入队、协议发送、日志。
- 为什么现在就值得做：P0-2、P1-2 的修复都落在这段代码上；先把它提取成一个可单测的 `TurnCommitter`（输入：`Turn` + 最终文本；输出：committed/persisted/enqueued 结果），修复与测试都会简单很多。其余部分（输出类型 → 协议帧的转换）可以暂时留在原处。
- 修改风险：低（纯提取，行为不变），前提是先补 P1-2 中描述的测试。

---

## 6. P2 优化项（可以后续处理）

| 编号 | 问题 | 证据 | 置信度 | 说明 / 建议 |
|---|---|---|---|---|
| P2-1 | 非 NotFound 的身份查询异常处理不一致 | `runtime.py:386-398` 只捕获 `IdentityNotFoundError`；`runtime.py:1090-1096` 同 | HIGH | 文本 Turn 时 DB 抖动 → 异常冒出接收循环，**整条 WebSocket 被关闭**；语音时异常冒到 `_recognize`，被误报为 `asr_failed`。建议降级为 Guest 或返回明确错误。 |
| P2-2 | 旧 utterance 的 Turn 可能在新 utterance 已开口后才启动 | `_on_asr_event` 在识别 Task 里 `await _resolve_voice_speaker` 再 `start_turn`（`runtime.py:1025-1055`），期间新的 speech_start 只取消“已存在”的 Turn | MEDIUM | 用户连续说两段时，第一段的回复可能在用户说第二段时开始播放，随后被第二段的 Final 取代。是否要合并/丢弃属于产品语义；技术上可用“utterance 代数”判断是否已过期。 |
| P2-3 | `_voice_joins` 在没有 Final/错误时不清理 | speech_start 创建（`runtime.py:976`），只在 Final/错误时 pop | LOW | 识别流无 Final 正常结束时（如 `FakeASR` 收到 0 字节），状态与声纹 Task 留到连接关闭。影响小。 |
| P2-4 | family/guest lane 是已废弃语义的兼容残留 | DB 两 lane 各裁剪 `max_turns`（`persistence.py:228-244`），内存是单窗口；`register(device_id, "family", ...)` 永远是 family（`runtime.py:314`）；`hello` 返回最多 2N 条而 LLM 只用 N 条 | HIGH | 无正确性 bug，但同一概念有两套模型，读代码时必须理解历史。等产品确认后用一次迁移去掉 lane。 |
| P2-5 | 同一进程 5 个 AsyncEngine | `persistence.py:129`、`deletion.py:118`、`recovery_limit.py:84`、`sqlalchemy_store.py:104`、`jobs.py:150` | HIGH | 默认每个池最多 15 个连接，单进程最多约 75 个；也使跨 Store 事务（P0-2 的理想修法）不可能。建议共享一个 engine/sessionmaker。 |
| P2-6 | 进程内协调器与“多 worker”假设冲突 | `DialogueCacheCoordinator`、`ProfileCacheCoordinator` 只在进程内；而 `SqlAlchemyRecoveryRateLimiter` 明确为多 worker 设计 | MEDIUM | 多进程部署时，删除成员不会清理其他进程活动连接的上下文/Profile。至少在文档中写明“单进程部署”约束。 |
| P2-7 | Profile 加载与删除的竞态 | `profile/session.py:58` `remove_snapshot` 不取消进行中的 `_load`，加载完成后再次 `set_snapshot`；`memos.py:49` 加载时可能重新绑定 Profile | MEDIUM | 与删除 worker 的 `delete_profile` 重叠时，MemOS 为已删除成员留下新 Profile。可在缓存里记录“已移除”集合。 |
| P2-8 | 内存版 Store 是“生产代码里的测试替身”，语义已漂移 | 内存删除 Store 不支持租约过期、`complete` 非幂等、`schedule` 非原子；`persistence_backend="memory"` 仍会创建 SQL 的 identity/memory job Store；SQL 删除 Store 内联重写了 `IdentityService.mark_identity_deletion_pending/delete_identity` 的逻辑 | HIGH | 单元测试测的是与生产不同的语义。建议把替身移到 `tests/`，或对两种实现运行同一套契约测试；删除重复的删除逻辑。 |
| P2-9 | `memory_jobs` 永久保存完整对话文本 | `jobs.py:116-117`，只在成员删除时清理 | HIGH | 第二份不受窗口限制的私人对话副本。完成/失败后清空文本或加 TTL。 |
| P2-10 | 关闭/崩溃时可能重复提交 MemOS | `add_completed_turn` 成功但 `store.submit` 前被取消（`close()` 直接 cancel worker），租约过期后重新提交 | MEDIUM | 文档已承认“极小窗口”，但 shutdown 取消让窗口变大。可在关闭时给当前 job 一个短暂完成预算。 |
| P2-11 | 安全加固项 | 恢复码限流以 `request.client.host` 为键（`identity/api.py:191`），反向代理后所有用户共享一个键，5 次失败即全体锁定；`POST /api/device` 无认证无限流；声纹服务直接运行时默认 token + `0.0.0.0` | MEDIUM | 当前本机/compose 部署下风险有限，上线前处理。 |
| P2-12 | `config.py` 用位置元组传递子配置 | `_parse_memory_config` 返回 11 元素元组（`config.py:553-650`），`from_mapping` 按位置解包，再按名字传给 `cls(...)`；新增一个配置要改 4 处 | HIGH | 同类型值（int/float）位置调换不会被类型检查发现。建议子解析函数直接返回 `dict` 供 `cls(**...)`，或拆成子 dataclass。配置本身数量（约 60 项）对应真实 Provider，未见明显无用项（`tts_audio_format` 只校验不使用，可删）。 |
| P2-13 | 应用在模块导入时创建 | `app.py:377-381` | HIGH | `import newtalk.app` 就会读取 `.env`、加载 ONNX、创建 DB engine；测试和工具脚本被迫承担副作用。改为 uvicorn factory（`create_app` + `--factory`）。 |
| P2-14 | 没有 Turn 级总时限 | LLM 只有 httpx 读超时（`openai_compatible.py:22-26`），TTS/ASR 是单次 recv 超时 | LOW | 持续慢速吐字的 Provider 可让一个 Turn 无限延长。可加整体 deadline。 |
| P2-15 | 测试组织 | `tests/test_websocket.py` 1274 行、无 `conftest.py`、模块级共享 `client`、约 15 处 sleep 驱动的时序、voiceprint/memory fake 在 3 个文件中各写一份 | HIGH | 见第 8 节。 |
| P2-16 | 前端 `web/app.js` 继续膨胀 | 912 行，`handleIncoming` 约 190 行的分支；`PcmPlayer`、`MicrophoneRecorder` 与 UI 同文件；十余个模块级可变 Map | MEDIUM | 本轮不是后端重点。按“协议客户端 / 播放器 / 录音器 / 视图”拆分时机由前端改动频率决定。 |

---

## 7. 大文件 / 高认知复杂度分析

| 文件 | 行数 | 结论 |
|---|---|---|
| `transport/runtime.py` | 1169 | **真正难维护**，但不应机械拆文件；按职责提取 2～3 个协作者 |
| `config.py` | 748 | 大但简单；问题在元组传参（P2-12），不在大小 |
| `memory/jobs.py` | 542 | 合理：outbox Store（SQL + 内存）+ worker，内聚 |
| `profile/memos.py` | 495 | 合理：一个外部 HTTP 协议的完整适配器 |
| `identity/deletion.py` | 398 | 中等：SQL/内存两份 Store + worker；内存 Store 应移出（P2-8） |
| `tests/test_websocket.py` | 1274 | 难维护：混合 6 类主题 + fake 定义 + 共享状态 |
| `web/app.js` / `styles.css` | 912 / 2583 | 前端热点，暂缓 |

### 7.1 `ConnectionRuntime`（`transport/runtime.py`）

**实际承担的职责**（按代码区域）：

1. 发送串行化与失败传播：`_OutboundSender`（61-175）
2. Turn/播放状态机：`_TurnState`（186-245）
3. 连接生命周期：`start` / `close`（338-341, 689-711）
4. 协议错误与事件去重：`send_error` / `remember_event`（353-376）
5. Turn 编排：上下文构造（Profile prompt + Dialogue 窗口）、创建、替换、取消（378-589）
6. 采集会话管理：`start/stop_audio_input`、`push_audio`（601-687）
7. Turn 输出 → 协议帧转换 + 时延统计（713-822, 890-917）
8. **Turn 提交**：身份复核、多连接同步、持久化、记忆入队（823-889, 919-947）
9. VAD/ASR 回调 → 协议帧（949-1057, 1139-1157）
10. **声纹汇合**：`_VoiceJoinState`、`_resolve_voice_speaker`、`_discard_voice_join`（247-262, 976-992, 1059-1137）
11. 清空对话（401-438）

**为什么难读（不是因为行数）**：

- **多执行上下文共享一个对象且未标注**：同一个对象的方法分别运行在接收循环（`push_audio`、`_on_speech_boundary`、`stop_audio_input`、`start_text_turn`）、识别 Task（`_on_asr_event` → `start_turn`）、Turn Task（`_stream_turn`）、发送 Task（`_OutboundSender._run`）中。阅读任何一个方法时，都必须先弄清“我现在在哪个 Task 里、可以 await 多久、谁持有锁”。P0-1、P1-1、P1-2 都源于这一点。
- 构造函数 17 个参数，其中 6 个只为提交/记忆服务。
- `_stream_turn` 约 205 行：6 种输出类型的 if/elif 链，`TurnCompleted` 分支嵌套 5 层。
- 约 170 行（约 15%）是结构化日志的格式参数，与业务逻辑交织。

**必须放在一起的**（拆开会让并发正确性更难证明）：

- `_TurnState` + `_turn_transition_lock` + `start_turn` / `_cancel_turn` + `_OutboundSender` 读取 `active_turn_id` 的过滤逻辑。这四者共同保证“旧 Turn 不会输出”，应留在同一个类里。

**可以提取的**（按收益排序）：

1. `TurnCommitter`（第 8 项）：纯业务，无 WebSocket 依赖，是 P0-2 / P1-2 的修复点。**收益最高、风险最低。**
2. `SpeakerResolver`（第 10 项）：`_voice_joins` 字典 + 汇合等待 + 身份解析，输入 utterance_id，输出 `_SpeakerResolution`。可独立测试超时、不可用、身份缺失等目前未覆盖的分支。
3. 上下文构造（第 5 项前半段，`start_turn` 中 451-496 行）：可以是一个纯函数，返回 `messages` 和 Profile 元数据。

**暂时不应拆的**：

- `_OutboundSender` 移到单独文件价值不大。它比必要的稍复杂：每帧创建一个 put Task，而且每个生产者都等待 `delivered`，所以队列实际上只起到“锁 + 失败传播”的作用。但它刚加固过、有测试，语义正确，不建议现在简化。
- 协议帧构造（各种 `send_json({...})`）。分散但直观，提取成 builder 只会多一层跳转。

**建议的第一步**：在类 docstring 或每个公开/回调方法上，用一行注释写明“运行在哪个 Task、是否持锁、可能阻塞多久”。这是零风险、最能降低认知负担的改动。

### 7.2 `config.py`

748 行中大部分是“读取 → 校验 → 默认值”的重复模式，单看每段都简单。真正的维护成本是 P2-12 中的位置元组：`AppConfig` 字段、子解析函数返回类型、解包、`cls(...)` 调用四处要同步修改。不需要拆文件，只需改变子解析函数的返回形式。

### 7.3 `tests/test_websocket.py`

1274 行、32 个测试，混合了协议基础、Dialogue、Profile、Memory、语音、打断六类主题，中间（262-372 行）定义了约 9 个 fake 类。模块级 `client = make_client()` 被多个测试共享，设备 Dialogue 状态会跨测试累积，测试结果依赖执行顺序。建议先引入 `conftest.py` 收拢 fake，再按主题拆文件。

---

## 8. 测试缺口

**总体评价**：测试数量多、覆盖率 84%，Provider 协议层和持久化逻辑测得不错。但**实时状态机最危险的路径恰好覆盖最少**，属于“测试很多，但关键状态机覆盖不足”。

`runtime.py` 中从未执行过的关键行（覆盖率报告）：

- 141-147：发送端丢弃旧 Turn 帧（项目最核心的安全网）
- 101-104、110-111、172-174：发送任务中途停止
- 652-683：整个 `stop_audio_input` 路径
- 706-708：`close()` 取消进行中的声纹任务
- 810-814：运行时处理 `AudioFailed`
- 1076-1082、1095-1096：声纹不可用/无效/失败、身份缺失

| 风险场景 | 状态 | 说明 |
|---|---|---|
| 新 Turn 替换进行中的 Turn | 已覆盖 | `test_concurrent_turn_replacement_leaves_no_orphan_stream_task`，但用 `TrackingRuntime` 替换了 `_stream_turn` |
| 打断发生在 LLM 流式输出中 / TTS 输出中 | 部分 | 现有测试的打断发生在首个 token 之前，或服务端已结束、仅浏览器在播放时 |
| 取消后不再收到旧 Turn 的帧（按 turn_id 与顺序断言） | **未覆盖** | 没有任何测试断言 `turn_cancelled` 之后不再出现旧 `turn_id` 的 `text_delta`/二进制帧 |
| 断开连接时所有 Task 被清理 | 弱 | `test_disconnect_during_stream_keeps_application_healthy` 只断言 `/health == 200` |
| ASR 失败后继续推送音频 | **未覆盖** | 即 P0-1 |
| `audio_input_stop` 时识别挂起 | **未覆盖** | 即 P1-1 |
| 提交过程中被打断 | **未覆盖** | 即 P1-2 |
| Provider 超时（ASR/TTS recv、LLM） | 未覆盖 | 只覆盖了声纹汇合超时 |
| LLM/TTS 在中途（已有输出后）失败 | 未覆盖 | 现有 fake 都在首个输出前抛错 |
| 删除与记忆写入 / Dialogue 追加的竞态 | **未覆盖** | 即 P0-2；`DialogueCacheCoordinator.remove_identity` 从未被执行 |
| 记忆 worker 在处理中被关闭 | 未覆盖 | |
| PostgreSQL 路径 | 默认跳过 | 7 个集成测试需要配置数据库；`sqlalchemy_store.py` 覆盖率 36% |

**测试质量问题**：

- 约 15 处依赖 sleep 的时序（如 `FakeLLM(chunk_delay_seconds=0.2)`），CI 负载高时可能抖动；`test_transport_runtime.py` 中用 Event/门控的写法更可靠，应推广。
- 有测试在测试线程用另一个事件循环修改服务状态，只因为 Store 是内存实现才能工作。
- 断言内部状态（`runtime._turn_state.task`、`_seen_event_ids`）。
- 重复的 fake：`make_client`、`guest()`、3 份声纹 fake、3 份记忆 fake、ASR/TTS 各一份 `FakeWebSocket`。

**建议优先补的 8 个测试**（同时作为重构保护网）：

1. ASR 失败后继续推送 ≥300 帧，接收循环仍响应 `ping`（P0-1）
2. 删除成员与记忆 worker 交错，断言删除后 provider 未收到写入（P0-2）
3. 真实流式输出中途打断：断言 `turn_cancelled`/`audio_stop` 之后没有旧 `turn_id` 的任何帧
4. `append` 阻塞时打断：内存窗口、DB、memory job 三者一致（P1-2）
5. 识别挂起时 `audio_input_stop`：`ping` 仍及时响应（P1-1）
6. 断开时断言 Turn Task、两个 producer、声纹 Task、识别 Task 全部结束
7. TTS 在 `audio_start` 之后失败：执行 runtime 的 `AudioFailed` 分支
8. 删除作业 / 记忆作业在 Provider 连续失败后最终收敛（P1-3、P1-4）

---

## 9. 不建议现在重构的地方

| 对象 | 理由 |
|---|---|
| `_OutboundSender` 的实现方式 | 有更简单的写法，但语义正确、刚加固过、有测试，改动收益小于风险 |
| 豆包 ASR 每个 utterance 新建一条连接 | 属于官方协议模型，没有容量数据前不改 |
| family/guest lane 的数据模型 | 需要产品确认 + 数据迁移，先在文档中标为兼容残留即可 |
| “生成中被打断的 Turn 不写入上下文”的语义 | 属于产品决定。P1-2 只处理**生成已完成**的 Turn |
| VoicePrint 单模型推理锁 | 需要实际并发数据 |
| `profile/memos.py`、`asr/doubao.py`、`tts/doubao.py` 的结构 | 大小与协议复杂度相称，内聚良好 |
| 前端大文件拆分 | 不阻塞后端正确性；等前端改动频率再决定 |
| 全面引入 DI 框架 / 事件总线 / 状态机库 | 不需要。当前问题靠“明确 Task 归属 + 提取 2～3 个协作者”就能解决 |

---

## 10. 推荐重构顺序

每一步都应是独立 PR，先补测试再改代码。

**Phase 0：补保护网（不改业务代码）**

- 补第 8 节测试 3、6、7（这三个在当前代码上应该能通过，用来冻结现有正确行为）
- 引入 `tests/conftest.py`，收拢重复的 fake
- `migrations/env.py` 导入全部模型 + CI 执行 `alembic check`（P1-5，几乎零风险，可以与本阶段合并）

**Phase 1：修 P0**

- P0-1：`AudioInputSession` 识别结束时摘除队列，`push` 不再 await 可能永不被消费的队列（附带测试 1）
- P0-2：记忆 job 领取 / 提交时校验成员仍 active；删除调度时取消该成员未完成的 job；Dialogue 追加在事务内校验成员状态（附带测试 2）

**Phase 2：实时链路的 P1**

- 提取 `TurnCommitter`（P1-6，纯提取）
- 在此基础上修 P1-2：Turn 完成后先转为“仅播放中”，提交不受打断影响（测试 4）
- P1-1：`audio_input_stop` 与打断不再在接收循环里等待外部清理（测试 5）
- 给 `ConnectionRuntime` 各方法标注运行所在的 Task

**Phase 3：后台作业**

- P1-3 / P1-4：删除作业与记忆作业使用独立重试策略；`submitted` 状态设轮询上限；退避参数可注入（测试 8）
- P2-9：`memory_jobs` 文本保留策略

**Phase 4：结构整理（按需）**

- 提取 `SpeakerResolver`（P2-1 一并修复）
- 共享 DB engine（P2-5）；`config.py` 子解析函数改为返回 dict（P2-12）；app factory（P2-13）
- 内存 Store 移入 `tests/` 或接入契约测试（P2-8）
- 按主题拆分 `test_websocket.py`

---

## Owner Reading Path

按顺序阅读，总计约 1500 行核心代码。目标是能自己回答：**“这段代码运行在哪个 Task 里？它 await 的时候谁可能改动共享状态？”**

| # | 位置 | 重点理解 |
|---|---|---|
| 1 | `src/newtalk/app.py` `create_app`（223-374） | 组合根：有哪些组件、哪些是后台 worker、`lifespan` 的启动/关闭顺序。留意 5 个 SQL Store 各建一个 engine |
| 2 | `src/newtalk/transport/websocket.py` `websocket_endpoint` + `_receive_messages`（19-177） | **接收循环里调用的每个 runtime 方法都会阻塞整个连接**。这是理解 P0-1/P1-1 的前提 |
| 3 | `runtime.py` `ConnectionRuntime.__init__` + `_TurnState`（186-336） | 连接级状态有哪些，谁拥有。`take_for_cancellation` 为什么要先取走再 await |
| 4 | `runtime.py` `start_turn` / `_cancel_turn`（440-589） | 锁保护的是什么；替换 Turn 时的事件顺序（`turn_cancelled` → `audio_stop` → `turn_started`） |
| 5 | `runtime.py` `_OutboundSender.send` / `_run`（61-175） | 为什么在发送点按 `turn_id` 过滤；`delivered` future 如何把发送失败传回调用者 |
| 6 | `src/newtalk/chat/service.py` `stream_turn`（249-369） | 两个 producer + 两个队列；为什么 `TurnCompleted` 要等 TTS 生成完；取消如何传递到 producer |
| 7 | `chat/service.py` `stream_reply` + `_execute_memory_search`（118-247） | 工具调用的一轮限制；Guest 不能读记忆的双重保护 |
| 8 | `runtime.py` `_stream_turn` + `_can_commit_turn`（713-947） | 输出 → 协议帧；`TurnCompleted` 分支里的提交顺序，以及它为什么会被打断（P1-2） |
| 9 | `src/newtalk/audio/session.py` 全文（210 行） | utterance 切分、pre-roll、识别 Task 的创建与回调；`push` 中 put 与 VAD 的顺序（P0-1） |
| 10 | `runtime.py` `_on_speech_boundary` → `_on_utterance_audio` → `_on_asr_event` → `_resolve_voice_speaker`（949-1137） | 语音 Turn 是在**识别 Task** 里创建的；声纹汇合的期限与降级为 Guest 的路径 |
| 11 | `src/newtalk/asr/doubao.py` `stream` / `_send_audio`（63-167） | Provider 层统一的“收发分离 + finally 取消”模式；失败时谁停止消费音频 |
| 12 | `src/newtalk/chat/session.py` 全文 + `chat/persistence.py` `open` / `append`（140-250） | 一个设备一个 session；内存窗口与 DB 两个 lane 的关系；`FOR UPDATE` 串行化 |
| 13 | `src/newtalk/memory/jobs.py` `MemoryWriteService._run` → `_process` + `SqlAlchemyMemoryJobStore.claim`（186-220, 401-520） | outbox 状态机：pending → processing → submitted → completed/failed；租约；重试预算 |
| 14 | `src/newtalk/identity/deletion.py` `schedule` + `IdentityDeletionService._process`（130-160, 340-390） | 删除是“先隐藏、后台清理、最后物理删除”；它和第 13 步的 worker 之间没有协调（P0-2） |
| 15 | `tests/test_transport_runtime.py` + `tests/test_websocket.py` 中 `test_vad_speech_start_*` 两个测试 | 现有测试怎样驱动并发场景；用 Event 门控而不是 sleep 的写法 |

---

## 附录 A：P0-1 复现脚本

放在仓库之外运行：`.venv/Scripts/python.exe -I repro_asr_stall.py <repo>/src`

```python
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from newtalk.audio.session import AudioInputSession
from newtalk.audio.model import VadEvent

class AlwaysSpeechVad:          # 模拟用户持续说话
    def __init__(self): self.started = False
    def process(self, pcm):
        if not self.started:
            self.started = True
            return [VadEvent("speech_start", 0.9, 0.0)]
        return []
    def flush(self): return []
    def reset(self): pass

class FailingASR:               # 模拟 Provider 错误 / recv 超时
    async def stream(self, audio_chunks, *, utterance_id):
        n = 0
        async for _ in audio_chunks:
            n += 1
            if n == 3:
                raise RuntimeError("provider error")
        yield None

async def noop(*a): pass
async def on_err(uid, exc): print("asr error delivered:", exc)

async def main():
    s = AudioInputSession(vad_stream=AlwaysSpeechVad(), recognizer=FailingASR(),
                          on_boundary=noop, on_asr_event=noop, on_asr_error=on_err)
    for i in range(400):
        try:
            await asyncio.wait_for(s.push(b"\x00" * 640), timeout=1.0)
        except TimeoutError:
            print(f"push() blocked forever at frame {i} (~{i*20} ms of audio)")
            return
    print("no stall")
asyncio.run(main())
```

实际输出：

```text
asr error delivered: provider error
push() blocked forever at frame 131 (~2620 ms of audio)
```

## 附录 B：与旧 `CODE_REVIEW_REPORT.md` 的对照

| 旧编号 | 当前状态 |
|---|---|
| P0-1 Turn 替换孤儿 Task | 已修复（锁 + `_TurnState`），有测试 |
| P0-2 sender 退出后永久等待 | 已修复（`asyncio.wait` + terminal error），有测试 |
| P1-2 音频 Session 关闭被识别拖住 | **断开路径已修（`abort`），`audio_input_stop` 路径仍存在** → 本报告 P1-1 |
| P1-3 声纹决定 Dialogue 通道 | 已修复（设备级共享窗口）；lane 残留 → P2-4 |
| P1-4 内存提交与持久化不一致 | 持久化失败已跳过 Memory；**打断导致的不一致是新问题** → P1-2 |
| P1-5 身份校验 fail-open | 已修复（fail-closed） |
| P1-12 恢复码限流单进程 | 已提供 PostgreSQL 实现；按 IP 取键的问题 → P2-11 |
| P1-13 VoicePrint 默认 token | compose 已缓解；直接运行仍有风险 → P2-11 |
| P2-1 去重集合无上限 | 已修复（4096 上限） |
