# P2 Architecture

P2 adds a text-chat vertical slice without moving chat behavior into the connection lifecycle.

```text
Browser
  |-- HTTP GET / ----------> static files in web/
  |-- HTTP GET /health ----> application health
  `-- WebSocket /ws -------> transport/websocket.py (connection lifecycle)
                                  |
                                  v
                             transport/text_chat.py (protocol events)
                                  |
                                  v
                             chat/ChatService
                                  |
                                  v
                             chat/FakeLLM

.env / process environment
  `-- config.py -----------> AppConfig -> app.py / Uvicorn
```

## Current responsibilities

- `newtalk.config` loads and validates the P1 runtime environment.
- `newtalk.logging_config` configures logs for the `newtalk` namespace.
- `newtalk.app` creates the FastAPI application, mounts routes, and owns startup/shutdown.
- `newtalk.transport.websocket` owns WebSocket framing, connection lifecycle, and event routing.
- `newtalk.transport.protocol` owns shared protocol version and error events.
- `newtalk.transport.text_chat` validates `text_input` and translates Turn output to WebSocket events.
- `newtalk.chat.models` defines the P2 `Turn` data boundary.
- `newtalk.chat.service` creates Turns and streams replies without knowing about WebSocket.
- `newtalk.chat.fake_llm` supplies deterministic P2 chunks without defining a Provider registry.
- `web/` renders streaming chat and protocol state using native browser APIs.
- `tests/` verifies behavior through in-process clients and a real Uvicorn subprocess.

## Deliberately absent

P2 does not define a real LLM Provider interface, dialogue history, Session storage,
ASR, TTS, Vision, Memory, Identity, Tool Calling, provider configuration, or Docker.
Those boundaries will be introduced only when the corresponding vertical Part is built.
