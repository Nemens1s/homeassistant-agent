import pytest
from pydantic import BaseModel

from app.tools import registry
from app.tools.base import Tier, ToolDefinition, ToolResult


class _Params(BaseModel):
    x: int = 0


async def _handler(params, ctx):
    return ToolResult.ok(params.x)


def _defn(name, tier=Tier.READ):
    return ToolDefinition(
        name=name, description="d", params_model=_Params, tier=tier, handler=_handler
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
