from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import numpy as np
import onnxruntime

from newtalk.audio.model import VadEvent, VoiceActivityStream


SAMPLE_RATE = 16000
WINDOW_SAMPLES = 512
WINDOW_BYTES = WINDOW_SAMPLES * 2
CONTEXT_SAMPLES = 64


class SileroVad:
    def __init__(
        self,
        model_path: Path,
        *,
        threshold: float = 0.5,
        threshold_low: float = 0.3,
        min_silence_duration_ms: int = 600,
        start_window_size: int = 5,
        start_window_threshold: int = 3,
    ) -> None:
        if not model_path.is_file():
            raise RuntimeError(f"Silero VAD model does not exist: {model_path}")
        if not 0 <= threshold_low < threshold <= 1:
            raise ValueError("VAD thresholds must satisfy 0 <= low < high <= 1")
        if min_silence_duration_ms <= 0:
            raise ValueError("VAD silence duration must be greater than zero")
        if not 1 <= start_window_threshold <= start_window_size:
            raise ValueError("Invalid VAD start window threshold")

        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
            sess_options=options,
        )
        self._threshold = threshold
        self._threshold_low = threshold_low
        self._min_silence_duration_ms = min_silence_duration_ms
        self._start_window_size = start_window_size
        self._start_window_threshold = start_window_threshold

    def create_stream(self) -> VoiceActivityStream:
        return SileroVadStream(
            _OnnxSileroPredictor(self._session),
            threshold=self._threshold,
            threshold_low=self._threshold_low,
            min_silence_duration_ms=self._min_silence_duration_ms,
            start_window_size=self._start_window_size,
            start_window_threshold=self._start_window_threshold,
        )


class SileroVadStream:
    def __init__(
        self,
        predictor: Callable[[bytes], float],
        *,
        threshold: float = 0.5,
        threshold_low: float = 0.3,
        min_silence_duration_ms: int = 600,
        start_window_size: int = 5,
        start_window_threshold: int = 3,
    ) -> None:
        self._predictor = predictor
        self._threshold = threshold
        self._threshold_low = threshold_low
        self._min_silence_samples = int(
            SAMPLE_RATE * min_silence_duration_ms / 1000
        )
        self._start_window_size = start_window_size
        self._start_window_threshold = start_window_threshold
        self._buffer = bytearray()
        self._voice_window: deque[bool] = deque(maxlen=start_window_size)
        self._last_is_voice = False
        self._speaking = False
        self._processed_samples = 0
        self._last_voice_sample = 0

    def process(self, pcm: bytes) -> list[VadEvent]:
        if len(pcm) % 2 != 0:
            raise ValueError("PCM S16LE frames must contain an even number of bytes")
        self._buffer.extend(pcm)
        events: list[VadEvent] = []

        while len(self._buffer) >= WINDOW_BYTES:
            chunk = bytes(self._buffer[:WINDOW_BYTES])
            del self._buffer[:WINDOW_BYTES]
            probability = float(self._predictor(chunk))
            self._processed_samples += WINDOW_SAMPLES

            if probability >= self._threshold:
                is_voice = True
            elif probability <= self._threshold_low:
                is_voice = False
            else:
                is_voice = self._last_is_voice
            self._last_is_voice = is_voice
            self._voice_window.append(is_voice)
            confirmed_voice = (
                self._voice_window.count(True) >= self._start_window_threshold
            )

            if confirmed_voice:
                self._last_voice_sample = self._processed_samples
                if not self._speaking:
                    self._speaking = True
                    events.append(self._event("speech_start", probability))
                continue

            if (
                self._speaking
                and self._processed_samples - self._last_voice_sample
                >= self._min_silence_samples
            ):
                self._speaking = False
                events.append(self._event("speech_end", probability))
                self._reset_model_state()

        return events

    def flush(self) -> list[VadEvent]:
        if not self._speaking:
            self.reset()
            return []
        event = self._event("speech_end", 0.0)
        self.reset()
        return [event]

    def reset(self) -> None:
        self._buffer.clear()
        self._voice_window.clear()
        self._last_is_voice = False
        self._speaking = False
        self._processed_samples = 0
        self._last_voice_sample = 0
        self._reset_model_state()

    def _event(
        self,
        kind: Literal["speech_start", "speech_end"],
        probability: float,
    ) -> VadEvent:
        return VadEvent(
            kind=kind,
            probability=probability,
            audio_ms=self._processed_samples * 1000 / SAMPLE_RATE,
        )

    def _reset_model_state(self) -> None:
        reset = getattr(self._predictor, "reset", None)
        if reset is not None:
            reset()
        self._voice_window.clear()
        self._last_is_voice = False


class _OnnxSileroPredictor:
    def __init__(self, session: Any) -> None:
        self._session = session
        self.reset()

    def __call__(self, pcm: bytes) -> float:
        samples = np.frombuffer(pcm, dtype="<i2")
        if samples.size != WINDOW_SAMPLES:
            raise ValueError(f"Silero VAD requires {WINDOW_SAMPLES} samples")
        audio = samples.astype(np.float32).reshape(1, -1) / 32768.0
        model_input = np.concatenate((self._context, audio), axis=1).astype(
            np.float32
        )
        output, state = self._session.run(
            None,
            {
                "input": model_input,
                "state": self._state,
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            },
        )
        self._state = state
        self._context = model_input[:, -CONTEXT_SAMPLES:]
        return float(output.item())

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)
