from newtalk.audio.vad import WINDOW_BYTES, SileroVadStream


class ProbabilitySequence:
    def __init__(self, values: list[float]) -> None:
        self._values = iter(values)
        self.reset_count = 0

    def __call__(self, pcm: bytes) -> float:
        assert len(pcm) == WINDOW_BYTES
        return next(self._values)

    def reset(self) -> None:
        self.reset_count += 1


def test_vad_uses_sliding_start_and_silence_end_thresholds() -> None:
    predictor = ProbabilitySequence([0.9, 0.8, 0.7, 0.1, 0.1, 0.1, 0.1])
    stream = SileroVadStream(
        predictor,
        min_silence_duration_ms=64,
        start_window_size=5,
        start_window_threshold=3,
    )

    events = stream.process(bytes(WINDOW_BYTES * 7))

    assert [event.kind for event in events] == ["speech_start", "speech_end"]
    assert events[0].audio_ms == 96.0
    assert events[1].audio_ms == 224.0
    assert predictor.reset_count == 1


def test_vad_silence_does_not_start_speech() -> None:
    stream = SileroVadStream(ProbabilitySequence([0.0] * 8))

    assert stream.process(bytes(WINDOW_BYTES * 8)) == []
    assert stream.flush() == []


def test_vad_flush_ends_active_speech() -> None:
    stream = SileroVadStream(ProbabilitySequence([0.9, 0.9, 0.9]))
    assert stream.process(bytes(WINDOW_BYTES * 3))[0].kind == "speech_start"

    events = stream.flush()

    assert len(events) == 1
    assert events[0].kind == "speech_end"


def test_vad_reports_last_confirmed_voice_without_changing_end_boundary() -> None:
    stream = SileroVadStream(
        ProbabilitySequence([0.9, 0.8, 0.7, 0.1, 0.1, 0.1, 0.1]),
        min_silence_duration_ms=64,
    )
    start, end = stream.process(bytes(WINDOW_BYTES * 7))
    assert start.audio_ms == 96
    assert end.audio_ms == 224
    # 滑动窗口在 160ms 仍确认语音；这是检测器参考点，不是人工标注的词尾。
    assert end.last_voice_audio_ms == 160
    assert end.audio_ms - end.last_voice_audio_ms == 64


def test_vad_flush_keeps_endpoint_before_reset_and_next_capture_starts_at_zero() -> None:
    stream = SileroVadStream(ProbabilitySequence([0.9] * 6))
    stream.process(bytes(WINDOW_BYTES * 3))
    end = stream.flush()[0]
    assert end.last_voice_audio_ms == 96
    assert stream.process(bytes(WINDOW_BYTES * 3))[0].audio_ms == 96
