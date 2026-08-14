# P1 Architecture

P1 establishes only the runtime boundary needed to validate a browser connection.

```text
Browser
  |-- HTTP GET / ----------> static files in web/
  |-- HTTP GET /health ----> application health
  `-- WebSocket /ws -------> transport/websocket.py

.env / process environment
  `-- config.py -----------> AppConfig -> app.py / Uvicorn
```

## Current responsibilities

- `newtalk.config` loads and validates the P1 runtime environment.
- `newtalk.logging_config` configures logs for the `newtalk` namespace.
- `newtalk.app` creates the FastAPI application, mounts routes, and owns startup/shutdown.
- `newtalk.transport.websocket` owns WebSocket framing and connection lifecycle.
- `web/` renders protocol state and sends test events.
- `tests/` verifies behavior through in-process clients and a real Uvicorn subprocess.

## Deliberately absent

P1 does not define Provider interfaces, chat orchestration, Turn state, Session storage,
ASR, LLM, TTS, Vision, Memory, Identity, Tool Calling, provider configuration, or Docker.
Those boundaries will be introduced only when the corresponding vertical Part is built.
