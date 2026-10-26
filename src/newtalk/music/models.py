from dataclasses import asdict, dataclass

from newtalk.media.models import MediaUnavailable


class MusicUnavailable(MediaUnavailable):
    pass


@dataclass(frozen=True, slots=True)
class Song:
    index: int
    title: str
    artist: str
    mid: str
    album: str = ""

    def payload(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SongAudio:
    song: Song
    url: str
    cover: str = ""
