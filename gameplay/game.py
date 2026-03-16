from __future__ import annotations

import time
from bisect import bisect_left, bisect_right
from typing import Any, Dict, Iterable, List, Optional, Set

import pygame

from gameplay.notes import Note
from gameplay.scoring import judge_timing
from .sound_manager import SoundManager


class Game:
    def __init__(self, notes: List[Note], config: Dict[str, Any]):
        # Ensure a stable chronological order for efficient lookups.
        self.notes = sorted(notes, key=lambda n: n.time)
        self.config = config
        self._note_times = [note.time for note in self.notes]
        self._lane_next_indices: Dict[int, int] = {lane: 0 for lane in range(4)}
        self._visible_start_idx = 0
        self._visible_buffer: List[Note] = []

        # Game state
        self.score = 0
        self.combo = 0
        self.max_combo = 0
        self.judgements: Dict[str, int] = {"Perfect": 0, "Great": 0, "Good": 0, "Miss": 0}
        self.current_index = 0  # index of next note to check for misses
        self.active_holds: Dict[int, Dict[str, Any]] = {}
        self.active_hold_notes: Set[Note] = set()
        self.current_time = 0.0
        self.start_time: Optional[float] = None
        self.paused = False
        self.pause_start: Optional[float] = None
        self.paused_duration = 0.0
        self.last_judgement_text = ""
        self.judgement_timer = 0.0
        self.base_score = self.config.get("base_score", 1000)
        self.hold_body_ratio = max(0.0, self.config.get("hold_body_ratio", 0.5))
        self.max_possible_score = self._calculate_max_possible_score()

        # Sound effects
        self.sound_manager = SoundManager(config)

    def _score_delta_for_judgement(self, judgement: str) -> int:
        multipliers = {"Perfect": 1.0, "Great": 0.8, "Good": 0.5}
        return int(self.base_score * multipliers.get(judgement, 0.0))

    def _calculate_max_possible_score(self) -> int:
        total = 0.0
        for note in self.notes:
            if getattr(note, "type", "tap") == "hold":
                total += self.base_score * (2.0 + self.hold_body_ratio)
            else:
                total += self.base_score
        return int(round(total))

    def _apply_judgement_result(self, judgement: str, increment_combo: bool = True):
        if judgement not in self.judgements:
            self.judgements[judgement] = 0
        self.judgements[judgement] += 1

        # Play judgement sound
        self._play_judgement_sound(judgement, increment_combo)
        
        if judgement == "Miss":
            self.combo = 0
        else:
            if increment_combo:
                self.combo += 1
                if self.combo > self.max_combo:
                    self.max_combo = self.combo
            self.score += self._score_delta_for_judgement(judgement)
        self.last_judgement_text = judgement
        self.judgement_timer = 0.5

    def _play_judgement_sound(self, judgement: str, increment_combo: bool = True):
        """Play appropriate sound effect for judgement"""
        if not self.sound_manager.enabled:
            return
            
        # Only play sounds for successful hits, not for misses
        if judgement in ["Perfect", "Great", "Good"]:
            sound_name = judgement.lower()
            self.sound_manager.play_sound(sound_name)

    def _update_hold_body_scores(self, play_time: float):
        body_ratio = self.hold_body_ratio
        if body_ratio <= 0:
            return
        for hold_info in self.active_holds.values():
            note = hold_info["note"]
            duration = getattr(note, "duration", 0.0)
            if duration <= 0:
                continue
            note_start = note.time
            note_end = note_start + duration
            last_scored = hold_info.get("scored_until", note_start)
            target = max(note_start, min(play_time, note_end))
            if target <= last_scored:
                continue
            progress_increment = (target - last_scored) / duration
            remaining = max(0.0, 1.0 - hold_info.get("body_progress", 0.0))
            actual_increment = min(progress_increment, remaining)
            hold_info["scored_until"] = target
            if actual_increment <= 0:
                continue
            hold_info["body_progress"] = hold_info.get("body_progress", 0.0) + actual_increment
            body_total = self.base_score * body_ratio
            buffer = hold_info.get("body_score_buffer", 0.0) + body_total * actual_increment
            payout = int(buffer)
            if payout > 0:
                self.score += payout
                buffer -= payout
            hold_info["body_score_buffer"] = buffer

    def _flush_hold_body_buffer(self, hold_info: Dict[str, Any]) -> None:
        """Award any remaining fractional body score when a hold note ends."""
        buffer = float(hold_info.get("body_score_buffer", 0.0))
        if buffer:
            payout = int(round(buffer))
            if payout:
                self.score += payout
        hold_info["body_score_buffer"] = 0.0

    def start(self):
        """Mark the game as started (set start time)."""
        self.start_time = time.perf_counter()
        self.current_time = 0.0

    def update_time(self):
        """Update the current audio time (seconds), accounting for pause offset."""
        if self.start_time is None or self.paused:
            return
        now = time.perf_counter()
        # Subtract total paused duration to get effective play time
        self.current_time = (now - self.start_time) - self.paused_duration

    def pause(self):
        if not self.paused:
            self.paused = True
            self.pause_start = time.perf_counter()
            pygame.mixer.music.pause()
            # Also pause sound effects
            pygame.mixer.pause()

    def resume(self):
        if self.paused:
            if self.pause_start:
                self.paused_duration += (time.perf_counter() - self.pause_start)
            self.paused = False
            self.pause_start = None
            pygame.mixer.music.unpause()
            # Also resume sound effects
            pygame.mixer.unpause()

    def restart(self):
        """Reset game state for restarting the song."""
        self.notes.sort(key=lambda n: n.time)
        self._note_times = [note.time for note in self.notes]
        self._lane_next_indices = {lane: 0 for lane in range(4)}
        self._visible_start_idx = 0

        self.score = 0
        self.combo = 0
        self.max_combo = 0
        self.judgements = {"Perfect": 0, "Great": 0, "Good": 0, "Miss": 0}
        self.current_index = 0
        self.current_time = 0.0
        self.start_time = time.perf_counter()
        self.paused = False
        self.pause_start = None
        self.paused_duration = 0.0
        self.last_judgement_text = ""
        self.judgement_timer = 0.0
        self.active_holds = {}
        self.active_hold_notes.clear()
        for note in self.notes:
            note.hit = False
            note.hit_time = None
        self._visible_buffer.clear()

    def _next_lane_candidate(self, lane: int) -> Optional[Note]:
        idx = self._lane_next_indices.get(lane, 0)
        notes = self.notes
        while idx < len(notes):
            note = notes[idx]
            if not note.hit and note.lane == lane:
                self._lane_next_indices[lane] = idx
                return note
            idx += 1
        self._lane_next_indices[lane] = len(notes)
        return None

    def _advance_lane_index(self, lane: int) -> None:
        current = self._lane_next_indices.get(lane, 0)
        self._lane_next_indices[lane] = min(len(self.notes), current + 1)

    def process_input(self, key: int):
        """
        Process a key press event for note hit.
        """
        key_map = self.config.get("key_map", {})
        if key not in key_map:
            return
        lane = key_map[key]
        candidate = self._next_lane_candidate(lane)
        if candidate is None:
            return
        offset_sec = self.config.get("audio_latency_ms", 0) / 1000.0
        play_time = self.current_time - offset_sec
        error = play_time - candidate.time
        judgement = judge_timing(error, self.config)
        if judgement is None:
            return
        candidate.hit = True
        candidate.hit_time = self.current_time

        # Play hold start sound for hold notes
        if getattr(candidate, "type", "tap") == "hold":
            self.sound_manager.play_sound("hold_start")
        
        self._apply_judgement_result(judgement)
        if getattr(candidate, "type", "tap") == "hold":
            duration = getattr(candidate, "duration", 0.0)
            note_start = candidate.time
            note_end = note_start + duration
            start_progress_time = max(note_start, min(play_time, note_end))
            if duration > 0:
                initial_progress = max(0.0, min(1.0, (start_progress_time - note_start) / duration))
            else:
                initial_progress = 0.0
            self.active_holds[lane] = {
                "note": candidate,
                "scored_until": start_progress_time,
                "body_progress": initial_progress,
                "body_score_buffer": 0.0,
            }
            self.active_hold_notes.add(candidate)
        self._advance_lane_index(lane)

    def update_notes_and_score(self):
        """Update note statuses for misses and update judgement text timer."""
        good_window = self.config.get("good_timing_ms", 100) / 1000.0
        offset_sec = self.config.get("audio_latency_ms", 0) / 1000.0
        # Mark notes as Miss if they passed beyond Good window
        miss_cutoff = (self.current_time - offset_sec) - good_window
        miss_idx = bisect_right(self._note_times, miss_cutoff, lo=self.current_index)
        for idx in range(self.current_index, miss_idx):
            note = self.notes[idx]
            if note.hit:
                continue
            note.hit = True
            note.hit_time = None
            self._apply_judgement_result("Miss", increment_combo=False)
            self._advance_lane_index(note.lane)
        self.current_index = miss_idx
        play_time = self.current_time - offset_sec
        self._update_hold_body_scores(play_time)
        release_window = self.config.get("good_timing_ms", 100) / 1000.0
        expired_holds = []
        for lane, hold_info in list(self.active_holds.items()):
            note = hold_info["note"]
            duration = getattr(note, "duration", 0.0)
            if duration <= 0:
                expired_holds.append(lane)
                continue
            hold_end = note.time + duration
            if play_time > hold_end + release_window:
                self._apply_judgement_result("Miss", increment_combo=False)
                expired_holds.append(lane)
        for lane in expired_holds:
            hold_info = self.active_holds[lane]
            self._flush_hold_body_buffer(hold_info)
            note = hold_info["note"]
            del self.active_holds[lane]
            self.active_hold_notes.discard(note)
        # Decrease judgement text display timer
        if self.judgement_timer > 0:
            target_fps = float(self.config.get("target_fps", 120))
            frame_interval = 1.0 / max(30.0, target_fps)
            self.judgement_timer = max(0.0, self.judgement_timer - frame_interval)

    def handle_key_release(self, key: int):
        """
        Handle key release events (for hold note completion detection).
        """
        key_map = self.config.get("key_map", {})
        if key not in key_map:
            return
        lane = key_map[key]
        hold_info = self.active_holds.get(lane)
        if not hold_info:
            return
        note = hold_info["note"]
        duration = getattr(note, "duration", 0.0)
        offset_sec = self.config.get("audio_latency_ms", 0) / 1000.0
        play_time = self.current_time - offset_sec
        # Ensure body scoring is up-to-date before evaluating release
        self._update_hold_body_scores(play_time)
        hold_end = note.time + duration
        good_window = self.config.get("good_timing_ms", 100) / 1000.0

        # Play hold end sound before applying judgement
        self.sound_manager.play_sound("hold_end")
        
        if duration > 0 and play_time < hold_end - good_window:
            self._apply_judgement_result("Miss", increment_combo=False)
        else:
            error = play_time - hold_end
            judgement = judge_timing(error, self.config)
            if judgement is None:
                self._apply_judgement_result("Miss", increment_combo=False)
            else:
                self._apply_judgement_result(judgement)
        self._flush_hold_body_buffer(hold_info)
        del self.active_holds[lane]
        self.active_hold_notes.discard(note)
        self._advance_lane_index(lane)

    def is_finished(self) -> bool:
        """Return True if all notes have been hit or missed."""
        return not self.active_holds and all(note.hit for note in self.notes)

    def iter_visible_notes(self, current_time: float, display_time: float) -> Iterable[Note]:
        """
        Yield notes that fall within the current display window.

        Uses binary search to avoid scanning the full note list each frame.
        """
        if not self.notes:
            self._visible_buffer.clear()
            return self._visible_buffer
        lead = 0.25  # seconds of history to keep visible for late hits
        tail_factor = 1.6
        start_time = current_time - lead
        end_time = current_time + display_time * tail_factor
        start_idx = bisect_left(self._note_times, start_time, lo=self._visible_start_idx)
        end_idx = bisect_right(self._note_times, end_time, lo=start_idx)
        self._visible_start_idx = max(0, start_idx - 4)
        visible = self._visible_buffer
        visible.clear()
        if start_idx < len(self.notes):
            stop = min(end_idx + 4, len(self.notes))
            visible.extend(self.notes[i] for i in range(start_idx, stop))
        # Always keep hold notes that are actively being pressed visible so the player can track release timing.
        if self.active_hold_notes:
            for note in self.active_hold_notes:
                if note not in visible:
                    visible.append(note)
        return visible
