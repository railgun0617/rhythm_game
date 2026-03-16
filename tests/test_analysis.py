import numpy as np
from analysis import analysis

def test_no_onset_detect():
    # Silence (zeros) should yield no detected onsets
    sr = 22050
    y = np.zeros(sr)
    onset_times, tempo, beat_times = analysis.detect_onsets_and_beats(y, sr)
    assert onset_times == [] or max(onset_times) == 0, "Expected no onsets for silence"
    assert tempo >= 0, "Tempo should be non-negative even if no beats"
    # Onset strength envelope for silence should be all zeros
    oenv = analysis.compute_onset_strength(y, sr)
    assert np.all(oenv == 0), "Onset strength should be zero for silence"
