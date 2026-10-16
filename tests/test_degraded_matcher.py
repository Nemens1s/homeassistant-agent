from app.degraded.matcher import match_action, normalise, score

ROWS = [
    {"entity_id": "script.ai_action_lights_on", "name": "AI Action: Living Room Lights On"},
    {"entity_id": "script.ai_action_lights_off", "name": "AI Action: Living Room Lights Off"},
    {"entity_id": "script.ai_action_stop_vacuum", "name": "AI Action: Stop Vacuum"},
    {"entity_id": "script.ai_action_start_vacuum", "name": "AI Action: Start Vacuum"},
    {"entity_id": "automation.ai_goodnight", "name": "AI Action: Goodnight"},
    {"entity_id": "script.ai_tuled", "name": "Tuled põlema elutoas"},
]


def test_normalise_drops_filler_and_prefix_but_keeps_on_off_start_stop():
    assert normalise("AI Action: Turn ON the lights, please!") == ["on", "lights"]
    assert normalise("start the vacuum") == ["start", "vacuum"]


def test_score_is_order_insensitive():
    assert score("lights living room on", "Living Room Lights On") == 1.0


def test_clear_winner_fires():
    match = match_action("turn on the living room lights", ROWS)
    assert match.winner["entity_id"] == "script.ai_action_lights_on"


def test_on_and_off_are_distinguished():
    match = match_action("lights on in the living room please", ROWS)
    assert match.winner["entity_id"] == "script.ai_action_lights_on"


def test_start_and_stop_are_distinguished():
    assert match_action("stop the vacuum", ROWS).winner["entity_id"] == "script.ai_action_stop_vacuum"
    assert match_action("start the vacuum", ROWS).winner["entity_id"] == "script.ai_action_start_vacuum"


def test_near_tie_gives_candidates():
    match = match_action("living room lights", ROWS)
    assert match.winner is None
    ids = []
    for row in match.candidates:
        ids.append(row["entity_id"])
    assert ids[:2] == ["script.ai_action_lights_on", "script.ai_action_lights_off"]


def test_single_word_ambiguous_gives_candidates():
    match = match_action("vacuum", ROWS)
    assert match.winner is None
    assert len(match.candidates) == 2


def test_non_english_name_matches_original_text():
    assert match_action("tuled põlema elutoas", ROWS).winner["entity_id"] == "script.ai_tuled"


def test_unrelated_text_matches_nothing():
    match = match_action("what's the temperature", ROWS)
    assert match.winner is None
    assert match.candidates == []


def test_empty_rows():
    match = match_action("lights", [])
    assert match.winner is None
    assert match.candidates == []


DOOR_ROWS = [
    {"entity_id": "script.ai_open_garage_door", "name": "AI Action: Open Garage Door"},
    {"entity_id": "script.ai_unlock_front_door", "name": "AI Action: Unlock Front Door"},
    {"entity_id": "script.ai_goodnight", "name": "AI Action: Goodnight"},
]


def test_question_does_not_fire_open_garage_door():
    match = match_action("is the garage door open?", DOOR_ROWS)
    assert match.winner is None


def test_question_does_not_fire_unlock_front_door():
    match = match_action("did you unlock the front door?", DOOR_ROWS)
    assert match.winner is None


def test_question_without_question_mark_does_not_fire():
    match = match_action("is the garage door open", DOOR_ROWS)
    assert match.winner is None


def test_imperative_still_fires_on_door_rows():
    match = match_action("open the garage door", DOOR_ROWS)
    assert match.winner["entity_id"] == "script.ai_open_garage_door"


def test_extra_words_outside_action_name_do_not_fire():
    match = match_action("turn on the living room lights and music", ROWS)
    assert match.winner is None


def test_one_extra_word_blocks_firing_even_with_high_score():
    # Scores ~0.84 with a clear margin, but "garage" is not in the name.
    text = "turn on the living room lights garage"
    assert score(text, "AI Action: Living Room Lights On") >= 0.80
    assert match_action(text, ROWS).winner is None
