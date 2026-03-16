# sound_manager.py - SIMPLIFIED VERSION
import pygame
from typing import Dict
import os

class SoundManager:
    def __init__(self, config: Dict):
        self.config = config
        self.sounds: Dict[str, pygame.mixer.Sound] = {}
        self.enabled = config["sound_effects"].get("enabled", True)
        
        # Don't initialize mixer here, just load sounds
        self._load_sounds()
        
    def _load_sounds(self):
        """Load all sound effects based on config"""
        if not self.enabled:
            return
            
        sound_config = self.config["sound_effects"]
        paths = sound_config.get("paths", {})
        volumes = sound_config.get("volumes", {})
        
        for name, path in paths.items():
            try:
                # Check if file exists before trying to load
                if os.path.exists(path):
                    # Store the path and volume, but don't load the sound yet
                    self.sounds[name] = {
                        'path': path,
                        'volume': volumes.get(name, 1.0),
                        'sound_object': None  # Will be loaded on first use
                    }
                    print(f"Registered sound: {name} from {path}")
                else:
                    print(f"Sound file not found: {path}")
            except Exception as e:
                print(f"Could not register sound {path}: {e}")
        
    def _ensure_sound_loaded(self, name: str) -> bool:
        """Load a sound if it hasn't been loaded yet"""
        if name not in self.sounds:
            return False
            
        sound_data = self.sounds[name]
        
        # If sound is already loaded, return True
        if sound_data['sound_object'] is not None:
            return True
            
        # Try to load the sound
        try:
            # Check if mixer is available
            if not pygame.mixer.get_init():
                print("Mixer not initialized, cannot load sound")
                return False
                
            sound = pygame.mixer.Sound(sound_data['path'])
            sound.set_volume(sound_data['volume'])
            sound_data['sound_object'] = sound
            print(f"Loaded sound: {name}")
            return True
        except (pygame.error, Exception) as e:
            print(f"Error loading sound {name}: {e}")
            return False
        
    def play_sound(self, name: str) -> bool:
        """Play a sound effect by name"""
        if not self.enabled or name not in self.sounds:
            return False
            
        # Ensure the sound is loaded
        if not self._ensure_sound_loaded(name):
            return False
            
        sound_data = self.sounds[name]
        try:
            sound_data['sound_object'].play()
            return True
        except pygame.error as e:
            print(f"Error playing sound {name}: {e}")
            return False
            
    # Remove set_master_volume and stop_all_sounds methods
    # since they require direct mixer access
