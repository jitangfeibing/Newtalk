# P1 WebSocket Protocol

Endpoint: `GET /ws` with a WebSocket upgrade.

P1 accepts JSON text frames only. Audio and image frames are not defined yet.

## Server hello

Sent immediately after the connection is accepted:

```json
{
  "type": "hello",
  "protocol_version": "0.1",
  "session_id": "generated UUID"
}
```

## Ping

Client event:

```json
{"type": "ping", "event_id": "client-generated ID"}
```

Server event:

```json
{
  "type": "pong",
  "session_id": "current session UUID",
  "event_id": "client-generated ID"
}
```

## Graceful close

The Web client requests an application-level close before the WebSocket closing
handshake:

```json
{"type": "close", "event_id": "client-generated ID"}
```

The server acknowledges the request and then closes the WebSocket with code `1000`:

```json
{
  "type": "closing",
  "session_id": "current session UUID",
  "event_id": "client-generated ID"
}
```

## Error

Invalid frames and unsupported events receive an error event without closing the socket:

```json
{
  "type": "error",
  "code": "unsupported_event",
  "message": "Unsupported event type: example",
  "event_id": "optional originating event ID"
}
```
