import asyncio

import pytest

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


class ScriptedVadStream(BoundaryVadStream):
    def __init__(self, boundaries: dict[int, str]) -> None:
        super().__init__()
        self.boundaries = boundaries

    def process(self, pcm: bytes) -> list[VadEvent]:
        del pcm
        self.calls += 1
        kind = self.boundaries.get(self.calls)
        if kind is None:
            return []
        return [VadEvent(kind, 0.8 if kind == "speech_start" else 0.1, self.calls * 20)]


class FailFirstASR(FakeASR):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.finished = asyncio.Event()

    async def stream(self, audio_chunks, *, utterance_id: str):
        del utterance_id
        self.calls += 1
        count = 0
        try:
            async for _ in audio_chunks:
                count += 1
                if self.calls == 1 and count == 3:
                    raise RuntimeError("test ASR failure")
            yield AsrFinal("recovered")
        finally:
            self.finished.set()


class GatedASR(FakeASR):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.chunks: list[bytes] = []

    async def stream(self, audio_chunks, *, utterance_id: str):
        del utterance_id
        self.started.set()
        try:
            await self.release.wait()
            async for chunk in audio_chunks:
                self.chunks.append(chunk)
            yield AsrFinal("complete")
        finally:
            self.finished.set()


class AudioRecorder:
    def __init__(self) -> None:
        self.boundaries: list[SpeechBoundary] = []
        self.events: list[tuple[str, AsrFinal]] = []
        self.errors: list[tuple[str, Exception]] = []
        self.audio: list[tuple[str, bytes]] = []
        self.failed = asyncio.Event()

    async def on_boundary(self, event: SpeechBoundary) -> None:
        self.boundaries.append(event)

    async def on_asr_event(self, utterance_id: str, event) -> None:
        self.events.append((utterance_id, event))

    async def on_asr_error(self, utterance_id: str, error: Exception) -> None:
        self.errors.append((utterance_id, error))
        self.failed.set()

    async def on_utterance_audio(self, utterance_id: str, pcm: bytes) -> None:
        self.audio.append((utterance_id, pcm))

    def session(self, vad_stream, recognizer) -> AudioInputSession:
        return AudioInputSession(
            vad_stream=vad_stream,
            recognizer=recognizer,
            on_boundary=self.on_boundary,
            on_asr_event=self.on_asr_event,
            on_asr_error=self.on_asr_error,
            on_utterance_audio=self.on_utterance_audio,
            pre_roll_ms=20,
        )


def test_audio_session_passes_vad_endpoint_to_boundary_without_changing_pcm():
    class TimedVad(BoundaryVadStream):
        def process(self, pcm):
            return [VadEvent(event.kind, event.probability, event.audio_ms,
                             20.0 if event.kind == 'speech_end' else None)
                    for event in super().process(pcm)]

    async def scenario():
        recorder = AudioRecorder()
        session = recorder.session(TimedVad(), FakeASR())
        try:
            await session.push(bytes(640))
            await session.push(bytes(640))
            await session.close()
            assert recorder.boundaries[-1].last_voice_audio_ms == 20
            assert recorder.boundaries[-1].audio_ms == 40
            assert recorder.audio[0][1] == bytes(1280)
        finally:
            await session.abort()

    asyncio.run(scenario())


def test_audio_session_routes_one_utterance_to_asr() -> None:
    async def run() -> None:
        boundaries: list[SpeechBoundary] = []
        recognized: list[tuple[str, AsrFinal]] = []
        utterance_audio: list[tuple[str, bytes]] = []

        async def on_boundary(event: SpeechBoundary) -> None:
            boundaries.append(event)

        async def on_asr_event(utterance_id: str, event) -> None:
            assert isinstance(event, AsrFinal)
            recognized.append((utterance_id, event))

        async def on_utterance_audio(utterance_id: str, pcm: bytes) -> None:
            utterance_audio.append((utterance_id, pcm))

        session = AudioInputSession(
            vad_stream=BoundaryVadStream(),
            recognizer=FakeASR("识别结果"),
            on_boundary=on_boundary,
            on_asr_event=on_asr_event,
            on_utterance_audio=on_utterance_audio,
            pre_roll_ms=20,
        )
        await session.push(bytes(640))
        await session.push(bytes(640))
        await session.close()

        assert [event.kind for event in boundaries] == ["speech_start", "speech_end"]
        assert len(recognized) == 1
        assert recognized[0][0] == boundaries[0].utterance_id
        assert recognized[0][1].text == "识别结果"
        assert utterance_audio == [(boundaries[0].utterance_id, bytes(1280))]

    asyncio.run(run())


def test_audio_session_flushes_voiceprint_audio_when_capture_stops_mid_speech() -> None:
    class OpenEndedVadStream(BoundaryVadStream):
        def process(self, pcm: bytes) -> list[VadEvent]:
            del pcm
            self.calls += 1
            return [VadEvent("speech_start", 0.8, 20.0)] if self.calls == 1 else []

    async def run() -> None:
        captured: list[bytes] = []

        async def ignore(*args) -> None:
            del args

        async def on_utterance_audio(utterance_id: str, pcm: bytes) -> None:
            del utterance_id
            captured.append(pcm)

        session = AudioInputSession(
            vad_stream=OpenEndedVadStream(),
            recognizer=FakeASR("识别结果"),
            on_boundary=ignore,
            on_asr_event=ignore,
            on_utterance_audio=on_utterance_audio,
            pre_roll_ms=20,
        )
        await session.push(bytes(640))
        await session.close()

        assert captured == [bytes(640)]

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


def test_audio_session_abort_cancels_hanging_recognition() -> None:
    class OpenEndedVadStream(BoundaryVadStream):
        def process(self, pcm: bytes) -> list[VadEvent]:
            del pcm
            self.calls += 1
            return [VadEvent("speech_start", 0.8, 20.0)] if self.calls == 1 else []

    class HangingASR(FakeASR):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.cancelled = False

        async def stream(self, audio_chunks, *, utterance_id: str):
            del utterance_id
            async for _ in audio_chunks:
                self.started.set()
                break
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            if False:
                yield AsrFinal("")

    async def run() -> None:
        recognizer = HangingASR()

        async def ignore(*args) -> None:
            del args

        session = AudioInputSession(
            vad_stream=OpenEndedVadStream(),
            recognizer=recognizer,
            on_boundary=ignore,
            on_asr_event=ignore,
            pre_roll_ms=20,
        )
        await session.push(bytes(640))
        await asyncio.wait_for(recognizer.started.wait(), timeout=1)

        await asyncio.wait_for(session.abort(), timeout=0.1)

        assert recognizer.cancelled is True

    asyncio.run(run())


def test_asr_failure_does_not_block_continued_audio() -> None:
    async def run() -> None:
        recorder = AudioRecorder()
        recognizer = FailFirstASR()
        vad = ScriptedVadStream({1: "speech_start"})
        session = recorder.session(vad, recognizer)
        try:
            for _ in range(3):
                await asyncio.wait_for(session.push(bytes(640)), timeout=1)
            await asyncio.wait_for(recognizer.finished.wait(), timeout=1)
            await asyncio.wait_for(recorder.failed.wait(), timeout=1)
            for _ in range(397):
                await asyncio.wait_for(session.push(bytes(640)), timeout=1)

            assert vad.calls == 400
            assert len(recorder.errors) == 1
            assert str(recorder.errors[0][1]) == "test ASR failure"
        finally:
            await session.abort()

    asyncio.run(run())


def test_asr_failure_keeps_speech_end_without_voiceprint_audio() -> None:
    async def run() -> None:
        recorder = AudioRecorder()
        recognizer = FailFirstASR()
        session = recorder.session(
            ScriptedVadStream({1: "speech_start", 4: "speech_end"}), recognizer
        )
        try:
            for _ in range(3):
                await asyncio.wait_for(session.push(bytes(640)), timeout=1)
            await asyncio.wait_for(recognizer.finished.wait(), timeout=1)
            await asyncio.wait_for(recorder.failed.wait(), timeout=1)
            await asyncio.wait_for(session.push(bytes(640)), timeout=1)
            await asyncio.wait_for(session.close(), timeout=1)

            assert [event.kind for event in recorder.boundaries] == [
                "speech_start", "speech_end"
            ]
            assert recorder.boundaries[0].utterance_id == recorder.boundaries[1].utterance_id
            assert len(recorder.errors) == 1
            assert recorder.audio == []
        finally:
            await session.abort()

    asyncio.run(run())


def test_next_utterance_recovers_after_asr_failure() -> None:
    async def run() -> None:
        recorder = AudioRecorder()
        recognizer = FailFirstASR()
        session = recorder.session(
            ScriptedVadStream({
                1: "speech_start", 4: "speech_end",
                5: "speech_start", 6: "speech_end",
            }),
            recognizer,
        )
        try:
            for _ in range(3):
                await asyncio.wait_for(session.push(bytes(640)), timeout=1)
            await asyncio.wait_for(recognizer.finished.wait(), timeout=1)
            await asyncio.wait_for(recorder.failed.wait(), timeout=1)
            for _ in range(3):
                await asyncio.wait_for(session.push(bytes(640)), timeout=1)
            await asyncio.wait_for(session.close(), timeout=1)

            first_id = recorder.boundaries[0].utterance_id
            second_id = recorder.boundaries[2].utterance_id
            assert first_id != second_id
            assert [event.kind for event in recorder.boundaries] == [
                "speech_start", "speech_end", "speech_start", "speech_end"
            ]
            assert recorder.events == [(second_id, AsrFinal("recovered"))]
            assert recorder.audio == [(second_id, bytes(1280))]
            assert len(recorder.errors) == 1
        finally:
            await session.abort()

    asyncio.run(run())


def test_slow_asr_start_buffers_three_seconds_without_losing_frames() -> None:
    async def run() -> None:
        recorder = AudioRecorder()
        recognizer = GatedASR()
        session = recorder.session(
            ScriptedVadStream({1: "speech_start", 150: "speech_end"}), recognizer
        )
        chunks = [index.to_bytes(2, "little") * 320 for index in range(150)]
        try:
            await asyncio.wait_for(session.push(chunks[0]), timeout=1)
            await asyncio.wait_for(recognizer.started.wait(), timeout=1)
            for chunk in chunks[1:]:
                await asyncio.wait_for(session.push(chunk), timeout=1)

            assert not recognizer.release.is_set()
            recognizer.release.set()
            await asyncio.wait_for(session.close(), timeout=1)

            assert recognizer.chunks == chunks
            assert recorder.events == [
                (recorder.boundaries[0].utterance_id, AsrFinal("complete"))
            ]
            assert recorder.errors == []
            assert recorder.audio == [
                (recorder.boundaries[0].utterance_id, b"".join(chunks))
            ]
        finally:
            recognizer.release.set()
            await session.abort()

    asyncio.run(run())


@pytest.mark.parametrize("end_frame", [500, 602])
def test_full_asr_queue_cancels_once_and_keeps_vad_running(end_frame: int) -> None:
    async def run() -> None:
        recorder = AudioRecorder()
        recognizer = GatedASR()
        session = recorder.session(
            ScriptedVadStream({1: "speech_start", end_frame: "speech_end"}), recognizer
        )
        try:
            await asyncio.wait_for(session.push(bytes(640)), timeout=1)
            await asyncio.wait_for(recognizer.started.wait(), timeout=1)
            for _ in range(end_frame - 1):
                await asyncio.wait_for(session.push(bytes(640)), timeout=1)
            await asyncio.wait_for(recognizer.finished.wait(), timeout=1)
            await asyncio.wait_for(session.close(), timeout=1)

            assert len(recorder.errors) == 1
            assert recorder.errors[0][0] == recorder.boundaries[0].utterance_id
            assert isinstance(recorder.errors[0][1], RuntimeError)
            assert [event.kind for event in recorder.boundaries] == [
                "speech_start", "speech_end"
            ]
            assert recorder.audio == []
            assert recorder.events == []
        finally:
            recognizer.release.set()
            await session.abort()

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True])
def test_recognition_ending_before_speech_end_does_not_block_audio(cancelled: bool) -> None:
    class EarlyEndingASR(FakeASR):
        def __init__(self) -> None:
            super().__init__()
            self.finished = asyncio.Event()
            self.chunk_count = 0

        async def stream(self, audio_chunks, *, utterance_id: str):
            del utterance_id
            try:
                async for _ in audio_chunks:
                    self.chunk_count += 1
                    if self.chunk_count == 3:
                        if cancelled:
                            raise asyncio.CancelledError
                        yield AsrFinal("early result")
                        return
            finally:
                self.finished.set()

    async def run() -> None:
        recorder = AudioRecorder()
        recognizer = EarlyEndingASR()
        vad = ScriptedVadStream({1: "speech_start", 602: "speech_end"})
        session = recorder.session(vad, recognizer)
        try:
            for _ in range(3):
                await asyncio.wait_for(session.push(bytes(640)), timeout=1)
            await asyncio.wait_for(recognizer.finished.wait(), timeout=1)
            for _ in range(599):
                await asyncio.wait_for(session.push(bytes(640)), timeout=1)
            await asyncio.wait_for(session.close(), timeout=1)

            assert vad.calls == 602
            assert recognizer.chunk_count == 3
            assert recorder.errors == []
            expected_events = [] if cancelled else [
                (recorder.boundaries[0].utterance_id, AsrFinal("early result"))
            ]
            assert recorder.events == expected_events
            assert [event.kind for event in recorder.boundaries] == [
                "speech_start", "speech_end"
            ]
        finally:
            await session.abort()

    asyncio.run(run())
