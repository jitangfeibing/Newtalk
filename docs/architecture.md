# P3 Architecture

P3 adds one real streaming model without moving vendor protocol details into the
WebSocket transport or Turn model.

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
                             chat/ChatModel
                              /        \
                             v          v
                         FakeLLM   OpenAICompatibleChatModel
                                          |
                                          v
                                  provider SSE endpoint

.env / process environment
  `-- config.py -----------> AppConfig -> app.py composition root
```

## Current responsibilities

- `newtalk.config` loads runtime and LLM selection values while keeping the API Key out of `repr`.
- `newtalk.logging_config` configures logs for the `newtalk` namespace.
- `newtalk.app` is the composition root. It selects Fake or OpenAI-compatible directly and owns shutdown.
- `newtalk.transport.websocket` owns WebSocket framing, connection lifecycle, and event routing.
- `newtalk.transport.protocol` owns shared protocol version and error events.
- `newtalk.transport.text_chat` validates `text_input` and translates Turn output to WebSocket events.
- `newtalk.chat.models` defines the `Turn` data boundary.
- `newtalk.chat.model` defines the minimal `stream(user_text)` and `aclose()` contract required by P3.
- `newtalk.chat.service` creates Turns, validates chunks, and logs first-token/total latency without knowing vendor APIs.
- `newtalk.chat.fake_llm` supplies deterministic chunks for local use and tests.
- `newtalk.chat.openai_compatible` translates the model contract to an asynchronous Chat Completions SSE stream.
- `web/` renders streaming chat and protocol state using native browser APIs.
- `tests/` verifies behavior through in-process clients and a real Uvicorn subprocess.

## Provider boundary

P3 has one concrete real implementation and one test implementation. It does not have
a registry, dynamic module loader, generic request object, tool calls, or a hierarchy of
vendor adapters. The second real model will be compared with this implementation before
introducing any broader abstraction.

## Deliberately absent

P3 does not define dialogue history, Session storage, ASR, TTS, Vision, Memory,
Identity, Tool Calling, Dynamic Prompt, or Docker. Each Turn still sends only its own
user text plus the optional static system prompt.
