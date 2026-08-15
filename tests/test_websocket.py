from fastapi.testclient import TestClient

from newtalk.app import create_app
from newtalk.chat import ChatService, FakeLLM


client = TestClient(create_app())


def receive_turn(websocket) -> tuple[dict, list[dict], dict]:
    started = websocket.receive_json()
    deltas = []
    while True:
        event = websocket.receive_json()
        if event["type"] == "text_delta":
            deltas.append(event)
            continue
        return started, deltas, event


def test_websocket_sends_hello_and_answers_ping() -> None:
    with client.websocket_connect("/ws") as websocket:
        hello = websocket.receive_json()
        assert hello["type"] == "hello"
        assert hello["protocol_version"] == "0.2"
        assert hello["session_id"]

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

        started, deltas, completed = receive_turn(websocket)

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


def test_same_text_with_different_events_creates_two_turns() -> None:
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()

        turn_ids = []
        for event_id in ("text-1", "text-2"):
            websocket.send_json(
                {"type": "text_input", "event_id": event_id, "text": "重复文本"}
            )
            started, _, completed = receive_turn(websocket)
            assert completed["type"] == "turn_completed"
            turn_ids.append(started["turn_id"])

        assert turn_ids[0] != turn_ids[1]


def test_duplicate_event_id_does_not_create_another_turn() -> None:
    event = {"type": "text_input", "event_id": "duplicate", "text": "只处理一次"}

    with client.websocket_connect("/ws") as websocket:
        websocket.receive_json()
        websocket.send_json(event)
        first_started, _, first_completed = receive_turn(websocket)
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
