from types import SimpleNamespace

import httpx
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

    async def get_state(self, entity_id):
        self.calls += 1
        if entity_id != "light.lamp":
            request = httpx.Request("GET", "http://ha/api/states/" + entity_id)
            raise httpx.HTTPStatusError("404", request=request,
                                        response=httpx.Response(404, request=request))
        return {"entity_id": entity_id, "state": "on"}


def _request(messages, context=None, tools=TOOLS):
    tool_objs = []
    for name in sorted(tools):
        tool_objs.append(SimpleNamespace(name=name))
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
async def test_lookups_run_only_after_the_cheap_gates():
    rest = _Rest()
    mw = PreFetchMiddleware(rest=rest)
    assert await mw.decide("turn on light.lamp", TOOLS) is None  # a command
    assert await mw.decide("Show me the history of light.lamp today?", TOOLS) is None
    assert rest.calls == 0
    assert (await mw.decide("Is light.desk on?", TOOLS)) is None  # HA does not have it
    assert rest.calls == 1


@pytest.mark.asyncio
async def test_room_question_without_a_ready_socket_is_not_guessed():
    mw = PreFetchMiddleware(rest=_Rest(), ws=SimpleNamespace(connected=False))
    assert await mw.decide("What devices are in the bedroom?", TOOLS) is None


def test_synthetic_call_id_fits_openai_limit():
    from app.agent.middleware.prefetch_middleware import PreFetch, synthetic_read_call
    call = synthetic_read_call(PreFetch("get_weather", {}, "keyword"))
    assert len(call.tool_calls[0]["id"]) <= 40


def test_entity_id_with_a_deferral_word_still_prefetches():
    got = decide_prefetch("What is sensor.reminders_count?", TOOLS, AREAS, {"sensor.reminders_count"})
    assert got.tool == "get_entity_state"


@pytest.mark.asyncio
async def test_slow_ha_does_not_hold_the_request():
    import asyncio

    class _Slow:
        async def get_state(self, entity_id):
            await asyncio.sleep(10)

    mw = PreFetchMiddleware(rest=_Slow(), lookup_timeout_s=0.05)
    assert await mw.decide("What is light.lamp?", TOOLS) is None


def test_sync_call_passes_through():
    mw = PreFetchMiddleware()
    assert mw.wrap_model_call(_request([HumanMessage("is anyone home?")]), lambda r: "model") == "model"
