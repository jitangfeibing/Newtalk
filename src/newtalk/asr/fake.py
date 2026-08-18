from collections.abc import AsyncIterator

from newtalk.asr.model import AsrEvent, AsrFinal


class FakeASR:
    def __init__(self, text: str = "这是一次语音输入测试") -> None:
        self._text = text

    async def stream(
        self,
        audio_chunks: AsyncIterator[bytes],
        *,
        utterance_id: str,
    ) -> AsyncIterator[AsrEvent]:
        del utterance_id
        byte_count = 0
        async for chunk in audio_chunks:
            byte_count += len(chunk)
        if byte_count > 0:
            yield AsrFinal(self._text)

    async def aclose(self) -> None:
        return None
