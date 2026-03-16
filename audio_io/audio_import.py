import os
import numpy as np
import librosa
from typing import Tuple

def _resample_if_needed(y: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if dst_sr and src_sr != dst_sr:
        y = librosa.resample(y, orig_sr=src_sr, target_sr=dst_sr)
    return y

def load_audio(file_path: str, sr: int = 44100) -> Tuple[np.ndarray, int]:
    """
    Robust loader with 4 strategies:
      1) soundfile (fast path for WAV/FLAC/AIFF)
      2) librosa.load (audioread → ffmpeg/gstreamer if available)
      3) scipy.io.wavfile (plain PCM WAV)
      4) ffmpeg CLI (if installed) → decode to temp WAV then read with soundfile
    Returns mono float32 in [-1, 1], at 'sr' if provided.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Audio not found: {file_path}")

    errors = []

    # 1) soundfile
    try:
        import soundfile as sf
        y, native_sr = sf.read(file_path, dtype="float32", always_2d=False)
        if y.ndim > 1:
            y = y.mean(axis=1)
        y = _resample_if_needed(y, native_sr, sr)
        return y, (sr or native_sr)
    except Exception as e:
        errors.append(f"soundfile: {type(e).__name__}: {e}")

    # 2) librosa.load
    try:
        y, loaded_sr = librosa.load(file_path, sr=sr, mono=True)
        return y, loaded_sr
    except Exception as e:
        errors.append(f"librosa.load: {type(e).__name__}: {e}")

    # 3) scipy WAV
    try:
        from scipy.io import wavfile
        wsr, x = wavfile.read(file_path)
        if x.dtype.kind in ("i", "u"):
            y = x.astype(np.float32) / np.iinfo(x.dtype).max
        else:
            y = x.astype(np.float32)
        if y.ndim > 1:
            y = y.mean(axis=1)
        y = _resample_if_needed(y, wsr, sr)
        return y, (sr or wsr)
    except Exception as e:
        errors.append(f"scipy.wavfile: {type(e).__name__}: {e}")

    # 4) ffmpeg CLI fallback
    try:
        import shutil, subprocess, tempfile
        if shutil.which("ffmpeg"):
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_path = tmp.name
            tmp.close()
            target_sr = sr or 44100
            cmd = [
                "ffmpeg", "-y", "-i", file_path,
                "-ac", "1", "-ar", str(target_sr),
                tmp_path
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "ffmpeg failed")
            import soundfile as sf
            y, native_sr = sf.read(tmp_path, dtype="float32", always_2d=False)
            os.remove(tmp_path)
            return y, native_sr
        else:
            errors.append("ffmpeg: not found on PATH")
    except Exception as e:
        errors.append(f"ffmpeg convert: {type(e).__name__}: {e}")

    raise RuntimeError(
        "Failed to load audio. Tried -> " + " | ".join(errors) +
        "\nTip (Windows): install FFmpeg (see README) or convert the file to PCM WAV."
    )
