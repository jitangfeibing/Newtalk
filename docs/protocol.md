# P2 WebSocket 协议

Endpoint：`GET /ws`，通过 WebSocket Upgrade 建立连接。

P2 只接收 JSON 文本帧。音频和图片帧尚未定义。协议版本为 `0.2`。

## Hello

连接接受后，服务端立即发送：

```json
{
  "type": "hello",
  "protocol_version": "0.2",
  "session_id": "generated UUID"
}
```

## 文本输入

客户端事件：

```json
{
  "type": "text_input",
  "event_id": "client-generated ID",
  "text": "你好"
}
```

规则：

- `event_id` 必须是非空字符串，并作为当前连接内的幂等键。
- `text` 去除首尾空白后必须非空，最多 4000 字符。
- 相同 `event_id` 重发返回 `duplicate_event`，不会创建第二个 Turn。
- 相同文本使用不同 `event_id` 发送时，代表两个用户行为，会创建两个 Turn。

## 流式回复

一个成功 Turn 按固定顺序发送事件。

开始：

```json
{
  "type": "turn_started",
  "session_id": "current session UUID",
  "turn_id": "generated UUID",
  "event_id": "originating event ID"
}
```

零个或多个文本增量：

```json
{
  "type": "text_delta",
  "turn_id": "current turn UUID",
  "event_id": "originating event ID",
  "sequence": 1,
  "delta": "我收到了："
}
```

完成：

```json
{
  "type": "turn_completed",
  "turn_id": "current turn UUID",
  "event_id": "originating event ID",
  "text": "我收到了：你好"
}
```

P2 Fake LLM 固定返回两段 delta：`我收到了：` 和用户文本。该行为用于稳定测试，不代表未来真实 LLM 的分段方式。

## Turn 失败

输入已创建 Turn，但回复生成失败时：

```json
{
  "type": "turn_failed",
  "turn_id": "current turn UUID",
  "event_id": "originating event ID",
  "code": "chat_failed",
  "message": "Unable to generate a reply"
}
```

失败不会主动关闭 WebSocket，客户端可以继续发送新事件。

## Ping

```json
{"type": "ping", "event_id": "client-generated ID"}
```

```json
{
  "type": "pong",
  "session_id": "current session UUID",
  "event_id": "client-generated ID"
}
```

## 正常关闭

客户端先发送应用层关闭请求：

```json
{"type": "close", "event_id": "client-generated ID"}
```

服务端确认后，以 WebSocket code `1000` 关闭：

```json
{
  "type": "closing",
  "session_id": "current session UUID",
  "event_id": "client-generated ID"
}
```

## 协议错误

非法 JSON、非法帧、无效字段和未知事件返回 `error`，通常不关闭连接：

```json
{
  "type": "error",
  "code": "invalid_text",
  "message": "text_input requires non-empty text",
  "event_id": "optional originating event ID"
}
```
