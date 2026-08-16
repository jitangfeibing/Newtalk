# P4 WebSocket 协议

Endpoint：`GET /ws`，协议版本 `0.3`。客户端只发送 JSON 文本帧；服务端发送 JSON 事件和 PCM 二进制帧。

## 建连

```json
{
  "type": "hello",
  "protocol_version": "0.3",
  "session_id": "generated UUID",
  "audio": {
    "codec": "pcm_s16le",
    "sample_rate": 24000,
    "channels": 1
  }
}
```

`audio` 是当前连接使用的服务端音频格式，浏览器据此创建 AudioContext。

## 文本 Turn

客户端输入：

```json
{"type":"text_input","event_id":"client ID","text":"你好"}
```

- `event_id` 是当前连接内的幂等键。
- 文本去除首尾空白后必须非空，最多 4000 字符。
- 一个有效 `text_input` 只创建一个 `turn_id`。

服务端首先发送：

```json
{
  "type": "turn_started",
  "session_id": "session UUID",
  "turn_id": "turn UUID",
  "event_id": "client ID"
}
```

随后发送零个或多个文本增量：

```json
{
  "type": "text_delta",
  "turn_id": "turn UUID",
  "event_id": "client ID",
  "sequence": 1,
  "delta": "你好"
}
```

LLM 文本与 TTS 音频并行产生，客户端不能假定 `text_delta` 与音频事件之间存在固定交错顺序。

## 音频流

第一帧 PCM 之前发送：

```json
{
  "type": "audio_start",
  "turn_id": "turn UUID",
  "stream_id": "audio stream UUID",
  "codec": "pcm_s16le",
  "sample_rate": 24000,
  "channels": 1
}
```

`audio_start` 之后的 WebSocket 二进制帧属于该 `stream_id`。P4 同一连接不会同时发送两条音频流，因此二进制帧本身不重复携带 ID。

正常结束：

```json
{
  "type": "audio_end",
  "turn_id": "turn UUID",
  "stream_id": "audio stream UUID",
  "frames": 12,
  "bytes": 48000
}
```

TTS 失败：

```json
{
  "type": "audio_failed",
  "turn_id": "turn UUID",
  "stream_id": "audio stream UUID",
  "message": "Unable to synthesize speech"
}
```

`audio_failed` 不等于 `turn_failed`。TTS 失败后文本仍可继续完成。

## Turn 完成与失败

文本和 TTS 管线结束后：

```json
{
  "type": "turn_completed",
  "turn_id": "turn UUID",
  "event_id": "client ID",
  "text": "完整回复"
}
```

LLM 或聊天核心失败时：

```json
{
  "type": "turn_failed",
  "turn_id": "turn UUID",
  "event_id": "client ID",
  "code": "chat_failed",
  "message": "Unable to generate a reply"
}
```

## 浏览器播放指标

AudioWorklet 真正取出第一批 PCM 样本时，客户端上报：

```json
{
  "type": "playback_started",
  "event_id": "client metric ID",
  "turn_id": "turn UUID",
  "stream_id": "audio stream UUID",
  "elapsed_ms": 347.3
}
```

`elapsed_ms` 从浏览器收到 `turn_started` 开始计算。该事件用于日志，不创建新 Turn。

## 其他事件

- `ping` 返回 `pong`。
- `close` 返回 `closing`，然后使用 WebSocket code `1000` 关闭。
- 非法 JSON、字段或未知事件返回结构化 `error`，通常不关闭连接。

## P4 调用链

```text
Web text_input
-> websocket_endpoint
-> handle_text_input
-> ChatService.stream_turn
   |-> ChatModel.stream -> TextDelta
   `-> StreamingTextSegmenter -> TextToSpeech.stream -> AudioFrame
-> text_delta / audio_start / binary PCM / audio_end / turn_completed
-> PcmPlayer -> AudioWorklet
-> playback_started metric
```
