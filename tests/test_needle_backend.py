from app.needle.backend import Decision, FakeBackend
from app.needle.menu import Menu, MenuItem

MENU = Menu(items=(MenuItem("automation.ai_goodnight", "Goodnight"),), signature="x")


async def test_fake_backend_returns_scripted_decision():
    backend = FakeBackend(Decision("automation.ai_goodnight", 0.9))
    d = await backend.classify("goodnight", MENU)
    assert d.entity_id == "automation.ai_goodnight"
    assert d.confidence == 0.9


async def test_fake_backend_per_message_lookup_and_default():
    backend = FakeBackend(by_message={"movie": Decision("automation.ai_movie", 0.95)})
    assert (await backend.classify("movie", MENU)).entity_id == "automation.ai_movie"
    fallback = await backend.classify("unknown", MENU)
    assert fallback.entity_id is None and fallback.confidence == 0.0
