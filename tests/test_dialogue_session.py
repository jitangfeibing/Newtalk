import pytest

from newtalk.chat import ChatMessage, ChatService, DialogueSession


def _completed_turn(service: ChatService, session: DialogueSession, text: str):
    return service.create_turn(
        session_id=session.session_id,
        user_text=text,
        messages=session.messages_for(text),
    )


def test_dialogue_session_builds_context_from_completed_turns() -> None:
    service = ChatService()
    session = DialogueSession("session-1", max_turns=3, max_chars=1000)
    first = _completed_turn(service, session, "我叫小明")
    session.commit(first, "你好，小明")

    assert session.messages_for("我叫什么？") == (
        ChatMessage("user", "我叫小明"),
        ChatMessage("assistant", "你好，小明"),
        ChatMessage("user", "我叫什么？"),
    )


def test_dialogue_session_keeps_only_the_latest_turns() -> None:
    service = ChatService()
    session = DialogueSession("session-1", max_turns=2, max_chars=1000)
    for index in range(3):
        turn = _completed_turn(service, session, f"用户{index}")
        session.commit(turn, f"助手{index}")

    assert [exchange.user_text for exchange in session.exchanges] == [
        "用户1",
        "用户2",
    ]
    assert session.messages_for("当前") == (
        ChatMessage("user", "用户1"),
        ChatMessage("assistant", "助手1"),
        ChatMessage("user", "用户2"),
        ChatMessage("assistant", "助手2"),
        ChatMessage("user", "当前"),
    )


def test_dialogue_session_applies_a_contiguous_character_window() -> None:
    service = ChatService()
    session = DialogueSession("session-1", max_turns=5, max_chars=12)
    for user, assistant in (("111", "aaa"), ("22", "bb"), ("3", "c")):
        turn = _completed_turn(service, session, user)
        session.commit(turn, assistant)

    assert session.messages_for("now") == (
        ChatMessage("user", "22"),
        ChatMessage("assistant", "bb"),
        ChatMessage("user", "3"),
        ChatMessage("assistant", "c"),
        ChatMessage("user", "now"),
    )


def test_dialogue_session_rejects_cross_session_and_duplicate_commits() -> None:
    service = ChatService()
    session = DialogueSession("session-1", max_turns=2, max_chars=100)
    turn = _completed_turn(service, session, "你好")
    session.commit(turn, "你好")

    with pytest.raises(ValueError, match="already been committed"):
        session.commit(turn, "重复")

    other_turn = service.create_turn(session_id="session-2", user_text="错误")
    with pytest.raises(ValueError, match="different session"):
        session.commit(other_turn, "错误")
