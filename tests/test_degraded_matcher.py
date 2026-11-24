import pytest

from app.degraded.matcher import is_question, match_action, normalise, score

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


# (text, is a question?) — punctuated and unpunctuated forms of each, since
# voice STT may or may not add the "?" (Parakeet adds it, Zipformer never does).
QUESTION_TABLE = [
    ("can you turn the lights on", False),
    ("Can you turn the lights on?", False),
    ("could you turn the lights on", False),
    ("Could you turn the lights on?", False),
    ("would you turn the lights on", False),
    ("Would you turn the lights on?", False),
    ("will you turn the lights on", False),
    ("Will you turn the lights on?", False),
    ("can you please turn the lights on", False),
    ("Can you please turn the lights on?", False),
    ("please can you turn the lights on", False),
    ("Please, can you turn the lights on?", False),
    ("can the lights be turned on", True),
    ("can the lights be turned on?", True),
    ("could it be the garage door", True),
    ("could it be the garage door?", True),
    ("will it rain", True),
    ("will it rain?", True),
    ("is the garage door open", True),
    ("is the garage door open?", True),
    ("are the lights on", True),
    ("are the lights on?", True),
    ("did you unlock the front door", True),
    ("did you unlock the front door?", True),
    ("does the vacuum work", True),
    ("does the vacuum work?", True),
    ("do the lights work", True),
    ("do the lights work?", True),
    ("was the door open", True),
    ("was the door open?", True),
    ("were the lights on", True),
    ("were the lights on?", True),
    ("what is on", True),
    ("what is on?", True),
    ("should you turn the lights on", True),
    ("turn the lights on", False),
    ("turn the lights on?", True),
]


@pytest.mark.parametrize("text, expected", QUESTION_TABLE)
def test_is_question_table(text, expected):
    assert is_question(text) is expected


LIGHT_ROWS = [
    {"entity_id": "script.ai_action_lights_on", "name": "AI Action: Lights On"},
    {"entity_id": "script.ai_action_lights_off", "name": "AI Action: Lights Off"},
    {"entity_id": "script.ai_action_stop_vacuum", "name": "AI Action: Stop Vacuum"},
]


@pytest.mark.parametrize("text", ["can you turn the lights on", "Can you turn the lights on?"])
def test_can_you_turn_the_lights_on_fires_lights_on(text):
    assert match_action(text, LIGHT_ROWS).winner["entity_id"] == "script.ai_action_lights_on"


@pytest.mark.parametrize("text", [
    "can you turn the living room lights on",
    "Can you turn the living room lights on?",
    "could you please turn on the living room lights",
    "Would you turn on the living room lights?",
    "will you turn the living room lights on",
])
def test_polite_command_fires_lights_on(text):
    match = match_action(text, ROWS)
    assert match.winner["entity_id"] == "script.ai_action_lights_on"


def test_modal_question_about_the_lights_does_not_fire():
    assert match_action("can the lights be turned on?", LIGHT_ROWS).winner is None
    assert match_action("can the lights be turned on", LIGHT_ROWS).winner is None


def test_polite_question_outside_the_action_name_does_not_fire():
    rows = ROWS + [{"entity_id": "script.ai_garage_camera", "name": "AI Action: Garage Camera Snapshot"}]
    assert match_action("can you see the garage camera?", rows).winner is None
