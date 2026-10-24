"""The per-case record the eval runner writes into each results file."""

from tests.evals.run import case_expectation, case_record, check, check_reached

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


# --- reached grading: did the run get there, in any order? -----------------

ACTIVITY = {"prompt": "Did it run today?", "expect_tool": "get_activity",
            "expect_params": {"range": "today"}}


def _trace(*calls, answer="Done."):
    trace = []
    for name, args in calls:
        trace.append({"calls": [{"name": name, "args": args}]})
        trace.append({"tool_result": '{"status":"ok"}'})
    if answer:
        trace.append({"answer": answer})
    return trace


def test_reached_when_expected_tool_comes_after_a_lookup():
    trace = _trace(("get_automations", {}), ("get_activity", {"range": "today"}))
    assert check_reached(ACTIVITY, trace) == (True, "")


def test_reached_checks_params_on_the_matching_call():
    trace = _trace(("get_activity", {"range": "last_hour"}))
    ok, reason = check_reached(ACTIVITY, trace)
    assert ok is False
    assert "range" in reason


def test_reached_any_matching_call_with_right_params_counts():
    trace = _trace(("get_activity", {"range": "last_hour"}), ("get_activity", {"range": "today"}))
    assert check_reached(ACTIVITY, trace) == (True, "")


def test_reached_fails_when_expected_tool_never_called():
    ok, reason = check_reached(ACTIVITY, _trace(("get_automations", {})))
    assert ok is False
    assert reason == "never called get_activity"


def test_reached_needs_a_final_answer():
    ok, reason = check_reached(ACTIVITY, _trace(("get_activity", {"range": "today"}), answer=""))
    assert (ok, reason) == (False, "no final answer")


def test_reached_fails_on_agent_error():
    trace = _trace(("get_activity", {"range": "today"}), answer="")
    trace.append({"error": "Connection error."})
    assert check_reached(ACTIVITY, trace) == (False, "agent error: Connection error.")


def test_reached_accepts_alternate_tool_without_param_check():
    case = {"prompt": "p", "expect_tool": "get_vacuum_state",
            "expect_any_tool": ["get_vacuum_state", "get_entity_state"]}
    trace = _trace(("get_entity_state", {"entity_id": "vacuum.x"}))
    assert check_reached(case, trace) == (True, "")


def test_forbidden_tool_anywhere_fails_reached():
    case = {"prompt": "p", "expect_tool": "schedule_on_state_change", "forbid_tools": ["trigger_action"]}
    trace = _trace(("trigger_action", {"entity_id": "script.x"}), ("schedule_on_state_change", {}))
    assert check_reached(case, trace) == (False, "called forbidden tool trigger_action")


def test_forbidden_tool_first_fails_direct():
    case = {"prompt": "p", "expect_tool": "schedule_on_state_change", "forbid_tools": ["trigger_action"]}
    ok, reason = check(case, [{"name": "trigger_action", "args": {}}])
    assert (ok, reason) == (False, "called forbidden tool trigger_action")


def test_banned_tool_anywhere_fails_reached():
    case = {"prompt": "p", "expect_not_tool": "trigger_action"}
    trace = _trace(("list_actions", {}), ("trigger_action", {}))
    assert check_reached(case, trace)[0] is False
    assert check_reached(case, _trace(("list_actions", {})))[0] is True


def test_record_carries_direct_grade():
    rec = case_record(CASE, True, "", 3.0, direct=(False, "called list_actions (args {})"))
    assert list(rec)[:3] == ["passed", "direct", "direct_reason"]
    assert rec["direct"] is False
    assert rec["direct_reason"] == "called list_actions (args {})"


def test_record_direct_pass_has_no_reason():
    rec = case_record(CASE, True, "", 3.0, direct=(True, ""))
    assert rec["direct"] is True
    assert "direct_reason" not in rec


# --- answering from the watched-entities home context ----------------------

VACUUM = {"prompt": "Is the vacuum running?", "expect_tool": "get_vacuum_state",
          "context_answer_ok": "vacuum.roborock_qrevo_s"}
WATCHED = ["vacuum.roborock_qrevo_s"]


def test_context_answer_passes_reached_when_entity_is_watched():
    trace = [{"thinking": "home context says docked", "answer": "No, it's docked."}]
    assert check_reached(VACUUM, trace, watched=WATCHED) == (True, "")


def test_context_answer_passes_direct_when_entity_is_watched():
    assert check(VACUUM, [], watched=WATCHED) == (True, "")


def test_context_answer_needs_the_entity_watched():
    trace = [{"answer": "No, it's docked."}]
    assert check_reached(VACUUM, trace, watched=[]) == (False, "never called get_vacuum_state")
    assert check(VACUUM, [], watched=[]) == (False, "no tool call")


def test_context_answer_still_needs_a_final_answer():
    assert check_reached(VACUUM, [{"thinking": "hm"}], watched=WATCHED) == (False, "no final answer")


def test_calling_the_tool_still_passes_with_context_available():
    trace = _trace(("get_vacuum_state", {}))
    assert check_reached(VACUUM, trace, watched=WATCHED) == (True, "")


def test_expectation_records_context_answer_ok():
    assert case_expectation(VACUUM)["context_answer_ok"] == "vacuum.roborock_qrevo_s"
