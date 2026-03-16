from typing import Optional, Dict

def judge_timing(error: float, config: Dict) -> Optional[str]:
    """Return the judgement for a given timing error (seconds), or None if outside all windows."""
    perfect_window = config.get("perfect_timing_ms", 30) / 1000.0
    great_window = config.get("great_timing_ms", 60) / 1000.0
    good_window = config.get("good_timing_ms", 100) / 1000.0
    tolerance = 0.02  # allow small drift from quantisation/latency calibration
    diff = abs(error)
    if diff <= perfect_window:
        return "Perfect"
    elif diff <= great_window + tolerance:
        return "Great"
    elif diff <= good_window + tolerance:
        return "Good"
    else:
        return None
