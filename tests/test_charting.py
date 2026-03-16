import numpy as np
from charting import chart_generator

def test_assign_notes_all_lanes():
    # Empty audio input to force fallback lane assignment (i % 4)
    y = np.array([])
    sr = 44100
    onsets = [0.1, 0.2, 0.3, 0.4, 0.5]
    notes = chart_generator.assign_notes_to_lanes(onsets, y, sr)
    lanes = [note["lane"] for note in notes]
    assert lanes[:5] == [0, 1, 2, 3, 0], f"Lane assignment cycle incorrect: {lanes}"

def test_hold_note_generation():
    # Construct onsets that produce a hold note
    y = np.zeros(1000)  # zeros yield low frequency centroid (for toggling)
    sr = 1000
    onsets = [0.0, 0.5, 2.0]  # note0 and note2 same lane (via toggling), gap 2.0s
    notes = chart_generator.assign_notes_to_lanes(onsets, y, sr)
    hold_notes = [n for n in notes if n["type"] == "hold"]
    assert hold_notes, "Expected at least one hold note"
    first_note = notes[0]
    assert first_note["type"] == "hold", "First note should be converted to hold"
    expected_dur = round(1.9, 1)  # ~next_time-gap minus 0.1
    assert abs(first_note.get("duration", 0) - expected_dur) < 0.2, "Hold duration not set correctly"

def test_difficulty_filtering():
    # Simulate deterministic onset selection by monkey-patching analysis functions
    total = 10
    onset_times = list(np.linspace(0, 5, total))
    strengths = list(range(total, 0, -1))
    chart_generator.detect_onsets_and_beats = lambda y, sr: (onset_times, 120.0, onset_times)
    chart_generator.compute_onset_strength = lambda y, sr: np.array(strengths)
    y_dummy = np.zeros(100)
    sr_dummy = 22050
    charts = chart_generator.generate_charts(y_dummy, sr_dummy, audio_filename="dummy.wav", seed=42)
    # Ensure charts are generated for all difficulties
    assert set(charts.keys()) == {"Easy", "Normal", "Hard", "Expert"}
    # Verify note count ordering by difficulty (Easy <= Normal <= Hard <= Expert)
    lengths = [len(charts[d]) for d in ["Easy", "Normal", "Hard", "Expert"]]
    assert lengths[0] <= lengths[1] <= lengths[2] <= lengths[3], "Note count should increase with difficulty"
