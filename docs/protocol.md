# P7.1 HTTP 与 WebSocket 协议

WebSocket Endpoint 为 `GET /ws`，协议版本 `0.5`。建连前必须通过 HTTP Device API 获得同源 HttpOnly Cookie；缺少或使用失效凭据时以 code `4401` 拒绝连接。

WebSocket 仍以 JSON 帧传控制事件、二进制帧传 PCM。P7.1 没有增加聊天消息类型；`hello.session_id` 仍标识当前内存 Dialogue Session，断线后不恢复。

## Device 与成员 HTTP API

- `GET /api/device`：读取当前 Cookie 对应的 Device，未注册返回 `401`。
- `POST /api/device`：创建家庭空间并设置 Cookie；恢复码只在首次创建响应中出现。
- `POST /api/device/recover`：使用恢复码重新绑定家庭并轮换设备凭据。
- `POST /api/device/recovery-code`：轮换恢复码，旧码立即失效。
- `GET /api/members`：列出当前 `device_id` 的成员。
- `POST /api/members`：创建成员。
- `PATCH /api/members/{identity_id}`：修改当前家庭成员。
- `DELETE /api/members/{identity_id}`：P7.1 删除当前仅有的本地成员资料；跨 VoicePrint/MemOS 完整删除在 P7.6 接入。

所有成员读写都由服务端从 Cookie 解析 `device_id`，客户端不能在请求体中指定其他家庭。

## 建连

服务端 `hello` 同时声明输入和输出格式：

```json
{
  "type": "hello",
  "protocol_version": "0.5",
  "session_id": "generated UUID",
  "device_id": "02:11:22:33:44:55",
  "audio": {
    "input": {"codec":"pcm_s16le","sample_rate":16000,"channels":1,"frame_duration_ms":20},
    "output": {"codec":"pcm_s16le","sample_rate":24000,"channels":1}
  }
}
```

## 麦克风输入

客户端先声明一次采集，然后持续发送二进制 PCM：

```json
{
  "type": "audio_input_start",
  "event_id": "client ID",
  "capture_id": "capture ID",
  "format": {"codec":"pcm_s16le","sample_rate":16000,"channels":1,"frame_duration_ms":20}
}
```

服务端确认 `audio_input_ready`。Silero 检测到边界时发送：

```json
{"type":"vad_speech_start","capture_id":"capture ID","utterance_id":"UUID","probability":0.87,"audio_ms":96.0}
{"type":"vad_speech_end","capture_id":"capture ID","utterance_id":"UUID","probability":0.08,"audio_ms":1248.0}
```

`vad_speech_start` 会取消当前旧 Turn。ASR 事件为：

```json
{"type":"asr_partial","utterance_id":"UUID","text":"中间文本"}
{"type":"asr_final","utterance_id":"UUID","text":"最终文本"}
```

只有非空 `asr_final` 创建一个新 Turn。Fake ASR 只产生固定 final；豆包 ASR 会产生去重后的 partial 和唯一 final。

识别失败不会关闭客户端连接：

```json
{"type":"asr_failed","utterance_id":"UUID","code":"recognition_failed","message":"Unable to recognize speech"}
```

停止采集：

```json
{"type":"audio_input_stop","event_id":"client ID","capture_id":"capture ID"}
```

服务端完成当前音频收尾后返回 `audio_input_stopped`。同一连接同时只允许一个 `capture_id`。

## Turn 和打断

文本仍使用 `text_input`。文本或 ASR Final 都进入相同的 Turn 流，依次可能产生 `turn_started`、`text_delta`、`audio_start`、服务端二进制 PCM、`audio_end` 和 `turn_completed`。

新文本输入或 VAD 说话开始取消旧 Turn：

```json
{"type":"turn_cancelled","turn_id":"old Turn UUID","reason":"barge_in"}
{"type":"audio_stop","turn_id":"old Turn UUID","stream_id":"old stream UUID or null","reason":"barge_in"}
```

客户端收到 `audio_stop` 必须立即清空播放队列。服务端先使旧 `turn_id` 失效，再取消 LLM/TTS 任务；发送队列会丢弃已经失效 Turn 的迟到帧。

## 错误和生命周期

- 二进制音频未先开始采集：`audio_input_not_started`。
- 输入格式不匹配：`unsupported_audio_format`。
- PCM 字节数不是 2 的倍数：`invalid_audio_frame`。
- Provider 鉴权、超时或协议失败：`asr_failed`。
- 同一连接重复开始采集：`audio_input_active`。
- `ping` 返回 `pong`；`close` 返回 `closing` 并以 code `1000` 关闭。
- `playback_started` 继续用于记录浏览器首播时间，不创建 Turn。

## P6 调用链

```text
Browser getUserMedia
-> mic-recorder-worklet (resample -> 16kHz / 20ms / PCM S16LE)
-> WebSocket binary frames
-> AudioInputSession -> SileroVadStream
   |-> speech_start -> cancel old Turn -> turn_cancelled + audio_stop
   `-> speech_end   -> finish ASR utterance
-> SpeechRecognizer.stream
   |-> FakeASR (test/CI)
   `-> DoubaoStreamingASR (100ms packets -> partial/final)
-> ConnectionRuntime.start_turn
-> DialogueSession.messages_for -> immutable Turn.messages
-> ChatService.stream_turn
-> text_delta + TTS binary PCM -> PcmPlayer / AudioWorklet
-> turn_completed -> DialogueSession.commit
```
