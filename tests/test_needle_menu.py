from app.needle.menu import Menu, MenuItem, MenuProvider


class FakeRest:
    def __init__(self, states):
        self._states = states
        self.calls = 0

    async def list_states(self):
        self.calls += 1
        return self._states


STATES = [
    {"entity_id": "light.kitchen", "attributes": {}},
    {"entity_id": "automation.ai_goodnight",
     "attributes": {"friendly_name": "Goodnight"}},
    {"entity_id": "automation.morning", "attributes": {"friendly_name": "Morning"}},
    {"entity_id": "automation.ai_movie", "attributes": {"friendly_name": "Movie time"}},
]


async def test_menu_keeps_only_ai_automations_with_names():
    provider = MenuProvider(FakeRest(STATES), ttl_s=60)
    menu = await provider.get()
    ids = []
    for item in menu.items:
        ids.append(item.entity_id)
    assert ids == ["automation.ai_goodnight", "automation.ai_movie"]  # sorted, prefix-filtered
    assert menu.items[0].name == "Goodnight"


async def test_menu_is_cached_within_ttl():
    rest = FakeRest(STATES)
    provider = MenuProvider(rest, ttl_s=60)
    await provider.get()
    await provider.get()
    assert rest.calls == 1  # second call served from cache


async def test_signature_changes_when_id_set_changes():
    a = await MenuProvider(FakeRest(STATES), ttl_s=60).get()
    fewer = [STATES[0], STATES[1]]  # drop ai_movie
    b = await MenuProvider(FakeRest(fewer), ttl_s=60).get()
    assert a.signature != b.signature
