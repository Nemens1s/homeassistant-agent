"""The per-case record the eval runner writes into each results file."""

from tests.evals.run import case_expectation, case_record

CASE = {
    "id": "x",
    "prompt": "Is it on?",
    "expect_tool": "get_entity_state",
    "expect_params": {"entity_id": "light.a"},
    "min_tier": 1,
}


def test_expectation_copies_only_expect_keys():
    assert case_expectation(CASE) == {
        "expect_tool": "get_entity_state",
        "expect_params": {"entity_id": "light.a"},
    }


def test_expectation_any_and_not_tool():
    case = {"prompt": "p", "expect_any_tool": ["a", "b"], "expect_not_tool": "c"}
    assert case_expectation(case) == {"expect_any_tool": ["a", "b"], "expect_not_tool": "c"}


def test_record_plain_pass():
    rec = case_record(CASE, True, "", 2.345)
    assert rec == {
        "passed": True,
        "prompt": "Is it on?",
        "expected": case_expectation(CASE),
        "duration_s": 2.3,
    }
    assert list(rec) == ["passed", "prompt", "expected", "duration_s"]


def test_record_fail_keeps_reason():
    rec = case_record(CASE, False, "no tool call", 1.0)
    assert rec["passed"] is False
    assert rec["reason"] == "no tool call"


def test_record_verbose_with_tool_errors_and_trace():
    trace = [{"tool_result": "{}"}]
    rec = case_record(CASE, True, "", 14.04, tool_errors=["feature_disabled"], trace=trace)
    assert list(rec) == ["passed", "tool_errors", "prompt", "expected", "duration_s", "trace"]
    assert rec["tool_errors"] == ["feature_disabled"]
    assert rec["trace"] is trace
    assert rec["duration_s"] == 14.0


def test_record_empty_tool_errors_omitted():
    assert "tool_errors" not in case_record(CASE, True, "", 1.0, tool_errors=[])
