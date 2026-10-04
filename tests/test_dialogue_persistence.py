import asyncio

from newtalk.chat import DialogueExchange, InMemoryDialogueStore


def exchange(turn_id: str, *, member: bool = False) -> DialogueExchange:
    return DialogueExchange(
        turn_id=turn_id,
        user_text=f"user-{turn_id}",
        assistant_text=f"assistant-{turn_id}",
        speaker_identity_id="member-1" if member else None,
        speaker_display_name="小明" if member else "Guest",
        speaker_relationship="家人" if member else None,
    )


def test_in_memory_dialogue_store_restores_scoped_sliding_windows() -> None:
    async def exercise() -> None:
        store = InMemoryDialogueStore()
        first = await store.open(device_id="02:00:00:00:00:01", max_turns=2)
        assert first.resumed is False

        for turn_id in ("guest-1", "guest-2", "guest-3"):
            assert await store.append(
                session_id=first.session_id,
                lane="guest",
                exchange=exchange(turn_id),
                max_turns=2,
            )
        assert await store.append(
            session_id=first.session_id,
            lane="family",
            exchange=exchange("family-1", member=True),
            max_turns=2,
        )
        assert not await store.append(
            session_id=first.session_id,
            lane="family",
            exchange=exchange("family-1", member=True),
            max_turns=2,
        )

        restored = await store.open(device_id="02:00:00:00:00:01", max_turns=2)
        other = await store.open(device_id="02:00:00:00:00:02", max_turns=2)

        assert restored.session_id == first.session_id
        assert restored.resumed is True
        assert [item.turn_id for item in restored.guest] == ["guest-2", "guest-3"]
        assert [item.turn_id for item in restored.family] == ["family-1"]
        assert [item.exchange.turn_id for item in restored.history] == [
            "guest-2",
            "guest-3",
            "family-1",
        ]
        assert other.session_id != first.session_id
        assert other.history == ()

        await store.delete_identity(
            device_id="02:00:00:00:00:01",
            identity_id="member-1",
        )
        cleaned = await store.open(device_id="02:00:00:00:00:01", max_turns=2)
        assert cleaned.family == ()
        assert [item.turn_id for item in cleaned.guest] == ["guest-2", "guest-3"]

    asyncio.run(exercise())
