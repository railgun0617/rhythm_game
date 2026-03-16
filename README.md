# 4-Lane Rhythm Game (AIST2010 Project) — Windows

This is a 4-lane rhythm game (PJSK / Muse Dash style) that imports MP3/WAV, auto-detects onsets/beats with `librosa`, autogenerates charts, and runs real-time gameplay with `pygame`.

> **Windows only.** Tested on Python 3.11–3.13 (x64).

---

## Quick start (Windows)

1) **Install Python 3.11–3.13 (64-bit)** from python.org.  
   Make sure the **`py` launcher** is installed (default) and **pip** is available.

2) **Install all dependencies** (one command):  
   Double-click `bootstrap.bat` **or** run it from a terminal opened in this folder:
   ```bat
   .\bootstrap.bat

### FFmpeg (recommended)
Some MP3 or non-standard WAV files require FFmpeg.

- The bootstrap script will auto-install it with winget if it’s missing.
- Or install manually:
  ```bat
  winget install --id=Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements

## Launching without a console window

- Double-click `launch_gui.pyw` (or run `pythonw launch_gui.pyw`). It imports `launch.main()` but uses the `pythonw` interpreter so only the pygame window appears.
- Game diagnostics are written to `logs/rhythm_master.log`. Set `RHYTHM_MASTER_DEBUG=1` before launching if you need to mirror logs to a console while debugging.

