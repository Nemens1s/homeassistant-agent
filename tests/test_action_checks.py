from app.config import Settings
from app.tools.context import ToolContext
from app.tools.helpers.actions import check_action


class FakeRest:
    async def get_script_config(self, object_id):
        return {"fields": {"room": {"required": True,
                                    "selector": {"select": {"options": ["hall"]}}}}}


def _ctx(**overrides):
    return ToolContext(settings=Settings(_env_file=None, **overrides), rest=FakeRest(), ws=None)


async def test_valid_script_with_params_passes():
    assert await check_action(_ctx(), "script.ai_action_send_vacuum", {"room": "hall"}) is None


async def test_notify_script_is_refused():
    result = await check_action(_ctx(), "script.ai_action_notify", {})
    assert result.error_code == "use_notify_user"


async def test_non_ai_action_is_refused():
    result = await check_action(_ctx(), "script.backup", {})
    assert result.error_code == "not_ai_controllable"


async def test_wrong_domain_is_refused():
    result = await check_action(_ctx(), "light.kitchen", {})
    assert result.error_code == "invalid_params"


async def test_domain_must_be_allowed():
    result = await check_action(_ctx(allowed_domains=["light"]), "script.ai_action_x", {})
    assert result.error_code == "domain_not_allowed"


async def test_automation_with_params_rejected():
    result = await check_action(_ctx(), "automation.ai_night", {"room": "hall"})
    assert result.error_code == "invalid_params"


async def test_missing_script_param_rejected():
    result = await check_action(_ctx(), "script.ai_action_send_vacuum", {})
    assert result.error_code == "invalid_params"
