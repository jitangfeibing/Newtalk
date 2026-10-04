import asyncio
import os
from uuid import uuid4

import pytest

from newtalk.chat import DialogueExchange, SqlAlchemyDialogueStore
from newtalk.identity import IdentityService, SqlAlchemyIdentityStore


DATABASE_URL = os.getenv("NEWTALK_TEST_DATABASE_URL")


def _exchange(turn_id: str, *, member_id: str | None = None) -> DialogueExchange:
    return DialogueExchange(
        turn_id=turn_id,
        user_text=f"user-{turn_id}",
        assistant_text=f"assistant-{turn_id}",
        speaker_identity_id=member_id,
        speaker_display_name="Member" if member_id else "Guest",
        speaker_relationship="Family" if member_id else None,
    )


@pytest.mark.integration
@pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL integration URL is not configured")
def test_postgres_dialogue_restores_and_prunes_each_lane() -> None:
    async def scenario() -> None:
        run_id = uuid4().hex[:12]
        guest_turn_ids = [f"{run_id}-guest-{index}" for index in range(1, 4)]
        family_turn_id = f"{run_id}-family-1"
        identities = IdentityService(SqlAlchemyIdentityStore(DATABASE_URL))
        dialogues = SqlAlchemyDialogueStore(DATABASE_URL)
        await identities.start()
        await dialogues.start()
        try:
            family = await identities.register_device()
            member = await identities.create_identity(
                device_id=family.device.device_id,
                display_name="Dialogue Member",
            )
            opened = await dialogues.open(
                device_id=family.device.device_id,
                max_turns=2,
            )
            assert opened.resumed is False

            for turn_id in guest_turn_ids:
                assert await dialogues.append(
                    session_id=opened.session_id,
                    lane="guest",
                    exchange=_exchange(turn_id),
                    max_turns=2,
                )
            assert await dialogues.append(
                session_id=opened.session_id,
                lane="family",
                exchange=_exchange(family_turn_id, member_id=member.identity_id),
                max_turns=2,
            )
            assert not await dialogues.append(
                session_id=opened.session_id,
                lane="family",
                exchange=_exchange(family_turn_id, member_id=member.identity_id),
                max_turns=2,
            )

            restored = await dialogues.open(
                device_id=family.device.device_id,
                max_turns=2,
            )
            assert restored.session_id == opened.session_id
            assert [item.turn_id for item in restored.guest] == guest_turn_ids[1:]
            assert [item.turn_id for item in restored.family] == [family_turn_id]

            await dialogues.delete_identity(
                device_id=family.device.device_id,
                identity_id=member.identity_id,
            )
            cleaned = await dialogues.open(
                device_id=family.device.device_id,
                max_turns=2,
            )
            assert cleaned.family == ()
            assert [item.turn_id for item in cleaned.guest] == guest_turn_ids[1:]
        finally:
            await dialogues.close()
            await identities.close()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL integration URL is not configured")
def test_postgres_dialogue_serializes_concurrent_appends() -> None:
    async def scenario() -> None:
        identities = IdentityService(SqlAlchemyIdentityStore(DATABASE_URL))
        dialogues = SqlAlchemyDialogueStore(DATABASE_URL)
        await identities.start()
        await dialogues.start()
        try:
            family = await identities.register_device()
            opened = await dialogues.open(
                device_id=family.device.device_id,
                max_turns=2,
            )
            run_id = uuid4().hex[:12]
            turn_ids = [f"{run_id}-concurrent-{index}" for index in range(4)]

            results = await asyncio.gather(
                *(
                    dialogues.append(
                        session_id=opened.session_id,
                        lane="guest",
                        exchange=_exchange(turn_id),
                        max_turns=2,
                    )
                    for turn_id in turn_ids
                )
            )

            assert results == [True, True, True, True]
            restored = await dialogues.open(
                device_id=family.device.device_id,
                max_turns=2,
            )
            assert len(restored.guest) == 2
            assert {item.turn_id for item in restored.guest}.issubset(turn_ids)
        finally:
            await dialogues.close()
            await identities.close()

    asyncio.run(scenario())
