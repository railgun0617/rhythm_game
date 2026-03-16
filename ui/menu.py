from __future__ import annotations

import json
import math
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import pygame

try:
    import pygame.freetype as freetype
except ImportError:  # pragma: no cover - optional dependency
    freetype = None

try:
    import tkinter as tk
except Exception:  # pragma: no cover - optional dependency
    tk = None

from config.config import DEFAULT_CONFIG
from charting.chart_generator import CHARTS_DIR
from persistence.scores import best_for, leaderboard, MAX_SCORES_PER_CHART
from ui import renderer
from ui.theme import (
    ASSETS_DIR,
    DEFAULT_FONT_PATH,
    COLOR_BACKGROUND,
    COLOR_NEON_CYAN,
    COLOR_ACCENT_MAGENTA,
    COLOR_ACCENT_MINT,
    COLOR_ERROR,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_ACTIVE,
    COLOR_GRID,
    PANEL_ALPHA,
    PANEL_RADIUS,
    PILL_RADIUS,
    GLOW_LAYER_SCALES,
    GLOW_LAYER_ALPHAS,
    STAR_LAYERS,
    STAR_COLORS,
    GRID_RES,
    GRID_SCROLL_SPEED,
    GlowCache,
    FuturisticBackdrop,
    blit_glass_panel,
)
from runtime_paths import CONFIG_FILE

# Style guide:
# - Palette: adjust COLOR_* constants to retheme the HUD quickly.
# - Glass geometry: tweak PANEL_RADIUS, PILL_RADIUS, PANEL_ALPHA for rounded-rect look.
# - Glow strength: edit GLOW_LAYER_SCALES/GLOW_LAYER_ALPHAS or GlowCache._tint() output.
# - Animation feel: tune ENTRY_ANIM_DURATION, SELECTION_SMOOTHING, PULSE_SPEED, GRID_SCROLL_SPEED.
# - Background density: set STAR_LAYERS, STAR_COLORS, and GRID_RES for particle/grid complexity.

CONFIG_PATH = CONFIG_FILE

TITLE = "Rhythm Master"  # <- game name on the front page

BOTTOM_PANEL_ALPHA = 84

ROW_GLOW_INTENSITY = 0.35
DIFF_GLOW_INTENSITY = 0.25
DIFFICULTY_PILL_HEIGHT = 58
DIFFICULTY_PILL_GAP = 14
DIFFICULTY_PANEL_FRAME = 96

ENTRY_ANIM_DURATION = 0.35
FOOTER_DELAY = 0.12
SELECTION_SMOOTHING = 12.0
SCROLL_SMOOTHING = 10.0
PULSE_SPEED = 2.2
TARGET_FPS_DEFAULT = 120
TITLE_SCROLL_SPEED = 40.0

DIFFICULTY_LABELS = ["Easy", "Normal", "Hard", "Expert"]
IMPORTABLE_EXTS = {".wav", ".mp3", ".ogg", ".flac"}


@dataclass
class SongInfo:
    base_name: str            # e.g., "unravel"
    display_name: str         # e.g., "Unravel"
    audio_path: Path          # assets/...wav
    available_difficulties: List[str]  # ["Easy", "Normal", "Hard", "Expert"] present in charts


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------
def discover_songs() -> List[SongInfo]:
    """Scan assets & charts directory to build SongInfo objects."""
    ASSETS_DIR.mkdir(exist_ok=True)
    CHARTS_DIR.mkdir(exist_ok=True)

    audio_by_base: Dict[str, Path] = {}
    for candidate in ASSETS_DIR.glob("*"):
        if candidate.suffix.lower() in {".wav", ".mp3", ".ogg", ".flac"} and candidate.is_file():
            audio_by_base[candidate.stem] = candidate

    diffs_by_base: Dict[str, List[str]] = {}
    for chart_path in CHARTS_DIR.glob("*.json"):
        name = chart_path.stem
        if name.endswith("_debug"):
            continue
        parts = name.rsplit("_", 1)
        if len(parts) != 2:
            continue
        base, diff = parts
        diff_label = diff.capitalize()
        if diff_label not in DIFFICULTY_LABELS:
            diff_label = diff_label.title()
        diffs_by_base.setdefault(base, [])
        if diff_label not in diffs_by_base[base]:
            diffs_by_base[base].append(diff_label)

    order = {label: idx for idx, label in enumerate(DIFFICULTY_LABELS)}
    songs: List[SongInfo] = []
    for base, audio_path in audio_by_base.items():
        display = base.replace("_", " ").title()
        diffs = sorted(diffs_by_base.get(base, []), key=lambda d: order.get(d, 99))
        songs.append(
            SongInfo(
                base_name=base,
                display_name=display,
                audio_path=audio_path,
                available_difficulties=diffs,
            )
        )

    # Support charts that specify an explicit audio_path (even if the audio is outside assets/)
    for base, diffs in diffs_by_base.items():
        if base in audio_by_base or not diffs:
            continue
        first_chart = CHARTS_DIR / f"{base}_{diffs[0].lower()}.json"
        try:
            with first_chart.open("r", encoding="utf-8") as fh:
                chart_data = json.load(fh)
        except Exception:
            continue
        audio_file = chart_data.get("audio_file")
        if not audio_file:
            continue
        display = base.replace("_", " ").title()
        songs.append(
            SongInfo(
                base_name=base,
                display_name=display,
                audio_path=Path(audio_file),
                available_difficulties=sorted(diffs, key=lambda d: order.get(d, 99)),
            )
        )

    songs.sort(key=lambda s: s.display_name.lower())
    return songs


# ---------------------------------------------------------------------------
# Utility math helpers
# ---------------------------------------------------------------------------
def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _ease_out_cubic(t: float) -> float:
    inv = 1.0 - t
    return 1.0 - inv * inv * inv


def _ease_out_quint(t: float) -> float:
    inv = 1.0 - t
    return 1.0 - inv * inv * inv * inv * inv


def _approach(current: float, target: float, speed: float, dt: float) -> float:
    """Critically damped smoothing between values."""
    diff = target - current
    step = diff * _clamp(speed * dt, 0.0, 1.0)
    return current + step


def _color_key(color: pygame.Color) -> Tuple[int, int, int, int]:
    return (int(color.r), int(color.g), int(color.b), int(color.a))


def _shorten_label(text: str, max_chars: int) -> str:
    text = text.strip()
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3] + "..."


# ---------------------------------------------------------------------------
# Text rendering & glow caching
# ---------------------------------------------------------------------------
class TextCache:
    """Pre-render text surfaces (regular + glow) to keep frame time low."""

    def __init__(self) -> None:
        self._font_cache: Dict[Tuple[int, bool], pygame.font.Font] = {}
        self._surface_cache: Dict[Tuple[str, int, Tuple[int, int, int, int], bool, bool], pygame.Surface] = {}
        self._preferred_fonts: Sequence[str] = (
            "segoe ui semibold",
            "rajdhani",
            "orbitron",
            "rubik",
            "futura",
            "arial",
        )
        self._font_file: Optional[Path] = DEFAULT_FONT_PATH if DEFAULT_FONT_PATH.exists() else None
        self._freetype_font_path = None
        if freetype:
            if self._font_file:
                self._freetype_font_path = str(self._font_file)
            else:
                for candidate in self._preferred_fonts:
                    match = pygame.font.match_font(candidate)
                    if match:
                        self._freetype_font_path = match
                        break

    def get(self, text: str, size: int, color: pygame.Color, *, glow: bool = False, bold: bool = False) -> pygame.Surface:
        key = (text, size, _color_key(color), glow, bold)
        if key in self._surface_cache:
            return self._surface_cache[key]

        surface = self._render(text, size, color, bold=bold)
        if glow:
            surface = self._with_glow(surface, color)
        self._surface_cache[key] = surface
        return surface

    def _render(self, text: str, size: int, color: pygame.Color, *, bold: bool) -> pygame.Surface:
        if freetype and self._freetype_font_path:
            font = freetype.Font(self._freetype_font_path, size=size)
            font.pad = True
            font.strong = bold
            surf, _ = font.render(text, fgcolor=color, bgcolor=None)
            return surf.convert_alpha()

        font = self._font(size, bold=bold)
        return font.render(text, True, color)

    def _font(self, size: int, *, bold: bool) -> pygame.font.Font:
        key = (size, bold)
        if key in self._font_cache:
            return self._font_cache[key]
        if self._font_file:
            font = pygame.font.Font(str(self._font_file), size)
            font.set_bold(bold)
        else:
            font = pygame.font.SysFont(self._preferred_fonts, size, bold=bold)
            if font is None:
                font = pygame.font.Font(None, size)
        self._font_cache[key] = font
        return font

    def _with_glow(self, base: pygame.Surface, color: pygame.Color) -> pygame.Surface:
        width, height = base.get_size()
        pad = max(6, int(max(width, height) * 0.2))
        canvas = pygame.Surface((width + pad * 2, height + pad * 2), pygame.SRCALPHA)
        canvas.blit(base, (pad, pad))

        # Soft glow layers using smooth scaling
        glow_color = pygame.Color(color)
        glow_color.a = 0
        for scale, alpha in ((1.8, 38), (1.45, 64), (1.2, 96)):
            scaled = pygame.transform.smoothscale(base, (int(width * scale), int(height * scale)))
            tint = pygame.Surface(scaled.get_size(), pygame.SRCALPHA)
            tint.fill((glow_color.r, glow_color.g, glow_color.b, alpha))
            scaled.blit(tint, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)
            canvas.blit(
                scaled,
                (pad + (width - scaled.get_width()) // 2, pad + (height - scaled.get_height()) // 2),
                special_flags=pygame.BLEND_ADD,
            )
        return canvas


# ---------------------------------------------------------------------------
# Menu scene
# ---------------------------------------------------------------------------
class MenuScene:
    def __init__(self, screen: pygame.Surface, config: Dict):
        self.screen = screen
        self.config = config
        self.songs = discover_songs()
        self.song_idx = 0 if self.songs else -1
        self.diff_idx = 1 if len(DIFFICULTY_LABELS) > 1 else 0
        self.difficulties = DIFFICULTY_LABELS

        self._text = TextCache()
        self._glow = GlowCache()
        self._backdrop = FuturisticBackdrop(screen.get_size())
        self._target_fps = self._resolve_target_fps(config)
        try:
            pygame.event.set_allowed(pygame.DROPFILE)
        except Exception:
            pass

        self.reduced_motion = bool(
            config.get("reduced_motion")
            or config.get("ui_reduced_motion")
            or config.get("ui", {}).get("reduced_motion", False)
        )

        self._time = 0.0
        self._entry_time = 0.0
        self._song_cursor = float(self.song_idx if self.song_idx >= 0 else 0.0)
        self._song_scroll = 0.0
        self._song_scroll_target = 0.0
        self._diff_cursor = float(self.diff_idx)

        self._song_row_rects: List[Tuple[int, pygame.Rect]] = []
        self._diff_pill_rects: List[pygame.Rect] = []
        self._hover_song: Optional[int] = None
        self._hover_diff: Optional[int] = None
        self._last_click_time = 0
        self._song_panel_rect = pygame.Rect(0, 0, 0, 0)
        self._difficulty_panel_rect = pygame.Rect(0, 0, 0, 0)
        self._settings_icon_rect = pygame.Rect(0, 0, 0, 0)
        self._settings_icon_hover = False
        self._overlay_active = False
        self._overlay_button_rects: Dict[str, pygame.Rect] = {}
        self._overlay_hover: Optional[str] = None
        self._overlay_option_index = 0
        self._overlay_panel_rect = pygame.Rect(0, 0, 0, 0)
        self._import_button_rect = pygame.Rect(0, 0, 0, 0)
        self._import_button_hover = False
        self._import_overlay_active = False
        self._import_panel_rect = pygame.Rect(0, 0, 0, 0)
        self._import_message = "Drop a WAV or MP3 file into this window."
        self._import_message_error = False
        self._import_pending_source: Optional[Path] = None
        self._import_name: str = ""
        self._import_input_rect = pygame.Rect(0, 0, 0, 0)
        self._import_input_active = False
        self._import_caret_index = 0
        self._import_scroll_offset = 0
        self._import_repeat_backup: Optional[Tuple[int, int]] = None
        self._cursor_mode: Optional[str] = None
        self._ibeam_supported = hasattr(pygame, "SYSTEM_CURSOR_IBEAM")
        self._song_changed()

    def _song_changed(self) -> None:
        if self.song_idx < 0:
            self._song_cursor = 0.0
            self._song_scroll_target = 0.0
            self._song_scroll = 0.0
            return
        self.song_idx = min(self.song_idx, len(self.songs) - 1)
        self._song_cursor = float(self.song_idx)
        self._song_scroll_target = float(self.song_idx)
        self._song_scroll = self._song_scroll_target

    def refresh_catalog(self) -> None:
        previous_base = None
        if 0 <= self.song_idx < len(self.songs):
            previous_base = self.songs[self.song_idx].base_name
        self.songs = discover_songs()
        if not self.songs:
            self.song_idx = -1
            self._song_cursor = 0.0
            self._song_scroll_target = 0.0
            self._song_scroll = 0.0
            return
        if previous_base:
            for idx, song in enumerate(self.songs):
                if song.base_name == previous_base:
                    self.song_idx = idx
                    break
            else:
                self.song_idx = min(self.song_idx, len(self.songs) - 1)
        else:
            self.song_idx = min(max(self.song_idx, 0), len(self.songs) - 1)
        self._song_changed()

    def loop(self) -> Tuple[str, Optional[SongInfo], Optional[str]]:
        clock = pygame.time.Clock()
        while True:
            dt = clock.tick(self._target_fps) / 1000.0
            self._time += dt
            self._entry_time = min(self._entry_time + dt, ENTRY_ANIM_DURATION + FOOTER_DELAY + 0.3)

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return ("quit", None, None)
                if self._import_overlay_active:
                    result = self._handle_import_event(event)
                    if result == "quit":
                        return ("quit", None, None)
                    continue
                if self._overlay_active:
                    overlay_result = self._handle_overlay_event(event)
                    if overlay_result == "quit":
                        return ("quit", None, None)
                    if overlay_result == "settings":
                        return ("settings", None, None)
                    continue
                if event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        self._open_overlay()
                        continue
                    if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        if self.song_idx >= 0:
                            return ("play", self.songs[self.song_idx], self.difficulties[self.diff_idx])
                    if event.key == pygame.K_TAB:
                        song = self.songs[self.song_idx] if (self.songs and self.song_idx >= 0) else None
                        diff = self.difficulties[self.diff_idx] if self.difficulties else None
                        return ("scores", song, diff)
                    if event.key in (pygame.K_UP, pygame.K_w):
                        self._select_song(self.song_idx - 1)
                    if event.key in (pygame.K_DOWN, pygame.K_s):
                        self._select_song(self.song_idx + 1)
                    if event.key in (pygame.K_LEFT, pygame.K_a):
                        self._select_difficulty(self.diff_idx - 1)
                    if event.key in (pygame.K_RIGHT, pygame.K_d):
                        self._select_difficulty(self.diff_idx + 1)
                elif event.type == pygame.MOUSEMOTION:
                    self._handle_mouse_motion(event.pos)
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if self._import_button_rect.collidepoint(event.pos):
                        self._open_import_overlay()
                        continue
                    if self._settings_icon_rect.collidepoint(event.pos):
                        return ("settings", None, None)
                    if self._handle_mouse_click(event.pos):
                        now = pygame.time.get_ticks()
                        if now - self._last_click_time < 300 and self.song_idx >= 0:
                            return ("play", self.songs[self.song_idx], self.difficulties[self.diff_idx])
                        self._last_click_time = now
                elif event.type == pygame.MOUSEWHEEL:
                    self._handle_mouse_wheel(event)

            self._draw(dt)
            pygame.display.flip()

        return ("quit", None, None)

    # ------------------------------------------------------------------
    def _resolve_target_fps(self, config: Dict) -> int:
        fps = config.get("target_fps", TARGET_FPS_DEFAULT)
        if fps < 60:
            return 60
        return min(144, int(fps))

    def _select_song(self, idx: int) -> None:
        if not self.songs:
            self.song_idx = -1
            return
        self.song_idx = int(_clamp(idx, 0, len(self.songs) - 1))

    def _select_difficulty(self, idx: int) -> None:
        self.diff_idx = idx % len(self.difficulties)

    def _handle_mouse_motion(self, pos: Tuple[int, int]) -> None:
        if self._modal_active():
            self._import_button_hover = False
            return
        self._update_song_hover(pos)

        self._hover_diff = None
        for idx, rect in enumerate(self._diff_pill_rects):
            if rect.collidepoint(pos):
                self._hover_diff = idx
                break
        self._settings_icon_hover = self._settings_icon_rect.collidepoint(pos)
        self._import_button_hover = self._import_button_rect.collidepoint(pos)

    def _update_song_hover(self, pos: Optional[Tuple[int, int]] = None) -> None:
        if self._modal_active():
            return
        if pos is None:
            pos = pygame.mouse.get_pos()
        self._hover_song = None
        for song_idx, rect in self._song_row_rects:
            if rect.collidepoint(pos):
                self._hover_song = song_idx
                break

    def _handle_mouse_click(self, pos: Tuple[int, int]) -> bool:
        if self._hover_song is not None:
            self._select_song(self._hover_song)
            return True
        if self._hover_diff is not None:
            self._select_difficulty(self._hover_diff)
            return True
        return False

    def _handle_mouse_wheel(self, event: pygame.event.Event) -> None:
        if self._modal_active():
            return
        mouse_pos = pygame.mouse.get_pos()
        if self._song_panel_rect.collidepoint(mouse_pos) and self.songs:
            delta = -event.y
            if delta:
                if self.song_idx < 0:
                    self.song_idx = 0
                self._select_song(self.song_idx + delta)
                return
        if self._difficulty_panel_rect.collidepoint(mouse_pos):
            delta = -event.y
            if delta:
                self._select_difficulty(self.diff_idx + delta)

    def _modal_active(self) -> bool:
        return self._overlay_active or self._import_overlay_active

    def _open_import_overlay(self) -> None:
        self._import_overlay_active = True
        self._import_message = "Drop a WAV or MP3 file into this window."
        self._import_message_error = False
        self._import_pending_source = None
        self._import_name = ""
        self._import_input_active = False
        self._import_caret_index = 0
        self._import_scroll_offset = 0
        self._enable_import_key_repeat()
        self._update_import_layout()

    def _close_import_overlay(self) -> None:
        self._import_overlay_active = False
        self._import_message = "Drop a WAV or MP3 file into this window."
        self._import_message_error = False
        self._import_pending_source = None
        self._import_name = ""
        self._import_button_hover = False
        self._import_input_active = False
        self._import_caret_index = 0
        self._import_scroll_offset = 0
        self._restore_import_key_repeat()
        self._set_cursor("arrow")

    def _enable_import_key_repeat(self) -> None:
        try:
            current = pygame.key.get_repeat()
        except Exception:
            current = (0, 0)
        self._import_repeat_backup = current
        try:
            pygame.key.set_repeat(280, 35)
        except Exception:
            pass

    def _restore_import_key_repeat(self) -> None:
        if self._import_repeat_backup is None:
            return
        delay, interval = self._import_repeat_backup
        try:
            if delay > 0 and interval > 0:
                pygame.key.set_repeat(delay, interval)
            else:
                pygame.key.set_repeat()
        except Exception:
            pass
        finally:
            self._import_repeat_backup = None

    def _handle_import_event(self, event: pygame.event.Event) -> Optional[str]:
        if event.type == pygame.QUIT:
            return "quit"
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_q):
                self._close_import_overlay()
                return "close"
            if self._import_pending_source:
                if (
                    self._import_input_active
                    and event.key == pygame.K_v
                    and event.mod & (pygame.KMOD_CTRL | pygame.KMOD_META)
                ):
                    pasted = self._clipboard_text()
                    if pasted:
                        self._insert_import_text(pasted)
                    return "handled"
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    self._finalize_import()
                    return "handled"
                if event.key == pygame.K_BACKSPACE:
                    if self._import_caret_index > 0:
                        self._import_name = (
                            self._import_name[: self._import_caret_index - 1]
                            + self._import_name[self._import_caret_index :]
                        )
                        self._import_caret_index -= 1
                        self._clamp_import_caret()
                    return "handled"
                if event.key == pygame.K_DELETE:
                    if self._import_caret_index < len(self._import_name):
                        self._import_name = (
                            self._import_name[: self._import_caret_index]
                            + self._import_name[self._import_caret_index + 1 :]
                        )
                        self._clamp_import_caret()
                    return "handled"
                if event.key == pygame.K_LEFT:
                    if self._import_caret_index > 0:
                        self._import_caret_index -= 1
                    else:
                        self._import_scroll_offset = max(0, self._import_scroll_offset - 10)
                    return "handled"
                if event.key == pygame.K_RIGHT:
                    if self._import_caret_index < len(self._import_name):
                        self._import_caret_index += 1
                    else:
                        self._import_scroll_offset += 10
                    return "handled"
                if event.key == pygame.K_HOME:
                    self._import_caret_index = 0
                    return "handled"
                if event.key == pygame.K_END:
                    self._import_caret_index = len(self._import_name)
                    return "handled"
                text = event.unicode
                if text:
                    self._insert_import_text(text)
                    return "handled"
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if not self._import_panel_rect.collidepoint(event.pos):
                self._close_import_overlay()
                return "close"
            if self._import_input_rect.collidepoint(event.pos):
                self._import_input_active = True
                self._import_caret_index = self._caret_index_from_mouse(event.pos[0])
            else:
                self._import_input_active = False
            return "handled"
        if event.type == pygame.DROPFILE:
            self._process_import_path(event.file)
            return "drop"
        return "handled"

    def _process_import_path(self, path_str: str) -> None:
        try:
            source = Path(path_str)
        except Exception:
            self._import_message = "Could not read dropped item."
            self._import_message_error = True
            return
        if not source.exists() or not source.is_file():
            self._import_message = "Only files can be imported."
            self._import_message_error = True
            return
        ext = source.suffix.lower()
        if ext not in IMPORTABLE_EXTS:
            self._import_message = "Unsupported format. Use WAV/MP3/OGG/FLAC."
            self._import_message_error = True
            return
        self._import_pending_source = source
        self._import_name = source.stem
        self._import_message = "Edit the name and press Enter to import."
        self._import_message_error = False
        self._import_input_active = True
        self._import_caret_index = len(self._import_name)
        self._import_scroll_offset = 0

    def _insert_import_text(self, text: str) -> None:
        if not self._import_pending_source:
            return
        if not text:
            return
        allowed: List[str] = []
        for ch in text:
            if ch in ("\r", "\n", "\t"):
                continue
            if ch in ("/", "\\"):
                continue
            allowed.append(ch)
        if not allowed:
            return
        remaining = 100 - len(self._import_name)
        if remaining <= 0:
            return
        insert = "".join(allowed)[:remaining]
        if not insert:
            return
        self._import_name = (
            self._import_name[: self._import_caret_index]
            + insert
            + self._import_name[self._import_caret_index :]
        )
        self._import_caret_index += len(insert)
        self._clamp_import_caret()

    def _clipboard_text(self) -> str:
        text = self._clipboard_from_tk()
        if text:
            return text
        return self._clipboard_from_scrap()

    def _clipboard_from_tk(self) -> str:
        if tk is None:
            return ""
        root = None
        try:
            root = tk.Tk()
            root.withdraw()
            data = root.clipboard_get()
        except Exception:
            data = ""
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
        if not data:
            return ""
        return str(data).replace("\x00", "")

    def _clipboard_from_scrap(self) -> str:
        scrap = getattr(pygame, "scrap", None)
        if scrap is None:
            return ""
        try:
            if not scrap.get_init():
                scrap.init()
            mode = getattr(pygame, "SCRAP_CLIPBOARD", None)
            if mode is not None and hasattr(scrap, "set_mode"):
                scrap.set_mode(mode)
            text = ""
            get_text = getattr(scrap, "get_text", None)
            if callable(get_text):
                for encoding in ("UTF-8", "UTF-16", "UTF-32", None):
                    try:
                        candidate = get_text(encoding)
                    except Exception:
                        candidate = None
                    if candidate:
                        text = candidate
                        break
            if text:
                return text.replace("\x00", "")

            unicode_type = getattr(pygame, "SCRAP_UNICODE", None)
            text_type = getattr(pygame, "SCRAP_TEXT", None)
            unicode_mode = False
            data = None
            if unicode_type is not None:
                try:
                    data = scrap.get(unicode_type)
                    unicode_mode = bool(data)
                except Exception:
                    data = None
                    unicode_mode = False
            if not data and text_type is not None:
                try:
                    data = scrap.get(text_type)
                except Exception:
                    return ""
        except Exception:
            return ""
        if not data:
            return ""
        if isinstance(data, bytes):
            if unicode_mode:
                try:
                    text = data.decode("utf-16-le")
                except UnicodeDecodeError:
                    text = data.decode("utf-16", errors="ignore")
            else:
                candidates = (
                    "utf-8-sig",
                    "utf-8",
                    "utf-16-le",
                    "utf-16",
                    "mbcs",
                    "utf-32",
                )
                for encoding in candidates:
                    try:
                        text = data.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    text = data.decode("utf-8", errors="ignore")
            return text.replace("\x00", "")
        return str(data).replace("\x00", "")

    def _sanitize_import_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        allowed = []
        for ch in value:
            if ch.isalnum() or ch in (" ", "-", "_"):
                allowed.append(ch)
            elif ch == ".":
                continue
        sanitized = "".join(allowed).strip()
        return sanitized or ""

    def _finalize_import(self) -> None:
        if not self._import_pending_source:
            return
        source = self._import_pending_source
        base = self._sanitize_import_name(self._import_name) or source.stem
        ext = source.suffix.lower()
        dest = ASSETS_DIR / f"{base}{ext}"
        counter = 1
        while dest.exists():
            dest = ASSETS_DIR / f"{base}_{counter}{ext}"
            counter += 1
        try:
            shutil.copy2(source, dest)
        except Exception as exc:
            self._import_message = f"Copy failed: {exc}"
            self._import_message_error = True
            return
        self.refresh_catalog()
        display_name = self._format_display_filename(dest.name, max_chars=14)
        self._import_message = f"Imported {display_name}. Drop another file or press Esc."
        self._import_message_error = False
        self._import_pending_source = None
        self._import_name = ""
        self._import_input_active = False
        self._import_caret_index = 0
        self._import_scroll_offset = 0

    def _set_cursor(self, mode: str) -> None:
        if not self._ibeam_supported:
            return
        if mode == self._cursor_mode:
            return
        try:
            if mode == "ibeam":
                pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_IBEAM)
            else:
                pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_ARROW)
            self._cursor_mode = mode
        except Exception:
            self._cursor_mode = None

    def _caret_index_from_mouse(self, mouse_x: int) -> int:
        text = self._import_name
        if not text:
            return 0
        left = self._import_input_rect.x + 12
        offset = self._import_scroll_offset
        rel_x = mouse_x - left + offset
        if rel_x <= 0:
            return 0
        color = COLOR_TEXT if self._import_pending_source else COLOR_TEXT_MUTED
        best_index = len(text)
        best_distance = float("inf")
        for idx in range(len(text) + 1):
            substring = text[:idx]
            width = self._text.get(substring, 26, color, glow=False, bold=False).get_width()
            distance = abs(width - rel_x)
            if distance < best_distance:
                best_distance = distance
                best_index = idx
        return best_index

    def _clamp_import_caret(self) -> None:
        self._import_caret_index = max(0, min(self._import_caret_index, len(self._import_name)))

    def _trim_with_ellipsis(self, text: str, max_width: int, color: pygame.Color) -> str:
        if max_width <= 0:
            return ""
        if self._text.get(text, 26, color, glow=False, bold=False).get_width() <= max_width:
            return text
        ellipsis = "..."
        for end in range(len(text), -1, -1):
            candidate = text[:end] + ellipsis
            if self._text.get(candidate, 26, color, glow=False, bold=False).get_width() <= max_width:
                return candidate
        return ellipsis

    def _format_display_filename(self, filename: str, max_chars: int = 24) -> str:
        if len(filename) <= max_chars:
            return filename
        stem, dot, ext = filename.rpartition(".")
        if not dot:
            return filename[: max(1, max_chars - 3)] + "..."
        suffix = f".{ext}"
        available = max_chars - len(suffix) - 3
        if available <= 0:
            return f"...{suffix}"
        return f"{stem[:available]}...{suffix}"

    def _update_import_layout(self) -> None:
        width, height = self.screen.get_size()
        panel_width = int(width * 0.8)
        panel_height = int(height * 0.65)
        panel_rect = pygame.Rect(0, 0, panel_width, panel_height)
        panel_rect.center = (width // 2, height // 2)
        self._import_panel_rect = panel_rect
        input_width = panel_rect.width - 220
        # input rect assigned dynamically in _draw_import_overlay
        self._import_input_rect = pygame.Rect(panel_rect.x + 110, panel_rect.y + 70, input_width, 46)

    def _draw_import_overlay(self, surface: pygame.Surface) -> None:
        overlay_surface = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
        overlay_surface.fill((5, 6, 14, 235))
        surface.blit(overlay_surface, (0, 0))
        self._update_import_layout()
        panel_rect = self._import_panel_rect
        panel_surface = pygame.Surface(panel_rect.size, pygame.SRCALPHA)
        pygame.draw.rect(
            panel_surface,
            pygame.Color(18, 24, 48, 235),
            panel_surface.get_rect(),
            border_radius=32,
        )
        pygame.draw.rect(
            panel_surface,
            pygame.Color(0, 229, 255, 110),
            panel_surface.get_rect(),
            width=3,
            border_radius=32,
        )
        surface.blit(panel_surface, panel_rect.topleft)

        title = self._text.get("Import Audio", 44, COLOR_TEXT, glow=True, bold=True)
        surface.blit(title, title.get_rect(midtop=(panel_rect.centerx, panel_rect.y - 20)))

        drop_rect = panel_rect.inflate(-160, -240)
        drop_rect.y = panel_rect.y + 140
        drop_rect.height = panel_rect.height - 320
        pygame.draw.rect(surface, (0, 0, 0, 0), drop_rect, border_radius=24)
        dash_color = pygame.Color(0, 229, 255, 130)
        pygame.draw.rect(surface, dash_color, drop_rect, width=3, border_radius=24)
        prompt = self._text.get("Drop files here", 32, COLOR_TEXT, glow=False, bold=True)
        surface.blit(prompt, prompt.get_rect(center=drop_rect.center))

        instructions = self._text.get(
            "Drag & drop WAV or MP3 files anywhere in this window.",
            24,
            COLOR_TEXT_MUTED,
            glow=False,
            bold=False,
        )
        surface.blit(instructions, instructions.get_rect(midbottom=(panel_rect.centerx, drop_rect.y - 14)))

        input_rect = pygame.Rect(drop_rect.x, drop_rect.bottom + 30, drop_rect.width, 46)
        self._import_input_rect = input_rect
        label = self._text.get("File name", 22, COLOR_TEXT_MUTED, glow=False, bold=False)
        surface.blit(label, label.get_rect(midleft=(input_rect.x, input_rect.y - 18)))
        pygame.draw.rect(surface, (0, 0, 0, 0), input_rect, border_radius=16)
        input_border = COLOR_ACCENT_MINT if self._import_pending_source else COLOR_TEXT_MUTED
        pygame.draw.rect(surface, input_border, input_rect, width=2, border_radius=16)
        name_display = self._import_name if self._import_pending_source else "Drop a file to set its name"
        name_color = COLOR_TEXT if self._import_pending_source else COLOR_TEXT_MUTED
        name_surface = self._text.get(name_display, 26, name_color, glow=False, bold=False)
        padding = 12
        visible_width = max(0, input_rect.width - padding * 2)
        caret_width = self._text.get(self._import_name[: self._import_caret_index], 26, name_color, glow=False, bold=False).get_width()
        max_offset = max(0, name_surface.get_width() - visible_width)
        offset = max(0, min(self._import_scroll_offset, max_offset))
        left_edge = input_rect.x + padding
        caret_screen_x = left_edge - offset + caret_width
        view_min = left_edge
        view_max = left_edge + visible_width
        if caret_screen_x < view_min:
            offset = max(0, min(max_offset, offset - (view_min - caret_screen_x)))
        elif caret_screen_x > view_max:
            offset = max(0, min(max_offset, offset + (caret_screen_x - view_max)))
        if offset != self._import_scroll_offset:
            self._import_scroll_offset = offset
        caret_screen_x = left_edge - offset + caret_width
        clip_rect = pygame.Rect(left_edge, input_rect.y + (input_rect.height - name_surface.get_height()) // 2, visible_width, name_surface.get_height())
        surface.set_clip(clip_rect)
        name_rect = name_surface.get_rect(midleft=(left_edge - offset, input_rect.centery))
        surface.blit(name_surface, name_rect)
        caret_visible = self._import_input_active and self._import_pending_source and (pygame.time.get_ticks() // 500) % 2 == 0
        if caret_visible:
            pygame.draw.line(
                surface,
                COLOR_TEXT,
                (caret_screen_x, input_rect.y + 10),
                (caret_screen_x, input_rect.bottom - 10),
                2,
            )
        surface.set_clip(None)

        status_color = COLOR_ERROR if self._import_message_error else COLOR_ACCENT_MINT
        status = self._text.get(self._import_message, 26, status_color, glow=False, bold=False)
        surface.blit(status, status.get_rect(midbottom=(panel_rect.centerx, panel_rect.bottom - 32)))

        cursor_mode = "ibeam" if input_rect.collidepoint(pygame.mouse.get_pos()) else "arrow"
        self._set_cursor(cursor_mode)


    # ------------------------------------------------------------------
    def _draw(self, dt: float) -> None:
        surface = self.screen
        width, height = surface.get_size()
        self._backdrop.draw(surface, self._time, reduced_motion=self.reduced_motion)

        self._song_row_rects = []
        self._diff_pill_rects = []

        layout = self._compute_layout(width, height)
        entry_progress = _clamp(self._entry_time / ENTRY_ANIM_DURATION, 0.0, 1.0) if ENTRY_ANIM_DURATION > 0 else 1.0
        entry_offset = _lerp(40.0, 0.0, _ease_out_cubic(entry_progress))

        song_rect = layout["song_panel"].move(0, int(entry_offset))
        diff_rect = layout["difficulty_panel"].move(0, int(entry_offset * 0.7))
        self._song_panel_rect = song_rect
        self._difficulty_panel_rect = diff_rect

        self._draw_song_panel(surface, song_rect)
        self._draw_difficulty_panel(surface, diff_rect)
        self._draw_title(surface, layout["title_area"], entry_progress)
        self._draw_bottom_panel(surface, layout["bottom_panel"], entry_progress)
        self._draw_footer(surface, layout["footer_area"], entry_progress)
        if self._overlay_active:
            self._draw_overlay(surface)
        if self._import_overlay_active:
            self._draw_import_overlay(surface)

    def _compute_layout(self, width: int, height: int) -> Dict[str, pygame.Rect]:
        margin = int(width * 0.06)
        gap = int(width * 0.035)
        title_area = pygame.Rect(0, int(height * 0.04), width, int(height * 0.1))
        gap_below_title = max(16, int(height * 0.02))
        top_margin = max(int(height * 0.13), title_area.bottom + gap_below_title)
        vertical_gap = max(12, int(height * 0.025))
        bottom_panel_height = max(140, int(height * 0.2))
        footer_height = max(40, int(height * 0.075))

        song_width = int(width * 0.42)
        diff_width = int(width * 0.3)
        panel_height = height - top_margin - bottom_panel_height - footer_height - vertical_gap
        panel_height = max(panel_height, height // 3)

        song_panel = pygame.Rect(margin, top_margin, song_width, panel_height)
        diff_panel_height = max(int(panel_height * 0.45), self._min_difficulty_panel_height())
        diff_panel_height = min(panel_height, diff_panel_height)
        diff_panel = pygame.Rect(song_panel.right + gap, top_margin, diff_width, diff_panel_height)
        bottom_panel = pygame.Rect(
            margin,
            height - footer_height - bottom_panel_height,
            width - margin * 2,
            bottom_panel_height - margin // 3,
        )
        footer_area = pygame.Rect(
            margin,
            height - footer_height + margin // 6,
            width - margin * 2,
            footer_height - margin // 6,
        )

        return {
            "song_panel": song_panel,
            "difficulty_panel": diff_panel,
            "bottom_panel": bottom_panel,
            "title_area": title_area,
            "footer_area": footer_area,
        }

    def _min_difficulty_panel_height(self) -> int:
        rows = max(1, len(self.difficulties))
        pill_stack = rows * DIFFICULTY_PILL_HEIGHT + max(0, rows - 1) * DIFFICULTY_PILL_GAP
        return DIFFICULTY_PANEL_FRAME + pill_stack

    def _draw_panel(self, surface: pygame.Surface, rect: pygame.Rect, alpha: int = PANEL_ALPHA, radius: int = PANEL_RADIUS) -> None:
        blit_glass_panel(surface, rect, alpha=alpha, radius=radius)

    def _draw_title(self, surface: pygame.Surface, rect: pygame.Rect, entry_progress: float) -> None:
        title = self._text.get(TITLE, int(rect.height * 0.75), COLOR_TEXT, glow=True, bold=True)
        title_rect = title.get_rect(center=(rect.centerx, rect.centery))
        offset = int((1.0 - _ease_out_quint(entry_progress)) * 80)
        surface.blit(title, (title_rect.x, title_rect.y + offset))

        button_width = 140
        button_height = 44
        padding = int(rect.height * 0.1)
        button_rect = pygame.Rect(
            rect.right - button_width - padding,
            rect.centery - button_height // 2 + offset // 2,
            button_width,
            button_height,
        )
        self._settings_icon_rect = button_rect
        button_surface = pygame.Surface(button_rect.size, pygame.SRCALPHA)
        base_alpha = 60 if not self._settings_icon_hover else 110
        pygame.draw.rect(
            button_surface,
            pygame.Color(255, 255, 255, base_alpha),
            button_surface.get_rect(),
            border_radius=18,
        )
        border_color = COLOR_NEON_CYAN if self._settings_icon_hover else COLOR_TEXT_MUTED
        pygame.draw.rect(
            button_surface,
            border_color,
            button_surface.get_rect(),
            width=2,
            border_radius=18,
        )
        surface.blit(button_surface, button_rect.topleft)
        label = self._text.get("Settings", 24, COLOR_TEXT if not self._settings_icon_hover else COLOR_TEXT_ACTIVE, glow=False, bold=True)
        surface.blit(label, label.get_rect(center=button_rect.center))

    def _draw_song_panel(self, surface: pygame.Surface, rect: pygame.Rect) -> None:
        self._draw_panel(surface, rect)

        header = self._text.get("Songs", 36, COLOR_TEXT, glow=False, bold=True)
        surface.blit(header, (rect.x + 26, rect.y + 18))

        button_width = 130
        button_height = 36
        button_rect = pygame.Rect(rect.right - button_width - 26, rect.y + 20, button_width, button_height)
        self._import_button_rect = button_rect
        import_surface = pygame.Surface(button_rect.size, pygame.SRCALPHA)
        base_alpha = 70 if not self._import_button_hover else 120
        pygame.draw.rect(import_surface, (255, 255, 255, base_alpha), import_surface.get_rect(), border_radius=16)
        border_color = COLOR_NEON_CYAN if self._import_button_hover else COLOR_TEXT_MUTED
        pygame.draw.rect(import_surface, border_color, import_surface.get_rect(), width=2, border_radius=16)
        surface.blit(import_surface, button_rect.topleft)
        label = self._text.get(
            "Import",
            22,
            COLOR_TEXT if not self._import_button_hover else COLOR_TEXT_ACTIVE,
            glow=False,
            bold=True,
        )
        surface.blit(label, label.get_rect(center=button_rect.center))

        if not self.songs:
            warning = self._text.get("No songs found.", 28, COLOR_ACCENT_MAGENTA, glow=False, bold=False)
            surface.blit(warning, warning.get_rect(midtop=(rect.centerx, rect.y + rect.height * 0.45)))
            return

        # --- use the same vertical size as the Difficulty pills ---
        row_height = DIFFICULTY_PILL_HEIGHT          # 58
        usable_height = rect.height - 96
        visible_rows = max(1, usable_height // row_height)
        max_scroll = max(0.0, len(self.songs) - visible_rows)

        if self.song_idx >= 0:
            if self.song_idx < int(self._song_scroll_target):
                self._song_scroll_target = float(self.song_idx)
            elif self.song_idx >= int(self._song_scroll_target + visible_rows):
                self._song_scroll_target = float(self.song_idx - visible_rows + 1)
        self._song_scroll_target = _clamp(self._song_scroll_target, 0.0, max_scroll)

        dt = max(1.0 / self._target_fps, 1e-4)
        self._song_scroll = _approach(self._song_scroll, self._song_scroll_target, SCROLL_SMOOTHING, dt)
        self._song_cursor = _approach(self._song_cursor, float(self.song_idx), SELECTION_SMOOTHING, dt)

        start_index = int(self._song_scroll)
        offset_y = rect.y + 72

        # vertical region where rows are allowed to appear
        list_rect = pygame.Rect(rect.x, rect.y + 64, rect.width, rect.height - 96)
        clip_rect = pygame.Rect(rect.x, rect.y + 64, rect.width, rect.height - 96)
        prev_clip = surface.get_clip()
        surface.set_clip(clip_rect)

        pulse = 0.0
        if not self.reduced_motion:
            pulse = math.sin(self._time * PULSE_SPEED) * 0.5 + 0.5

        self._song_row_rects = []

        for i in range(visible_rows + 2):
            song_idx = start_index + i
            if song_idx >= len(self.songs):
                break
            song = self.songs[song_idx]

            y = offset_y + (i - (self._song_scroll - start_index)) * row_height

            # --- geometry matches Difficulty pills exactly ---
            # Difficulty pill_rect:
            #   x = rect.x + 24 + 6  (= rect.x + 30)
            #   w = rect.width - 48 - 12  (= rect.width - 60)
            #   h = DIFFICULTY_PILL_HEIGHT - 6  (= 52)
            row_rect = pygame.Rect(
                rect.x + 30,
                int(y),
                rect.width - 60,
                DIFFICULTY_PILL_HEIGHT - 6,
            )

            # skip rows completely outside the scrollable area
            if row_rect.bottom < list_rect.top or row_rect.top > list_rect.bottom:
                continue

            self._song_row_rects.append((song_idx, row_rect))

            selected = math.isclose(song_idx, round(self._song_cursor))
            hovered = self._hover_song == song_idx

            # Text colours: same logic as Difficulty
            if selected or hovered:
                base_color = COLOR_TEXT_ACTIVE
            else:
                base_color = COLOR_TEXT_MUTED

            # --- Glow rect chosen so largest glow layer fits exactly in the Songs panel ---
            scale_max = max(GLOW_LAYER_SCALES)  # 1.25
            glow_width = int(rect.width / scale_max)  # so glow_width * 1.25 ~= panel width
            glow_height = row_rect.height

            glow_rect = pygame.Rect(0, 0, glow_width, glow_height)
            glow_rect.center = row_rect.center  # same center as the song row

            if selected:
                pulse_intensity = math.sin(self._time * (PULSE_SPEED + 0.4)) * 0.5 + 0.5
                motion_factor = (1.0 - self.reduced_motion) * pulse_intensity
                intensity = 1.0 + (ROW_GLOW_INTENSITY * motion_factor)
                self._glow.blit(surface, glow_rect, COLOR_NEON_CYAN, PILL_RADIUS, intensity)
            elif hovered:
                self._glow.blit(surface, glow_rect, COLOR_ACCENT_MAGENTA, PILL_RADIUS, 0.25)

            # Inner white pill – same as Difficulty
            fill_alpha = 36
            if selected:
                fill_alpha = 110
            elif hovered:
                fill_alpha = 70
            pygame.draw.rect(surface, (255, 255, 255, fill_alpha), row_rect, border_radius=PILL_RADIUS)


            # Text + "No charts" label
            name_surface = self._text.get(song.display_name, 30, base_color, glow=False, bold=selected or hovered)
            name_left = row_rect.x + 20
            name_right_limit = row_rect.right - 20
            info_rect = None

            info_surface = None
            info_rect = None
            if not song.available_difficulties:
                info_surface = self._text.get("No charts", 22, COLOR_ACCENT_MAGENTA, glow=False, bold=False)
                info_rect = info_surface.get_rect(midright=(row_rect.right - 20, row_rect.centery))
                name_right_limit = min(name_right_limit, info_rect.left - 12)

            name_width_available = max(40, name_right_limit - name_left)
            needs_scroll = (selected or hovered) and name_surface.get_width() > name_width_available

            if needs_scroll:
                offset, pattern = self._marquee_params(name_surface.get_width(), name_width_available)
                self._blit_marquee_text(surface, name_surface, name_left, row_rect, offset, name_width_available, pattern)
            else:
                dest = name_surface.get_rect(midleft=(name_left, row_rect.centery))
                surface.blit(name_surface, dest)

            if info_surface and info_rect:
                surface.blit(info_surface, info_rect)

        surface.set_clip(prev_clip)
        self._update_song_hover()


    def _draw_difficulty_panel(self, surface: pygame.Surface, rect: pygame.Rect) -> None:
        self._draw_panel(surface, rect)

        header = self._text.get("Difficulty", 34, COLOR_TEXT, glow=False, bold=True)
        surface.blit(header, (rect.x + 24, rect.y + 18))

        pill_height = DIFFICULTY_PILL_HEIGHT
        padding = DIFFICULTY_PILL_GAP
        pill_area_height = max(0, rect.height - DIFFICULTY_PANEL_FRAME)
        pill_area = pygame.Rect(rect.x + 24, rect.y + 74, rect.width - 48, pill_area_height)

        dt = max(1.0 / self._target_fps, 1e-4)
        self._diff_cursor = _approach(self._diff_cursor, float(self.diff_idx), SELECTION_SMOOTHING, dt)

        for index, diff in enumerate(self.difficulties):
            pill_rect = pygame.Rect(
                pill_area.x + 6,
                int(pill_area.y + index * (pill_height + padding)),
                pill_area.width - 12,
                pill_height - 6,
            )
            self._diff_pill_rects.append(pill_rect)
            selected = math.isclose(index, round(self._diff_cursor))
            hovered = self._hover_diff == index

            if selected:
                pulse = math.sin(self._time * (PULSE_SPEED + 0.4)) * 0.5 + 0.5
                motion_factor = (1.0 - self.reduced_motion) * pulse
                intensity = 1.0 + (DIFF_GLOW_INTENSITY * motion_factor)
                self._glow.blit(surface, pill_rect, COLOR_NEON_CYAN, PILL_RADIUS, intensity)
            elif hovered:
                self._glow.blit(surface, pill_rect, COLOR_ACCENT_MAGENTA, PILL_RADIUS, 0.25)

            fill_alpha = 36
            if selected:
                fill_alpha = 110
            elif hovered:
                fill_alpha = 70
            pygame.draw.rect(surface, (255, 255, 255, fill_alpha), pill_rect, border_radius=PILL_RADIUS)

            if selected or hovered:
                label_color = COLOR_TEXT_ACTIVE
            else:
                label_color = COLOR_TEXT_MUTED
            label = self._text.get(diff, 28, label_color, glow=False, bold=True)
            surface.blit(label, label.get_rect(center=pill_rect.center))

    def _draw_bottom_panel(self, surface: pygame.Surface, rect: pygame.Rect, entry_progress: float) -> None:
        panel_alpha = int(_lerp(0, BOTTOM_PANEL_ALPHA, _clamp(entry_progress + 0.1, 0.0, 1.0)))
        self._draw_panel(surface, rect, alpha=panel_alpha, radius=PANEL_RADIUS)
        if self.song_idx < 0 or not self.songs:
            info = self._text.get("Select a song to get started.", 26, COLOR_TEXT_MUTED, glow=False, bold=False)
            surface.blit(info, info.get_rect(midleft=(rect.x + 28, rect.centery)))
            return

        song = self.songs[self.song_idx]
        difficulty = self.difficulties[self.diff_idx]
        best = best_for(song.base_name, difficulty)

        title_text = f"{_shorten_label(song.display_name, 20)} - {difficulty}"
        title = self._text.get(title_text, 30, COLOR_TEXT, glow=False, bold=True)
        surface.blit(title, (rect.x + 26, rect.y + 24))

        if best:
            line = (
                f"Best: {best.score:,} pts   "
                f"Accuracy {best.accuracy:.1f}%   "
                f"Max Combo {best.max_combo}   "
                f"P/G/Gd/M {best.judgements.get('Perfect', 0)}/"
                f"{best.judgements.get('Great', 0)}/"
                f"{best.judgements.get('Good', 0)}/"
                f"{best.judgements.get('Miss', 0)}"
            )
            info = self._text.get(line, 24, COLOR_TEXT_MUTED, glow=False, bold=False)
        else:
            # Highlight the empty slot with mint to encourage the player to chase a first clear.
            info = self._text.get("No scores logged for this chart yet.", 24, COLOR_ACCENT_MINT, glow=False, bold=False)
        info_y = rect.bottom - 42
        surface.blit(info, (rect.x + 26, info_y))

    def _draw_footer(self, surface: pygame.Surface, rect: pygame.Rect, entry_progress: float) -> None:
        fade = _clamp((self._entry_time - FOOTER_DELAY) / (ENTRY_ANIM_DURATION * 0.8), 0.0, 1.0)
        fade = _ease_out_cubic(fade)
        hint = "Enter Play   Up/Down Song   Left/Right Difficulty   Tab Scores   Esc Menu"
        footer = self._text.get(hint, 22, COLOR_TEXT_MUTED, glow=False, bold=False)
        footer.set_alpha(int(255 * fade))
        surface.blit(footer, footer.get_rect(midleft=(rect.x, rect.centery)))

    def _marquee_params(self, text_width: int, slot_width: int) -> Tuple[float, float]:
        overflow = max(0, text_width - slot_width)
        if overflow <= 0:
            return (0.0, 0.0)
        gap = max(40.0, min(text_width, slot_width * 0.4))
        pattern = text_width + gap
        if pattern <= 0:
            return (0.0, 0.0)
        offset = (self._time * TITLE_SCROLL_SPEED) % pattern
        return (offset, pattern)

    def _blit_marquee_text(
        self,
        surface: pygame.Surface,
        text_surface: pygame.Surface,
        left: int,
        row_rect: pygame.Rect,
        offset: float,
        width: int,
        pattern_width: float,
    ) -> None:
        clip_rect = pygame.Rect(left, row_rect.y, width, row_rect.height)
        prev_clip = surface.get_clip()
        surface.set_clip(clip_rect)
        start = left - offset
        while start > left - pattern_width:
            start -= pattern_width
        while start < left + width:
            dest = text_surface.get_rect(midleft=(int(start), row_rect.centery))
            surface.blit(text_surface, dest)
            start += pattern_width
        surface.set_clip(prev_clip)

    def _open_overlay(self) -> None:
        self._overlay_active = True
        self._overlay_hover = None
        self._overlay_option_index = 0
        self._update_overlay_layout()

    def _update_overlay_layout(self) -> None:
        width, height = self.screen.get_size()
        panel_width = min(int(width * 0.55), 600)
        panel_height = min(int(height * 0.4), 300)
        panel_rect = pygame.Rect(0, 0, panel_width, panel_height)
        panel_rect.center = (width // 2, height // 2)
        button_height = 64
        button_padding = 34
        button_width = (panel_width - button_padding * 3) // 2
        button_y = panel_rect.bottom - button_height - button_padding
        quit_rect = pygame.Rect(panel_rect.left + button_padding, button_y, button_width, button_height)
        settings_rect = pygame.Rect(quit_rect.right + button_padding, button_y, button_width, button_height)
        self._overlay_panel_rect = panel_rect
        self._overlay_button_rects = {"quit": quit_rect, "settings": settings_rect}

    def _handle_overlay_event(self, event: pygame.event.Event) -> Optional[str]:
        self._update_overlay_layout()
        options = ["quit", "settings"]
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_ESCAPE, pygame.K_q):
                self._overlay_active = False
                self._overlay_hover = None
                return None
            if event.key in (pygame.K_LEFT, pygame.K_a):
                self._overlay_option_index = max(0, self._overlay_option_index - 1)
            elif event.key in (pygame.K_RIGHT, pygame.K_d):
                self._overlay_option_index = min(len(options) - 1, self._overlay_option_index + 1)
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                return options[self._overlay_option_index]
        elif event.type == pygame.MOUSEMOTION:
            self._handle_overlay_mouse_motion(event.pos)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for name, rect in self._overlay_button_rects.items():
                if rect.collidepoint(event.pos):
                    return name
        return None

    def _handle_overlay_mouse_motion(self, pos: Tuple[int, int]) -> None:
        hover = None
        for name, rect in self._overlay_button_rects.items():
            if rect.collidepoint(pos):
                hover = name
                break
        self._overlay_hover = hover
        options = ["quit", "settings"]
        if hover in options:
            self._overlay_option_index = options.index(hover)

    def _draw_overlay(self, surface: pygame.Surface) -> None:
        size = surface.get_size()
        overlay_surface = pygame.Surface(size, pygame.SRCALPHA)
        overlay_surface.fill((2, 4, 10, 235))
        surface.blit(overlay_surface, (0, 0))

        self._update_overlay_layout()
        panel_rect = self._overlay_panel_rect
        panel_surface = pygame.Surface(panel_rect.size, pygame.SRCALPHA)
        pygame.draw.rect(
            panel_surface,
            pygame.Color(16, 22, 44, 240),
            panel_surface.get_rect(),
            border_radius=24,
        )
        pygame.draw.rect(
            panel_surface,
            pygame.Color(0, 229, 255, 90),
            panel_surface.get_rect(),
            width=2,
            border_radius=24,
        )
        surface.blit(panel_surface, panel_rect.topleft)

        title = self._text.get("Menu", 38, COLOR_TEXT, glow=False, bold=True)
        surface.blit(title, title.get_rect(midtop=(panel_rect.centerx, panel_rect.y + 24)))

        subtitle = self._text.get("Resume selection or open settings.", 22, COLOR_ACCENT_MINT, glow=False, bold=False)
        surface.blit(subtitle, subtitle.get_rect(midtop=(panel_rect.centerx, panel_rect.y + 74)))

        for idx, (name, label) in enumerate((("quit", "Quit Game"), ("settings", "Settings"))):
            rect = self._overlay_button_rects.get(name, pygame.Rect(0, 0, 0, 0))
            selected = idx == self._overlay_option_index
            hovered = self._overlay_hover == name
            self._draw_overlay_button(surface, rect, label, selected=selected, hovered=hovered)

    def _draw_overlay_button(
        self,
        surface: pygame.Surface,
        rect: pygame.Rect,
        label: str,
        *,
        selected: bool,
        hovered: bool,
    ) -> None:
        button_surface = pygame.Surface(rect.size, pygame.SRCALPHA)
        base_color = pygame.Color(24, 32, 68, 220)
        if selected:
            base_color = pygame.Color(0, 229, 255, 220)
        elif hovered:
            base_color = pygame.Color(40, 48, 96, 240)
        pygame.draw.rect(
            button_surface,
            base_color,
            button_surface.get_rect(),
            border_radius=16,
        )
        border_color = COLOR_NEON_CYAN if (selected or hovered) else COLOR_TEXT_MUTED
        pygame.draw.rect(
            button_surface,
            border_color,
            button_surface.get_rect(),
            width=3 if selected else 2,
            border_radius=16,
        )
        surface.blit(button_surface, rect.topleft)
        text_color = COLOR_TEXT_ACTIVE if selected else COLOR_TEXT
        if not selected and hovered:
            text_color = COLOR_ACCENT_MINT
        label_surface = self._text.get(label, 26, text_color, glow=False, bold=True)
        surface.blit(label_surface, label_surface.get_rect(center=rect.center))


# ---------------------------------------------------------------------------
# Scores scene
# ---------------------------------------------------------------------------
class ScoresScene:
    def __init__(self, screen: pygame.Surface, config: Dict, song: Optional[SongInfo] = None, difficulty: Optional[str] = None):
        self.screen = screen
        self.config = config
        self.song = song
        self.difficulty = difficulty

        self._text = TextCache()
        self._glow = GlowCache()
        self._backdrop = FuturisticBackdrop(screen.get_size())
        self._target_fps = self._resolve_target_fps(config)
        self.reduced_motion = bool(
            config.get("reduced_motion")
            or config.get("ui_reduced_motion")
            or config.get("ui", {}).get("reduced_motion", False)
        )

        self._rows = leaderboard(
            song=self.song.base_name if self.song else None,
            difficulty=self.difficulty,
            limit=MAX_SCORES_PER_CHART,
        )
        self._index = 0
        self._row_rects: List[Tuple[int, pygame.Rect]] = []
        self._time = 0.0
        self._entry_time = 0.0
        self._scroll = 0.0
        self._scroll_target = 0.0

    def loop(self) -> str:
        clock = pygame.time.Clock()
        while True:
            dt = clock.tick(self._target_fps) / 1000.0
            self._time += dt
            self._entry_time = min(self._entry_time + dt, ENTRY_ANIM_DURATION + 0.2)

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return "quit"
                if event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_TAB, pygame.K_q, pygame.K_RETURN, pygame.K_KP_ENTER):
                        return "back"
                    if event.key in (pygame.K_DOWN, pygame.K_s):
                        self._index = min(len(self._rows) - 1, self._index + 1)
                    if event.key in (pygame.K_UP, pygame.K_w):
                        self._index = max(0, self._index - 1)
                elif event.type == pygame.MOUSEMOTION:
                    self._handle_mouse_motion(event.pos)
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if self._handle_mouse_click(event.pos):
                        return "back"
                elif event.type == pygame.MOUSEWHEEL:
                    if self._rows:
                        delta = -event.y
                        if delta:
                            self._index = int(_clamp(self._index + delta, 0, len(self._rows) - 1))

            self._draw()
            pygame.display.flip()

    def _resolve_target_fps(self, config: Dict) -> int:
        fps = config.get("target_fps", TARGET_FPS_DEFAULT)
        if fps < 60:
            return 60
        return min(144, int(fps))

    def _handle_mouse_motion(self, pos: Tuple[int, int]) -> None:
        for global_idx, rect in self._row_rects:
            if rect.collidepoint(pos):
                self._index = global_idx
                break

    def _handle_mouse_click(self, pos: Tuple[int, int]) -> bool:
        for _, rect in self._row_rects:
            if rect.collidepoint(pos):
                return True
        return False

    def _draw(self) -> None:
        surface = self.screen
        surface_size = surface.get_size()
        self._backdrop.draw(surface, self._time, reduced_motion=self.reduced_motion)

        layout = self._layout(surface_size)
        self._draw_header(surface, layout["title_area"])
        self._draw_table(surface, layout["table_area"])
        self._draw_footer(surface, layout["footer"])

    def _layout(self, size: Tuple[int, int]) -> Dict[str, pygame.Rect]:
        width, height = size
        margin = int(width * 0.06)
        table_height = height - int(height * 0.34)
        title_area = pygame.Rect(0, int(height * 0.04), width, int(height * 0.1))
        table_area = pygame.Rect(margin, int(height * 0.26), width - margin * 2, table_height)
        footer = pygame.Rect(margin, height - margin, width - margin * 2, 40)
        return {"title_area": title_area, "table_area": table_area, "footer": footer}

    def _draw_header(self, surface: pygame.Surface, rect: pygame.Rect) -> None:
        title_surface = self._text.get("Scores", int(rect.height * 0.75), COLOR_TEXT, glow=True, bold=True)
        surface.blit(title_surface, title_surface.get_rect(center=(rect.centerx, rect.centery)))

        subtitle_parts = []
        if self.song:
            subtitle_parts.append(_shorten_label(self.song.display_name, 30))
        if self.difficulty:
            subtitle_parts.append(self.difficulty)
        if subtitle_parts:
            subtitle = " - ".join(subtitle_parts)
            subtitle_surface = self._text.get(subtitle, 28, COLOR_TEXT_MUTED, glow=False, bold=False)
            surface.blit(subtitle_surface, subtitle_surface.get_rect(center=(rect.centerx, rect.bottom + 36)))

    def _draw_table(self, surface: pygame.Surface, rect: pygame.Rect) -> None:
        self._draw_panel(surface, rect, alpha=PANEL_ALPHA + 20, radius=PANEL_RADIUS)

        content_rect = pygame.Rect(rect.x + 24, rect.y + 32, rect.width - 48, rect.height - 64)
        content_rect.width = max(200, content_rect.width)
        content_rect.height = max(120, content_rect.height)
        header_y = content_rect.y - 20
        header_font = 26
        value_font = 24
        row_height = 42

        column_specs = [
            {"label": "#", "width": 0.08, "align": "right", "min_size": 18},
            {"label": "When (UTC)", "width": 0.3, "align": "left", "min_size": 18},
            {"label": "Score", "width": 0.18, "align": "right", "bold": True, "min_size": 18},
            {"label": "Acc%", "width": 0.14, "align": "right", "min_size": 18},
            {"label": "Max Combo", "width": 0.13, "align": "right", "min_size": 18},
            {"label": "P/G/Gd/M", "width": 0.17, "align": "right", "min_size": 16},
        ]

        prev_clip = surface.get_clip()
        surface.set_clip(rect)
        header_color = pygame.Color(0, 0, 0)

        available_width = content_rect.width
        column_rects: List[pygame.Rect] = []
        cursor = content_rect.x
        for idx, spec in enumerate(column_specs):
            if idx == len(column_specs) - 1:
                width = content_rect.right - cursor
            else:
                width = max(40, int(round(available_width * spec["width"])))
            column_rects.append(pygame.Rect(cursor, header_y, width, row_height))
            cursor += width

        for spec, col_rect in zip(column_specs, column_rects):
            header_surface = self._render_text_within(
                spec["label"],
                header_font,
                header_color,
                max(12, col_rect.width - 12),
                bold=True,
                min_size=spec.get("min_size", 16),
            )
            dest = header_surface.get_rect()
            align = spec.get("align", "left")
            if align == "right":
                dest.midright = (col_rect.right - 6, col_rect.centery)
            elif align == "center":
                dest.center = (col_rect.center, col_rect.centery)
            else:
                dest.midleft = (col_rect.left + 6, col_rect.centery)
            surface.blit(header_surface, dest)

        list_area = pygame.Rect(content_rect.x, header_y + 40, content_rect.width, content_rect.bottom - (header_y + 40))
        start_y = list_area.y + 12
        list_height = max(1, content_rect.bottom - start_y)
        max_rows = max(1, list_height // row_height)

        pulse = 0.0
        if not self.reduced_motion:
            # Subtle hover pulse keeps the leaderboard lively; reduce multiplier for calmer UI.
            pulse = math.sin(self._time * (PULSE_SPEED + 0.25)) * 0.5 + 0.5

        song_label = _shorten_label(self.song.display_name, 30) if self.song else None

        if not self._rows:
            if song_label and self.difficulty:
                msg = f"No {self.difficulty} scores for {song_label} yet."
            elif song_label:
                msg = f"No scores for {song_label} yet."
            else:
                msg = "No records yet. Play a chart to leave a trace!"
            empty = self._render_text_within(msg, 26, header_color, content_rect.width - 20, bold=False)
            surface.blit(empty, empty.get_rect(center=content_rect.center))
            surface.set_clip(prev_clip)
            return

        surface.set_clip(list_area)

        self._index = int(_clamp(self._index, 0, max(0, len(self._rows) - 1)))

        max_scroll = max(0.0, len(self._rows) - max_rows)
        if self._index < int(self._scroll_target):
            self._scroll_target = float(self._index)
        elif self._index >= int(self._scroll_target + max_rows):
            self._scroll_target = float(self._index - max_rows + 1)
        self._scroll_target = _clamp(self._scroll_target, 0.0, max_scroll)

        dt = max(1.0 / self._target_fps, 1e-4)
        self._scroll = _approach(self._scroll, self._scroll_target, SCROLL_SMOOTHING, dt)
        start_index = int(self._scroll)
        fractional_offset = self._scroll - start_index
        visible_rows = self._rows[start_index:start_index + max_rows + 2]

        self._row_rects = []

        for idx, row in enumerate(visible_rows):
            global_index = start_index + idx
            y = start_y + (idx - fractional_offset) * row_height
            row_rect = pygame.Rect(content_rect.x, int(y), content_rect.width, row_height - 6)
            if row_rect.bottom < list_area.top or row_rect.top > list_area.bottom:
                continue
            self._row_rects.append((global_index, row_rect))

            selected = global_index == self._index
            hovered = row_rect.collidepoint(pygame.mouse.get_pos())

            pygame.draw.rect(surface, (255, 255, 255, 60), row_rect, border_radius=12)

            timestamp = row.timestamp.replace("T", " ").replace("Z", "")[:19]
            entries = [
                f"{global_index + 1}",
                timestamp,
                f"{row.score:,}",
                f"{row.accuracy:.1f}%",
                f"{row.max_combo:,}",
                "{}/{}/{}/{}".format(
                    row.judgements.get("Perfect", 0),
                    row.judgements.get("Great", 0),
                    row.judgements.get("Good", 0),
                    row.judgements.get("Miss", 0),
                ),
            ]
            for col_text, spec, col_rect in zip(entries, column_specs, column_rects):
                color = pygame.Color(0, 0, 0)
                bold = spec.get("bold", False)
                surf = self._render_text_within(
                    col_text,
                    value_font,
                    color,
                    max(12, col_rect.width - 12),
                    bold=bold,
                    min_size=spec.get("min_size", 16),
                )
                dest = surf.get_rect()
                align = spec.get("align", "left")
                if align == "right":
                    dest.midright = (col_rect.right - 6, row_rect.centery)
                elif align == "center":
                    dest.center = (col_rect.center, row_rect.centery)
                else:
                    dest.midleft = (col_rect.left + 6, row_rect.centery)
                surface.blit(surf, dest)

        surface.set_clip(prev_clip)

    def _draw_panel(self, surface: pygame.Surface, rect: pygame.Rect, alpha: int = PANEL_ALPHA, radius: int = PANEL_RADIUS) -> None:
        blit_glass_panel(surface, rect, alpha=alpha, radius=radius)

    def _draw_footer(self, surface: pygame.Surface, rect: pygame.Rect) -> None:
        hint = "Esc/Tab/Enter Back"
        footer = self._text.get(hint, 22, COLOR_TEXT_MUTED, glow=False, bold=False)
        fade = _clamp(self._entry_time / ENTRY_ANIM_DURATION, 0.0, 1.0)
        footer.set_alpha(int(255 * fade))
        surface.blit(footer, footer.get_rect(midleft=(rect.x, rect.centery)))

    def _render_text_within(
        self,
        text: str,
        size: int,
        color: pygame.Color,
        max_width: int,
        *,
        bold: bool = False,
        min_size: int = 14,
    ) -> pygame.Surface:
        max_width = max(8, max_width)
        current_size = int(size)
        surface = self._text.get(text, current_size, color, glow=False, bold=bold)
        while surface.get_width() > max_width and current_size > min_size:
            current_size -= 1
            surface = self._text.get(text, current_size, color, glow=False, bold=bold)
        if surface.get_width() <= max_width:
            return surface
        trimmed = text
        while trimmed:
            trimmed = trimmed[:-1]
            surface = self._text.get(trimmed, current_size, color, glow=False, bold=bold)
            if surface.get_width() <= max_width:
                return surface
        return self._text.get("", current_size, color, glow=False, bold=bold)


class SettingsScene:
    def __init__(self, screen: pygame.Surface, config: Dict):
        self.screen = screen
        self.config = config
        self._text = TextCache()
        self._glow = GlowCache()
        self._backdrop = FuturisticBackdrop(screen.get_size())
        self._target_fps = self._resolve_target_fps(config)
        self.reduced_motion = bool(
            config.get('reduced_motion')
            or config.get('ui_reduced_motion')
            or config.get('ui', {}).get('reduced_motion', False)
        )
        self._time = 0.0
        self._init_state(config)

    def _init_state(self, config: Dict) -> None:
        default_bindings = DEFAULT_CONFIG.get('key_bindings', {})
        bindings = config.get('key_bindings', {})
        lane_candidates = []
        for key in set(default_bindings.keys()) | set(bindings.keys()):
            try:
                lane_candidates.append(int(key))
            except Exception:
                continue
        if not lane_candidates:
            lane_candidates = list(range(4))
        self._lane_order = sorted(set(lane_candidates))
        self._settings_key_bindings: Dict[int, str] = {}
        for lane in self._lane_order:
            key_name = bindings.get(str(lane), default_bindings.get(str(lane), ''))
            self._settings_key_bindings[lane] = key_name or ''
        try:
            initial_volume = float(config.get('volume', DEFAULT_CONFIG.get('volume', 0.8)))
        except Exception:
            initial_volume = DEFAULT_CONFIG.get('volume', 0.8)
        self._volume_value = _clamp(initial_volume, 0.0, 1.0)
        self._volume_slider_rect = pygame.Rect(0, 0, 0, 0)
        self._volume_handle_rect = pygame.Rect(0, 0, 0, 0)
        self._volume_dragging = False
        self._key_capture_lane: Optional[int] = None
        self._settings_invalid_lanes: Set[int] = set()
        self._key_binding_rects: Dict[int, pygame.Rect] = {}
        self._settings_hover_key_lane: Optional[int] = None
        self._settings_hover_button: Optional[str] = None
        self._settings_save_rect = pygame.Rect(0, 0, 0, 0)
        self._settings_default_rect = pygame.Rect(0, 0, 0, 0)
        self._settings_dirty = False
        self._settings_status_text = ''
        self._settings_status_time = 0.0
        self._recompute_binding_validity()
        self._update_settings_dirty_flag()

    def loop(self) -> str:
        clock = pygame.time.Clock()
        while True:
            dt = clock.tick(self._target_fps) / 1000.0
            self._time += dt
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return 'quit'
                if event.type == pygame.KEYDOWN:
                    if self._handle_key_capture(event):
                        continue
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        return 'back'
                    if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER) and self._can_save_settings():
                        self._save_settings()
                elif event.type == pygame.MOUSEMOTION:
                    self._handle_mouse_motion(event.pos)
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    self._handle_mouse_click(event.pos)
                elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                    self._volume_dragging = False

            self._draw()
            pygame.display.flip()

    def _resolve_target_fps(self, config: Dict) -> int:
        fps = config.get('target_fps', TARGET_FPS_DEFAULT)
        if fps < 60:
            return 60
        return min(144, int(fps))

    def _handle_mouse_motion(self, pos: Tuple[int, int]) -> None:
        if self._volume_dragging:
            self._set_volume_from_position(pos[0])
        self._settings_hover_key_lane = None
        for lane, rect in self._key_binding_rects.items():
            if rect.collidepoint(pos):
                self._settings_hover_key_lane = lane
                break
        button = None
        if self._settings_save_rect.collidepoint(pos):
            button = 'save'
        elif self._settings_default_rect.collidepoint(pos):
            button = 'default'
        self._settings_hover_button = button

    def _handle_mouse_click(self, pos: Tuple[int, int]) -> None:
        if self._volume_slider_rect.collidepoint(pos) or self._volume_handle_rect.collidepoint(pos):
            self._volume_dragging = True
            self._set_volume_from_position(pos[0])
            return
        for lane, rect in self._key_binding_rects.items():
            if rect.collidepoint(pos):
                self._key_capture_lane = lane
                self._settings_status_text = 'Press a key...'
                self._settings_status_time = self._time
                return
        if self._settings_default_rect.collidepoint(pos):
            self._apply_settings_defaults()
            return
        if self._settings_save_rect.collidepoint(pos) and self._can_save_settings():
            self._save_settings()

    def _handle_key_capture(self, event: pygame.event.Event) -> bool:
        if self._key_capture_lane is None:
            return False
        lane = self._key_capture_lane
        if event.key in (pygame.K_ESCAPE, pygame.K_q):
            self._key_capture_lane = None
            return True
        if event.key == pygame.K_UNKNOWN:
            key_name = ''
        else:
            try:
                key_name = pygame.key.name(event.key)
            except Exception:
                key_name = ''
        self._settings_key_bindings[lane] = key_name
        self._key_capture_lane = None
        self._recompute_binding_validity()
        self._update_settings_dirty_flag()
        self._settings_status_text = ''
        return True

    def _draw(self) -> None:
        surface = self.screen
        size = surface.get_size()
        self._backdrop.draw(surface, self._time, reduced_motion=self.reduced_motion)

        layout = self._layout(size)
        self._draw_header(surface, layout['header'])
        self._draw_content(surface, layout['content'])
        self._draw_footer(surface, layout['footer'])

    def _layout(self, size: Tuple[int, int]) -> Dict[str, pygame.Rect]:
        width, height = size
        margin = int(width * 0.07)
        header_rect = pygame.Rect(0, int(height * 0.07), width, int(height * 0.12))
        content_rect = pygame.Rect(margin, header_rect.bottom + 10, width - margin * 2, height - header_rect.bottom - margin * 2)
        footer_rect = pygame.Rect(margin, height - margin, width - margin * 2, 40)
        return {'header': header_rect, 'content': content_rect, 'footer': footer_rect}

    def _draw_header(self, surface: pygame.Surface, rect: pygame.Rect) -> None:
        title = self._text.get('Settings', int(rect.height * 0.75), COLOR_TEXT, glow=True, bold=True)
        title_rect = title.get_rect(midleft=(int(rect.width * 0.12), rect.centery - int(rect.height * 0.2)))
        surface.blit(title, title_rect)
        subtitle = self._text.get('Adjust audio and lane controls.', 28, COLOR_TEXT_MUTED, glow=False, bold=False)
        content_rect = self._layout(surface.get_size())['content']
        subtitle_rect = subtitle.get_rect(midleft=(int(rect.width * 0.12), content_rect.y - 20))
        surface.blit(subtitle, subtitle_rect)

    def _draw_content(self, surface: pygame.Surface, rect: pygame.Rect) -> None:
        self._draw_panel(surface, rect, alpha=PANEL_ALPHA + 10, radius=PANEL_RADIUS)
        padding = 32
        inner = pygame.Rect(rect.x + padding, rect.y + padding, rect.width - padding * 2, rect.height - padding * 2)

        volume_label = self._text.get('Master Volume', 24, COLOR_TEXT_MUTED, glow=False, bold=False)
        surface.blit(volume_label, (inner.x, inner.y))

        slider_top = inner.y + volume_label.get_height() + 10
        slider_height = 14
        slider_rect = pygame.Rect(inner.x, slider_top, inner.width, slider_height)
        slider_surface = pygame.Surface(slider_rect.size, pygame.SRCALPHA)
        pygame.draw.rect(slider_surface, (255, 255, 255, 32), slider_surface.get_rect(), border_radius=slider_height)
        fill_width = int(slider_rect.width * _clamp(self._volume_value, 0.0, 1.0))
        if fill_width > 0:
            fill_rect = pygame.Rect(0, 0, fill_width, slider_height)
            pygame.draw.rect(slider_surface, COLOR_ACCENT_MINT, fill_rect, border_radius=slider_height)
        surface.blit(slider_surface, slider_rect.topleft)
        handle_center = slider_rect.x + fill_width
        handle_center = _clamp(handle_center, slider_rect.x, slider_rect.right)
        handle_rect = pygame.Rect(0, 0, 18, 24)
        handle_rect.center = (int(handle_center), slider_rect.centery)
        pygame.draw.rect(surface, COLOR_NEON_CYAN if self._volume_dragging else COLOR_TEXT, handle_rect, border_radius=12)
        value_label = self._text.get(f"{int(self._volume_value * 100):3d}%", 24, COLOR_TEXT, glow=False, bold=True)
        surface.blit(value_label, (inner.right - value_label.get_width(), inner.y))
        self._volume_slider_rect = slider_rect
        self._volume_handle_rect = handle_rect

        rows_top = slider_rect.bottom + 40
        row_height = 52
        self._key_binding_rects = {}
        for idx, lane in enumerate(self._lane_order):
            row_y = rows_top + idx * row_height
            label = self._text.get(f'Lane {lane + 1}', 24, COLOR_TEXT_MUTED, glow=False, bold=False)
            surface.blit(label, (inner.x, row_y))
            pill_rect = pygame.Rect(inner.x + 160, row_y - 6, inner.width - 160, 42)
            self._key_binding_rects[lane] = pill_rect
            pill_surface = pygame.Surface(pill_rect.size, pygame.SRCALPHA)
            invalid = lane in self._settings_invalid_lanes
            capturing = self._key_capture_lane == lane
            hovered = self._settings_hover_key_lane == lane
            fill_color = pygame.Color(255, 255, 255, 28)
            border_color = COLOR_TEXT_MUTED
            text_color = COLOR_TEXT
            if invalid:
                fill_color = pygame.Color(COLOR_ERROR.r, COLOR_ERROR.g, COLOR_ERROR.b, 80)
                border_color = COLOR_ERROR
                text_color = COLOR_ERROR
            elif capturing:
                fill_color = pygame.Color(COLOR_ACCENT_MINT.r, COLOR_ACCENT_MINT.g, COLOR_ACCENT_MINT.b, 100)
                border_color = COLOR_ACCENT_MINT
            elif hovered:
                border_color = COLOR_ACCENT_MAGENTA
            pygame.draw.rect(pill_surface, fill_color, pill_surface.get_rect(), border_radius=14)
            pygame.draw.rect(pill_surface, border_color, pill_surface.get_rect(), width=2, border_radius=14)
            surface.blit(pill_surface, pill_rect.topleft)

            if capturing:
                value_text = 'Press a key...'
            else:
                value = self._settings_key_bindings.get(lane, '')
                value_text = value.upper() if value else 'Unassigned'
            value_surface = self._text.get(value_text, 26, text_color, glow=False, bold=True)
            surface.blit(value_surface, value_surface.get_rect(center=pill_rect.center))

        button_gap = 16
        button_height = 50
        button_width = max(140, (inner.width - button_gap) // 2)
        default_rect = pygame.Rect(inner.x, inner.bottom - button_height, button_width, button_height)
        save_rect = pygame.Rect(default_rect.right + button_gap, inner.bottom - button_height, button_width, button_height)
        if save_rect.right > inner.right:
            overflow = save_rect.right - inner.right
            save_rect.width = max(120, save_rect.width - overflow)
        self._settings_default_rect = default_rect
        self._settings_save_rect = save_rect

        self._draw_button(
            surface,
            default_rect,
            'Default',
            enabled=True,
            hovered=self._settings_hover_button == 'default',
        )
        self._draw_button(
            surface,
            save_rect,
            'Save',
            enabled=self._can_save_settings(),
            hovered=self._settings_hover_button == 'save' and self._can_save_settings(),
        )

        status_text, status_color = self._settings_status_message()
        status_surface = self._text.get(status_text, 22, status_color, glow=False, bold=False)
        surface.blit(status_surface, (inner.x, default_rect.y - 34))

    def _draw_footer(self, surface: pygame.Surface, rect: pygame.Rect) -> None:
        hint = 'Esc Back   Enter Save'
        footer = self._text.get(hint, 22, COLOR_TEXT_MUTED, glow=False, bold=False)
        surface.blit(footer, footer.get_rect(midleft=(rect.x, rect.centery)))

    def _draw_panel(self, surface: pygame.Surface, rect: pygame.Rect, alpha: int = PANEL_ALPHA, radius: int = PANEL_RADIUS) -> None:
        blit_glass_panel(surface, rect, alpha=alpha, radius=radius)

    def _draw_button(
        self,
        surface: pygame.Surface,
        rect: pygame.Rect,
        label: str,
        *,
        enabled: bool = True,
        hovered: bool = False,
    ) -> None:
        button_surface = pygame.Surface(rect.size, pygame.SRCALPHA)
        base_alpha = 90 if enabled else 30
        pygame.draw.rect(
            button_surface,
            pygame.Color(255, 255, 255, base_alpha),
            button_surface.get_rect(),
            border_radius=14,
        )
        border_color = COLOR_NEON_CYAN if hovered and enabled else COLOR_TEXT_MUTED
        pygame.draw.rect(
            button_surface,
            border_color,
            button_surface.get_rect(),
            width=2,
            border_radius=14,
        )
        surface.blit(button_surface, rect.topleft)
        text_color = COLOR_TEXT if enabled else COLOR_TEXT_MUTED
        if hovered and enabled:
            text_color = COLOR_TEXT_ACTIVE
        label_surface = self._text.get(label, 24, text_color, glow=False, bold=True)
        surface.blit(label_surface, label_surface.get_rect(center=rect.center))

    def _set_volume_from_position(self, mouse_x: int) -> None:
        if self._volume_slider_rect.width <= 0:
            return
        pct = (mouse_x - self._volume_slider_rect.x) / float(self._volume_slider_rect.width)
        new_value = _clamp(pct, 0.0, 1.0)
        if abs(new_value - self._volume_value) > 1e-3:
            self._volume_value = new_value
            self._update_settings_dirty_flag()

    def _apply_settings_defaults(self) -> None:
        defaults = DEFAULT_CONFIG.get('key_bindings', {})
        for lane in self._lane_order:
            self._settings_key_bindings[lane] = defaults.get(str(lane), '')
        default_volume = DEFAULT_CONFIG.get('volume', 0.8)
        try:
            default_volume = float(default_volume)
        except Exception:
            default_volume = 0.8
        self._volume_value = _clamp(default_volume, 0.0, 1.0)
        self._key_capture_lane = None
        self._recompute_binding_validity()
        self._update_settings_dirty_flag()
        self._settings_status_text = 'Defaults restored'
        self._settings_status_time = self._time

    def _settings_status_message(self) -> Tuple[str, pygame.Color]:
        if self._settings_invalid_lanes:
            return ('Resolve red bindings before saving.', COLOR_ERROR)
        if self._key_capture_lane is not None:
            return (f"Press a key for lane {self._key_capture_lane + 1}.", COLOR_ACCENT_MINT)
        window = 3.0
        if self._settings_status_text and (self._time - self._settings_status_time) < window:
            color = COLOR_ACCENT_MINT
            if 'error' in self._settings_status_text.lower():
                color = COLOR_ERROR
            return (self._settings_status_text, color)
        if self._settings_dirty:
            return ('Unsaved changes', COLOR_TEXT_MUTED)
        return ('Settings up to date', COLOR_TEXT_MUTED)

    def _serialize_key_bindings(self) -> Dict[str, str]:
        return {str(lane): self._settings_key_bindings.get(lane, '') for lane in self._lane_order}

    def _save_settings(self) -> None:
        payload: Dict[str, object] = {}
        try:
            with CONFIG_PATH.open('r', encoding='utf-8') as fh:
                payload = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            payload = {}
        payload['volume'] = round(float(self._volume_value), 3)
        payload['key_bindings'] = self._serialize_key_bindings()
        with CONFIG_PATH.open('w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=4)
            fh.write("\n")
        self._apply_saved_settings(payload)
        self._settings_status_text = 'Saved'
        self._settings_status_time = self._time

    def _apply_saved_settings(self, payload: Dict[str, object]) -> None:
        try:
            saved_volume = float(payload.get('volume', self._volume_value))
        except Exception:
            saved_volume = self._volume_value
        saved_volume = _clamp(saved_volume, 0.0, 1.0)
        self.config['volume'] = saved_volume
        bindings_obj = payload.get('key_bindings', self._serialize_key_bindings())
        if not isinstance(bindings_obj, dict):
            bindings_obj = self._serialize_key_bindings()
        binding_dict = {str(k): str(v) for k, v in bindings_obj.items()}
        self.config['key_bindings'] = binding_dict
        key_map: Dict[int, int] = {}
        for lane_str, key_name in binding_dict.items():
            try:
                lane = int(lane_str)
                code = pygame.key.key_code(key_name)
            except Exception:
                continue
            if code == pygame.K_UNKNOWN:
                continue
            key_map[code] = lane
        self.config['key_map'] = key_map
        self._update_settings_dirty_flag()

    def _update_settings_dirty_flag(self) -> None:
        config_bindings = self.config.get('key_bindings', {})
        if not isinstance(config_bindings, dict):
            config_bindings = {}
        serialized = self._serialize_key_bindings()
        try:
            config_volume = float(self.config.get('volume', DEFAULT_CONFIG.get('volume', 0.8)))
        except Exception:
            config_volume = DEFAULT_CONFIG.get('volume', 0.8)
        config_volume = _clamp(config_volume, 0.0, 1.0)
        sorted_current = dict(sorted({str(k): str(v) for k, v in config_bindings.items()}.items()))
        sorted_serialized = dict(sorted(serialized.items()))
        same_bindings = sorted_current == sorted_serialized
        same_volume = abs(config_volume - self._volume_value) < 1e-3
        self._settings_dirty = not (same_bindings and same_volume)

    def _recompute_binding_validity(self) -> None:
        invalid: Set[int] = set()
        seen_codes: Dict[int, int] = {}
        for lane in self._lane_order:
            key_name = (self._settings_key_bindings.get(lane, '') or '').strip()
            if not key_name:
                invalid.add(lane)
                continue
            try:
                key_code = pygame.key.key_code(key_name)
            except Exception:
                key_code = None
            if key_code is None or key_code == pygame.K_UNKNOWN:
                invalid.add(lane)
                continue
            if key_code in seen_codes:
                invalid.add(lane)
                invalid.add(seen_codes[key_code])
            else:
                seen_codes[key_code] = lane
        self._settings_invalid_lanes = invalid

    def _can_save_settings(self) -> bool:
        return bool(not self._settings_invalid_lanes and self._settings_dirty and self._key_capture_lane is None)


class ResultsScene:
    """Animated results screen that matches the menu aesthetic."""

    def __init__(self, screen: pygame.Surface, config: Dict) -> None:
        self.screen = screen
        self.config = config
        self._target_fps = self._resolve_target_fps(config)
        self._clock = pygame.time.Clock()

    def loop(self, game) -> str:
        action = "exit"
        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                    action = "exit"
                    break
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_r:
                        action = "replay"
                        running = False
                        break
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        running = False
                        action = "exit"
                        break
            renderer.draw_results(self.screen, game)
            self._clock.tick(self._target_fps)
        return action

    def _resolve_target_fps(self, config: Dict) -> int:
        fps = config.get("target_fps", TARGET_FPS_DEFAULT)
        try:
            fps = int(fps)
        except Exception:
            fps = TARGET_FPS_DEFAULT
        return max(30, min(240, fps))

