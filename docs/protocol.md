# P5-A WebSocket 协议

Endpoint：`GET /ws`，协议版本 `0.4`。WebSocket 是全双工连接：客户端 JSON 帧传控制事件，客户端二进制帧传麦克风 PCM；服务端 JSON 帧传对话事件，服务端二进制帧传 TTS PCM。二进制帧的含义由发送方向区分。

## 建连

服务端 `hello` 同时声明输入和输出格式：

```json
{
  "type": "hello",
  "protocol_version": "0.4",
  "session_id": "generated UUID",
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

只有非空 `asr_final` 创建一个新 Turn。P5-A 使用 Fake ASR，因此只产生固定最终文本，不产生 partial。

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
- 同一连接重复开始采集：`audio_input_active`。
- `ping` 返回 `pong`；`close` 返回 `closing` 并以 code `1000` 关闭。
- `playback_started` 继续用于记录浏览器首播时间，不创建 Turn。

## P5-A 调用链

```text
Browser getUserMedia
-> mic-recorder-worklet (resample -> 16kHz / 20ms / PCM S16LE)
-> WebSocket binary frames
-> AudioInputSession -> SileroVadStream
   |-> speech_start -> cancel old Turn -> turn_cancelled + audio_stop
   `-> speech_end   -> finish ASR utterance
-> SpeechRecognizer.stream -> asr_final
-> ConnectionRuntime.start_turn -> ChatService.stream_turn
-> text_delta + TTS binary PCM -> PcmPlayer / AudioWorklet
```
