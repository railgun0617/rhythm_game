from gameplay.game import Game
from gameplay.notes import Note

def make_config():
    return {
        "audio_latency_ms": 0,
        "perfect_timing_ms": 30,
        "great_timing_ms": 60,
        "good_timing_ms": 100,
        "base_score": 1000,
        "key_map": {1: 0}  # Map dummy key code 1 to lane 0
    }

def test_perfect_hit():
    note = Note(lane=0, time=1.0, type="tap")
    config = make_config()
    game = Game([note], config)
    # Simulate hitting exactly on time
    game.current_time = 1.0
    game.process_input(1)
    assert game.judgements["Perfect"] == 1
    assert game.score == 1000
    assert game.combo == 1 and game.max_combo == 1

def test_early_and_late_hit_no_effect():
    note = Note(lane=0, time=1.0, type="tap")
    config = make_config()
    game = Game([note], config)
    # Early press (far before note)
    game.current_time = 0.5
    game.process_input(1)
    assert game.judgements["Perfect"] == 0 and game.judgements["Miss"] == 0
    # Late press (far after note)
    game.current_time = 1.2
    game.process_input(1)
    assert all(v == 0 for v in game.judgements.values()), "No judgement for very late press"

def test_miss_and_combo_reset():
    notes = [Note(lane=0, time=1.0, type="tap"), Note(lane=0, time=2.0, type="tap")]
    config = make_config()
    game = Game(notes, config)
    # Hit first note
    game.current_time = 1.0
    game.process_input(1)
    assert game.combo == 1
    # Let second note pass without hit
    game.current_time = 2.2
    game.update_notes_and_score()
    assert game.judgements["Miss"] == 1
    assert game.combo == 0 and game.max_combo == 1

def test_latency_offset():
    # With positive latency, allow later hit without miss
    note = Note(lane=0, time=1.0, type="tap")
    config = make_config()
    config["audio_latency_ms"] = 100  # 100 ms delay
    game = Game([note], config)
    # Press 150ms after note time (would be miss without offset, should count with offset)
    game.current_time = 1.15
    game.process_input(1)
    # 150ms late with 100ms offset => 50ms error (Good/Great)
    assert game.judgements["Good"] == 1 or game.judgements["Great"] == 1 or game.judgements["Perfect"] == 1
