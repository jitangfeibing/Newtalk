# P7.3 Architecture

P7.3 将独立 VoicePrint 识别结果接入语音 Turn，并建立 Member Family Dialogue 与 Guest Dialogue 的明确边界。

```text
Browser HTTP -> Device/Member API -> IdentityService -> IdentityStore
                |                                    |-> PostgreSQL (runtime)
                `-> HttpOnly device cookie           `-> InMemory (unit tests only)

Browser 3 x WAV -> authenticated member VoicePrint API
              -> HttpVoicePrintClient -> internal VoicePrint FastAPI
                   -> validate PCM WAV -> CAM++ Embedder -> normalized average
                   -> device_id + identity_id scoped PostgreSQL template

Browser cookie -> authenticated WebSocket
Browser text_input + selected identity -----------+
Browser microphone -> recorder AudioWorklet       |
       -> 16k mono PCM -> WebSocket binary        |
                                                   v
WebSocket receive loop -> ConnectionRuntime
                              |-> Family Dialogue / Guest Dialogue -> immutable Turn
                              |                                         `-> ChatService -> LLM/TTS
                              `-> AudioInputSession
                                   |-> per-capture Silero state
                                   |-> SpeechRecognizer -> ASR partial/final -----+
                                   `-> utterance PCM -> VoicePrint identify ------+
                                                                                  |
                                      utterance_id join -> Member or Guest --------+

all server output -> one ConnectionRuntime send queue -> WebSocket
```

## 当前职责

- `newtalk.app` 是组合入口，按配置选择 ChatService、Silero VAD、Fake ASR 或豆包 ASR，并关闭有生命周期的 Provider。
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

## 当前边界

- 豆包 ASR 每个 utterance 新建一条 Provider WebSocket，尚未复用连接。
- `enable_nonstream=false`，本地 Silero 仍是当前语音边界和打断的唯一判定来源。
- 输入固定为 16kHz、16-bit、单声道 PCM，输出继续采用 TTS 配置的 PCM 采样率。
- 浏览器启用系统回声消除、降噪和自动增益；服务端 AEC 尚未实现，真实扬声器场景仍需手工测试。
- Silero 模型固定为 v6.2.1 并随仓库保存，运行时不联网下载。
- 浏览器“停止播放”仍是本地操作；`audio_stop` 才表示服务端 Turn 已取消。
- 声纹录入、识别、`speaker_identity_id` 映射和 Guest 降级已进入主链。
- `deterministic-test-v1` 只供 CI；真实 CAM++ 已完成模型加载和录入，识别分数、阈值与有限等待期限仍需在家庭样本中校准。
- 长期 Memory、Profile、Vision 和 Tool 仍未进入当前运行链。
- Session 当前与 WebSocket 连接同生命周期，刷新页面后历史清空；跨连接恢复和长期 Memory 尚未实现。
- 当前消息角色只有 `user` 和 `assistant`；Tool 消息等到 P9 出现真实 Tool 调用时再扩展契约。
