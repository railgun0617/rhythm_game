
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import threading
import queue

import pygame

from audio_io.audio_import import load_audio
from charting.chart_generator import generate_charts, CHARTS_DIR
from config.config import load_config
from gameplay.game import Game
from gameplay.notes import Note
from ui import renderer
from ui.menu import MenuScene, ScoresScene, SettingsScene, SongInfo, ResultsScene, _shorten_label
from persistence.scores import add_score
from logging_utils import setup_logging
from runtime_paths import ASSETS_DIR


# Use the same fallback font path as the rest of the UI for full CJK coverage.
FONT_PATH = ASSETS_DIR / "fonts" / "NotoSansCJKjp-Regular.otf"
SILENCE_TAIL_PADDING = 0.5  # seconds of buffer after detected audio end
SILENCE_FADE_MS = 350

logger = logging.getLogger(__name__)

class GenerationLoadingScreen:
    """Simple progress overlay shown while beatmaps are generated."""

    def __init__(self, screen: pygame.Surface, song_name: str, difficulty: str) -> None:
        self.screen = screen
        self.song_name = _shorten_label(song_name, 24)
        self.difficulty = difficulty
        self._target_progress = 0.0
        self._visible_progress = 0.0
        self._prediction_total_ms: Optional[float] = None
        self._prediction_cap = 0.9999
        self.stage = "Preparing analysis..."
        self._active = True
        self._last_draw = 0
        self._prediction_start_ms = pygame.time.get_ticks()
        self._title_font = self._load_font(40, bold=True)
        self._body_font = self._load_font(24, bold=False)
        self._percent_font = self._load_font(22, bold=True)
        self._percent_on_fill = pygame.Color(6, 9, 24)
        self._percent_on_bg = pygame.Color(0, 229, 255)
        self.update(0.0, self.stage)

    def _load_font(self, size: int, bold: bool) -> pygame.font.Font:
        if FONT_PATH.exists():
            font = pygame.font.Font(str(FONT_PATH), size)
            font.set_bold(bold)
            return font
        font = pygame.font.SysFont("segoe ui", size, bold=bold)
        if font:
            return font
        return pygame.font.Font(None, size)

    def update(self, progress: Optional[float], stage: Optional[str] = None, *, immediate: bool = False) -> None:
        if not self._active:
            return
        if progress is not None:
            new_target = max(0.0, min(1.0, float(progress)))
            if immediate and new_target >= self._target_progress:
                self._target_progress = new_target
                self._visible_progress = new_target
            else:
                self._target_progress = max(self._target_progress, new_target)
        if stage:
            self.stage = stage
        now = pygame.time.get_ticks()
        self._apply_prediction(now)
        if now - self._last_draw < 16 and self._visible_progress < 1.0:
            return
        self._last_draw = now
        self._draw()

    def complete(self, message: str = "Beatmap ready!") -> None:
        if not self._active:
            return
        self.update(1.0, message, immediate=True)
        pygame.display.flip()
        pygame.event.pump()
        pygame.time.delay(150)
        self._active = False

    def set_prediction_window(self, audio_seconds: float) -> None:
        if audio_seconds <= 0:
            return
        estimated = max(0.0, float(audio_seconds)) * 1000.0
        self._prediction_total_ms = max(120000.0, min(420000.0, estimated))
        self._prediction_start_ms = pygame.time.get_ticks()

    def _apply_prediction(self, now: int) -> None:
        if not self._prediction_total_ms:
            return
        elapsed = max(0.0, now - self._prediction_start_ms)
        ratio = min(1.0, elapsed / self._prediction_total_ms)
        predicted = ratio * self._prediction_cap
        if predicted > self._target_progress:
            self._target_progress = predicted

    def _draw(self) -> None:
        surface = self.screen
        width, height = surface.get_size()
        surface.fill((6, 9, 24))
        panel = pygame.Rect(0, 0, min(560, width - 80), 260)
        panel.center = (width // 2, height // 2)
        pygame.draw.rect(surface, (18, 26, 52), panel, border_radius=28)
        pygame.draw.rect(surface, (0, 229, 255), panel, width=3, border_radius=28)

        title = self._title_font.render("Generating Beatmap", True, (245, 250, 255))
        surface.blit(title, title.get_rect(midtop=(panel.centerx, panel.y + 22)))

        subtitle = f"{self.song_name} - {self.difficulty}"
        subtitle_surface = self._body_font.render(subtitle, True, (160, 185, 255))
        surface.blit(subtitle_surface, subtitle_surface.get_rect(midtop=(panel.centerx, panel.y + 80)))

        stage_surface = self._body_font.render(self.stage, True, (120, 150, 255))
        surface.blit(stage_surface, stage_surface.get_rect(midbottom=(panel.centerx, panel.bottom - 90)))

        # Smooth step toward the latest progress target for a more natural bar.
        delta = self._target_progress - self._visible_progress
        if abs(delta) > 1e-4:
            step = max(0.01, abs(delta) * 0.25)
            if delta > 0:
                self._visible_progress = min(self._visible_progress + step, self._target_progress)
            else:
                self._visible_progress = max(self._visible_progress - step, self._target_progress)

        current_progress = self._visible_progress

        bar_rect = pygame.Rect(panel.x + 40, panel.bottom - 80, panel.width - 80, 34)
        pygame.draw.rect(surface, (12, 18, 32), bar_rect, border_radius=18)
        fill_width = int(bar_rect.width * current_progress)
        if fill_width > 0:
            fill_rect = pygame.Rect(bar_rect.x, bar_rect.y, fill_width, bar_rect.height)
            pygame.draw.rect(surface, (0, 229, 255), fill_rect, border_radius=18)

        percent_text = f"{current_progress * 100:6.2f}%"
        char_metrics = []
        total_width = 0
        text_height = 0
        for ch in percent_text:
            width, height = self._percent_font.size(ch)
            char_metrics.append((ch, width))
            total_width += width
            text_height = max(text_height, height)
        baseline_x = bar_rect.centerx - total_width // 2
        baseline_y = bar_rect.centery - text_height // 2
        cursor_x = baseline_x
        fill_limit = bar_rect.x + fill_width
        for ch, width in char_metrics:
            char_center = cursor_x + width / 2
            if fill_width > 0 and bar_rect.x <= char_center <= fill_limit:
                color = self._percent_on_fill
            else:
                color = self._percent_on_bg
            glyph = self._percent_font.render(ch, True, color)
            surface.blit(glyph, (int(cursor_x), baseline_y))
            cursor_x += width

        pygame.display.flip()
        pygame.event.pump()

def _load_chart(screen: Optional[pygame.Surface], song: SongInfo, difficulty: str) -> Dict:
    # Load chart JSON; generate if missing.
    CHARTS_DIR.mkdir(exist_ok=True)
    chart_path = CHARTS_DIR / f"{song.base_name}_{difficulty.lower()}.json"
    if not chart_path.exists():
        # generate charts for all difficulties for this song
        loader = GenerationLoadingScreen(screen, song.display_name, difficulty) if screen else None
        try:
            if loader:
                loader.update(0.0, "Loading audio...")
            y, sr = load_audio(str(song.audio_path), sr=44100)
            if loader:
                duration = float(len(y)) / float(sr or 1)
                loader.set_prediction_window(duration)
                loader.update(None, "Analyzing waveforms...")

            if loader:
                progress_queue: "queue.Queue[tuple[Optional[float], str]]" = queue.Queue()
                worker_error: Dict[str, Exception] = {}

                def _progress(value: float, stage: str) -> None:
                    progress_queue.put((None, stage or "Generating chart..."))

                def _worker() -> None:
                    try:
                        generate_charts(
                            y,
                            sr,
                            audio_filename=str(song.audio_path),
                            progress_callback=_progress,
                        )
                    except Exception as exc:
                        worker_error["error"] = exc

                thread = threading.Thread(target=_worker, daemon=True)
                loader.update(None, "Generating chart...")
                thread.start()

                def _drain_queue() -> None:
                    while True:
                        try:
                            value, stage = progress_queue.get_nowait()
                        except queue.Empty:
                            break
                        loader.update(value, stage)

                while thread.is_alive():
                    _drain_queue()
                    loader.update(None)
                    pygame.time.delay(16)
                thread.join()
                _drain_queue()
                if worker_error:
                    raise worker_error["error"]
                loader.update(0.9999, "Finalizing files...", immediate=True)
            else:
                generate_charts(
                    y,
                    sr,
                    audio_filename=str(song.audio_path),
                    progress_callback=None,
                )
        finally:
            if loader:
                loader.complete()
        if not chart_path.exists():
            # fallback: normal
            fallback = CHARTS_DIR / f"{song.base_name}_normal.json"
            if not fallback.exists():
                raise RuntimeError("Failed to generate charts.")
            chart_path = fallback
    with chart_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)

def _prepare_notes(data: Dict, config: Dict) -> List[Note]:
    notes_data = data.get("notes", [])
    lead_offset = 0.0
    try:
        lead_offset = float(
            data.get("audio_effective_start", data.get("audio_lead_in", 0.0)) or 0.0
        )
    except (TypeError, ValueError):
        lead_offset = 0.0
    if lead_offset > 0 and notes_data:
        adjusted = []
        for d in notes_data:
            nd = dict(d)
            nd["time"] = round(max(0.0, float(nd.get("time", 0.0)) - lead_offset), 6)
            adjusted.append(nd)
        notes_data = adjusted
    # ensure a minimum 'lead' so earliest note is visible before hit line
    min_chart_lead_time = config.get("min_chart_lead_time", config.get("note_display_time", 2.0))
    try:
        min_lead_target = float(min_chart_lead_time)
    except Exception:
        min_lead_target = 2.0
    min_lead_target = max(0.0, min_lead_target)
    if notes_data:
        earliest = min(d.get("time", 0.0) for d in notes_data)
        if earliest < min_lead_target:
            lead_time = min_lead_target - earliest
            adjusted = []
            for d in notes_data:
                nd = dict(d)
                nd["time"] = round(nd.get("time", 0.0) + lead_time, 3)
                adjusted.append(nd)
            notes_data = adjusted
    notes = [Note(lane=d["lane"], time=d["time"], type=d.get("type","tap"), duration=d.get("duration",0.0)) for d in notes_data]
    return notes

def _play_song(screen: pygame.Surface, song: SongInfo, difficulty: str, config: Dict) -> None:
    # Load chart (generate if necessary) and notes
    chart = _load_chart(screen, song, difficulty)
    notes = _prepare_notes(chart, config)

    # Initialize mixer BEFORE creating Game object
    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            print("Mixer initialized successfully")
    except pygame.error as e:
        print(f"Could not initialize audio mixer: {e}")
        # Continue without audio - the game should still work
    
    duration_hint = chart.get("audio_effective_duration", chart.get("audio_duration", 0.0))
    try:
        duration_hint = max(0.0, float(duration_hint))
    except (TypeError, ValueError):
        duration_hint = 0.0
    chart_note_tail = 0.0
    for note in notes:
        note_end = note.time + getattr(note, "duration", 0.0)
        if note_end > chart_note_tail:
            chart_note_tail = note_end
    playback_base_duration = max(duration_hint, chart_note_tail)
    playback_cutoff = playback_base_duration + SILENCE_TAIL_PADDING if playback_base_duration > 0 else None
    playback_fade_started = False

    # Audio
    pygame.mixer.quit()
    # pre-init mixer to reduce latency (standard 44.1kHz stereo)
    pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=512)
    pygame.mixer.init()
    pygame.mixer.music.load(str(song.audio_path))
    pygame.mixer.music.set_volume(float(config.get("volume", 1.0)))
    start_offset = 0.0
    try:
        start_offset = float(chart.get("audio_effective_start", chart.get("audio_lead_in", 0.0)) or 0.0)
    except (TypeError, ValueError):
        start_offset = 0.0

    # Annotate config with metadata for downstream HUD/result display.
    config["song_display_name"] = song.display_name
    config["song_name"] = song.base_name
    config["difficulty_label"] = difficulty
    config["difficulty"] = difficulty

    # Prepare game
    game = Game(notes, config)

    def _countdown_handle_event(event, *, allow_pause_cancel: bool) -> Optional[str]:
        if event.type == pygame.QUIT:
            return "quit"
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_r:
                return "restart"
            if event.key == pygame.K_q:
                return "quit"
            if event.key == pygame.K_ESCAPE:
                return "pause" if allow_pause_cancel else "quit"
            if allow_pause_cancel and event.key == pygame.K_p:
                return "pause"
        return None

    def _run_interactive_countdown(background: pygame.Surface, sequence, gap_ms: int, *, allow_pause_cancel: bool) -> str:
        clock = pygame.time.Clock()
        idx = 0
        phase = "label"
        phase_start = pygame.time.get_ticks()
        while idx < len(sequence):
            for event in pygame.event.get():
                action = _countdown_handle_event(event, allow_pause_cancel=allow_pause_cancel)
                if action:
                    return action
            screen.blit(background, (0, 0))
            if phase == "label":
                label = sequence[idx][0]
                renderer.draw_countdown(screen, label, flip=False, apply_overlay=False)
            else:
                renderer.draw_countdown(screen, None, flip=False, apply_overlay=False)
            pygame.display.flip()
            clock.tick(60)
            now = pygame.time.get_ticks()
            if phase == "label":
                if now - phase_start >= sequence[idx][1]:
                    if idx == len(sequence) - 1:
                        idx += 1
                    else:
                        phase = "gap"
                        phase_start = now
            elif phase == "gap":
                if now - phase_start >= gap_ms:
                    idx += 1
                    phase = "label"
                    phase_start = now
        return "continue"

    def _pause_menu_wait(background: pygame.Surface | None = None) -> str:
        if background is None:
            renderer.draw_game(screen, game, flip=False, show_pause_overlay=False)
            renderer.draw_pause_overlay(screen, show_text=True)
        else:
            screen.blit(background, (0, 0))
            renderer.draw_pause_overlay(screen, show_text=True)
        pygame.display.flip()
        clock = pygame.time.Clock()
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return "quit"
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_q:
                        return "quit"
                    if event.key == pygame.K_r:
                        return "restart"
                    if event.key in (pygame.K_ESCAPE, pygame.K_p):
                        return "resume"
            clock.tick(60)

    # Countdown
    renderer.draw_game(screen, game, flip=True)  # draw empty lanes
    renderer.draw_pause_overlay(screen, show_text=False)
    start_background = screen.copy()
    countdown_sequence = [("3", 550), ("2", 550), ("1", 550), ("Go!", 380)]
    gap_ms = 100
    countdown_result = _run_interactive_countdown(start_background, countdown_sequence, gap_ms, allow_pause_cancel=True)
    while True:
        if countdown_result == "quit":
            return
        if countdown_result == "restart":
            _play_song(screen, song, difficulty, config)
            return
        if countdown_result == "pause":
            pause_action = _pause_menu_wait(start_background)
            if pause_action == "quit":
                return
            if pause_action == "restart":
                _play_song(screen, song, difficulty, config)
                return
            countdown_result = _run_interactive_countdown(
                start_background,
                countdown_sequence,
                gap_ms,
                allow_pause_cancel=True,
            )
            continue
        break

    # Start!
    game.start()
    pygame.mixer.music.set_volume(float(config.get("volume", 1.0)))
    pygame.mixer.music.play(start=max(0.0, start_offset))

    clock = pygame.time.Clock()
    paused = False
    running = True
    want_restart = False
    completed_song = False

    while running:
        # events
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
                want_restart = False
                break
            if event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_p):
                    # toggle pause
                    if not paused:
                        paused = True
                        game.pause()
                    else:
                        resuming = True
                        while resuming:
                            renderer.draw_game(screen, game, flip=False, show_pause_overlay=False)
                            renderer.draw_pause_overlay(screen, show_text=False)
                            paused_overlay = screen.copy()
                            resume_sequence = [("3", 450), ("2", 450), ("1", 450), ("Go!", 320)]
                            resume_gap = 90
                            resume_result = _run_interactive_countdown(
                                paused_overlay,
                                resume_sequence,
                                resume_gap,
                                allow_pause_cancel=True,
                            )
                            if resume_result == "quit":
                                running = False
                                want_restart = False
                                resuming = False
                                break
                            if resume_result == "restart":
                                want_restart = True
                                running = False
                                resuming = False
                                break
                            if resume_result == "pause":
                                pause_action = _pause_menu_wait()
                                if pause_action == "quit":
                                    running = False
                                    want_restart = False
                                    resuming = False
                                    break
                                if pause_action == "restart":
                                    want_restart = True
                                    running = False
                                    resuming = False
                                    break
                                continue
                            game.resume()
                            paused = False
                            resuming = False
                elif event.key == pygame.K_r:
                    want_restart = True
                    running = False
                elif event.key == pygame.K_q:
                    want_restart = False
                    running = False
                else:
                    game.process_input(event.key)
            elif event.type == pygame.KEYUP:
                game.handle_key_release(event.key)

        if not running:
            break

        if not paused:
            game.update_time()
            game.update_notes_and_score()

        renderer.draw_game(screen, game, flip=True)
        clock.tick(config.get("target_fps", 120))

        if playback_cutoff and not playback_fade_started and game.current_time >= playback_cutoff:
            fade_ms = int(config.get("silence_fade_ms", SILENCE_FADE_MS))
            pygame.mixer.music.fadeout(max(0, fade_ms))
            playback_fade_started = True
            detected_end = playback_base_duration
            logger.info(
                "Auto-stopping audio at %.2fs (detected end %.2fs)",
                playback_cutoff,
                detected_end,
            )

        # finish condition
        if game.is_finished() and not pygame.mixer.music.get_busy():
            completed_song = True
            break

    if want_restart:
        pygame.mixer.music.stop()
        # recursive restart
        _play_song(screen, song, difficulty, config)
        return

    if not completed_song:
        pygame.mixer.music.stop()
        return

    # results
    pygame.mixer.music.stop()
    pygame.time.delay(500)
    # persist score before showing results
    max_score = getattr(game, "max_possible_score", 0)
    accuracy = (game.score / max_score * 100.0) if max_score > 0 else 0.0
    add_score(
        song=song.base_name,
        difficulty=difficulty,
        score=game.score,
        max_score=max_score,
        accuracy=accuracy,
        max_combo=game.max_combo,
        judgements=game.judgements
    )

    result_scene = ResultsScene(screen, config)
    result_action = result_scene.loop(game)
    if result_action == "replay":
        _play_song(screen, song, difficulty, config)
        return


def _want_console_logging() -> bool:
    flag = os.environ.get("RHYTHM_MASTER_DEBUG", "")
    return flag.strip().lower() in {"1", "true", "yes", "on"}


def main():
    setup_logging(enable_console=_want_console_logging())
    pygame.init()
    pygame.display.set_caption("Rhythm Master")
    config = load_config()
    screen = pygame.display.set_mode((980, 720))

    menu = MenuScene(screen, config)
    while True:
        action, song, diff = menu.loop()
        if action == "quit":
            break
        elif action == "scores":
            ScoresScene(screen, config, song=song, difficulty=diff).loop()
            # loop back to the menu
            continue
        elif action == "settings":
            SettingsScene(screen, config).loop()
            continue
        elif action == "play" and song and diff:
            # Play selection
            _play_song(screen, song, diff, config)
            menu.refresh_catalog()

    pygame.quit()


if __name__ == "__main__":
    main()
