from io import BytesIO
from pathlib import Path
import tempfile
import threading
import wave

import numpy as np


class SpeakerEmbedder:
    model_name: str

    def start(self) -> None:
        return None

    def embed(self, wav_bytes: bytes) -> np.ndarray:
        raise NotImplementedError


def normalize_embedding(value: np.ndarray) -> np.ndarray:
    embedding = np.asarray(value, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(embedding))
    if not embedding.size or not np.isfinite(norm) or norm <= 1e-8:
        raise ValueError("speaker embedding is empty or invalid")
    return embedding / norm


class DeterministicEmbedder(SpeakerEmbedder):
    """Fast test backend. It is not a production speaker recognizer."""

    model_name = "deterministic-test-v1"

    def embed(self, wav_bytes: bytes) -> np.ndarray:
        with wave.open(BytesIO(wav_bytes), "rb") as stream:
            samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")
        signal = samples.astype(np.float32) / 32768.0
        if signal.size < 32:
            raise ValueError("audio has too few samples")
        spectrum = np.abs(np.fft.rfft(signal * np.hanning(signal.size)))
        bands = np.array_split(spectrum, 64)
        features = np.asarray([float(np.log1p(band.mean())) for band in bands])
        return normalize_embedding(features)


class CampPlusEmbedder(SpeakerEmbedder):
    def __init__(self, *, model_id: str, device: str) -> None:
        self.model_name = model_id
        self._model_id = model_id
        self._device = device
        self._pipeline = None
        self._lock = threading.Lock()

    def start(self) -> None:
        try:
            from modelscope.pipelines import pipeline
            from modelscope.utils.constant import Tasks
        except ImportError as exc:
            raise RuntimeError(
                "CAM++ dependencies are missing; install newtalk-voiceprint[campplus]"
            ) from exc
        self._pipeline = pipeline(
            task=Tasks.speaker_verification,
            model=self._model_id,
            device=self._device,
        )

    def embed(self, wav_bytes: bytes) -> np.ndarray:
        if self._pipeline is None:
            raise RuntimeError("CAM++ model is not initialized")
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as audio:
                audio.write(wav_bytes)
                path = Path(audio.name)
            with self._lock:
                result = self._pipeline([str(path)], output_emb=True)
            value = result["embs"][0]
            if hasattr(value, "detach"):
                value = value.detach().cpu().numpy()
            return normalize_embedding(value)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
