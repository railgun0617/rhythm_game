from gameplay import scoring

def test_judgement_windows():
    config = {"perfect_timing_ms": 50, "great_timing_ms": 100, "good_timing_ms": 200}
    # Within each timing window
    assert scoring.judge_timing(0.00, config) == "Perfect"
    assert scoring.judge_timing(0.05, config) == "Perfect"
    assert scoring.judge_timing(0.06, config) == "Great"
    assert scoring.judge_timing(0.10, config) == "Great"
    assert scoring.judge_timing(0.15, config) == "Good"
    assert scoring.judge_timing(0.20, config) == "Good"
    # Outside Good window
    assert scoring.judge_timing(0.25, config) is None
    # Symmetry for early hits (negative error)
    assert scoring.judge_timing(-0.04, config) == "Perfect"
    assert scoring.judge_timing(-0.12, config) == "Great"
    assert scoring.judge_timing(-0.18, config) == "Good"
    assert scoring.judge_timing(-0.25, config) is None
