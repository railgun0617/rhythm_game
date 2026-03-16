from __future__ import annotations

import math
import random
from typing import Dict, List, Tuple

import pygame

from runtime_paths import ASSETS_DIR

DEFAULT_FONT_PATH = ASSETS_DIR / "fonts" / "NotoSansCJKjp-Regular.otf"

COLOR_BACKGROUND = pygame.Color("#0B0F1A")
COLOR_NEON_CYAN = pygame.Color("#00E5FF")
COLOR_ACCENT_MAGENTA = pygame.Color("#9C27FF")
COLOR_ACCENT_MINT = pygame.Color("#00FF9D")
COLOR_ERROR = pygame.Color("#FF5F6D")
COLOR_TEXT = pygame.Color("#F5FAFF")
COLOR_TEXT_MUTED = pygame.Color("#AABDFF")
COLOR_TEXT_ACTIVE = pygame.Color("#0B1836")
COLOR_GRID = pygame.Color("#1C2440")

PANEL_ALPHA = 72
PANEL_RADIUS = 18
PILL_RADIUS = 16

GLOW_LAYER_SCALES = (1.25, 1.12, 1.05)
GLOW_LAYER_ALPHAS = (60, 85, 110)

STAR_LAYERS = (
    {"count": 130, "speed": 12.0, "size": 1, "alpha": 40},
    {"count": 110, "speed": 28.0, "size": 2, "alpha": 70},
    {"count": 80, "speed": 44.0, "size": 3, "alpha": 120},
)
STAR_COLORS = (
    pygame.Color(0, 229, 255, 255),
    pygame.Color(156, 39, 255, 255),
    pygame.Color(0, 255, 157, 255),
)
GRID_RES = (360, 200)
GRID_SCROLL_SPEED = 40.0


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def color_key(color: pygame.Color) -> Tuple[int, int, int, int]:
    return (int(color.r), int(color.g), int(color.b), int(color.a))


def blit_glass_panel(surface: pygame.Surface, rect: pygame.Rect, alpha: int = PANEL_ALPHA, radius: int = PANEL_RADIUS) -> None:
    """Draw a reusable frosted-glass rectangle onto the target surface."""
    panel = pygame.Surface(rect.size, pygame.SRCALPHA)
    glass_color = pygame.Color(255, 255, 255, alpha)
    pygame.draw.rect(panel, glass_color, panel.get_rect(), border_radius=radius)

    gradient = pygame.Surface(rect.size, pygame.SRCALPHA)
    for y in range(rect.height):
        strength = clamp(1.0 - y / max(1, rect.height), 0.0, 1.0)
        gradient.fill((60, 70, 120, int(16 * strength)), pygame.Rect(0, y, rect.width, 1))
    panel.blit(gradient, (0, 0), special_flags=pygame.BLEND_ADD)
    surface.blit(panel, rect.topleft)


class GlowCache:
    """Reusable glow rectangles to avoid per-frame smoothing."""

    def __init__(self) -> None:
        self._cache: Dict[Tuple[int, int, Tuple[int, int, int, int], int], List[Tuple[pygame.Surface, int]]] = {}

    def blit(self, surface: pygame.Surface, rect: pygame.Rect, color: pygame.Color, radius: int, intensity: float) -> None:
        layers = self._layers(rect.size, color, radius)
        intensity = clamp(intensity, 0.0, 2.0)
        for surf, alpha in layers:
            if alpha <= 0:
                continue
            overlay = surf.copy()
            overlay.set_alpha(int(alpha * intensity))
            surface.blit(
                overlay,
                (rect.centerx - overlay.get_width() // 2, rect.centery - overlay.get_height() // 2),
                special_flags=pygame.BLEND_ADD,
            )

    def _layers(self, size: Tuple[int, int], color: pygame.Color, radius: int) -> List[Tuple[pygame.Surface, int]]:
        key = (size[0], size[1], color_key(color), radius)
        if key in self._cache:
            return self._cache[key]

        width = max(1, size[0])
        height = max(1, size[1])
        base = pygame.Surface((width, height), pygame.SRCALPHA)
        pygame.draw.rect(base, (color.r, color.g, color.b, 255), base.get_rect(), border_radius=radius)

        layers: List[Tuple[pygame.Surface, int]] = []
        for scale, alpha in zip(GLOW_LAYER_SCALES, GLOW_LAYER_ALPHAS):
            scaled = pygame.transform.smoothscale(
                base,
                (max(1, int(width * scale)), max(1, int(height * scale))),
            )
            layers.append((scaled, alpha))
        self._cache[key] = layers
        return layers


class FuturisticBackdrop:
    """Animated neon gradient + starfield background."""

    def __init__(self, size: Tuple[int, int]) -> None:
        self.size = size
        self.gradient_surface = self._build_gradient(size)
        self.grid_surface = self._build_grid_surface(GRID_RES)
        self.stars = self._spawn_stars(size)
        self._last_elapsed = 0.0
        self._grid_scroll = 0.0

    def ensure_size(self, size: Tuple[int, int]) -> None:
        if size == self.size:
            return
        self.size = size
        self.gradient_surface = self._build_gradient(size)
        self.stars = self._spawn_stars(size)

    def draw(self, surface: pygame.Surface, elapsed: float, *, reduced_motion: bool, show_grid: bool = True) -> None:
        width, height = surface.get_size()
        self.ensure_size((width, height))
        dt = clamp(elapsed - self._last_elapsed, 0.0, 0.1)
        self._last_elapsed = elapsed
        self._draw_gradient(surface, elapsed, reduced_motion=reduced_motion)
        self._draw_starfield(surface, elapsed, dt=dt, reduced_motion=reduced_motion)
        if show_grid:
            self._draw_grid(surface, dt=dt, reduced_motion=reduced_motion)

    def _build_gradient(self, size: Tuple[int, int]) -> pygame.Surface:
        width, height = size
        gradient_height = max(height * 2, 1)
        surface = pygame.Surface((width, gradient_height))

        top = pygame.Color(11, 17, 34)
        mid = pygame.Color(23, 30, 60)
        bottom = pygame.Color(35, 15, 48)

        for y in range(gradient_height):
            t = y / float(gradient_height - 1 or 1)
            if t < 0.5:
                c = pygame.Color(
                    int(lerp(top.r, mid.r, t * 2)),
                    int(lerp(top.g, mid.g, t * 2)),
                    int(lerp(top.b, mid.b, t * 2)),
                )
            else:
                c = pygame.Color(
                    int(lerp(mid.r, bottom.r, (t - 0.5) * 2)),
                    int(lerp(mid.g, bottom.g, (t - 0.5) * 2)),
                    int(lerp(mid.b, bottom.b, (t - 0.5) * 2)),
                )
            surface.fill(c, rect=pygame.Rect(0, y, width, 1))
        return surface.convert()

    def _draw_gradient(self, surface: pygame.Surface, elapsed: float, *, reduced_motion: bool) -> None:
        width, height = surface.get_size()
        gradient_height = self.gradient_surface.get_height()
        scroll_speed = 18.0 if not reduced_motion else 6.0
        offset = int((elapsed * scroll_speed) % gradient_height)
        rect = pygame.Rect(0, offset, width, height)
        surface.blit(self.gradient_surface, (0, 0), rect)
        if offset + height > gradient_height:
            remainder = offset + height - gradient_height
            surface.blit(self.gradient_surface, (0, height - remainder), pygame.Rect(0, 0, width, remainder))

    def _spawn_stars(self, size: Tuple[int, int]) -> List[Dict[str, float]]:
        width, height = size
        stars: List[Dict[str, float]] = []
        for layer_index, spec in enumerate(STAR_LAYERS):
            for _ in range(spec["count"]):
                stars.append(
                    {
                        "x": random.uniform(0, width),
                        "y": random.uniform(0, height),
                        "layer": layer_index,
                        "speed": spec["speed"],
                        "size": spec["size"],
                    }
                )
        return stars

    def _draw_starfield(self, surface: pygame.Surface, elapsed: float, *, dt: float, reduced_motion: bool) -> None:
        width, height = surface.get_size()
        for star in self.stars:
            layer_index = star["layer"]
            spec = STAR_LAYERS[layer_index]
            color = STAR_COLORS[layer_index % len(STAR_COLORS)]
            alpha = int(spec["alpha"] * (0.45 + 0.55 * math.sin(elapsed * 0.45 + layer_index)))
            star_size = spec["size"]

            if not reduced_motion:
                star["y"] += spec["speed"] * dt
                if star["y"] > height:
                    star["y"] = -random.uniform(0, 20)
                    star["x"] = random.uniform(0, width)
            else:
                alpha = int(alpha * 0.55)

            rect = pygame.Rect(int(star["x"]), int(star["y"]), star_size, star_size)
            if alpha <= 0:
                continue
            star_color = pygame.Color(color.r, color.g, color.b, alpha)
            surface.fill(star_color, rect=rect)

    def _build_grid_surface(self, res: Tuple[int, int]) -> pygame.Surface:
        return pygame.Surface(res, pygame.SRCALPHA)

    def _draw_grid(self, surface: pygame.Surface, *, dt: float, reduced_motion: bool) -> None:
        width, height = surface.get_size()
        grid_height = int(height * 0.45)
        grid_top = height - grid_height
        grid = pygame.Surface((width, grid_height), pygame.SRCALPHA)
        grid.fill((0, 0, 0, 0))

        horizon_y = int(grid_height * 0.08)
        lines = 24
        speed = GRID_SCROLL_SPEED * (0.3 if reduced_motion else 1.0)
        self._grid_scroll = (self._grid_scroll + speed * dt) % grid_height

        for i in range(lines):
            t = i / float(lines)
            base_y = horizon_y + (t * grid_height) ** 1.1
            alpha = int(lerp(30, 160, t))
            color = pygame.Color(COLOR_GRID.r, COLOR_GRID.g, COLOR_GRID.b, alpha)
            offset_y = (base_y + self._grid_scroll) % grid_height
            for candidate in (offset_y, offset_y - grid_height):
                if -2.0 <= candidate <= grid_height + 2.0:
                    pygame.draw.aaline(grid, color, (0.0, candidate), (float(width), candidate), blend=1)

        columns = 14
        center_x = width / 2.0
        for i in range(-columns, columns + 1):
            t = abs(i) / float(columns or 1)
            alpha = int(lerp(50, 10, t))
            color = pygame.Color(COLOR_GRID.r, COLOR_GRID.g, COLOR_GRID.b, alpha)
            x = center_x + i * (width / columns) * 0.18
            pygame.draw.line(grid, color, (int(x), grid_height), (int(center_x + i * 4), 0), 1)

        haze = pygame.Surface((width, grid_height), pygame.SRCALPHA)
        for y in range(grid_height):
            strength = clamp(1.0 - y / grid_height, 0.0, 1.0)
            haze_color = pygame.Color(COLOR_NEON_CYAN.r, COLOR_NEON_CYAN.g, COLOR_NEON_CYAN.b, int(32 * strength))
            haze.fill(haze_color, rect=pygame.Rect(0, y, width, 1))
        grid.blit(haze, (0, 0), special_flags=pygame.BLEND_ADD)

        flag = getattr(pygame, "BLEND_ALPHA_SDL", 0)
        if flag:
            surface.blit(grid, (0, grid_top), special_flags=flag)
        else:
            surface.blit(grid, (0, grid_top))
