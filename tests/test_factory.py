from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.factory import (
    build_agent,
    build_system_prompt,
    timestamped_system,
    trim_history,
)
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
    assert build_system_prompt(s, tmp_path) == "Base prompt."


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


def test_build_agent_compiles_with_read_tools():
    registry._reset_for_tests()
    settings = Settings(_env_file=None, max_tier=1)
    ctx = ToolContext(settings=settings, rest=None, ws=None)
    agent = build_agent(settings, ctx)
    assert agent is not None
    tier1 = {t.name for t in registry.tools_for_tier(1)}
    tier2 = {t.name for t in registry.tools_for_tier(2)}
    assert tier1  # registry loaded
    assert tier1 == tier2  # iteration 1: nothing above READ exists
    assert {"get_entity_state", "list_entities", "load_skill"} <= tier1
    registry._reset_for_tests()
