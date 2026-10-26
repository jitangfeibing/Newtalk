import asyncio
import math
import sys
import wave
from array import array
from collections import OrderedDict
from contextlib import aclosing
from io import BytesIO

from newtalk.reminders.models import Reminder
from newtalk.tts import TextToSpeech


class ReminderAudio:
    def __init__(self, synthesizer: TextToSpeech) -> None:
        self.synthesizer = synthesizer
        self._slots = asyncio.Semaphore(2)
        self._cache: OrderedDict[str, bytes] = OrderedDict()

    async def render(self, item: Reminder) -> bytes:
        key = item.delivery_id
        if key is None:
            raise ValueError("Reminder has no delivery")
        if key in self._cache:
            return self._cache[key]
        async with asyncio.timeout(15), self._slots:
            audio_format = self.synthesizer.audio_format
            if audio_format.codec != "pcm_s16le" or audio_format.channels != 1:
                raise ValueError("Reminder audio requires mono PCM16")
            rate = audio_format.sample_rate
            pcm = bytearray()

            async def text():
                yield f"提醒你：{item.content}"

            async with aclosing(self.synthesizer.stream(text(), turn_id=f"reminder-{key}")) as frames:
                async for frame in frames:
                    if not isinstance(frame, bytes) or not frame or len(frame) % 2:
                        raise ValueError("Invalid reminder audio frame")
                    pcm.extend(frame)
                    if len(pcm) > rate * 2 * 60:
                        raise ValueError("Reminder audio exceeds one minute")
            if not pcm:
                raise ValueError("Reminder TTS returned no audio")
            cue = array("h", (int(3000 * math.sin(2 * math.pi * 660 * index / rate)
                                  * math.sin(math.pi * index / (rate // 5)))
                              for index in range(rate // 5)))
            if sys.byteorder != "little":
                cue.byteswap()
            result = BytesIO()
            with wave.open(result, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes(cue.tobytes() + bytes(rate // 10 * 2) + pcm)
            audio = result.getvalue()
            self._cache[key] = audio
            while len(self._cache) > 16:
                self._cache.popitem(last=False)
            return audio

    async def close(self) -> None:
        self._cache.clear()
        await self.synthesizer.aclose()
