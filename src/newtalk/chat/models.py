from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Turn:
    turn_id: str
    session_id: str
    user_text: str
    created_at: datetime
