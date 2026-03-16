
Rhythm Master Front Page & Score System
====================================

What's new:
- A front page UI to select songs and difficulty with your keyboard.
- A persistent score log (scores/scores.jsonl) and an in-game Scores page.
- A new entrypoint: launch.py (run it to start the game).

How to start:
-------------
- Make sure your Python env satisfies requirements.txt (pygame, librosa, etc.).
- Then run:  python -m rhythm_game.launch
  (or: python rhythm_game/launch.py)

Controls:
---------
Front page:
- Up/Down: choose song
- Left/Right: choose difficulty (Easy/Normal/Hard/Expert)
- Enter: Play
- Tab: View scores
- Esc/Q: Quit

During gameplay:
- D F J K: hit lanes (default; configurable in config.json)
- Esc/P: pause/resume
- R: restart
- Q: end song

Results screen:
- R: replay the same song+difficulty
- Esc/Q: back to main menu

Score storage:
--------------
- Every finished run is appended to scores/scores.jsonl.
- From the front page, you'll see your best score per song+difficulty.
- From the Scores page (Tab), you can browse the top results.

Notes:
------
- If a song has no charts yet, the launcher will auto-generate charts the first time you play it.
- Place audio files (WAV/MP3/OGG/FLAC) into rhythm_game/assets.
- Charts are written/read from rhythm_game/charts.
- You can change the game title by updating TITLE in ui/menu.py (defaults to "Rhythm Master").
