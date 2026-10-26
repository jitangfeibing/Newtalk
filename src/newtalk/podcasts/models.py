from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Album:
    index: int
    title: str
    anchor: str
    paid: bool = False
    tracks: int = 0


@dataclass(frozen=True, slots=True)
class Episode:
    title: str
    order: int | None
    duration: int
    url: str = field(repr=False)
    paid: bool = False

    @property
    def playable(self) -> bool:
        return bool(self.url) and not self.paid


@dataclass(frozen=True, slots=True)
class EpisodePage:
    album: Album
    page: int
    max_page: int
    episodes: tuple[Episode, ...]
