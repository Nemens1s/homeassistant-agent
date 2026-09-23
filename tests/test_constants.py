from app import constants


def test_script_prefixes():
    assert constants.AI_SCRIPT_PREFIX == "script.ai_"
    assert constants.AI_SCRIPT_PREFIX_ACTION == "script.ai_action"
    # narrow prefix is a stricter form of the broad one
    assert constants.AI_SCRIPT_PREFIX_ACTION.startswith(constants.AI_SCRIPT_PREFIX)
