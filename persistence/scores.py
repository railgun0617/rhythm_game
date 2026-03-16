
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from datetime import datetime

from runtime_paths import SCORES_DIR

SCORES_DIR.mkdir(parents=True, exist_ok=True)
MAX_SCORES_PER_CHART = 50

@dataclass
class ScoreEntry:
    timestamp: str
    song: str
    difficulty: str
    score: int
    max_score: int
    accuracy: float
    max_combo: int
    judgements: Dict[str, int]


def _score_sort_key(entry: ScoreEntry):
    return (entry.score, entry.accuracy, entry.timestamp)

def _chart_file(song: str, difficulty: str) -> Path:
    safe_song = song.replace("/", "_")
    safe_diff = difficulty.lower()
    return SCORES_DIR / f"{safe_song}_{safe_diff}.jsonl"


def _load_chart(song: str, difficulty: str) -> List[ScoreEntry]:
    entries: List[ScoreEntry] = []
    chart_file = _chart_file(song, difficulty)
    if not chart_file.exists():
        return entries
    with chart_file.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                entries.append(ScoreEntry(**obj))
            except Exception:
                continue
    entries.sort(key=_score_sort_key, reverse=True)
    return entries


def _write_chart(song: str, difficulty: str, entries: Sequence[ScoreEntry]) -> None:
    chart_file = _chart_file(song, difficulty)
    SCORES_DIR.mkdir(parents=True, exist_ok=True)
    with chart_file.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


def _load_all() -> List[ScoreEntry]:
    entries: List[ScoreEntry] = []
    if not SCORES_DIR.exists():
        return entries
    for chart_file in SCORES_DIR.glob("*.jsonl"):
        with chart_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    entries.append(ScoreEntry(**obj))
                except Exception:
                    continue
    entries.sort(key=_score_sort_key, reverse=True)
    return entries


def add_score(song: str, difficulty: str, *, score: int, max_score: int, accuracy: float, max_combo: int, judgements: Dict[str, int]) -> ScoreEntry:
    entry = ScoreEntry(
        timestamp=datetime.utcnow().isoformat(timespec="seconds")+"Z",
        song=song,
        difficulty=difficulty,
        score=score,
        max_score=max_score,
        accuracy=accuracy,
        max_combo=max_combo,
        judgements=judgements
    )
    entries = _load_chart(song, difficulty)
    entries.append(entry)
    entries.sort(key=_score_sort_key, reverse=True)
    if len(entries) > MAX_SCORES_PER_CHART:
        entries = entries[:MAX_SCORES_PER_CHART]
    _write_chart(song, difficulty, entries)
    return entry

def best_for(song: str, difficulty: str) -> Optional[ScoreEntry]:
    rows = _load_chart(song, difficulty)
    return rows[0] if rows else None

def leaderboard(song: Optional[str] = None, difficulty: Optional[str] = None, limit: int = 25) -> List[ScoreEntry]:
    if song and difficulty:
        rows = _load_chart(song, difficulty)
    else:
        rows = _load_all()
        if song is not None:
            rows = [r for r in rows if r.song == song]
        if difficulty is not None:
            rows = [r for r in rows if r.difficulty.lower() == difficulty.lower()]
    return rows[:limit]
