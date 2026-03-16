from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import pygame
from gameplay.game import Game
from ui.theme import (
    COLOR_ACCENT_MAGENTA,
    COLOR_ACCENT_MINT,
    COLOR_NEON_CYAN,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    FuturisticBackdrop,
    GlowCache,
    PANEL_ALPHA,
    PANEL_RADIUS,
    DEFAULT_FONT_PATH,
    blit_glass_panel,
)

# Colors
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GREY = (100, 100, 100)
LANE_COLORS = [
    (0, 150, 255),   # Blue
    (0, 200, 0),     # Green
    (255, 200, 0),   # Yellow
    (180, 0, 255)    # Purple
]

_FONT_CACHE: dict[int, pygame.font.Font] = {}
_STATIC_CACHE: dict[str, object] = {
    "size": None,
    "surface": None,
    "lane_width": 0,
    "lane_lefts": [0, 0, 0, 0],
    "lane_centers": [0, 0, 0, 0],
    "hit_line_y": 0,
    "hold_width": 0,
    "note_heads": [],
    "head_height": 10,
}
_HUD_CACHE: Dict[str, Dict[str, object]] = {}
_OVERLAY_CACHE: Dict[Tuple[Tuple[int, int], int], pygame.Surface] = {}
_RESULT_BACKDROP: FuturisticBackdrop | None = None
_RESULT_GLOW = GlowCache()
_THEME_FONT_CACHE: Dict[int, pygame.font.Font] = {}
_DEFAULT_THEME_FONT = Path(DEFAULT_FONT_PATH) if isinstance(DEFAULT_FONT_PATH, Path) else Path(str(DEFAULT_FONT_PATH))
GRADE_COLORS = {
    "S": pygame.Color(255, 215, 0),    # Gold
    "A": pygame.Color(76, 175, 80),    # Bright green
    "B": pygame.Color(33, 150, 243),   # Light blue
    "C": pygame.Color(255, 152, 0),    # Orange
    "D": pygame.Color(220, 20, 60),    # Red
}


def _get_font(size: int) -> pygame.font.Font:
    font = _FONT_CACHE.get(size)
    if font is None:
        font = pygame.font.SysFont(None, size)
        _FONT_CACHE[size] = font
    return font


def _ensure_static_assets(screen: pygame.Surface) -> dict[str, object]:
    size = screen.get_size()
    cache_size = _STATIC_CACHE["size"]
    if cache_size == size and _STATIC_CACHE["surface"] is not None:
        return _STATIC_CACHE

    width, height = size
    lane_width = max(1, width // 4)
    hit_line_y = max(120, height - 100)

    base = pygame.Surface(size).convert()
    base.fill(BLACK)
    for lane in range(1, 4):
        x = lane * lane_width
        pygame.draw.line(base, GREY, (x, 0), (x, hit_line_y), 2)
    head_width = max(1, int(lane_width * 0.8))
    head_height = 10
    pygame.draw.line(base, WHITE, (0, hit_line_y), (width, hit_line_y), 4)

    lane_lefts = [lane * lane_width for lane in range(4)]
    lane_centers = [left + lane_width // 2 for left in lane_lefts]
    note_heads = []
    for lane in range(4):
        surf = pygame.Surface((head_width, head_height), pygame.SRCALPHA)
        surf.fill(LANE_COLORS[lane])
        note_heads.append(surf)

    _STATIC_CACHE.update({
        "size": size,
        "surface": base,
        "lane_width": lane_width,
        "lane_lefts": lane_lefts,
        "lane_centers": lane_centers,
        "hit_line_y": hit_line_y,
        "hold_width": max(1, int(lane_width * 0.5)),
        "note_heads": note_heads,
        "head_height": head_height,
    })
    return _STATIC_CACHE


def _get_text_surface(
    key: str,
    text: str,
    font: pygame.font.Font,
    color: Tuple[int, int, int] = WHITE,
) -> pygame.Surface:
    cache = _HUD_CACHE.setdefault(key, {"text": None, "font_id": None, "color": None, "surface": None})
    font_id = id(font)
    if cache["text"] != text or cache["font_id"] != font_id or cache["color"] != color:
        cache["text"] = text
        cache["font_id"] = font_id
        cache["color"] = color
        cache["surface"] = font.render(text, True, color)
    return cache["surface"]


def _get_overlay_surface(size: Tuple[int, int], alpha: int) -> pygame.Surface:
    key = (size, alpha)
    surface = _OVERLAY_CACHE.get(key)
    if surface is None:
        surface = pygame.Surface(size, pygame.SRCALPHA)
        surface.fill((0, 0, 0, alpha))
        _OVERLAY_CACHE[key] = surface
    return surface


def _get_theme_font(size: int) -> pygame.font.Font:
    font = _THEME_FONT_CACHE.get(size)
    if font is not None:
        return font
    if _DEFAULT_THEME_FONT.exists():
        font = pygame.font.Font(str(_DEFAULT_THEME_FONT), size)
    else:
        font = pygame.font.SysFont("segoe ui", size)
    _THEME_FONT_CACHE[size] = font
    return font


def _results_layout(width: int, height: int) -> Dict[str, pygame.Rect]:
    """Single wide panel anchored near the bottom, similar to the menu footer."""
    margin = int(width * 0.06)
    title_rect = pygame.Rect(0, int(height * 0.05), width, int(height * 0.1))
    panel_top = max(int(height * 0.22), title_rect.bottom + 30)
    bottom_margin = max(int(height * 0.08), 80)
    panel_height = max(220, height - panel_top - bottom_margin)
    panel_rect = pygame.Rect(margin, panel_top, width - margin * 2, panel_height)
    return {"title": title_rect, "content_panel": panel_rect}


def _config_label(config: Dict[str, Any], keys: Tuple[str, ...], fallback: str) -> str:
    """Best-effort helper to fetch a readable label (song/difficulty) from config dicts."""
    sources: list[Dict[str, Any]] = []
    for candidate_key in ("", "ui", "metadata"):
        if not candidate_key:
            source = config
        else:
            source = config.get(candidate_key, {})
        if isinstance(source, dict):
            sources.append(source)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str):
                value = value.strip()
                if value:
                    return value
    return fallback


def _ellipsize(text: str, max_len: int) -> str:
    clean = (text or "").strip()
    if len(clean) <= max_len:
        return clean
    if max_len <= 3:
        return clean[:max_len]
    return clean[: max_len - 3].rstrip() + "..."


def draw_game(screen: pygame.Surface, game: Game, *, flip: bool = True, show_pause_overlay: bool = True):
    """Draw the gameplay screen (lanes, notes, HUD elements)."""
    assets = _ensure_static_assets(screen)
    base_surface = assets["surface"]
    lane_width = assets["lane_width"]
    lane_lefts = assets["lane_lefts"]
    lane_centers = assets["lane_centers"]
    hit_line_y = assets["hit_line_y"]
    hold_width = assets["hold_width"]
    note_heads = assets["note_heads"]
    head_height = assets["head_height"]
    width, height = screen.get_size()

    screen.blit(base_surface, (0, 0))

    current_time = game.current_time - (game.config.get("audio_latency_ms", 0) / 1000.0)
    note_display_time = game.config.get("note_display_time", 2.0)
    visible_notes = game.iter_visible_notes(current_time, note_display_time)
    if note_display_time <= 0:
        note_display_time = 0.001  # prevent division by zero
    inv_display_time = 1.0 / note_display_time

    for note in visible_notes:
        is_hold = getattr(note, "type", "tap") == "hold"
        hold_active = is_hold and note in game.active_hold_notes
        if note.hit and not hold_active:
            continue
        time_to_note = note.time - current_time
        if is_hold:
            end_time = note.time + getattr(note, "duration", 0.0)
            if time_to_note > note_display_time * 1.5:
                continue
            if (current_time - end_time) > 0.1 and not hold_active:
                continue
        else:
            if time_to_note < -0.1 or time_to_note > note_display_time * 1.5:
                continue

        lane = note.lane
        frac = max(0.0, min(1.0, time_to_note * inv_display_time))
        head_bottom = hit_line_y - frac * hit_line_y
        color = LANE_COLORS[lane]
        lane_left = lane_lefts[lane]
        center_x = lane_centers[lane]

        if is_hold:
            end_time = note.time + getattr(note, "duration", 0.0)
            tail_frac = max(0.0, min(1.0, (end_time - current_time) * inv_display_time))
            tail_bottom = hit_line_y - tail_frac * hit_line_y
            body_top = max(tail_bottom, 0.0)
            body_bottom = min(head_bottom, hit_line_y)
            hold_height_px = body_bottom - body_top
            if hold_height_px > 0.0:
                body_rect = pygame.Rect(
                    lane_left + (lane_width - hold_width) // 2,
                    int(body_top),
                    hold_width,
                    max(1, int(hold_height_px)),
                )
                pygame.draw.rect(screen, color, body_rect)

            head_surface = note_heads[lane]
            head_rect = head_surface.get_rect()
            head_rect.centerx = center_x
            head_rect.bottom = int(head_bottom)
            screen.blit(head_surface, head_rect)
        else:
            head_surface = note_heads[lane]
            head_rect = head_surface.get_rect()
            head_rect.centerx = center_x
            head_rect.bottom = int(head_bottom)
            screen.blit(head_surface, head_rect)
    font_small = _get_font(36)
    font_large = _get_font(72)
    score_surf = _get_text_surface("score_small", f"Score: {game.score}", font_small, WHITE)
    screen.blit(score_surf, (10, 10))

    if game.combo > 0:
        combo_surf = _get_text_surface("combo_large", f"{game.combo}x", font_large, WHITE)
        combo_rect = combo_surf.get_rect(center=(width // 2, 50))
        screen.blit(combo_surf, combo_rect)

    if game.judgement_timer > 0 and game.last_judgement_text:
        judg_surf = _get_text_surface("judgement_large", game.last_judgement_text, font_large, WHITE)
        judg_rect = judg_surf.get_rect(center=(width // 2, hit_line_y - 50))
        screen.blit(judg_surf, judg_rect)

    if game.paused and show_pause_overlay:
        draw_pause_overlay(screen, show_text=True)

    if flip:
        pygame.display.flip()


def draw_countdown(
    screen: pygame.Surface,
    message: str | None,
    *,
    flip: bool = True,
    apply_overlay: bool = True,
) -> None:
    """Overlay a countdown message (or just the dimmer) on top of the current view."""
    width, height = screen.get_size()
    if apply_overlay:
        overlay = _get_overlay_surface(screen.get_size(), 160)
        screen.blit(overlay, (0, 0))
    if message:
        font = _get_font(96)
        cache_key = f"countdown_text_{message}"
        text_surf = _get_text_surface(cache_key, message, font, WHITE)
        text_rect = text_surf.get_rect(center=(width // 2, height // 2))
        screen.blit(text_surf, text_rect)
    if flip:
        pygame.display.flip()


def draw_pause_overlay(screen: pygame.Surface, *, show_text: bool = True) -> None:
    """Render the translucent pause overlay, optionally with text."""
    overlay = _get_overlay_surface(screen.get_size(), 150)
    screen.blit(overlay, (0, 0))
    if not show_text:
        return
    width, height = screen.get_size()
    font_large = _get_font(72)
    font_small = _get_font(36)
    pause_text = _get_text_surface("pause_large", "PAUSED", font_large, WHITE)
    pause_rect = pause_text.get_rect(center=(width // 2, height // 2))
    screen.blit(pause_text, pause_rect)
    instr_text = _get_text_surface(
        "pause_instr",
        "Esc=Resume  R=Restart  Q=Quit",
        font_small,
        WHITE,
    )
    instr_rect = instr_text.get_rect(center=(width // 2, height // 2 + 50))
    screen.blit(instr_text, instr_rect)


def draw_results(screen: pygame.Surface, game: Game):
    """Draw the results screen with final stats and grade."""
    width, height = screen.get_size()
    elapsed = pygame.time.get_ticks() / 1000.0
    global _RESULT_BACKDROP
    if _RESULT_BACKDROP is None:
        _RESULT_BACKDROP = FuturisticBackdrop(screen.get_size())
    _RESULT_BACKDROP.draw(screen, elapsed, reduced_motion=False, show_grid=False)

    layout = _results_layout(width, height)
    title_rect = layout["title"]
    panel_rect = layout["content_panel"]

    font_large = _get_theme_font(72)
    font_body = _get_theme_font(32)
    font_small = _get_theme_font(24)
    font_track = _get_theme_font(42)

    title_surf = _get_text_surface("results_title", "Results", font_large, COLOR_TEXT)
    screen.blit(title_surf, title_surf.get_rect(center=title_rect.center))

    blit_glass_panel(screen, panel_rect, alpha=PANEL_ALPHA, radius=PANEL_RADIUS)

    song_label = _config_label(
        game.config,
        ("song_display_name", "song_name", "song", "selected_song", "track"),
        "Unknown Track",
    )
    display_song = _ellipsize(song_label, 20)
    difficulty_label = _config_label(
        game.config,
        ("difficulty_label", "difficulty", "selected_difficulty", "chart_difficulty"),
        "Unknown Difficulty",
    )

    max_score = getattr(game, "max_possible_score", 0)
    accuracy = (game.score / max_score * 100.0) if max_score > 0 else 0.0
    grade = "D"
    if accuracy >= 95.0:
        grade = "S"
    elif accuracy >= 90.0:
        grade = "A"
    elif accuracy >= 80.0:
        grade = "B"
    elif accuracy >= 70.0:
        grade = "C"

    pj = game.judgements.get("Perfect", 0)
    gj = game.judgements.get("Great", 0)
    bj = game.judgements.get("Good", 0)
    mj = game.judgements.get("Miss", 0)

    track_line = f"{display_song} -- {difficulty_label}"
    track_text = _get_text_surface("results_track_line", track_line, font_track, COLOR_TEXT)
    track_rect = track_text.get_rect(center=(panel_rect.centerx, panel_rect.y + 50))
    screen.blit(track_text, track_rect)

    grade_font_size = max(80, min(int(panel_rect.width * 0.18), 140))
    grade_font = _get_theme_font(grade_font_size)
    grade_color = GRADE_COLORS.get(grade, COLOR_TEXT)
    grade_surf = _get_text_surface("results_grade_big", grade, grade_font, grade_color)
    grade_rect = grade_surf.get_rect(midtop=(panel_rect.right - 150, track_rect.bottom + 10))
    screen.blit(grade_surf, grade_rect)

    score_value = f"{game.score:,} / {max_score:,}" if max_score else f"{game.score:,}"
    list_items = [
        ("Score", score_value),
        ("Max Combo", f"{game.max_combo:,}"),
        ("Perfect", pj),
        ("Great", gj),
        ("Good", bj),
        ("Miss", mj),
    ]

    text_y = track_rect.bottom + 30
    for label, value in list_items:
        cache_key = f"results_line_{label.lower().replace(' ', '_')}"
        surf = _get_text_surface(cache_key, f"{label}: {value}", font_body, COLOR_TEXT)
        screen.blit(surf, (panel_rect.x + 32, text_y))
        text_y += font_body.get_linesize() + 6

    cont_text = _get_text_surface("results_continue", "Esc/Q: Exit  |  R: Replay", font_small, COLOR_TEXT)
    cont_rect = cont_text.get_rect(bottomleft=(max(20, panel_rect.x), height - 18))
    screen.blit(cont_text, cont_rect)
    pygame.display.flip()
