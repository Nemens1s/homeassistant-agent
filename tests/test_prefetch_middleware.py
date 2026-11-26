from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.middleware.prefetch_middleware import (
    PREFETCH_MODEL,
    PREFETCH_PREFIX,
    PreFetchMiddleware,
    decide_prefetch,
)
from app.agent.middleware.telemetry_middleware import synthetic_path

AREAS = [{"area_id": "bedroom", "name": "Bedroom", "aliases": []}]
TOOLS = {"get_entity_state", "search_entities", "list_devices", "get_areas",
         "get_person_locations", "get_weather"}


@pytest.mark.parametrize("text, expected", [
    ("What is light.lamp?", ("get_entity_state", {"entity_id": "light.lamp"})),
    ("Are light.lamp and light.desk on?", None),
    ("Is light.lamp or light.desk on", None),
    ("what's in the bedroom", ("list_devices", {"area": "Bedroom"})),
    ("what rooms do I have", ("get_areas", {})),
    ("is anyone home?", ("get_person_locations", {})),
    ("is it raining and is anyone home?", None),
    ("remind me when someone gets home", None),
    ("Is the vacuum running?", None),  # get_vacuum_state is not enabled
])
def test_decide_prefetch_table(text, expected):
    got = decide_prefetch(text, TOOLS, AREAS, {"light.lamp", "light.desk"})
    if expected is None:
        assert got is None
    else:
        assert (got.tool, got.args) == expected


class _Rest:
    def __init__(self):
        self.calls = 0

    async def list_states(self):
        self.calls += 1
        return [{"entity_id": "light.lamp"}]


def _request(messages, context=None, tools=TOOLS):
    tool_objs = [SimpleNamespace(name=n) for n in sorted(tools)]
    return SimpleNamespace(messages=messages, tools=tool_objs,
                           runtime=SimpleNamespace(context=context))


async def _fail_handler(request):
    raise AssertionError("handler must not be called")


@pytest.mark.asyncio
async def test_step_one_emits_synthetic_call_without_the_model():
    mw = PreFetchMiddleware(rest=_Rest())
    resp = await mw.awrap_model_call(_request([HumanMessage("What is light.lamp?")]), _fail_handler)
    assert isinstance(resp, AIMessage)
    call = resp.tool_calls[0]
    assert call["name"] == "get_entity_state"
    assert call["args"] == {"entity_id": "light.lamp"}
    assert call["id"].startswith(PREFETCH_PREFIX)
    assert resp.response_metadata["model_name"] == PREFETCH_MODEL
    assert synthetic_path(PREFETCH_MODEL) == "prefetch"


@pytest.mark.asyncio
async def test_step_two_calls_the_model_with_the_tool_result():
    mw = PreFetchMiddleware(rest=_Rest())
    call_id = PREFETCH_PREFIX + "abc"
    messages = [
        HumanMessage("is anyone home?"),
        AIMessage(content="", tool_calls=[{"name": "get_person_locations", "args": {}, "id": call_id}]),
        ToolMessage(content='{"status":"ok"}', tool_call_id=call_id),
    ]
    seen = []

    async def handler(request):
        seen.append(request.messages[-1])
        return AIMessage(content="Everyone is out.")

    resp = await mw.awrap_model_call(_request(messages), handler)
    assert resp.content == "Everyone is out."
    assert isinstance(seen[0], ToolMessage)


@pytest.mark.asyncio
async def test_event_runs_are_untouched():
    mw = PreFetchMiddleware(rest=_Rest())
    called = []

    async def handler(request):
        called.append(True)
        return AIMessage(content="ok")

    await mw.awrap_model_call(_request([HumanMessage("is anyone home?")], context={"fast_path": False}),
                              handler)
    assert called == [True]


@pytest.mark.asyncio
async def test_tool_missing_after_subsetting_falls_through():
    mw = PreFetchMiddleware(rest=_Rest())
    called = []

    async def handler(request):
        called.append(True)
        return AIMessage(content="ok")

    await mw.awrap_model_call(_request([HumanMessage("is anyone home?")], tools={"search_entities"}),
                              handler)
    assert called == [True]


@pytest.mark.asyncio
async def test_state_list_is_cached_and_failure_falls_through():
    rest = _Rest()
    mw = PreFetchMiddleware(rest=rest)
    await mw.decide("What is light.lamp?", TOOLS)
    await mw.decide("What is light.lamp?", TOOLS)
    assert rest.calls == 1

    class _Broken:
        async def list_states(self):
            raise RuntimeError("down")

    assert await PreFetchMiddleware(rest=_Broken()).decide("What is light.lamp?", TOOLS) is None
