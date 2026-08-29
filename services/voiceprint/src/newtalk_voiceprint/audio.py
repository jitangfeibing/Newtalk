from dataclasses import dataclass
from io import BytesIO
import wave


class InvalidAudioError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedAudio:
    wav_bytes: bytes
    duration_seconds: float
    frame_count: int


def validate_pcm_wav(
    wav_bytes: bytes,
    *,
    min_seconds: float,
    max_seconds: float,
    max_bytes: int,
) -> ValidatedAudio:
    if not wav_bytes or len(wav_bytes) > max_bytes:
        raise InvalidAudioError("audio size is invalid")
    try:
        with wave.open(BytesIO(wav_bytes), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frame_count = stream.getnframes()
            compression = stream.getcomptype()
    except (EOFError, wave.Error) as exc:
        raise InvalidAudioError("audio must be a valid PCM WAV file") from exc
    if channels != 1 or sample_width != 2 or sample_rate != 16_000 or compression != "NONE":
        raise InvalidAudioError("audio must be 16kHz mono PCM16 WAV")
    duration_seconds = frame_count / sample_rate
    if not min_seconds <= duration_seconds <= max_seconds:
        raise InvalidAudioError(
            f"audio duration must be between {min_seconds:g} and {max_seconds:g} seconds"
        )
    return ValidatedAudio(wav_bytes, duration_seconds, frame_count)

