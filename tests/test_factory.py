from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.middleware.context_window_middleware import timestamped_system, trim_history
from app.agent.factory import build_agent, build_system_prompt
from app.agent.middleware.loop_guard_reset_middleware import LoopGuardResetMiddleware
from app.agent.middleware.tool_subset_middleware import ToolSubsetMiddleware, latest_human_text
from app.tools.adapter import LoopGuard
from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


def test_build_system_prompt_lists_skills(tmp_path):
    (tmp_path / "a_skill.md").write_text(
        "---\nname: a_skill\ndescription: does a thing\n---\nbody"
    )
    s = Settings(_env_file=None, system_prompt="Base prompt.")
    prompt = build_system_prompt(s, tmp_path)
    assert prompt.startswith("Base prompt.")
    assert "- a_skill: does a thing" in prompt


def test_build_system_prompt_no_skills(tmp_path):
    s = Settings(_env_file=None, system_prompt="Base prompt.")
    prompt = build_system_prompt(s, tmp_path)
    assert prompt.startswith("Base prompt.")
    assert "CHOOSING A TOOL" in prompt  # routing block always appended
    assert "SKILL PLAYBOOKS" not in prompt


def test_timestamped_system_injects_current_time():
    msg = timestamped_system("Base.")
    assert isinstance(msg, SystemMessage)
    assert msg.content.startswith("Base.")
    assert f"Current time: {datetime.now().year}-" in msg.content


def test_trim_history_keeps_recent_and_starts_on_human():
    messages = []
    for i in range(50):
        messages.append(HumanMessage(f"question {i} " + "x" * 200))
        messages.append(AIMessage(f"answer {i} " + "y" * 200))
    trimmed = trim_history(messages, max_tokens=500)
    assert 0 < len(trimmed) < len(messages)
    assert trimmed[0].type == "human"
    assert trimmed[-1].content == messages[-1].content


def test_trim_history_never_returns_empty():
    huge = [HumanMessage("z" * 100000)]
    trimmed = trim_history(huge, max_tokens=10)
    assert trimmed == huge[-1:]


def test_loop_guard_reset_middleware_clears_guard_on_before_agent():
    guard = LoopGuard()
    guard._last = {"a": ("some_tool", "{}")}
    middleware = LoopGuardResetMiddleware(guard)
    middleware.before_agent(None, None)
    assert guard._last == {}


def test_system_prompt_mentions_control_only_at_tier2(tmp_path):
    s1 = Settings(_env_file=None, system_prompt="Base.", max_tier=1)
    s2 = Settings(_env_file=None, system_prompt="Base.", max_tier=2)
    p1 = build_system_prompt(s1, tmp_path)
    p2 = build_system_prompt(s2, tmp_path)
    assert "automation.ai_" not in p1  # no action guidance at tier 1
    assert "automation.ai_" in p2
    assert "trigger" in p2.lower()


def test_build_agent_compiles_with_read_tools():
    registry._reset_for_tests()
    settings = Settings(_env_file=None, max_tier=1)
    ctx = ToolContext(settings=settings, rest=None, ws=None)
    agent = build_agent(settings, ctx)
    assert agent is not None
    tier1 = {t.name for t in registry.tools_for_tier(1)}
    tier2 = {t.name for t in registry.tools_for_tier(2)}
    assert tier1  # registry loaded
    assert tier2 - tier1 == {"trigger_automation"}
    assert {"get_entity_state", "list_entities", "load_skill"} <= tier1
    registry._reset_for_tests()


def test_latest_human_text_picks_most_recent_human():
    msgs = [
        HumanMessage("first question"),
        AIMessage("some answer"),
        HumanMessage("second question"),
        ToolMessage("tool output", tool_call_id="x"),
    ]
    assert latest_human_text(msgs) == "second question"


def test_latest_human_text_handles_multimodal_content():
    msgs = [HumanMessage(content=[{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}])]
    assert latest_human_text(msgs) == "hello world"


def test_latest_human_text_empty_when_no_human():
    assert latest_human_text([AIMessage("only ai")]) == ""


def _model_request(tools, messages):
    from langchain.agents.middleware import ModelRequest

    return ModelRequest(
        model=object(), messages=messages, system_message=None, tool_choice=None,
        tools=tools, response_format=None, state={}, runtime=None,
    )


def test_tool_subset_middleware_trims_to_message():
    class T:
        def __init__(self, name):
            self.name = name

    tools = [T("list_entities"), T("get_weather"), T("get_vacuum_state"), T("trigger_automation")]
    captured = {}

    def handler(req):
        captured["names"] = {t.name for t in req.tools}
        return AIMessage("ok")

    req = _model_request(tools, [HumanMessage("what's the weather?")])
    ToolSubsetMiddleware().wrap_model_call(req, handler)
    assert "get_weather" in captured["names"]
    assert "get_vacuum_state" not in captured["names"]  # trimmed
    assert "list_entities" in captured["names"]  # core survives


def test_tool_subsetting_flag_toggles_middleware():
    from app.agent.factory import build_middleware
    from app.tools.adapter import LoopGuard

    def mws(enabled):
        s = Settings(_env_file=None, enable_tool_subsetting=enabled)
        return build_middleware(s, LoopGuard(), "prompt", 1024)

    assert any(isinstance(m, ToolSubsetMiddleware) for m in mws(True))
    assert not any(isinstance(m, ToolSubsetMiddleware) for m in mws(False))
