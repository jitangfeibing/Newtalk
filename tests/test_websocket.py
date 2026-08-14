from fastapi.testclient import TestClient

from newtalk.app import create_app


client = TestClient(create_app())


def test_websocket_sends_hello_and_answers_ping() -> None:
    with client.websocket_connect("/ws") as websocket:
        hello = websocket.receive_json()
        assert hello["type"] == "hello"
        assert hello["protocol_version"] == "0.1"
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
