# P4 Architecture

P4 adds streaming speech without moving the Doubao binary protocol into the WebSocket
transport. One user event still creates one Turn.

```text
Browser text_input
  -> transport/websocket.py
  -> transport/text_chat.py
  -> ChatService.stream_turn()
       |-> ChatModel.stream() -> text_delta -> outbound queue
       |                         |
       |                         `-> StreamingTextSegmenter -> TTS text queue
       |
       `-> TextToSpeech.stream() -> PCM frames -> outbound queue
                                      |
                                      v
                           transport sends one ordered stream
                              |-> JSON audio_start/audio_end
                              `-> binary PCM frames
                                      |
                                      v
                           Browser AudioWorklet player

.env / process environment
  -> config.py -> AppConfig -> app.py composition root
                          |-> FakeLLM / OpenAICompatibleChatModel
                          `-> FakeTTS / DoubaoTTS
```

## Current responsibilities

- `newtalk.app` selects one LLM and one TTS directly and owns their shutdown.
- `newtalk.chat.service` creates Turns and coordinates the concurrent LLM/TTS producers.
- `newtalk.chat.models` contains typed text/audio outputs independent of WebSocket and vendors.
- `newtalk.tts.model` defines only `audio_format`, `stream(text_chunks, turn_id)`, and `aclose()`.
- `newtalk.tts.segmenter` converts arbitrary LLM deltas into speakable text segments.
- `newtalk.tts.doubao` owns V3 authentication headers, binary events, sessions, errors, and PCM responses.
- `newtalk.transport.text_chat` maps typed Turn outputs to JSON or binary WebSocket frames.
- `web/app.js` owns browser audio state; `web/pcm-player-worklet.js` owns the real-time PCM queue.

## Concurrency and failure boundary

`ChatService.stream_turn()` starts one text producer and one audio producer. The text
producer continues sending `text_delta` while feeding segmented text to TTS. Both
producers write to one outbound queue, so only the transport coroutine writes to the
client WebSocket. Closing the output generator cancels both producers and closes the
active provider stream.

An LLM failure fails the Turn. A TTS failure emits `audio_failed`, but the LLM text can
still finish and emit `turn_completed`.

## Deliberate P4 limits

- Audio is 16-bit little-endian mono PCM; Opus negotiation is not implemented yet.
- Doubao opens one provider WebSocket per Turn; connection reuse is not implemented.
- Doubao is direct by default because `websockets` otherwise inherits the Windows
  system proxy; `NEWTALK_TTS_USE_SYSTEM_PROXY=true` explicitly opts back in.
- The Stop button clears browser playback locally. It does not cancel the server Turn.
- The connection still processes one Turn synchronously, so microphone barge-in and
  server-side cancellation remain P5 work.
- Dialogue history, Session persistence, ASR, Vision, Memory, Tools, and a Provider
  registry remain absent.
