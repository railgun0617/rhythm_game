# config.py
import json
import pygame


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


DEFAULT_CONFIG = {
    "audio_latency_ms": 0,
    "scroll_speed": 1.0,
    "perfect_timing_ms": 45,
    "great_timing_ms": 90,
    "good_timing_ms": 150,
    "base_score": 1000,
    "hold_body_ratio": 0.5,
    "target_fps": 120,
    "volume": 0.8,
    "key_bindings": {"0": "d", "1": "f", "2": "j", "3": "k"},
    # Add sound effects configuration
    "sound_effects": {
        "enabled": True,
        "master_volume": 0.7,
        "paths": {
            "perfect": "assets/sounds/perfect.wav",
            "great": "assets/sounds/great.wav",
            "good": "assets/sounds/good.wav",
            "hold_start": "assets/sounds/hold_start.wav",
            "hold_end": "assets/sounds/hold_end.wav",
        },
        "volumes": {
            "perfect": 0.8,
            "great": 0.7,
            "good": 0.6,
            "hold_start": 0.7,
            "hold_end": 0.7,
        }
    }
}

def load_config(config_path: str = "config.json") -> dict:
    """Load configuration from JSON file, applying defaults for missing values."""
    try:
        with open(config_path, 'r') as f:
            user_conf = json.load(f)
    except FileNotFoundError:
        user_conf = {}
    config = DEFAULT_CONFIG.copy()
    config.update(user_conf)
    try:
        config["volume"] = float(config.get("volume", DEFAULT_CONFIG["volume"]))
    except Exception:
        config["volume"] = DEFAULT_CONFIG["volume"]
    config["volume"] = _clamp(config["volume"], 0.0, 1.0)
    # Derived values
    config["note_display_time"] = 2.0 / config.get("scroll_speed", 1.0)
    # Map key names to pygame key codes
    key_map: dict = {}
    bindings = config.get("key_bindings", {})
    for lane_str, key_name in bindings.items():
        try:
            lane = int(lane_str)
        except:
            continue
        try:
            key_code = pygame.key.key_code(key_name)
        except:
            key_code = None
        if key_code is not None:
            key_map[key_code] = lane
    config["key_map"] = key_map
    
    # Ensure sound effects config exists and has proper structure
    if "sound_effects" not in config:
        config["sound_effects"] = DEFAULT_CONFIG["sound_effects"].copy()
    else:
        # Merge with default sound effects to ensure all keys exist
        sound_effects = DEFAULT_CONFIG["sound_effects"].copy()
        sound_effects.update(config["sound_effects"])
        config["sound_effects"] = sound_effects
        
    return config
