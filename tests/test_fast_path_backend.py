import pytest
from app.fast_path.backend import Decision, FakeBackend
from app.needle.menu import Menu

_EMPTY_MENU = Menu(items=(), signature="")

@pytest.mark.asyncio
async def test_fakebackend_scripted_decision_and_name():
    b = FakeBackend(decision=Decision(entity_id="automation.ai_action_night", confidence=0.9))
    assert b.name == "fake"
    d = await b.classify("night", _EMPTY_MENU)
    assert d == Decision(entity_id="automation.ai_action_night", confidence=0.9)

@pytest.mark.asyncio
async def test_fakebackend_by_message_and_default_noop():
    b = FakeBackend(by_message={"night": Decision("automation.ai_action_night", 0.9)})
    assert (await b.classify("night", _EMPTY_MENU)).entity_id == "automation.ai_action_night"
    assert await b.classify("weather?", _EMPTY_MENU) == Decision(entity_id=None, confidence=0.0)
