import json

from app.tools.base import Tier, ToolResult, bound_rows


def test_tier_values():
    assert Tier.READ == 1
    assert Tier.ACTION == 2


def test_tool_result_ok_json_is_compact():
    out = ToolResult.ok({"a": 1}).to_json()
    assert out == '{"status":"ok","data":{"a":1}}'


def test_tool_result_error_shape():
    out = json.loads(ToolResult.error("entity_not_found", "no such entity").to_json())
    assert out["status"] == "error"
    assert out["error"] == {"code": "entity_not_found", "message": "no such entity"}


def test_tool_result_error_with_data():
    out = json.loads(
        ToolResult.error("entity_not_found", "nope", data={"did_you_mean": ["light.kitchen"]}).to_json()
    )
    assert out["data"] == {"did_you_mean": ["light.kitchen"]}


def test_bound_rows_under_limit():
    env = bound_rows([1, 2], max_rows=5)
    assert env == {"rows": [1, 2], "total": 2}


def test_bound_rows_truncates_and_reports_total():
    env = bound_rows(list(range(10)), max_rows=3, hint="narrow it")
    assert env["rows"] == [0, 1, 2]
    assert env["total"] == 10
    assert env["truncated"] is True
    assert env["hint"] == "narrow it"
