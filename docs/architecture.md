# P7.5 Architecture

P7.5 在 Profile Snapshot 基础上增加按需长期记忆检索和后台写入。Profile 仍由连接后台预取；普通 Turn 不查询 MemOS，只有 Member 的主 LLM 主动调用 `memory_search` 时才进入检索链。

```text
Browser HTTP -> Device/Member API -> IdentityService -> IdentityStore
                |                                    |-> PostgreSQL (runtime)
                `-> HttpOnly device cookie           `-> InMemory (unit tests only)

Browser 3 x WAV -> authenticated member VoicePrint API
              -> HttpVoicePrintClient -> internal VoicePrint FastAPI
                   -> validate PCM WAV -> CAM++ Embedder -> normalized average
                   -> device_id + identity_id scoped PostgreSQL template

Browser cookie -> authenticated WebSocket
       -> SessionProfileCache -> background MemosProfileProvider
              |                    |-> get Profile
              |                    `-> bind missing Profile Template
              `-> identity_id scoped immutable Snapshot
Browser text_input + selected identity -----------+
Browser microphone -> recorder AudioWorklet       |
       -> 16k mono PCM -> WebSocket binary        |
                                                   v
WebSocket receive loop -> ConnectionRuntime
                              |-> current Member Profile Snapshot
                              |-> Family Dialogue / Guest Dialogue -> immutable Turn
                              |                                         `-> ChatService
                              |                                              |-> LLM direct text -> TTS
                              |                                              `-> memory_search Tool
                              |                                                   -> MemOS Search
                              |                                                   -> second LLM round -> TTS
                              `-> AudioInputSession
                                   |-> per-capture Silero state
                                   |-> SpeechRecognizer -> ASR partial/final -----+
                                   `-> utterance PCM -> VoicePrint identify ------+
                                                                                  |
                                      utterance_id join -> Member or Guest --------+

all server output -> one ConnectionRuntime send queue -> WebSocket

completed current Member Turn
       -> PostgreSQL memory_jobs Outbox
       -> background MemoryWriteService
       -> MemOS Add Message (async mode)
```

## 当前职责

- `newtalk.app` 是组合入口，按配置选择 ChatService、Silero VAD、Fake ASR 或豆包 ASR，并关闭有生命周期的 Provider。
- `newtalk.memory.provider.MemoryProvider` 是 P7.5 实际需要的最小 Memory 契约，包含 Profile 准备、长期记忆查询和已完成 Turn 写入；默认实现关闭。
- `newtalk.profile.memos.MemosProfileProvider` 当前同时实现上述契约，负责 Profile Template 懒绑定、Search Memory 和 Add Message 的 HTTP 协议细节。
- `newtalk.chat.service.ChatService` 只给 Member 注册 `memory_search`，每个 Turn 最多执行一次，并使用 Turn 内不可变身份建立服务端 Scope。
- `newtalk.memory.jobs.MemoryWriteService` 消费 PostgreSQL Outbox；本地入队按 `turn_id` 去重，任务领取使用租约，远端失败有限重试。
- `newtalk.profile.session.SessionProfileCache` 在连接开始后异步预取成员 Profile，按 `identity_id` 保存 Snapshot；Turn 读取只访问内存，不等待 MemOS。
- `newtalk.identity.api` 处理 Device Cookie、恢复限速和成员 HTTP API，不向浏览器暴露凭据摘要。
- `newtalk.identity.service.IdentityService` 生成设备标识、凭据和恢复码，并编排认证、恢复和成员操作。
- `newtalk.identity.store.IdentityStore` 是持久化契约；正式进程使用异步 SQLAlchemy/PostgreSQL，内存实现只用于自动测试。
- Alembic migration 是 schema 唯一建立方式，应用启动不会隐式创建数据表。
- `newtalk.voiceprint.client` 是主服务需要的最小内部 HTTP 契约；除录入和删除外，也负责提交当前 utterance WAV 并返回匹配身份、分数和耗时。
- `services/voiceprint` 独立加载 CAM++、校验 16k PCM WAV、串行保护模型推理并保存模板，不进入主服务 Python 环境。
- VoicePrint 只查询请求 `device_id` 下 Active Identity 的模板；同名成员和其他家庭不会进入候选集。
- `web/voiceprint-recorder.js` 复用 AudioWorklet 重采样链，录音前停止聊天麦克风和 TTS 播放。
- `newtalk.transport.websocket` 只接受连接、解析帧类型和分派协议事件。
- `newtalk.transport.runtime.ConnectionRuntime` 保存单连接的 Family/Guest Dialogue、活动 Turn、utterance 汇合状态、采集会话、任务和发送队列。
- `newtalk.chat.session.DialogueSession` 保存成功完成的用户/助手交换；Member 共用 Family Dialogue，Guest 使用独立 Dialogue，历史用户消息携带说话人标签。
- `newtalk.chat.models.Turn` 固定保存 `device_id`、`speaker_identity_id` 和显示信息；`messages` 是创建 Turn 时的不可变上下文快照。
- `newtalk.audio.session.AudioInputSession` 把连续 PCM 切成 utterance，维护 pre-roll，并把语音段交给 ASR。
- `newtalk.audio.vad.SileroVadStream` 保存每条采集流独立的 ONNX recurrent state、双阈值、滑窗和静音结束状态。
- `newtalk.asr.model.SpeechRecognizer` 只定义真实调用需要的音频流输入与 partial/final 输出。
- `newtalk.asr.doubao.DoubaoStreamingASR` 负责豆包鉴权、二进制协议、100ms PCM 分包和并发收发，不负责 VAD、Turn 或聊天。
- `web/mic-recorder-worklet.js` 重采样并切 20ms 帧；`web/pcm-player-worklet.js` 继续负责播放。

## 并发和取消

WebSocket 接收循环不再等待整个回复结束。每个 Turn 在独立 task 中运行，所以连接可继续接收麦克风帧、ping、新文本和关闭事件。

每条连接只有一个发送 task。LLM、TTS、VAD 和 ASR 产生的输出都先进入同一队列，避免多个协程同时写 WebSocket。队列项可携带 `turn_id`；旧 Turn 被取消后，尚未发送的迟到项会被丢弃。

`speech_start` 只负责打断，不创建 Turn；`asr_final` 才创建新 Turn。这保证一段用户语音不会因多个 VAD 帧或 ASR partial 创建多个对话轮次。

声纹使用同一 utterance 的完整 PCM。ASR Final 到达后只等待配置的有限期限；匹配成功后创建 Member Turn，超时、低分、音频过短、服务不可用或身份已删除均创建 Guest Turn。Turn 一旦创建，迟到声纹不能改变其身份。

只有当前活动 Turn 成功产生 `TurnCompleted` 时才提交 Dialogue History，并在提交后发送 `turn_completed`。取消、生成失败和旧 Turn 迟到结果不会提交，因此下一轮不会看到半截助手回复。

同一个提交点还会把 Member Turn 写入 `memory_jobs`。Runtime 只等待一次本地数据库入队，不等待 MemOS；Guest 不进入队列。多个 Worker 通过 PostgreSQL `FOR UPDATE SKIP LOCKED` 竞争任务，租约过期后可由其他 Worker 恢复。该链路提供本地至少一次投递，MemOS 接收成功但本地完成标记失败时仍可能产生远端重复，后续需要结合真实 MemOS 幂等行为验收。

主 LLM 调用 `memory_search` 时会增加一次 MemOS 查询和第二次 LLM 调用。查询超时或失败被转换为 Tool Result，第二轮模型仍可根据 Dialogue/Profile 回答；Tool Call 及 Tool Result 不进入 TTS，只有最终文本播放。

## 当前边界

- 豆包 ASR 每个 utterance 新建一条 Provider WebSocket，尚未复用连接。
- `enable_nonstream=false`，本地 Silero 仍是当前语音边界和打断的唯一判定来源。
- 输入固定为 16kHz、16-bit、单声道 PCM，输出继续采用 TTS 配置的 PCM 采样率。
- 浏览器启用系统回声消除、降噪和自动增益；服务端 AEC 尚未实现，真实扬声器场景仍需手工测试。
- Silero 模型固定为 v6.2.1 并随仓库保存，运行时不联网下载。
- 浏览器“停止播放”仍是本地操作；`audio_stop` 才表示服务端 Turn 已取消。
- 声纹录入、识别、`speaker_identity_id` 映射和 Guest 降级已进入主链。
- `deterministic-test-v1` 只供 CI；真实 CAM++ 已完成模型加载和录入，识别分数、阈值与有限等待期限仍需在家庭样本中校准。
- Profile Snapshot、`memory_search` 和后台长期记忆写入已进入 Member 运行链；Memory Center、Vision 和其他实用 Tool 尚未实现。
- Session 当前与 WebSocket 连接同生命周期，刷新页面后历史清空；跨连接恢复在 P7.7 实现。
- 当前消息角色为 `system`、`user`、`assistant` 和内部 `tool`；WebSocket 不暴露中间 Tool 消息。
- Profile 预取失败在当前 Session 内不会持续重试；该轮及后续轮次按无 Profile 聊天，重新连接后可再次预取。
- 真实 MemOS Search/Add/Profile 尚未使用用户账号完成端到端验收，当前外部协议结论来自官方文档和 Mock 测试。
