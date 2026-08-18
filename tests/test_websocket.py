import json

from fastapi.testclient import TestClient

from newtalk.app import create_app
from newtalk.asr import AsrFinal, FakeASR
from newtalk.audio import INPUT_AUDIO_FORMAT, VadEvent
from newtalk.chat import ChatService, FakeLLM
from newtalk.config import AppConfig


client = TestClient(create_app(AppConfig()))


def receive_turn(websocket) -> tuple[dict, list[dict], dict, list[dict], list[bytes]]:
    started = websocket.receive_json()
    deltas: list[dict] = []
    audio_events: list[dict] = []
    audio_frames: list[bytes] = []
    while True:
        frame = websocket.receive()
        if frame.get("bytes") is not None:
            audio_frames.append(frame["bytes"])
            continue
        event = json.loads(frame["text"])
        if event["type"] == "text_delta":
            deltas.append(event)
            continue
        if event["type"] in {"audio_start", "audio_end", "audio_failed"}:
            audio_events.append(event)
            continue
        return started, deltas, event, audio_events, audio_frames


def test_websocket_sends_hello_and_answers_ping() -> None:
    with client.websocket_connect("/ws") as websocket:
        hello = websocket.receive_json()
        assert hello["type"] == "hello"
        assert hello["protocol_version"] == "0.4"
        assert hello["session_id"]
        assert hello["audio"] == {
            "input": {
                "codec": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration_ms": 20,
            },
            "output": {
                "codec": "pcm_s16le",
                "sample_rate": 24000,
                "channels": 1,
            },
        }

        websocket.send_json({"type": "ping", "event_id": "test-ping"})
        pong = websocket.receive_json()

        assert pong == {
            "type": "pong",
            "session_id": hello["session_id"],
            "event_id": "test-ping",
        }


def test_websocket_rejects_unknown_event_without_closing() -> None:
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "not-ready", "event_id": "test-event"})

        error = websocket.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "unsupported_event"
        assert error["event_id"] == "test-event"


def test_non_string_event_id_is_not_echoed() -> None:
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json(
            {"type": "not-supported", "event_id": {"unexpected": "object"}}
        )

        response = websocket.receive_json()

        assert response["type"] == "error"
        assert "event_id" not in response


def test_close_event_acknowledges_and_closes_normally() -> None:
    with client.websocket_connect("/ws") as websocket:
        hello = websocket.receive_json()
        websocket.send_json({"type": "close", "event_id": "test-close"})

        assert websocket.receive_json() == {
            "type": "closing",
            "session_id": hello["session_id"],
            "event_id": "test-close",
        }
        close_frame = websocket.receive()

        assert close_frame["type"] == "websocket.close"
        assert close_frame["code"] == 1000


def test_text_input_streams_one_turn() -> None:
    with client.websocket_connect("/ws") as websocket:
        hello = websocket.receive_json()
        websocket.send_json(
            {"type": "text_input", "event_id": "text-1", "text": " 你好 "}
        )

        started, deltas, completed, audio_events, audio_frames = receive_turn(websocket)

        assert started == {
            "type": "turn_started",
            "session_id": hello["session_id"],
            "turn_id": started["turn_id"],
            "event_id": "text-1",
        }
        assert [event["delta"] for event in deltas] == ["我收到了：", "你好"]
        assert [event["sequence"] for event in deltas] == [1, 2]
        assert all(event["turn_id"] == started["turn_id"] for event in deltas)
        assert completed == {
            "type": "turn_completed",
            "turn_id": started["turn_id"],
            "event_id": "text-1",
            "text": "我收到了：你好",
        }
        assert [event["type"] for event in audio_events] == [
            "audio_start",
            "audio_end",
        ]
        assert audio_events[0]["stream_id"] == audio_events[1]["stream_id"]
        assert audio_events[0]["turn_id"] == started["turn_id"]
        assert audio_frames and all(frame for frame in audio_frames)


def test_same_text_with_different_events_creates_two_turns() -> None:
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()

        turn_ids = []
        for event_id in ("text-1", "text-2"):
            websocket.send_json(
                {"type": "text_input", "event_id": event_id, "text": "重复文本"}
            )
            started, _, completed, _, _ = receive_turn(websocket)
            assert completed["type"] == "turn_completed"
            turn_ids.append(started["turn_id"])

        assert turn_ids[0] != turn_ids[1]


def test_duplicate_event_id_does_not_create_another_turn() -> None:
    event = {"type": "text_input", "event_id": "duplicate", "text": "只处理一次"}

    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json(event)
        first_started, _, first_completed, _, _ = receive_turn(websocket)
        assert first_completed["type"] == "turn_completed"

        websocket.send_json(event)
        error = websocket.receive_json()

        assert first_started["turn_id"]
        assert error["type"] == "error"
        assert error["code"] == "duplicate_event"
        assert error["event_id"] == "duplicate"


def test_invalid_text_does_not_start_a_turn() -> None:
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json(
            {"type": "text_input", "event_id": "empty", "text": "   "}
        )

        error = websocket.receive_json()

        assert error["type"] == "error"
        assert error["code"] == "invalid_text"
        assert "turn_id" not in error


class FailingLLM(FakeLLM):
    async def stream(self, user_text: str):
        if False:
            yield user_text
        raise RuntimeError("deterministic test failure")


def test_chat_failure_keeps_websocket_available() -> None:
    failing_client = TestClient(
        create_app(chat_service=ChatService(FailingLLM(chunk_delay_seconds=0)))
    )

    with failing_client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json(
            {"type": "text_input", "event_id": "failure", "text": "触发失败"}
        )

        started = websocket.receive_json()
        failed = websocket.receive_json()
        assert started["type"] == "turn_started"
        assert failed == {
            "type": "turn_failed",
            "turn_id": started["turn_id"],
            "event_id": "failure",
            "code": "chat_failed",
            "message": "Unable to generate a reply",
        }

        websocket.send_json({"type": "ping", "event_id": "after-failure"})
        assert websocket.receive_json()["type"] == "pong"


def test_disconnect_during_stream_keeps_application_healthy() -> None:
    slow_client = TestClient(
        create_app(chat_service=ChatService(FakeLLM(chunk_delay_seconds=0.1)))
    )

    with slow_client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json(
            {"type": "text_input", "event_id": "disconnect", "text": "稍后断开"}
        )
        assert websocket.receive_json()["type"] == "turn_started"
        websocket.close()

    assert slow_client.get("/health").status_code == 200


class ScriptedVadStream:
    def __init__(self) -> None:
        self.frame_count = 0

    def process(self, pcm: bytes) -> list[VadEvent]:
        del pcm
        self.frame_count += 1
        if self.frame_count == 1:
            return [VadEvent("speech_start", 0.9, 20.0)]
        if self.frame_count == 2:
            return [VadEvent("speech_end", 0.1, 40.0)]
        return []

    def flush(self) -> list[VadEvent]:
        return []

    def reset(self) -> None:
        return None


class ScriptedVad:
    def create_stream(self) -> ScriptedVadStream:
        return ScriptedVadStream()


class FailingASR(FakeASR):
    async def stream(self, audio_chunks, *, utterance_id: str):
        async for _ in audio_chunks:
            break
        if False:
            yield AsrFinal(utterance_id)
        raise RuntimeError("deterministic ASR failure")


def audio_input_start(capture_id: str = "capture-1") -> dict:
    return {
        "type": "audio_input_start",
        "event_id": "audio-start-1",
        "capture_id": capture_id,
        "format": {
            "codec": INPUT_AUDIO_FORMAT.codec,
            "sample_rate": INPUT_AUDIO_FORMAT.sample_rate,
            "channels": INPUT_AUDIO_FORMAT.channels,
            "frame_duration_ms": INPUT_AUDIO_FORMAT.frame_duration_ms,
        },
    }


def test_microphone_audio_creates_one_voice_turn() -> None:
    voice_client = TestClient(
        create_app(
            AppConfig(),
            vad=ScriptedVad(),
            recognizer=FakeASR("语音测试"),
        )
    )
    with voice_client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json(audio_input_start())
        assert websocket.receive_json()["type"] == "audio_input_ready"

        websocket.send_bytes(bytes(640))
        assert websocket.receive_json()["type"] == "vad_speech_start"
        websocket.send_bytes(bytes(640))

        event_types: list[str] = []
        turn_started_count = 0
        final_text = None
        while final_text is None:
            frame = websocket.receive()
            if frame.get("bytes") is not None:
                continue
            event = json.loads(frame["text"])
            event_types.append(event["type"])
            if event["type"] == "turn_started":
                turn_started_count += 1
            if event["type"] == "turn_completed":
                final_text = event["text"]

        assert event_types[:3] == ["vad_speech_end", "asr_final", "turn_started"]
        assert turn_started_count == 1
        assert final_text == "我收到了：语音测试"


def test_vad_speech_start_cancels_the_active_turn() -> None:
    barge_in_client = TestClient(
        create_app(
            AppConfig(),
            chat_service=ChatService(FakeLLM(chunk_delay_seconds=0.2)),
            vad=ScriptedVad(),
            recognizer=FakeASR(),
        )
    )
    with barge_in_client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json(audio_input_start())
        assert websocket.receive_json()["type"] == "audio_input_ready"
        websocket.send_json(
            {"type": "text_input", "event_id": "old-turn", "text": "不要说完"}
        )
        old_turn = websocket.receive_json()
        assert old_turn["type"] == "turn_started"

        websocket.send_bytes(bytes(640))
        assert websocket.receive_json()["type"] == "vad_speech_start"
        cancelled = websocket.receive_json()
        stopped = websocket.receive_json()

        assert cancelled == {
            "type": "turn_cancelled",
            "turn_id": old_turn["turn_id"],
            "reason": "barge_in",
        }
        assert stopped["type"] == "audio_stop"
        assert stopped["turn_id"] == old_turn["turn_id"]
        assert stopped["reason"] == "barge_in"


def test_asr_failure_is_reported_without_closing_websocket() -> None:
    failing_asr_client = TestClient(
        create_app(
            AppConfig(),
            vad=ScriptedVad(),
            recognizer=FailingASR(),
        )
    )
    with failing_asr_client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json(audio_input_start())
        websocket.receive_json()
        websocket.send_bytes(bytes(640))
        boundary = websocket.receive_json()
        failed = websocket.receive_json()

        assert boundary["type"] == "vad_speech_start"
        assert failed == {
            "type": "asr_failed",
            "utterance_id": boundary["utterance_id"],
            "code": "recognition_failed",
            "message": "Unable to recognize speech",
        }

        websocket.send_json({"type": "ping", "event_id": "after-asr-failure"})
        assert websocket.receive_json()["type"] == "pong"
