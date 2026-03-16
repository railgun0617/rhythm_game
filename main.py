import sys
import argparse
import json
import logging
from pathlib import Path
import tempfile
import time
import wave

import numpy as np
import pygame
from audio_io.audio_import import load_audio
from config.config import load_config
from charting.chart_generator import generate_charts, CHARTS_DIR
from gameplay.game import Game
from gameplay.notes import Note
from ui import renderer
from logging_utils import setup_logging
from runtime_paths import ASSETS_DIR, CONFIG_FILE

logger = logging.getLogger(__name__)

WINDOWEVENT = getattr(pygame, "WINDOWEVENT", None)
WINDOWEVENT_EXPOSED = getattr(pygame, "WINDOWEVENT_EXPOSED", None)
WINDOWEVENT_SHOWN = getattr(pygame, "WINDOWEVENT_SHOWN", None)
VIDEOEXPOSE = getattr(pygame, "VIDEOEXPOSE", None)


def _is_expose_event(event: pygame.event.Event) -> bool:
    """Return True if the event indicates the window needs to be redrawn."""
    if WINDOWEVENT is not None and event.type == WINDOWEVENT:
        sub_event = getattr(event, "event", None)
        return sub_event in {WINDOWEVENT_EXPOSED, WINDOWEVENT_SHOWN}
    if VIDEOEXPOSE is not None and event.type == VIDEOEXPOSE:
        return True
    return False

def create_silence_wav(duration: float, sample_rate: int = 44100, channels: int = 2) -> Path:
    """Create a temporary WAV file containing silence for the given duration (seconds)."""
    frames = max(1, int(round(duration * sample_rate)))
    silence = np.zeros((frames, channels), dtype=np.int16)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    tmp_path = Path(tmp.name)
    tmp.close()
    with wave.open(str(tmp_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit samples
        wf.setframerate(sample_rate)
        wf.writeframes(silence.tobytes())
    return tmp_path

def main():
    setup_logging(enable_console=True)
    parser = argparse.ArgumentParser(description="4-Lane Rhythm Game")
    parser.add_argument("--song", type=str, help="Path to audio file (.mp3 or .wav)")
    parser.add_argument("--difficulty", type=str, choices=["Easy", "Normal", "Hard", "Expert"],
                        default="Normal", help="Difficulty level to play")
    parser.add_argument("--seed", type=int, help="Random seed for chart generation")
    args = parser.parse_args()

    audio_path = Path(args.song).expanduser() if args.song else ASSETS_DIR / "sample_music.wav"
    difficulty = args.difficulty
    seed = args.seed

    pygame.init()
    config = load_config(str(CONFIG_FILE))
    target_fps = int(max(30, config.get("target_fps", 120)))

    running = True
    while running:
        base_name = audio_path.stem
        chart_path = CHARTS_DIR / f"{base_name}_{difficulty.lower()}.json"
        notes_data = None
        if chart_path.exists():
            try:
                with chart_path.open("r", encoding="utf-8") as f:
                    chart_data = json.load(f)
                if chart_data.get("difficulty", "").lower() == difficulty.lower():
                    notes_data = chart_data.get("notes", [])
            except Exception as e:
                logger.warning("Failed to load chart from %s: %s. Regenerating...", chart_path, e)
        if notes_data is None:
            try:
                y, sr = load_audio(str(audio_path))
            except Exception as e:
                logger.error("Error loading audio file: %s", e)
                return
            charts = generate_charts(y, sr, audio_filename=str(audio_path), seed=seed)
            if difficulty not in charts:
                logger.error("No chart available for difficulty %s", difficulty)
                return
            notes_data = charts[difficulty]
        min_lead_target = float(config.get("min_chart_lead_time", config.get("note_display_time", 2.0)))
        min_lead_target = max(0.0, min_lead_target)
        lead_time = 0.0
        if notes_data:
            earliest_time = min(d.get("time", 0.0) for d in notes_data)
            if earliest_time < min_lead_target:
                lead_time = min_lead_target - earliest_time
                adjusted_notes = []
                for d in notes_data:
                    nd = dict(d)
                    nd["time"] = round(nd.get("time", 0.0) + lead_time, 3)
                    adjusted_notes.append(nd)
                notes_data = adjusted_notes
        notes = [Note(lane=d["lane"], time=d["time"], type=d["type"], duration=d.get("duration", 0.0))
                 for d in notes_data]

        pygame.mixer.init(frequency=44100)
        screen = pygame.display.set_mode((1280, 720))
        pygame.display.set_caption("4-Lane Rhythm Game")
        silence_path: Path | None = None
        try:
            if lead_time > 0.0:
                silence_path = create_silence_wav(lead_time)
                pygame.mixer.music.load(str(silence_path))
                pygame.mixer.music.queue(str(audio_path))
            else:
                pygame.mixer.music.load(str(audio_path))
            pygame.mixer.music.set_volume(float(config.get("volume", 1.0)))
        except Exception as e:
            if silence_path and silence_path.exists():
                try:
                    silence_path.unlink()
                except OSError:
                    pass
            logger.error("Failed to load audio for playback: %s", e)
            return

        display_name = audio_path.stem.replace("_", " ").title()
        config["song_display_name"] = display_name
        config["song_name"] = audio_path.stem
        config["difficulty_label"] = difficulty
        config["difficulty"] = difficulty

        game = Game(notes, config)
        clock = pygame.time.Clock()
        want_restart = False

        countdown_seconds = max(0.0, float(config.get("countdown_seconds", 3)))
        if countdown_seconds > 0:
            countdown_clock = pygame.time.Clock()
            countdown_start = time.perf_counter()
            while True:
                elapsed = time.perf_counter() - countdown_start
                remaining = countdown_seconds - elapsed
                if remaining <= 0:
                    break
                message_value = int(remaining) + 1
                message_text = f"Starting in {message_value}..."
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        running = False
                        want_restart = False
                        break
                    if _is_expose_event(event):
                        renderer.draw_game(screen, game, flip=False)
                        renderer.draw_countdown(screen, message_text, flip=True)
                        continue
                    if event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                        running = False
                        want_restart = False
                        break
                if not running:
                    break
                renderer.draw_game(screen, game, flip=False)
                renderer.draw_countdown(screen, message_text, flip=True)
                countdown_clock.tick(target_fps)
            if not running:
                break
            renderer.draw_game(screen, game, flip=False)
            renderer.draw_countdown(screen, "Go!", flip=True)
            pygame.time.delay(500)
            pygame.event.clear()

        pygame.mixer.music.set_volume(float(config.get("volume", 1.0)))
        pygame.mixer.music.play()
        game.start()

        # Gameplay loop
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                    want_restart = False
                    pygame.mixer.music.stop()
                    break
                if _is_expose_event(event):
                    renderer.draw_game(screen, game)
                    continue
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        if not game.paused:
                            game.pause()
                        else:
                            game.resume()
                    elif event.key == pygame.K_r:
                        want_restart = True
                        pygame.mixer.music.stop()
                        break
                    elif event.key == pygame.K_q:
                        running = False
                        want_restart = False
                        pygame.mixer.music.stop()
                        break
                    else:
                        if not game.paused:
                            game.process_input(event.key)
                elif event.type == pygame.KEYUP:
                    if not game.paused:
                        game.handle_key_release(event.key)
            if not running or want_restart:
                break
            if not game.paused:
                game.update_time()
                game.update_notes_and_score()
            renderer.draw_game(screen, game)
            # End conditions: song finished or all notes hit
            if game.is_finished():
                pygame.mixer.music.stop()
                break
            clock.tick(target_fps)
        # End of gameplay loop

        if silence_path and silence_path.exists():
            try:
                silence_path.unlink()
            except OSError:
                pass
            silence_path = None

        if not running:
            break
        if want_restart:
            continue

        # Show results screen
        renderer.draw_results(screen, game)
        results_done = False
        while not results_done:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    results_done = True
                    running = False
                    break
                if _is_expose_event(event):
                    renderer.draw_results(screen, game)
                    continue
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_r:
                        want_restart = True
                        results_done = True
                    elif event.key == pygame.K_ESCAPE or event.key == pygame.K_q:
                        results_done = True
                        running = False
            pygame.time.delay(100)
        if want_restart and running:
            continue
        else:
            running = False

    pygame.quit()

if __name__ == "__main__":
    main()
