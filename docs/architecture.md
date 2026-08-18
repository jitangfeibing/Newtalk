# P5-A Architecture

P5-A 在不改变现有 LLM/TTS 契约的前提下加入麦克风、VAD、ASR 输入和可取消 Turn。原小智的 Silero 参数作为参考，但没有复制其 `ConnectionHandler` 状态耦合。

```text
Browser text_input -------------------------------+
Browser microphone -> recorder AudioWorklet       |
       -> 16k mono PCM -> WebSocket binary        |
                                                   v
WebSocket receive loop -> ConnectionRuntime -> one active Turn
                              |                `-> ChatService -> LLM/TTS
                              `-> AudioInputSession
                                   |-> per-capture Silero state
                                   `-> SpeechRecognizer -> ASR Final

all server output -> one ConnectionRuntime send queue -> WebSocket
```

## 当前职责

- `newtalk.app` 是组合入口，选择 ChatService、Silero VAD 和 Fake ASR，并关闭有生命周期的 Provider。
- `newtalk.transport.websocket` 只接受连接、解析帧类型和分派协议事件。
- `newtalk.transport.runtime.ConnectionRuntime` 保存单连接的 session、活动 Turn、采集会话、任务和发送队列。
- `newtalk.audio.session.AudioInputSession` 把连续 PCM 切成 utterance，维护 pre-roll，并把语音段交给 ASR。
- `newtalk.audio.vad.SileroVadStream` 保存每条采集流独立的 ONNX recurrent state、双阈值、滑窗和静音结束状态。
- `newtalk.asr.model.SpeechRecognizer` 只定义真实调用需要的音频流输入与 partial/final 输出。
- `web/mic-recorder-worklet.js` 重采样并切 20ms 帧；`web/pcm-player-worklet.js` 继续负责播放。

## 并发和取消

WebSocket 接收循环不再等待整个回复结束。每个 Turn 在独立 task 中运行，所以连接可继续接收麦克风帧、ping、新文本和关闭事件。

每条连接只有一个发送 task。LLM、TTS、VAD 和 ASR 产生的输出都先进入同一队列，避免多个协程同时写 WebSocket。队列项可携带 `turn_id`；旧 Turn 被取消后，尚未发送的迟到项会被丢弃。

`speech_start` 只负责打断，不创建 Turn；`asr_final` 才创建新 Turn。这保证一段用户语音不会因多个 VAD 帧或 ASR partial 创建多个对话轮次。

## 当前边界

- P5-A 的 ASR 是 Fake，只用于验证控制流；真实豆包流式 ASR 尚未接入。
- 输入固定为 16kHz、16-bit、单声道 PCM，输出继续采用 TTS 配置的 PCM 采样率。
- 浏览器启用系统回声消除、降噪和自动增益；服务端 AEC 尚未实现，真实扬声器场景仍需手工测试。
- Silero 模型固定为 v6.2.1 并随仓库保存，运行时不联网下载。
- 浏览器“停止播放”仍是本地操作；`audio_stop` 才表示服务端 Turn 已取消。
- Dialogue Context、Memory、Identity、Vision 和 Tool 仍未进入当前运行链。
