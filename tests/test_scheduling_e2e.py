"""Schedule through the real agent graph, fire through the real listener and
runner: the saved action runs with no model call at fire time."""

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent import factory as factory_mod
from app.config import Settings
from app.events.listener import EventListener
from app.events.runner import EventRunner
from app.memory.store import NoteStore
from app.tools import registry
from app.tools.context import ToolContext

VACUUM = "vacuum.roborock_qrevo_s"


class CountingModel(FakeMessagesListChatModel):
    calls: int = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def _agenerate(self, *args, **kwargs):
        self.calls += 1
        return await super()._agenerate(*args, **kwargs)


class FakeRest:
    def __init__(self):
        self.calls = []

    async def get_state(self, entity_id):
        if entity_id == VACUUM:
            return {"entity_id": entity_id, "state": "docked", "attributes": {}}
        if entity_id == "input_boolean.ai_triggered_actions":
            return {"entity_id": entity_id, "state": "on", "attributes": {}}
        return {"entity_id": entity_id, "state": "off",
                "attributes": {"friendly_name": "Stop Vacuum"}}

    async def list_states(self):
        return [{"entity_id": "script.ai_action_stop_vacuum", "state": "off",
                 "attributes": {"friendly_name": "Stop Vacuum"}}]

    async def get_script_config(self, object_id):
        return {}

    async def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, data))
        return []


async def test_scheduled_stop_runs_without_model_at_fire_time(monkeypatch):
    registry._reset_for_tests()
    settings = Settings(_env_file=None, max_tier=2, watched_entities=[VACUUM])
    store = NoteStore("")
    rest = FakeRest()
    ctx = ToolContext(settings=settings, rest=rest, ws=None, notes=store)
    model = CountingModel(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "schedule_on_state_change",
            "args": {"entity_id": VACUUM, "to_state": "cleaning",
                     "instruction": "Stop the vacuum.",
                     "action_entity_id": "script.ai_action_stop_vacuum"},
            "id": "c1",
        }]),
        AIMessage(content="Scheduled: I'll stop the vacuum once it starts cleaning."),
    ])
    monkeypatch.setattr(factory_mod, "build_llm", lambda s: model)
    agent = factory_mod.build_agent(settings, ctx)

    await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Once the vacuum starts, stop it"}]},
        config={"configurable": {"thread_id": "chat"}, "recursion_limit": 15},
    )
    assert model.calls == 2
    assert len(store.list_recent()) == 1

    runner = EventRunner(lambda: agent, settings, ctx)
    listener = EventListener(None, store, runner, settings, rest)
    await listener.handle_event({"variables": {"trigger": {
        "entity_id": VACUUM, "from_state": {"state": "docked"}, "to_state": {"state": "cleaning"},
    }}})

    assert model.calls == 2  # no model call when the task fired
    assert ("script", "ai_action_stop_vacuum", {}) in rest.calls
    note = store.list_recent()[0]
    assert (note.status, note.outcome) == ("fired", "done")
    store.close()
    registry._reset_for_tests()
