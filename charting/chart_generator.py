from __future__ import annotations

import bisect
import json
import os
import random
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import time

import librosa
import numpy as np

from analysis.analysis import (
    DEFAULT_HARM_WEIGHT,
    DEFAULT_PERC_WEIGHT,
    candidate_onsets_sweep,
    compute_novelty_components,
    detect_onsets_and_beats,
    fuse_onset_env,
    meter_confidence,
    pitch_track,
    strong_beat_track,
)
from runtime_paths import CHARTS_DIR

logger = logging.getLogger(__name__)

# Fraction of detected per-beat candidates to keep per difficulty
DIFFICULTY_FRACTION = {
    "Easy": 0.25,
    "Normal": 0.50,
    "Hard": 0.75,
    "Expert": 1.00,
}

# Backfill policy: when gaps exceed the threshold, sample the beat grid
BACKFILL_POLICY = {
    "Easy":   None,
    "Normal": {"gap_threshold": 3.5, "subdiv": 1, "min_spacing": 0.12},
    "Hard":   {"gap_threshold": 2.5, "subdiv": 2, "min_spacing": 0.11},
    "Expert": {"gap_threshold": 1.25, "subdiv": 2, "min_spacing": 0.10},
}

# Motif library (length-16 phrases over lanes [0,1,2,3])
MOTIFS: List[List[int]] = [
    [0, 1, 2, 3, 2, 1, 0, 1, 2, 3, 2, 1, 0, 1, 2, 3],  # staircase + return
    [1, 1, 2, 2, 3, 3, 2, 2, 1, 1, 0, 0, 1, 1, 2, 2],  # jack pairs
    [0, 2, 1, 3, 0, 2, 1, 3, 1, 3, 0, 2, 1, 3, 0, 2],  # ping-pong cross
    [2, 1, 0, 1, 2, 3, 2, 1, 0, 1, 2, 3, 2, 1, 0, 1],  # center-out wave
    [3, 2, 1, 0, 1, 2, 3, 2, 1, 0, 1, 2, 3, 2, 1, 0],  # reverse wave
]

_NOTE_WINDOW = 0.10  # 100 ms window to look for candidates per beat slot
_NMS_RADIUS = 0.08   # 80 ms NMS radius for timing de-duplication
_LANE_JUMP_LIMIT = 1
_PHRASE_HOLD_THRESHOLD = 0.12
_FILL_THRESHOLD = {"Hard": 0.35, "Expert": 0.28}


def _ms(t: float) -> int:
    return int(round(float(t) * 1000))


def _report_progress(callback: Optional[Callable[[float, str], None]], value: float, stage: str) -> None:
    if not callback:
        return
    clamped = max(0.0, min(1.0, float(value)))
    try:
        callback(clamped, stage)
    except Exception:
        # Progress reporting must never break generation.
        pass


def _choose_motif(idx: int) -> List[int]:
    return MOTIFS[idx % len(MOTIFS)]


def _nearest_index(times: np.ndarray, t: float) -> int:
    if times.size == 0:
        return 0
    return int(np.argmin(np.abs(times - t)))


def _interp(times: np.ndarray, values: np.ndarray, t: float) -> float:
    if times.size == 0 or values.size == 0:
        return 0.0
    if t <= times[0]:
        return float(values[0])
    if t >= times[-1]:
        return float(values[-1])
    return float(np.interp(t, times, values))


def _safe_z(value: float, mean: float, std: float) -> float:
    if std < 1e-6:
        return 0.0
    return (value - mean) / std


def _smooth_f0_sequence(values: np.ndarray, strength: float = 0.25) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    mask = np.isfinite(arr) & (arr > 0)
    if mask.sum() < 2:
        return arr
    seq = arr[mask]
    alpha = float(np.clip(strength, 0.05, 0.95))
    forward = seq.copy()
    for i in range(1, len(seq)):
        forward[i] = alpha * forward[i - 1] + (1.0 - alpha) * seq[i]
    backward = forward.copy()
    for i in range(len(seq) - 2, -1, -1):
        backward[i] = alpha * backward[i + 1] + (1.0 - alpha) * forward[i]
    arr[mask] = backward
    return arr


def _lane_from_pitch(f0: float, lo: float, hi: float) -> int:
    if not np.isfinite(f0) or f0 <= 0 or hi <= lo:
        return -1
    x = (np.log2(f0) - np.log2(lo)) / (np.log2(hi) - np.log2(lo))
    lane = int(np.floor(np.clip(x, 0.0, 0.9999) * 4.0))
    return int(np.clip(lane, 0, 3))


def _detect_audio_activity(
    y: np.ndarray, sr: int, threshold_db: float = -50.0
) -> Tuple[float, float]:
    """Estimate the active audio span (start/end seconds) ignoring silence."""
    y = np.asarray(y, dtype=float)
    if y.ndim > 1:
        y = np.mean(y, axis=0)
    if y.size == 0 or sr <= 0:
        return 0.0, 0.0
    frame_length = 2048
    hop = 512
    rms = librosa.feature.rms(y=y, frame_length=frame_length, hop_length=hop, center=True)[0]
    if rms.size == 0:
        duration = float(len(y)) / float(sr)
        return 0.0, duration
    power = rms**2
    ref = np.max(power) + 1e-12
    db = librosa.power_to_db(power + 1e-12, ref=ref)
    active = np.where(db > threshold_db)[0]
    duration = float(len(y)) / float(sr)
    if not active.size:
        return 0.0, duration
    start_frame = int(active[0])
    end_frame = int(active[-1])
    start_sample = max(0, start_frame * hop)
    end_sample = min(len(y), (end_frame + 1) * hop)
    start_time = float(start_sample) / float(sr)
    end_time = float(end_sample) / float(sr)
    end_time = max(start_time, min(duration, end_time))
    return max(0.0, start_time), end_time


def assign_notes_to_lanes(
    onset_times: List[float],
    y: np.ndarray,
    sr: int,
    f0_times: Optional[np.ndarray] = None,
    f0_values: Optional[np.ndarray] = None,
) -> List[Dict]:
    """
    Map onset times onto 4 lanes with light melody awareness.

    - Prefer melody-driven lanes via f0 when supplied (or computed lazily).
    - Fall back to spectral centroid bins, then i % 4 cycling when audio is unavailable.
    - Restrict lane jumps to <= 2 steps to smooth hand motion.
    - Accent phrases every 16th/8th onset with short holds when spacing permits.
    """
    notes: List[Dict] = []
    if not onset_times:
        return notes

    if y is None or y.size == 0:
        for i, t in enumerate(onset_times):
            notes.append({"time": float(t), "lane": i % 4, "type": "tap"})
        return notes

    y = np.asarray(y, dtype=float)
    if y.ndim > 1:
        y = np.mean(y, axis=0)

    if f0_times is None or f0_values is None:
        f0_times, f0_values = pitch_track(y, sr)
        f0_values = _smooth_f0_sequence(f0_values)

    valid_f0 = f0_values[np.isfinite(f0_values) & (f0_values > 0)]
    lo = float(np.percentile(valid_f0, 15)) if valid_f0.size else 110.0
    hi = float(np.percentile(valid_f0, 85)) if valid_f0.size else 880.0
    percussion_only = valid_f0.size < max(4, len(onset_times) // 4)
    perc_motif = _choose_motif(0) if percussion_only else None

    def lane_by_centroid(segment: np.ndarray) -> int:
        if segment.size == 0:
            return 0
        centroid = librosa.feature.spectral_centroid(y=segment, sr=sr)
        freq = float(centroid[0, 0]) if centroid.size else 0.0
        if freq < 500:
            return 0
        if freq < 2000:
            return 1
        if freq < 5000:
            return 2
        return 3

    prev_lane = 1
    ioi = np.median(np.diff(onset_times)) if len(onset_times) > 1 else 0.25
    for idx, t in enumerate(onset_times):
        seg_start = int(max(0, (t - 0.05) * sr))
        seg_end = int(min(len(y), (t + 0.05) * sr))
        segment = y[seg_start:seg_end]

        lane = -1
        if percussion_only and perc_motif:
            lane = int(perc_motif[idx % len(perc_motif)])
        else:
            if f0_times is not None and f0_times.size:
                j = _nearest_index(f0_times, t)
                lane = _lane_from_pitch(float(f0_values[j]), lo, hi)
            if lane < 0:
                lane = lane_by_centroid(segment) if segment.size else (idx % 4)

        if idx > 0 and abs(lane - prev_lane) > _LANE_JUMP_LIMIT:
            step = _LANE_JUMP_LIMIT if lane > prev_lane else -_LANE_JUMP_LIMIT
            lane = prev_lane + step
        prev_lane = lane

        note = {"time": float(t), "lane": int(lane), "type": "tap"}

        if idx % 16 == 0 and idx + 1 < len(onset_times):
            gap = onset_times[idx + 1] - t
            if gap > _PHRASE_HOLD_THRESHOLD:
                note["type"] = "hold"
                note["duration"] = round(2.0 * ioi, 3)
        elif idx % 8 == 0 and idx % 16 != 0 and idx + 1 < len(onset_times):
            gap = onset_times[idx + 1] - t
            if gap > _PHRASE_HOLD_THRESHOLD:
                note["type"] = "hold"
                note["duration"] = round(1.4 * ioi, 3)

        notes.append(note)

    return _resolve_hold_lane_conflicts(notes)


def _sanitize_holds(notes: List[Dict], max_simultaneous_holds: int = 2) -> None:
    """Ensure we do not exceed the simultaneous hold cap; convert extras to taps."""
    active: List[float] = []
    for note in sorted(notes, key=lambda n: n["time"]):
        time = float(note["time"])
        active = [end for end in active if end > time]
        if note.get("type") != "hold":
            continue
        duration = float(note.get("duration", 0.0))
        if duration <= 0 or len(active) >= max_simultaneous_holds:
            note["type"] = "tap"
            note.pop("duration", None)
        else:
            active.append(time + duration)


def _fill_sparse_with_motifs(
    notes: List[Dict],
    meter_conf: float = 1.0,
    dt_thresh: float = 0.26,
    min_consec: int = 40,
    motif_cache: Optional[Dict[Tuple[int, ...], List[int]]] = None,
) -> None:
    """
    Rewrite long uniform streams with 16-step motifs to add variety.

    - Identify segments whose inter-onset spacing stays below dt_thresh.
    - Re-map lanes using rotating motifs (staircase, ping-pong, jack pairs...).
    - Accent phrase starts (every 8th/16th step) with short holds when spacing allows.
    """
    if meter_conf < 0.35 or len(notes) < min_consec:
        return
    if motif_cache is None:
        motif_cache = {}
    dt_thresh = float(np.interp(meter_conf, [0.35, 1.0], [0.34, dt_thresh]))
    min_consec = int(np.interp(meter_conf, [0.35, 1.0], [70, min_consec]))
    times = [n["time"] for n in notes]
    dts = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    if not dts:
        return

    segments: List[Tuple[int, int]] = []
    i = 0
    while i < len(dts):
        if dts[i] <= dt_thresh:
            j = i
            while j < len(dts) and dts[j] <= dt_thresh:
                j += 1
            if j - i >= min_consec:
                segments.append((i, j))
            i = j
        else:
            i += 1

    for motif_id, (start, end) in enumerate(segments):
        seg_notes = notes[start:end + 1]
        if len(seg_notes) < min_consec:
            continue
        local_times = [n["time"] for n in seg_notes]
        local_dts = [local_times[k + 1] - local_times[k] for k in range(len(local_times) - 1)]
        if not local_dts:
            continue
        ioi = float(np.median(local_dts))

        quant_shape = tuple(int(np.clip(round((d / ioi) * 8.0), 0, 12)) for d in local_dts[:16])
        base_motif = motif_cache.get(quant_shape)
        if base_motif is None:
            base_motif = _choose_motif(motif_id + len(motif_cache))
            motif_cache[quant_shape] = base_motif[:]
        motif = base_motif[:]
        prev_lane = notes[start - 1]["lane"] if start > 0 else seg_notes[0]["lane"]
        offset = min(range(16), key=lambda x: abs(motif[x] - prev_lane))
        motif = motif[offset:] + motif[:offset]

        for idx, note in enumerate(seg_notes):
            motif_lane = motif[idx % 16]
            note["lane"] = int(motif_lane)
            g = idx % 16
            if g == 0:
                note["type"] = "hold"
                note["duration"] = round(2.0 * ioi, 3)
            elif g == 8:
                note["type"] = "hold"
                note["duration"] = round(1.4 * ioi, 3)
            else:
                note.setdefault("type", "tap")


def _resolve_hold_lane_conflicts(notes: List[Dict]) -> List[Dict]:
    """
    Prevent taps/holds from landing on the same lane while a hold is active.

    Strategy:
      - Track active hold end-times per lane.
      - If a new note arrives on a held lane, try to move it to the closest free lane.
      - If no lane is free, drop the conflicting tap (better than forcing impossible input).
    """
    if not notes:
        return notes

    hold_end = [float("-inf")] * 4
    resolved: List[Dict] = []

    for note in sorted(notes, key=lambda n: n["time"]):
        lane = int(note["lane"])
        time = float(note["time"])

        if hold_end[lane] > time + 1e-5:
            available = [l for l in range(4) if hold_end[l] <= time + 1e-5]
            if available:
                new_lane = min(available, key=lambda l: (abs(l - lane), l))
                note = dict(note)
                note["lane"] = new_lane
                lane = new_lane
            else:
                # Skip this note; all lanes occupied by holds at this instant.
                continue

        resolved.append(note)
        if note.get("type") == "hold":
            duration = max(0.0, float(note.get("duration", 0.0)))
            hold_end[lane] = max(hold_end[lane], time + duration)

    resolved.sort(key=lambda n: n["time"])
    return resolved


def _per_beat_grid(beat_times: List[float], *, subdiv: int) -> List[float]:
    """Create a beat grid with the requested subdivision; stores metadata on the function."""
    grid: List[float] = []
    metadata: List[Dict] = []
    subdiv = max(1, int(subdiv))
    if not beat_times:
        _per_beat_grid.last_meta = metadata  # type: ignore[attr-defined]
        return grid
    for idx, beat in enumerate(beat_times):
        beat = float(beat)
        grid.append(beat)
        metadata.append({"time": beat, "beat_index": idx, "sub_index": 0, "subdiv": subdiv})
        if idx + 1 >= len(beat_times) or subdiv == 1:
            continue
        next_beat = float(beat_times[idx + 1])
        step = (next_beat - beat) / subdiv
        for sub in range(1, subdiv):
            t = beat + sub * step
            grid.append(t)
            metadata.append({"time": t, "beat_index": idx, "sub_index": sub, "subdiv": subdiv})
    _per_beat_grid.last_meta = metadata  # type: ignore[attr-defined]
    return grid


def _time_nms(pairs: List[Tuple[float, float]], radius: float = _NMS_RADIUS) -> List[float]:
    """Greedy NMS on (time, score) pairs."""
    if not pairs:
        return []
    selected: List[float] = []
    for t, score in sorted(pairs, key=lambda x: (-(x[1]), x[0])):
        if all(abs(t - keep) > radius for keep in selected):
            selected.append(float(t))
    return sorted(selected)


def _beat_index(beat_times: List[float], t: float) -> int:
    if not beat_times:
        return 0
    idx = bisect.bisect_right(beat_times, t) - 1
    return max(idx, 0)


def _pitch_change_indicator(f0_times: Optional[np.ndarray], f0_values: Optional[np.ndarray], t: float) -> float:
    if f0_times is None or f0_values is None or f0_times.size == 0:
        return 0.0
    prev_t = max(f0_times[0], t - 0.12)
    curr = float(np.interp(t, f0_times, f0_values))
    prev = float(np.interp(prev_t, f0_times, f0_values))
    if not (np.isfinite(curr) and np.isfinite(prev) and curr > 0 and prev > 0):
        return 0.0
    delta = abs(np.log2(curr) - np.log2(prev))
    return float(np.clip(delta / 0.25, 0.0, 1.0))


def _select_per_beat_candidates(
    grid: List[float],
    candidate_times: List[float],
    env_bundle: Dict[str, np.ndarray | Dict],
    f0_times: Optional[np.ndarray],
    f0_values: Optional[np.ndarray],
    window: float = _NOTE_WINDOW,
) -> List[float]:
    """
    Score candidate onset times per beat slot and choose the highest-scoring option.

    score = 0.60*z(novelty_fused) + 0.25*gauss(|t - grid|, σ=40ms)
            + 0.10*Δpitch_indicator + 0.05*z(energy)
    """
    meter_conf = float(env_bundle.get("meter_conf", 1.0))  # type: ignore[arg-type]
    fused = np.asarray(env_bundle.get("fused", np.array([])), dtype=float)
    percussive = np.asarray(env_bundle.get("percussive", np.array([])), dtype=float)
    harmonic = np.asarray(env_bundle.get("harmonic", np.array([])), dtype=float)
    energy = np.asarray(env_bundle.get("energy", np.array([])), dtype=float)
    times = np.asarray(env_bundle.get("times", np.array([])), dtype=float)
    meta_map: Dict[int, Dict] = env_bundle.get("candidate_meta", {})  # type: ignore[assignment]

    fused_mean = float(np.mean(fused)) if fused.size else 0.0
    fused_std = float(np.std(fused)) if fused.size else 1.0
    energy_mean = float(np.mean(energy)) if energy.size else 0.0
    energy_std = float(np.std(energy)) if energy.size else 1.0

    candidate_times = sorted(float(t) for t in candidate_times)
    if meter_conf < 0.3:
        pairs = [(t, 1.0) for t in candidate_times]
        kept_times = _time_nms(pairs, radius=_NMS_RADIUS)
        _select_per_beat_candidates.last_debug = []  # type: ignore[attr-defined]
        return kept_times
    rows_idx: List[Tuple[int, Dict]] = []
    pairs: List[Tuple[float, float]] = []

    cpu_guess = os.cpu_count() or 1
    worker_count = max(1, min(len(grid), min(4, cpu_guess)))
    chunk = max(1, len(grid) // worker_count)

    def _score_slice(start: int, end: int) -> Tuple[List[Tuple[int, Dict]], List[Tuple[float, float]]]:
        local_rows: List[Tuple[int, Dict]] = []
        local_pairs: List[Tuple[float, float]] = []
        for local_idx in range(start, end):
            grid_time = grid[local_idx]
            window_candidates = [
                t for t in candidate_times
                if abs(t - grid_time) <= window
            ]

            best_time: Optional[float] = None
            best_score = -1e9
            best_row: Dict = {}

            for cand_time in window_candidates:
                fused_val = _interp(times, fused, cand_time)
                novelty_z = _safe_z(fused_val, fused_mean, fused_std)
                energy_val = _interp(times, energy, cand_time)
                energy_z = _safe_z(energy_val, energy_mean, energy_std)
                gauss = np.exp(-0.5 * ((cand_time - grid_time) / 0.04) ** 2)
                pitch_ind = _pitch_change_indicator(f0_times, f0_values, cand_time)
                score = 0.60 * novelty_z + 0.25 * gauss + 0.10 * pitch_ind + 0.05 * energy_z

                if score > best_score:
                    meta = meta_map.get(_ms(cand_time), {})
                    best_score = score
                    best_time = cand_time
                    best_row = {
                        "grid_time": float(grid_time),
                        "candidate_time": float(cand_time),
                        "score": float(score),
                        "novelty_fused": float(fused_val),
                        "novelty_perc": _interp(times, percussive, cand_time),
                        "novelty_harm": _interp(times, harmonic, cand_time),
                        "energy_val": float(energy_val),
                        "pitch_indicator": float(pitch_ind),
                        "grid_alignment": float(gauss),
                        "ratio_used": meta.get("best_ratio"),
                        "delta_used": meta.get("best_delta"),
                        "is_fill": False,
                        "beat_index": None,
                        "candidate_ms": _ms(cand_time),
                    }

            if best_time is None:
                local_rows.append((local_idx, {
                    "grid_time": float(grid_time),
                    "candidate_time": None,
                    "score": 0.0,
                    "novelty_fused": _interp(times, fused, grid_time),
                    "novelty_perc": _interp(times, percussive, grid_time),
                    "novelty_harm": _interp(times, harmonic, grid_time),
                    "energy_val": _interp(times, energy, grid_time),
                    "pitch_indicator": 0.0,
                    "grid_alignment": 0.0,
                    "ratio_used": None,
                    "delta_used": None,
                    "is_fill": False,
                    "beat_index": None,
                    "candidate_ms": None,
                }))
            else:
                local_rows.append((local_idx, best_row))
                local_pairs.append((float(best_time), float(best_row["score"])))
        return local_rows, local_pairs

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(_score_slice, start, min(len(grid), start + chunk))
            for start in range(0, len(grid), chunk)
        ]
        for future in futures:
            part_rows, part_pairs = future.result()
            rows_idx.extend(part_rows)
            pairs.extend(part_pairs)

    rows_idx.sort(key=lambda item: item[0])
    rows = [row for _, row in rows_idx]

    kept_times = _time_nms(pairs, radius=_NMS_RADIUS)
    kept_keys = {_ms(t) for t in kept_times}
    for row in rows:
        key = row.get("candidate_ms")
        if key is None or key not in kept_keys:
            row["candidate_time"] = None
            row["score"] = 0.0
            row["ratio_used"] = None
            row["delta_used"] = None

    _select_per_beat_candidates.last_debug = rows  # type: ignore[attr-defined]
    return kept_times


def _apply_backfill(
    selected_times: List[float],
    grid: List[float],
    env_bundle: Dict[str, np.ndarray],
    policy: Dict[str, float],
) -> List[float]:
    """Fill long gaps using beat/sub-beat grid points where novelty supports it."""
    if not selected_times:
        return selected_times
    gap_threshold = policy["gap_threshold"]
    min_spacing = policy["min_spacing"]
    subdiv = max(1, int(policy["subdiv"]))

    fused = np.asarray(env_bundle.get("fused", np.array([])), dtype=float)
    times = np.asarray(env_bundle.get("times", np.array([])), dtype=float)

    result = sorted(float(t) for t in selected_times)
    additions: List[Tuple[float, float]] = []
    grid_sorted = sorted(set(float(t) for t in grid))

    for left, right in zip(result, result[1:]):
        gap = right - left
        if gap <= gap_threshold:
            continue
        gap_points = [
            g for g in grid_sorted
            if (left + min_spacing) <= g <= (right - min_spacing)
        ]
        count = 0
        for g in gap_points:
            novelty = _interp(times, fused, g)
            if novelty < 0.2:
                continue
            additions.append((g, novelty))
            count += 1
            if count >= subdiv:
                break

    if additions:
        result.extend([t for t, _ in additions])
        result = sorted(set(result))

    return result


def generate_charts(
    y: np.ndarray,
    sr: int,
    *,
    audio_filename: Optional[str] = None,
    seed: Optional[int] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, List[Dict]]:
    """Generate four difficulty charts with beat-locked, melody-aware placement."""
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)

    raw_duration = float(len(y)) / float(sr or 1)
    active_start, active_end = _detect_audio_activity(y, sr)
    lead_in = max(0.0, min(raw_duration, active_start))
    active_end = max(lead_in, min(raw_duration, active_end))
    tail_silence = max(0.0, raw_duration - active_end)
    effective_duration = max(0.0, active_end - lead_in)

    analysis_start = time.perf_counter()
    _report_progress(progress_callback, 0.02, "Analyzing audio...")
    novels = compute_novelty_components(y, sr)
    fused_env = fuse_onset_env(novels, DEFAULT_PERC_WEIGHT, DEFAULT_HARM_WEIGHT)
    base_onsets, base_tempo, base_beats = detect_onsets_and_beats(y, sr)
    onset_env = fused_env.copy()
    analysis_elapsed = time.perf_counter() - analysis_start
    logger.info("novelty + beat detection completed in %.2fs", analysis_elapsed)
    _report_progress(progress_callback, 0.15, "Estimating rhythm...")
    tempo = base_tempo
    beat_times = base_beats
    if not beat_times:
        tempo, beat_times = strong_beat_track(fused_env, sr)
    meter_conf = meter_confidence(beat_times, fused_env, novels.get("times", np.array([])))

    candidate_start = time.perf_counter()
    candidate_times = set(candidate_onsets_sweep(novels, sr))
    candidate_meta = {
        _ms(entry["time"]): entry
        for entry in novels.get("_candidate_details", [])
    }
    for t in base_onsets:
        key = _ms(t)
        if key not in candidate_meta:
            candidate_meta[key] = {
                "time": float(t),
                "score": _interp(novels.get("times", np.array([])), fused_env, t),
                "best_ratio": None,
                "best_delta": None,
                "source": "baseline",
            }
        candidate_times.add(float(t))
    candidate_elapsed = time.perf_counter() - candidate_start
    logger.info(
        "candidate sweep finished in %.2fs (total candidates: %s)",
        candidate_elapsed,
        len(candidate_times),
    )

    env_bundle = {
        "fused": fused_env,
        "percussive": novels.get("percussive", np.array([])),
        "harmonic": novels.get("harmonic", np.array([])),
        "energy": novels.get("energy", np.array([])),
        "times": novels.get("times", np.array([])),
        "candidate_meta": candidate_meta,
        "meter_conf": meter_conf,
    }

    f0_times, f0_values = pitch_track(y, sr)
    f0_values = _smooth_f0_sequence(f0_values)
    _report_progress(progress_callback, 0.32, "Scoring candidate notes...")

    charts: Dict[str, List[Dict]] = {}
    debug_payload: Dict[str, List[Dict]] = {}
    motif_cache: Dict[Tuple[int, ...], List[int]] = {}

    if not beat_times and candidate_times:
        beat_times = sorted(candidate_times)
        meter_conf = min(meter_conf, 0.35)
        env_bundle["meter_conf"] = meter_conf

    difficulties = ("Easy", "Normal", "Hard", "Expert")
    build_start = time.perf_counter()
    per_difficulty_span = 0.5 / max(1, len(difficulties))
    for idx, difficulty in enumerate(difficulties):
        start_progress = 0.4 + idx * per_difficulty_span
        _report_progress(progress_callback, start_progress, f"Building {difficulty} chart...")
        if difficulty in ("Hard", "Expert"):
            subdiv = 2
        else:
            subdiv = 1
        grid = _per_beat_grid(beat_times, subdiv=subdiv)
        if difficulty == "Expert" and beat_times:
            # add quarter-beat subdivisions in high-novelty passages
            extra_points: List[float] = []
            fused_times = env_bundle["times"]
            fused_vals = env_bundle["fused"]
            for idx in range(len(beat_times) - 1):
                beat = beat_times[idx]
                next_beat = beat_times[idx + 1]
                mask = (fused_times >= beat) & (fused_times < next_beat)
                seg_vals = fused_vals[mask] if np.any(mask) else np.array([])
                if seg_vals.size and float(np.mean(seg_vals)) >= 0.45:
                    step = (next_beat - beat) / 4.0
                    extra_points.extend([beat + step, beat + 3 * step])
            if extra_points:
                grid = sorted(set(grid + extra_points))

        stretch_hi = 0.85 if difficulty != "Expert" else 0.70
        adaptive_window = _NOTE_WINDOW * float(np.interp(meter_conf, [0.0, 1.0], [1.6, stretch_hi]))
        selected_times = _select_per_beat_candidates(
            grid,
            sorted(candidate_times),
            env_bundle,
            f0_times,
            f0_values,
            window=adaptive_window,
        )
        rows = [dict(row) for row in getattr(_select_per_beat_candidates, "last_debug", [])]

        for row in rows:
            row["beat_index"] = _beat_index(beat_times, row["grid_time"])
            row["selected"] = False

        candidate_rows = [row for row in rows if row.get("candidate_time") is not None]
        target_count = int(round(len(candidate_rows) * DIFFICULTY_FRACTION[difficulty]))
        if candidate_rows and target_count < 1:
            target_count = 1

        candidate_rows_sorted = sorted(
            candidate_rows,
            key=lambda r: (-r["score"], r["grid_time"])
        )

        selected_set: Dict[int, Dict] = {}
        for row in candidate_rows_sorted[:target_count]:
            row["selected"] = True
            if row["candidate_time"] is not None:
                selected_set[_ms(row["candidate_time"])] = row

        if difficulty in ("Hard", "Expert"):
            fill_threshold = _FILL_THRESHOLD[difficulty]
            beat_groups: Dict[int, List[Dict]] = {}
            for row in rows:
                beat_groups.setdefault(row["beat_index"], []).append(row)
            for group in beat_groups.values():
                if max(r["novelty_fused"] for r in group) < fill_threshold:
                    continue
                if not any(r.get("selected") for r in group):
                    best = max(group, key=lambda r: (r["score"], -abs(r["grid_alignment"] - 1.0)))
                    if best.get("candidate_time") is None:
                        best["candidate_time"] = best["grid_time"]
                        best["is_fill"] = True
                        best["score"] = float(max(best["score"], best["novelty_fused"] * 0.4))
                        best["ratio_used"] = "fill"
                        best["delta_used"] = None
                    best["selected"] = True
                    if best.get("candidate_time") is not None:
                        selected_set[_ms(best["candidate_time"])] = best

        selected_times = sorted(
            row["candidate_time"]
            for row in rows
            if row.get("selected") and row.get("candidate_time") is not None
        )

        policy = BACKFILL_POLICY.get(difficulty)
        if policy:
            before_keys = {_ms(t) for t in selected_times}
            selected_times = _apply_backfill(selected_times, grid, env_bundle, policy)
            after_keys = {_ms(t) for t in selected_times}
            added_keys = after_keys - before_keys
            for key in added_keys:
                t = key / 1000.0
                rows.append({
                    "grid_time": t,
                    "candidate_time": t,
                    "score": 0.2,
                    "novelty_fused": _interp(env_bundle["times"], env_bundle["fused"], t),
                    "novelty_perc": _interp(env_bundle["times"], env_bundle["percussive"], t),
                    "novelty_harm": _interp(env_bundle["times"], env_bundle["harmonic"], t),
                    "energy_val": _interp(env_bundle["times"], env_bundle["energy"], t),
                    "pitch_indicator": 0.0,
                    "grid_alignment": 1.0,
                    "ratio_used": "backfill",
                    "delta_used": None,
                    "is_fill": True,
                    "beat_index": _beat_index(beat_times, t),
                    "candidate_ms": key,
                    "selected": True,
                })

        notes = assign_notes_to_lanes(selected_times, y, sr, f0_times=f0_times, f0_values=f0_values)
        if difficulty in ("Hard", "Expert"):
            _fill_sparse_with_motifs(notes, meter_conf=meter_conf, motif_cache=motif_cache)
        notes = _resolve_hold_lane_conflicts(notes)
        max_holds = 3 if difficulty in ("Hard", "Expert") else 2
        _sanitize_holds(notes, max_simultaneous_holds=max_holds)
        charts[difficulty] = notes
        debug_payload[difficulty] = sorted(rows, key=lambda r: r["grid_time"])
        end_progress = start_progress + per_difficulty_span
        _report_progress(progress_callback, end_progress, f"{difficulty} chart ready")

    base_name = Path(audio_filename).stem if audio_filename else "generated_chart"
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    _report_progress(progress_callback, 0.95, "Saving charts...")
    for diff, notes in charts.items():
        chart_data = {
            "song_name": base_name,
            "audio_file": str(audio_filename) if audio_filename else None,
            "difficulty": diff,
            "notes": notes,
            "audio_duration": round(raw_duration, 6),
            "audio_effective_start": round(lead_in, 6),
            "audio_effective_end": round(active_end, 6),
            "audio_effective_duration": round(effective_duration, 6),
            "audio_lead_in": round(lead_in, 6),
            "audio_tail_silence": round(tail_silence, 6),
        }
        file_path = CHARTS_DIR / f"{base_name}_{diff.lower()}.json"
        with file_path.open("w", encoding="utf-8") as fh:
            json.dump(chart_data, fh, indent=2)

    if audio_filename:
        debug_path = CHARTS_DIR / f"{base_name}_debug.json"
        with debug_path.open("w", encoding="utf-8") as fh:
            json.dump(debug_payload, fh, indent=2, default=float)

    total_elapsed = time.perf_counter() - analysis_start
    build_elapsed = time.perf_counter() - build_start
    logger.info(
        "chart build completed in %.2fs (total pipeline %.2fs)",
        build_elapsed,
        total_elapsed,
    )
    _report_progress(progress_callback, 1.0, "Finished")
    return charts
