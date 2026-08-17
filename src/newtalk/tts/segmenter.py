HARD_BOUNDARIES = frozenset("。！？!?；;\n")
SOFT_BOUNDARIES = frozenset("，,、：:")


class StreamingTextSegmenter:
    def __init__(self, *, min_chars: int = 12, max_chars: int = 60) -> None:
        if min_chars <= 0 or max_chars < min_chars:
            raise ValueError("Invalid text segment limits")
        self._min_chars = min_chars
        self._max_chars = max_chars
        self._buffer = ""

    def push(self, delta: str) -> list[str]:
        self._buffer += delta
        segments: list[str] = []

        while self._buffer:
            boundary = self._find_boundary()
            if boundary is None:
                break
            segment = self._buffer[:boundary].strip()
            self._buffer = self._buffer[boundary:]
            if segment:
                segments.append(segment)

        return segments

    def flush(self) -> str | None:
        segment = self._buffer.strip()
        self._buffer = ""
        return segment or None

    def _find_boundary(self) -> int | None:
        for index, character in enumerate(self._buffer, start=1):
            if character in HARD_BOUNDARIES:
                return index
            if character in SOFT_BOUNDARIES and index >= self._min_chars:
                return index

        if len(self._buffer) < self._max_chars:
            return None

        for index in range(self._max_chars, self._min_chars - 1, -1):
            if self._buffer[index - 1].isspace():
                return index
        return self._max_chars
