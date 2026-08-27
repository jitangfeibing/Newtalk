# P6 Architecture

P6 在完整语音链路上加入连接级 Session 和有限 Dialogue Context。Session 历史属于 `ConnectionRuntime`，不会放进全局 `ChatService`，因此并发 WebSocket 不共享上下文。

```text
Browser text_input -------------------------------+
Browser microphone -> recorder AudioWorklet       |
       -> 16k mono PCM -> WebSocket binary        |
                                                   v
WebSocket receive loop -> ConnectionRuntime -> DialogueSession -> message snapshot
                              |                `-> one active Turn -> ChatService -> LLM/TTS
                              `-> AudioInputSession
                                   |-> per-capture Silero state
                                   `-> SpeechRecognizer
                                        |-> FakeASR (test/CI)
                                        `-> DoubaoStreamingASR -> partial/final

all server output -> one ConnectionRuntime send queue -> WebSocket
```

## 当前职责

- `newtalk.app` 是组合入口，按配置选择 ChatService、Silero VAD、Fake ASR 或豆包 ASR，并关闭有生命周期的 Provider。
- `newtalk.transport.websocket` 只接受连接、解析帧类型和分派协议事件。
- `newtalk.transport.runtime.ConnectionRuntime` 保存单连接的 session、活动 Turn、采集会话、任务和发送队列。
- `newtalk.chat.session.DialogueSession` 只保存当前连接内成功完成的用户/助手交换，并按轮数和字符数构建连续窗口。
- `newtalk.chat.models.Turn.messages` 是创建 Turn 时的不可变上下文快照，生成过程中不会被后续输入修改。
- `newtalk.audio.session.AudioInputSession` 把连续 PCM 切成 utterance，维护 pre-roll，并把语音段交给 ASR。
- `newtalk.audio.vad.SileroVadStream` 保存每条采集流独立的 ONNX recurrent state、双阈值、滑窗和静音结束状态。
- `newtalk.asr.model.SpeechRecognizer` 只定义真实调用需要的音频流输入与 partial/final 输出。
- `newtalk.asr.doubao.DoubaoStreamingASR` 负责豆包鉴权、二进制协议、100ms PCM 分包和并发收发，不负责 VAD、Turn 或聊天。
- `web/mic-recorder-worklet.js` 重采样并切 20ms 帧；`web/pcm-player-worklet.js` 继续负责播放。

## 并发和取消

WebSocket 接收循环不再等待整个回复结束。每个 Turn 在独立 task 中运行，所以连接可继续接收麦克风帧、ping、新文本和关闭事件。

每条连接只有一个发送 task。LLM、TTS、VAD 和 ASR 产生的输出都先进入同一队列，避免多个协程同时写 WebSocket。队列项可携带 `turn_id`；旧 Turn 被取消后，尚未发送的迟到项会被丢弃。

`speech_start` 只负责打断，不创建 Turn；`asr_final` 才创建新 Turn。这保证一段用户语音不会因多个 VAD 帧或 ASR partial 创建多个对话轮次。

只有当前活动 Turn 成功产生 `TurnCompleted` 时才提交 Dialogue History，并在提交后发送 `turn_completed`。取消、生成失败和旧 Turn 迟到结果不会提交，因此下一轮不会看到半截助手回复。

## 当前边界

- 豆包 ASR 每个 utterance 新建一条 Provider WebSocket，尚未复用连接。
- `enable_nonstream=false`，本地 Silero 仍是当前语音边界和打断的唯一判定来源。
- 输入固定为 16kHz、16-bit、单声道 PCM，输出继续采用 TTS 配置的 PCM 采样率。
- 浏览器启用系统回声消除、降噪和自动增益；服务端 AEC 尚未实现，真实扬声器场景仍需手工测试。
- Silero 模型固定为 v6.2.1 并随仓库保存，运行时不联网下载。
- 浏览器“停止播放”仍是本地操作；`audio_stop` 才表示服务端 Turn 已取消。
- 长期 Memory、Identity、Vision 和 Tool 仍未进入当前运行链。
- Session 当前与 WebSocket 连接同生命周期，刷新页面后历史清空；跨连接恢复和长期 Memory 尚未实现。
- 当前消息角色只有 `user` 和 `assistant`；Tool 消息等到 P9 出现真实 Tool 调用时再扩展契约。
