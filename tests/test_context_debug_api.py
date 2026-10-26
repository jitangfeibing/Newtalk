import json

from fastapi.testclient import TestClient

from newtalk.chat import ChatService
from tests.test_websocket import make_client


def test_debug_endpoints_require_device_cookie() -> None:
    registered = make_client()
    anonymous = TestClient(registered.app)
    assert anonymous.get("/api/debug/context").status_code == 401
    assert anonymous.get("/api/debug/context/guessed-turn").status_code == 401


def test_disabled_debug_is_different_from_empty_cache() -> None:
    client = make_client()
    response = client.get("/api/debug/context")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["turns"] == []
    assert response.headers["cache-control"] == "no-store"
    detail = client.get("/api/debug/context/guessed-turn")
    assert detail.status_code == 503
    assert detail.headers["cache-control"] == "no-store"


def test_context_dashboard_only_reads_current_device(tmp_path) -> None:
    from newtalk.context_debug import ContextDebugger

    debugger = ContextDebugger(enabled=True, path=tmp_path / "debug.jsonl")
    first = make_client(chat_service=ChatService(context_debug=debugger))
    second = TestClient(first.app)
    assert second.post("/api/device").status_code == 201
    first_id = first.get("/api/device").json()["device_id"]
    second_id = second.get("/api/device").json()["device_id"]
    try:
        for device_id, turn_id, text in [(first_id, "private-one", "private-A"),
                                         (second_id, "private-two", "private-B")]:
            debugger.record("context_selected", device_id=device_id, turn_id=turn_id,
                            identity={"display_name": "小明"}, source="text",
                            messages=[{"role": "user", "content": text}], history=[])
            debugger.record("llm_request", device_id=device_id, turn_id=turn_id,
                            request_index=1, request={"model": "test", "messages": []})
            debugger.record("turn_end", device_id=device_id, turn_id=turn_id, outcome="completed")

        response = first.get("/api/debug/context")
        assert response.status_code == 200
        assert [turn["turn_id"] for turn in response.json()["turns"]] == ["private-one"]
        assert "private-B" not in response.text
        assert response.json()["turns"][0]["request_count"] == 1
        assert response.headers["cache-control"] == "no-store"
        detail = first.get("/api/debug/context/private-one")
        assert detail.status_code == 200
        assert len(detail.json()["events"]) == 3
        assert "private-A" in detail.text
        assert "private-B" not in detail.text
        assert detail.headers["cache-control"] == "no-store"
        foreign = first.get("/api/debug/context/private-two")
        assert foreign.status_code == 404
        assert foreign.headers["cache-control"] == "no-store"
        assert first.get("/api/debug/context/not-existing").status_code == 404
    finally:
        debugger.close()


def test_dashboard_cache_has_ttl_and_capacity_limits(tmp_path) -> None:
    from newtalk.context_debug import ContextDebugger

    now = [100.0]
    debugger = ContextDebugger(enabled=True, path=tmp_path / "debug.jsonl",
                               max_cached_events=2, retention_seconds=30,
                               clock=lambda: now[0])
    try:
        for index in range(3):
            debugger.record("example", device_id="one", turn_id=str(index))
        assert [event["turn_id"] for event in debugger.events_for_device("one")] == ["1", "2"]
        now[0] += 31
        assert debugger.events_for_device("one") == []
    finally:
        debugger.close()


def test_dashboard_cache_is_detached_and_large_records_are_not_kept(tmp_path) -> None:
    from newtalk.context_debug import ContextDebugger

    debugger = ContextDebugger(enabled=True, path=tmp_path / "debug.jsonl", max_cached_chars=500)
    request = {"messages": [{"role": "user", "content": "original"}]}
    try:
        debugger.record("llm_request", device_id="one", turn_id="one", request=request)
        request["messages"][0]["content"] = "modified"
        event = debugger.events_for_device("one")[0]
        assert event["request"]["messages"][0]["content"] == "original"
        event["request"]["messages"][0]["content"] = "changed-by-reader"
        assert debugger.events_for_device("one")[0]["request"]["messages"][0]["content"] == "original"
        debugger.record("example", device_id="one", turn_id="large", content="x" * 1000)
        assert all(event["turn_id"] != "large" for event in debugger.events_for_device("one"))
        assert "large" in (tmp_path / "debug.jsonl").read_text(encoding="utf-8")
    finally:
        debugger.close()


def test_dashboard_does_not_read_old_files(tmp_path) -> None:
    from newtalk.context_debug import ContextDebugger

    path = tmp_path / "debug.jsonl"
    path.write_text(json.dumps({"device_id": "one", "turn_id": "old"}), encoding="utf-8")
    debugger = ContextDebugger(enabled=True, path=path)
    try:
        assert debugger.events_for_device("one") == []
    finally:
        debugger.close()
