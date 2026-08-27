import asyncio

from newtalk.asr import AsrFinal, FakeASR
from newtalk.audio import AudioInputSession, SpeechBoundary, VadEvent


class BoundaryVadStream:
    def __init__(self) -> None:
        self.calls = 0

    def process(self, pcm: bytes) -> list[VadEvent]:
        del pcm
        self.calls += 1
        if self.calls == 1:
            return [VadEvent("speech_start", 0.8, 20.0)]
        if self.calls == 2:
            return [VadEvent("speech_end", 0.1, 40.0)]
        return []

    def flush(self) -> list[VadEvent]:
        return []

    def reset(self) -> None:
        return None


def test_audio_session_routes_one_utterance_to_asr() -> None:
    async def run() -> None:
        boundaries: list[SpeechBoundary] = []
        recognized: list[tuple[str, AsrFinal]] = []

        async def on_boundary(event: SpeechBoundary) -> None:
            boundaries.append(event)

        async def on_asr_event(utterance_id: str, event) -> None:
            assert isinstance(event, AsrFinal)
            recognized.append((utterance_id, event))

        session = AudioInputSession(
            vad_stream=BoundaryVadStream(),
            recognizer=FakeASR("识别结果"),
            on_boundary=on_boundary,
            on_asr_event=on_asr_event,
            pre_roll_ms=20,
        )
        await session.push(bytes(640))
        await session.push(bytes(640))
        await session.close()

        assert [event.kind for event in boundaries] == ["speech_start", "speech_end"]
        assert len(recognized) == 1
        assert recognized[0][0] == boundaries[0].utterance_id
        assert recognized[0][1].text == "识别结果"

    asyncio.run(run())


def test_audio_session_reports_recognizer_failure() -> None:
    class FailingASR(FakeASR):
        async def stream(self, audio_chunks, *, utterance_id: str):
            async for _ in audio_chunks:
                break
            if False:
                yield AsrFinal(utterance_id)
            raise RuntimeError("test ASR failure")

    async def run() -> None:
        failures: list[tuple[str, str]] = []

        async def on_boundary(event: SpeechBoundary) -> None:
            del event

        async def on_asr_event(utterance_id: str, event) -> None:
            del utterance_id, event

        async def on_asr_error(utterance_id: str, error: Exception) -> None:
            failures.append((utterance_id, str(error)))

        session = AudioInputSession(
            vad_stream=BoundaryVadStream(),
            recognizer=FailingASR(),
            on_boundary=on_boundary,
            on_asr_event=on_asr_event,
            on_asr_error=on_asr_error,
            pre_roll_ms=20,
        )
        await session.push(bytes(640))
        await session.push(bytes(640))
        await session.close()

        assert len(failures) == 1
        assert failures[0][1] == "test ASR failure"

    asyncio.run(run())
