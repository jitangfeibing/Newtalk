from contextlib import aclosing
import asyncio
import os

import pytest

from newtalk.app import create_synthesizer
from newtalk.config import load_config


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("NEWTALK_RUN_LIVE_TTS") != "1",
        reason="set NEWTALK_RUN_LIVE_TTS=1 to call the configured real TTS",
    ),
]


async def one_text():
    yield "你好，我是 Newtalk。这是一次流式语音合成测试。"


def test_real_tts_returns_pcm_audio() -> None:
    async def exercise() -> list[bytes]:
        config = load_config()
        if config.tts_backend != "doubao":
            pytest.skip("NEWTALK_TTS_BACKEND must be doubao for the live smoke test")

        synthesizer = create_synthesizer(config)
        frames: list[bytes] = []
        try:
            async with aclosing(
                synthesizer.stream(one_text(), turn_id="live-tts-smoke-test")
            ) as stream:
                async for frame in stream:
                    frames.append(frame)
        finally:
            await synthesizer.aclose()
        return frames

    frames = asyncio.run(exercise())

    assert frames
    assert sum(len(frame) for frame in frames) > 1000
    assert all(len(frame) % 2 == 0 for frame in frames)
