from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import librosa
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy.ndimage import median_filter

logger = logging.getLogger(__name__)

HOP_LENGTH = 512
SUPERFLUX_BANDS = 6
DEFAULT_PERC_WEIGHT = 0.6
DEFAULT_HARM_WEIGHT = 0.4
NMS_WINDOW_SECONDS = 0.08
_CANDIDATE_TOLERANCE = 0.05  # seconds when clustering detections


@dataclass
class _CandidateEvidence:
    time: float
    value: float
    ratio: Tuple[float, float]
    delta: float


def _normalize(x: np.ndarray | None) -> np.ndarray:
    if x is None:
        return np.array([], dtype=float)
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x
    max_val = np.max(np.abs(x))
    if max_val <= 0:
        return x
    return x / max_val


def _spectrogram_hpss(y: np.ndarray, sr: int) -> Tuple[np.ndarray, np.ndarray]:
    target_sr = 22050
    if sr != target_sr:
        y_resampled = librosa.resample(y, orig_sr=sr, target_sr=target_sr)
    else:
        y_resampled = y
    n_fft = 1024
    hop = 256
    S = librosa.stft(y_resampled, n_fft=n_fft, hop_length=hop, center=True)
    if S.size == 0:
        return y.copy(), y.copy()
    H, P = librosa.decompose.hpss(S, kernel_size=(13, 31))
    y_harm = librosa.istft(H, hop_length=hop, length=len(y_resampled))
    y_perc = librosa.istft(P, hop_length=hop, length=len(y_resampled))
    if sr != target_sr:
        y_harm = librosa.resample(y_harm, orig_sr=target_sr, target_sr=sr)
        y_perc = librosa.resample(y_perc, orig_sr=target_sr, target_sr=sr)
    return y_harm, y_perc


def _mel_spectrogram(y: np.ndarray, sr: int, *, n_mels: int = 96) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    if y.ndim > 1:
        y = np.mean(y, axis=0)
    if y.size == 0:
        return np.zeros((n_mels, 0), dtype=float)
    mel = librosa.feature.melspectrogram(
        y=y,
        sr=sr,
        n_fft=2048,
        hop_length=HOP_LENGTH,
        n_mels=n_mels,
        power=2.0,
        center=True,
    )
    return mel


def _sliding_median(values: np.ndarray, frames: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0 or frames <= 1:
        return arr.copy()
    frames = int(max(1, frames))
    if frames % 2 == 0:
        frames += 1
    if arr.size < frames:
        return np.full_like(arr, np.median(arr))
    pad = frames // 2
    padded = np.pad(arr, pad, mode="edge")
    windows = sliding_window_view(padded, frames)
    med = np.median(windows, axis=-1)
    return med


def _superflux_multiband(
    y: np.ndarray,
    sr: int,
    *,
    bands: int = SUPERFLUX_BANDS,
    mel_spect: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Compute a Superflux-style novelty curve using multi-band spectral flux."""
    y = np.asarray(y, dtype=float)
    if y.ndim > 1:
        y = np.mean(y, axis=0)
    if y.size == 0:
        return np.zeros(0, dtype=float)

    if mel_spect is None:
        mel = _mel_spectrogram(y, sr, n_mels=max(36, bands * 12))
    else:
        mel = mel_spect
    if mel.size == 0:
        return np.zeros(0, dtype=float)

    mel = np.log1p(mel)
    med_width = max(1, min(mel.shape[1] - 1, 21))
    if med_width > 1:
        reference = median_filter(mel, size=(1, med_width), mode="nearest")
        whitened = np.maximum(mel - reference, 0.0)
    else:
        whitened = mel

    band_flux: List[np.ndarray] = []
    for band_bins in np.array_split(whitened, max(1, bands), axis=0):
        if band_bins.size == 0 or band_bins.shape[1] < 2:
            continue
        diff = np.maximum(band_bins[:, 1:] - band_bins[:, :-1], 0.0)
        band_flux.append(np.sum(diff, axis=0))

    if not band_flux:
        flux = np.zeros(whitened.shape[1], dtype=float)
    else:
        flux = np.mean(band_flux, axis=0)

    flux = np.pad(flux, (1, 0))  # keep array aligned with frame count
    return _normalize(flux)

def _safe_interp(times: np.ndarray, values: np.ndarray, t: float) -> float:
    if times.size == 0 or values.size == 0:
        return 0.0
    if t <= times[0]:
        return float(values[0])
    if t >= times[-1]:
        return float(values[-1])
    return float(np.interp(t, times, values))


def _cluster_candidates(raw: List[_CandidateEvidence]) -> List[Dict]:
    """Group raw candidate detections that fall within _CANDIDATE_TOLERANCE."""
    clusters: List[Dict] = []
    for ev in sorted(raw, key=lambda e: e.value, reverse=True):
        chosen = None
        for cluster in clusters:
            if abs(cluster["time"] - ev.time) <= _CANDIDATE_TOLERANCE:
                chosen = cluster
                break
        if chosen is None:
            chosen = {
                "time": ev.time,
                "score": ev.value,
                "evidence": [ev],
            }
            clusters.append(chosen)
        else:
            chosen["evidence"].append(ev)
            if ev.value > chosen["score"]:
                chosen["time"] = ev.time
                chosen["score"] = ev.value
    return clusters


def _nms_candidates(candidates: List[Dict], base_radius: float = NMS_WINDOW_SECONDS) -> List[Dict]:
    """Greedy non-max suppression with adaptive radius based on local spacing."""
    if not candidates:
        return []

    indexed = list(enumerate(candidates))
    by_time = sorted(indexed, key=lambda item: item[1]["time"])
    local_radius: Dict[int, float] = {}
    if len(by_time) == 1:
        local_radius[by_time[0][0]] = base_radius
    else:
        times = np.array([cand["time"] for _, cand in by_time], dtype=float)
        diffs = np.diff(times)
        for idx, (orig_idx, _) in enumerate(by_time):
            if len(by_time) == 1:
                span = base_radius
            elif idx == 0:
                span = diffs[0]
            elif idx == len(by_time) - 1:
                span = diffs[-1]
            else:
                span = 0.5 * (diffs[idx - 1] + diffs[idx])
            span = float(np.clip(span, base_radius * 0.5, base_radius * 2.0))
            local_radius[orig_idx] = span

    selected: List[Dict] = []
    for orig_idx, cand in sorted(indexed, key=lambda item: item[1]["score"], reverse=True):
        radius = local_radius.get(orig_idx, base_radius)
        if all(abs(cand["time"] - keep["time"]) > radius for keep in selected):
            selected.append(cand)
    return sorted(selected, key=lambda c: c["time"])


def compute_novelty_components(
    y: np.ndarray,
    sr: int,
    *,
    progress_hook: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, np.ndarray]:
    """
    Compute percussive, harmonic, and energy novelty streams for downstream fusion.

    Steps:
      1. HPSS separates harmonic/percussive components in a single pass.
      2. Percussive novelty: spectral flux via `librosa.onset.onset_strength`.
      3. Harmonic novelty: CQT magnitude → dB → flux onset strength to catch vocals.
      4. Energy novelty: positive derivative of frame-wise RMS.
    """
    if y.ndim > 1:
        y = np.mean(y, axis=0)
    y = _normalize(np.asarray(y, dtype=float))

    if y.size == 0:
        zeros = np.zeros(0, dtype=float)
        return {
            "percussive": zeros,
            "harmonic": zeros,
            "energy": zeros,
            "times": zeros,
        }

    hpss_start = time.perf_counter()
    y_harm, y_perc = _spectrogram_hpss(y, sr)
    if progress_hook:
        progress_hook(0.35, "Analyzing audio (HPSS complete)")
    hpss_elapsed = time.perf_counter() - hpss_start

    def _percussive_job() -> Tuple[np.ndarray, float]:
        start = time.perf_counter()
        mel = _mel_spectrogram(y_perc, sr, n_mels=max(36, SUPERFLUX_BANDS * 12))
        oenv = _superflux_multiband(y_perc, sr, mel_spect=mel)
        return oenv, time.perf_counter() - start

    def _harmonic_job() -> Tuple[np.ndarray, float]:
        start = time.perf_counter()
        mel_harm = _mel_spectrogram(y_harm, sr)
        if mel_harm.size == 0:
            oenv = np.zeros_like(y_perc)
        else:
            mel_db = librosa.power_to_db(mel_harm, ref=np.max)
            oenv = librosa.onset.onset_strength(
                S=mel_db,
                sr=sr,
                hop_length=HOP_LENGTH,
            )
        return oenv, time.perf_counter() - start

    def _energy_job() -> Tuple[np.ndarray, float]:
        start = time.perf_counter()
        rms = librosa.feature.rms(y=y, hop_length=HOP_LENGTH, center=True)[0]
        if rms.size == 0:
            energy_local = np.zeros(0, dtype=float)
        else:
            energy_local = np.maximum(0.0, np.diff(rms, prepend=rms[0]))
        return energy_local, time.perf_counter() - start

    with ThreadPoolExecutor(max_workers=3) as executor:
        perc_future = executor.submit(_percussive_job)
        harm_future = executor.submit(_harmonic_job)
        energy_future = executor.submit(_energy_job)
        oenv_perc, perc_time = perc_future.result()
        oenv_harm, harm_time = harm_future.result()
        energy, rms_elapsed = energy_future.result()

    if oenv_harm.size == 0 or oenv_harm.shape != oenv_perc.shape:
        oenv_harm = np.zeros_like(oenv_perc)
    if energy.size == 0 or energy.shape != oenv_perc.shape:
        energy = np.zeros_like(oenv_perc)

    mel_elapsed = perc_time + harm_time
    if progress_hook:
        progress_hook(0.65, "Analyzing audio (mel features ready)")
        progress_hook(0.85, "Analyzing audio (energy envelope ready)")

    times = librosa.times_like(oenv_perc, sr=sr, hop_length=HOP_LENGTH)

    percussive_onsets = librosa.onset.onset_detect(
        onset_envelope=oenv_perc,
        sr=sr,
        hop_length=HOP_LENGTH,
        units="time",
        backtrack=True,
        pre_max=3,
        post_max=3,
        pre_avg=3,
        post_avg=3,
        delta=0.03,
    ).tolist()

    logger.info(
        "HPSS %.2fs | Mel/Superflux %.2fs | RMS %.2fs",
        hpss_elapsed,
        mel_elapsed,
        rms_elapsed,
    )
    if progress_hook:
        progress_hook(0.98, "Analyzing audio (percussive peaks)")
    return {
        "percussive": _normalize(oenv_perc),
        "harmonic": _normalize(oenv_harm),
        "energy": _normalize(energy),
        "times": times.astype(float),
        "_percussive_onsets": percussive_onsets,
    }


def fuse_onset_env(
    novels: Dict[str, np.ndarray],
    perc_weight: float,
    harm_weight: float,
) -> np.ndarray:
    """Blend individual novelty streams (percussive, harmonic, optional energy)."""
    perc = np.asarray(novels.get("percussive", np.array([], dtype=float)), dtype=float)
    harm = np.asarray(novels.get("harmonic", np.array([], dtype=float)), dtype=float)
    energy = np.asarray(novels.get("energy", np.array([], dtype=float)), dtype=float)

    if perc.size == 0 and harm.size == 0 and energy.size == 0:
        return np.zeros(0, dtype=float)

    fused = perc_weight * _normalize(perc) + harm_weight * _normalize(harm)
    if energy.size:
        fused = fused + 0.2 * _normalize(energy)
    return _normalize(fused)


def candidate_onsets_sweep(
    novels: Dict[str, np.ndarray],
    sr: int,
    deltas: List[float] | Tuple[float, ...] = (0.07, 0.10, 0.13),
    ratios: List[Tuple[float, float]] | Tuple[Tuple[float, float], ...] = (
        (0.8, 0.2),
        (0.6, 0.4),
        (0.4, 0.6),
        (0.2, 0.8),
    ),
) -> List[float]:
    """
    Sweep ratio/threshold combinations to build a rich candidate pool.

    Each ratio/threshold pair contributes onset detections over the fused envelope.
    Results are clustered (≤50 ms) and merged with a final NMS pass (≤80 ms).
    Metadata (contributing ratios/deltas) is stored back on `novels["_candidate_details"]`.
    """
    times = np.asarray(novels.get("times", np.array([], dtype=float)), dtype=float)
    energy_series = np.asarray(novels.get("energy", np.array([], dtype=float)), dtype=float)
    raw_events: List[_CandidateEvidence] = []

    def _detect_for_combo(
        fused_env: np.ndarray,
        local_median: np.ndarray,
        ratio: Tuple[float, float],
        delta: float,
    ) -> List[_CandidateEvidence]:
        onset_times = librosa.onset.onset_detect(
            onset_envelope=fused_env,
            sr=sr,
            hop_length=HOP_LENGTH,
            units="time",
            backtrack=True,
            delta=float(delta),
        )
        events: List[_CandidateEvidence] = []
        if fused_env.size == 0:
            return events
        for t in onset_times:
            val = _safe_interp(times, fused_env, float(t))
            if local_median.size:
                idx = int(np.clip(np.searchsorted(times, t), 0, local_median.size - 1))
                threshold = local_median[idx] + float(delta)
                if val < threshold:
                    continue
            events.append(
                _CandidateEvidence(
                    time=float(t),
                    value=float(val),
                    ratio=tuple(ratio),
                    delta=float(delta),
                )
            )
        return events

    cpu_guess = os.cpu_count() or 1
    max_workers = max(1, min(len(deltas), min(4, cpu_guess)))
    for ratio in ratios:
        fused = fuse_onset_env(novels, ratio[0], ratio[1])
        if fused.size == 0:
            continue
        seconds_per_frame = HOP_LENGTH / sr
        window_frames = max(3, int(round(0.6 / seconds_per_frame)))
        local_median = _sliding_median(fused, window_frames)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(_detect_for_combo, fused, local_median, ratio, float(delta))
                for delta in deltas
            ]
            for future in futures:
                raw_events.extend(future.result())

    clusters = _cluster_candidates(raw_events)
    selected = _nms_candidates(clusters)

    for cand in selected:
        evidences = cand["evidence"]
        best_ev = max(evidences, key=lambda e: e.value) if evidences else None
        cand["best_ratio"] = best_ev.ratio if best_ev else None
        cand["best_delta"] = best_ev.delta if best_ev else None

    novels["_candidate_details"] = selected
    return [float(c["time"]) for c in selected]


def meter_confidence(beat_times: List[float], fused_env: np.ndarray, times: np.ndarray) -> float:
    if not beat_times or len(beat_times) < 3:
        return 0.3
    intervals = np.diff(beat_times)
    mean_iv = float(np.mean(intervals))
    if mean_iv <= 1e-6:
        return 0.3
    std_iv = float(np.std(intervals))
    cv = std_iv / mean_iv if mean_iv > 0 else 1.0
    spacing_score = float(np.exp(-3.0 * cv))

    if fused_env.size == 0 or times.size == 0:
        return float(np.clip(spacing_score, 0.0, 1.0))
    idx = np.searchsorted(times, beat_times)
    idx = np.clip(idx, 0, fused_env.size - 1)
    beat_energy = float(np.mean(fused_env[idx]))
    overall = float(np.mean(fused_env)) + 1e-6
    energy_ratio = beat_energy / overall
    energy_score = float(np.clip((energy_ratio - 0.7) / 1.3, 0.0, 1.0))
    return float(np.clip(0.6 * spacing_score + 0.4 * energy_score, 0.0, 1.0))


def strong_beat_track(onset_env_fused: np.ndarray, sr: int) -> Tuple[float, List[float]]:
    """Robust tempo + beat tracking over the fused envelope (trim disabled)."""
    if onset_env_fused.size == 0:
        return 0.0, []
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env_fused,
        sr=sr,
        hop_length=HOP_LENGTH,
        trim=False,
    )
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP_LENGTH)
    return float(tempo), beat_times.astype(float).tolist()


def pitch_track(y: np.ndarray, sr: int) -> Tuple[np.ndarray, np.ndarray]:
    """Melody-aware pitch track from the harmonic component with gap interpolation."""
    if y.ndim > 1:
        y = np.mean(y, axis=0)
    y = _normalize(np.asarray(y, dtype=float))
    if y.size == 0:
        return np.zeros(0, dtype=float), np.zeros(0, dtype=float)

    y_harm, _ = librosa.effects.hpss(y)
    fmin = librosa.note_to_hz("C2")
    nyquist = sr / 2.0
    fmax_nominal = librosa.note_to_hz("C6")
    fmax = min(fmax_nominal, 0.98 * nyquist)
    if fmax <= fmin:
        fmax = max(fmin * 1.01, 0.9 * nyquist)
    f0 = librosa.yin(
        y_harm,
        fmin=fmin,
        fmax=fmax,
        sr=sr,
        hop_length=HOP_LENGTH,
    )
    times = librosa.times_like(f0, sr=sr, hop_length=HOP_LENGTH).astype(float)
    f0 = np.asarray(f0, dtype=float)
    mask = ~np.isfinite(f0) | (f0 <= 0)
    if np.all(mask):
        return times, np.zeros_like(times)
    if np.any(mask):
        good_idx = np.where(~mask)[0]
        interp_idx = np.where(mask)[0]
        if good_idx.size >= 2:
            f0[mask] = np.interp(interp_idx, good_idx, f0[good_idx])
        else:
            fill_val = float(np.nanmean(f0[~mask])) if good_idx.size else 0.0
            f0[mask] = fill_val
    return times, f0


def detect_onsets_and_beats(
    y: np.ndarray,
    sr: int,
    *,
    novels: Optional[Dict[str, np.ndarray]] = None,
    fused: Optional[np.ndarray] = None,
) -> Tuple[List[float], float, List[float]]:
    """
    Detect onset times (s), global tempo (BPM) and beat grid (s) using the fused novelty ensemble.

    Implements the beat-locked detection pass described in the project brief:
      - Build novelty components via `compute_novelty_components`
      - Fuse with default weights (percussive 0.6, harmonic 0.4, +energy assist)
      - Run onset detection with backtracking and a permissive delta
      - Track beats with trim disabled to keep the full grid
    """
    if novels is None:
        novels = compute_novelty_components(y, sr)
    if fused is None:
        fused = fuse_onset_env(novels, DEFAULT_PERC_WEIGHT, DEFAULT_HARM_WEIGHT)

    if fused.size == 0:
        return [], 0.0, []

    onset_times = librosa.onset.onset_detect(
        onset_envelope=fused,
        sr=sr,
        hop_length=HOP_LENGTH,
        units="time",
        backtrack=True,
        delta=0.10,
    ).tolist()

    tempo, beat_times = strong_beat_track(fused, sr)
    return [float(t) for t in onset_times], float(tempo), beat_times


def compute_onset_strength(
    y: np.ndarray,
    sr: int,
    *,
    novels: Optional[Dict[str, np.ndarray]] = None,
) -> np.ndarray:
    """Return the fused onset envelope used for ranking onset candidates."""
    if novels is None:
        novels = compute_novelty_components(y, sr)
    return fuse_onset_env(novels, DEFAULT_PERC_WEIGHT, DEFAULT_HARM_WEIGHT)
