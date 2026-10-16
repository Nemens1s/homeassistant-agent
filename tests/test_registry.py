import json
import logging

import pytest
from pydantic import BaseModel

from app.config import Settings
from app.tools import registry
from app.tools.adapter import build_tools
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.context import ToolContext


class _Params(BaseModel):
    x: int = 0


async def _handler(params, ctx):
    return ToolResult.ok(params.x)


def _defn(name, tier=Tier.READ):
    return ToolDefinition(
        name=name, description="d", params_model=_Params, tier=tier, handler=_handler
    )


async def _true(ctx):
    return True


async def _false(ctx):
    return False


async def _raises(ctx):
    raise RuntimeError("HA down")


def _avail_defn(name, available=None):
    async def h(p, c):
        return ToolResult.ok(None)
    return ToolDefinition(
        name=name, description="d", params_model=BaseModel, tier=Tier.READ,
        handler=h, available=available,
    )


@pytest.fixture(autouse=True)
def clean_registry():
    registry._reset_for_tests()
    yield
    registry._reset_for_tests()


def test_register_and_get():
    d = _defn("alpha")
    registry.register(d)
    assert registry.get("alpha") is d
    assert registry.get("missing") is None


def test_duplicate_name_rejected():
    registry.register(_defn("alpha"))
    with pytest.raises(ValueError):
        registry.register(_defn("alpha"))


def test_tools_for_tier_filters_and_sorts():
    registry.register(_defn("zeta", Tier.READ))
    registry.register(_defn("beta", Tier.ACTION))
    registry.register(_defn("alpha", Tier.READ))
    read_only = registry.tools_for_tier(1)
    assert [t.name for t in read_only] == ["alpha", "zeta"]
    everything = registry.tools_for_tier(2)
    assert [t.name for t in everything] == ["alpha", "beta", "zeta"]


_HOME_SPECIFIC = ("roborock", "qrevo", "tallinn")


def test_no_home_specific_names_in_tool_text():
    registry._reset_for_tests()
    registry.load_all()
    offenders = []
    for t in registry.all_tools():
        text = (t.description + json.dumps(t.params_model.model_json_schema())).lower()
        for word in _HOME_SPECIFIC:
            if word in text:
                offenders.append((t.name, word))
    assert offenders == []


async def test_resolve_enabled_applies_probe_and_disabled_list():
    registry._reset_for_tests()
    registry.register(_avail_defn("always"))
    registry.register(_avail_defn("has_device", _true))
    registry.register(_avail_defn("no_device", _false))
    registry.register(_avail_defn("probe_fails", _raises))
    registry.register(_avail_defn("user_off", _true))
    ctx = ToolContext(settings=Settings(_env_file=None, disabled_tools=["user_off"]), rest=None)
    assert await registry.resolve_enabled(ctx) == {"always", "has_device", "probe_fails"}


async def test_refresh_enabled_updates_the_set_in_place():
    registry._reset_for_tests()
    home = {"vacuum": False}

    async def _has_vacuum(ctx):
        return home["vacuum"]

    registry.register(_avail_defn("always"))
    registry.register(_avail_defn("vacuum_tool", _has_vacuum))
    ctx = ToolContext(settings=Settings(_env_file=None), rest=None)
    enabled = await registry.resolve_enabled(ctx)
    shared = enabled  # the router and event runner hold this same object

    assert await registry.refresh_enabled(ctx, enabled) is False
    home["vacuum"] = True  # HA finished booting
    assert await registry.refresh_enabled(ctx, enabled) is True
    assert shared == {"always", "vacuum_tool"}


def test_build_tools_respects_enabled():
    registry._reset_for_tests()
    registry.register(_avail_defn("a"))
    registry.register(_avail_defn("b"))
    ctx = ToolContext(settings=Settings(_env_file=None), rest=None)
    names = []
    for t in build_tools(ctx, max_tier=1, enabled={"a"}):
        names.append(t.name)
    assert names == ["a"]


async def test_refresh_does_not_repeat_the_unavailable_log(caplog):
    registry._reset_for_tests()
    registry.register(_avail_defn("no_device", _false))
    ctx = ToolContext(settings=Settings(_env_file=None), rest=None)
    with caplog.at_level(logging.INFO, logger="agent.tools"):
        enabled = await registry.resolve_enabled(ctx)
        assert "tool=no_device not registered" in caplog.text
        caplog.clear()
        await registry.refresh_enabled(ctx, enabled)
    assert "not registered" not in caplog.text
