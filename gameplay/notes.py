from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True, eq=False)
class Note:
    lane: int
    time: float
    type: str = "tap"
    duration: float = 0.0
    hit: bool = False
    hit_time: Optional[float] = None
