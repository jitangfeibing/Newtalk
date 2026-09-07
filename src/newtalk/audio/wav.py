from io import BytesIO
import wave

from newtalk.audio.model import INPUT_AUDIO_FORMAT


def pcm_s16le_to_wav(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % 2:
        raise ValueError("PCM S16LE audio must contain complete samples")
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(INPUT_AUDIO_FORMAT.channels)
        stream.setsampwidth(2)
        stream.setframerate(INPUT_AUDIO_FORMAT.sample_rate)
        stream.writeframes(pcm)
    return output.getvalue()
