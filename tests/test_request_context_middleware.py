from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.middleware import request_context_middleware as rcm
from app.agent.middleware.request_context_middleware import (
    RequestContextMiddleware,
    request_context_line,
    with_request_context,
)
from app.agent.run_scope import RequestFacts, RunScope

_AREA = RequestFacts(area_id="living_room", area_name="Living Room")
_PERSON = RequestFacts(person_name="Ilja", person_entity_id="person.ilja")
_BOTH = RequestFacts("living_room", "Living Room", "Ilja", "person.ilja")


def _model_request(messages):
    return ModelRequest(
        model=object(), messages=messages, system_message=None, tool_choice=None,
        tools=[], response_format=None, state={}, runtime=None,
    )


def test_line_area_and_person():
    assert request_context_line(_BOTH) == (
        'Request context: the request came from a device in area "Living Room" '
        "(area_id: living_room); speaker: Ilja (person.ilja). "
        "If the user names a room, use that room instead."
    )


def test_line_area_only():
    assert request_context_line(_AREA) == (
        'Request context: the request came from a device in area "Living Room" '
        "(area_id: living_room). If the user names a room, use that room instead."
    )


def test_line_person_only():
    assert request_context_line(_PERSON) == "Request context: speaker: Ilja (person.ilja)."


def test_line_empty_is_none():
    assert request_context_line(RequestFacts()) is None


def test_appends_line_without_mutating_original():
    msgs = [HumanMessage("dim the lights to 15%")]
    result = with_request_context(msgs, "X")
    assert msgs[0].content == "dim the lights to 15%"
    assert result[0].content == "dim the lights to 15%\n\nX"


def test_line_only_on_latest_user_turn():
    msgs = [HumanMessage("a"), AIMessage("b"), HumanMessage("c")]
    result = with_request_context(msgs, "X")
    assert result[0].content == "a"
    assert result[1].content == "b"
    assert result[2].content == "c\n\nX"


def test_no_user_turn_is_none():
    assert with_request_context([AIMessage("b")], "X") is None


def test_middleware_passes_through_when_no_facts(monkeypatch):
    monkeypatch.setattr(rcm, "scope_from_running_graph", lambda: RunScope())
    request = _model_request([HumanMessage("hi")])
    seen = []
    RequestContextMiddleware().wrap_model_call(request, lambda r: seen.append(r) or AIMessage("ok"))
    assert seen[0] is request


async def test_middleware_appends_when_facts(monkeypatch):
    facts = RequestFacts("bedroom", "Bedroom", None, None)
    monkeypatch.setattr(rcm, "scope_from_running_graph", lambda: RunScope(request=facts))
    seen = []

    async def handler(r):
        seen.append(r)
        return AIMessage("ok")

    await RequestContextMiddleware().awrap_model_call(_model_request([HumanMessage("hi")]), handler)
    assert seen[0].messages[0].content.startswith("hi\n\nRequest context: ")
    assert '"Bedroom" (area_id: bedroom)' in seen[0].messages[0].content
