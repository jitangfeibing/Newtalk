import asyncio
import os

import pytest

from newtalk.app import create_chat_service
from newtalk.config import load_config


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("NEWTALK_RUN_LIVE_LLM") != "1",
        reason="set NEWTALK_RUN_LIVE_LLM=1 to call the configured real LLM",
    ),
]


def test_configured_llm_returns_streamed_text() -> None:
    async def exercise() -> str:
        config = load_config()
        if config.llm_backend != "openai":
            pytest.skip("NEWTALK_LLM_BACKEND must be openai for the live smoke test")

        service = create_chat_service(config)
        turn = service.create_turn(
            session_id="live-smoke-test",
            user_text="请只回复：Newtalk P3 正常",
        )
        try:
            return "".join([chunk async for chunk in service.stream_reply(turn)])
        finally:
            await service.aclose()

    assert asyncio.run(exercise()).strip()
