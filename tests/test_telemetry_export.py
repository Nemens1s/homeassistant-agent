import json

import yaml

from app.telemetry.store import open_store
from app.telemetry.export import export_fast_path, export_evals


def _seed_fast_path(db):
    conn = open_store(db)
    conn.execute(
        "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
        "VALUES ('r1','2026-09-17T00:00:00Z','ui','goodnight','fast_path','ok')"
    )
    conn.execute(
        "INSERT INTO menu_snapshots (hash, items_json, first_seen) "
        "VALUES ('m1', ?, '2026-09-17T00:00:00Z')",
        (json.dumps([{"entity_id": "automation.ai_action_night", "name": "Night", "description": "goodnight"}]),),
    )
    conn.execute(
        "INSERT INTO fast_path_decisions (request_id, backend, menu_hash, entity_id, confidence, threshold, accepted) "
        "VALUES ('r1','fake','m1','automation.ai_action_night',0.9,0.0,1)"
    )
    conn.commit()
    return conn


def test_export_fast_path_jsonl(tmp_path):
    db = str(tmp_path / "t.sqlite")
    conn = _seed_fast_path(db)
    conn.close()
    out = tmp_path / "fp.jsonl"
    export_fast_path(db, str(out), labelled_only=False)
    line = json.loads(out.read_text().splitlines()[0])
    assert line["utterance"] == "goodnight"
    assert line["prediction"] == "automation.ai_action_night"
    assert line["menu"][0]["entity_id"] == "automation.ai_action_night"


def test_export_fast_path_labelled_only_filters(tmp_path):
    db = str(tmp_path / "t.sqlite")
    conn = _seed_fast_path(db)
    conn.commit()
    conn.close()
    out = tmp_path / "fp.jsonl"
    export_fast_path(db, str(out), labelled_only=True)
    # r1 has no label → nothing exported.
    assert out.read_text().strip() == ""


def test_export_evals_yaml_loads_and_uses_correction(tmp_path):
    db = str(tmp_path / "t.sqlite")
    conn = open_store(db)
    # A labelled request corrected to a different tool.
    conn.execute(
        "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
        "VALUES ('r1','2026-09-17T00:00:00Z','ui','what lights do I have','agent','ok')"
    )
    conn.execute(
        "INSERT INTO tool_calls (span_id, request_id, seq, tool, args_json, status) "
        "VALUES ('s1','r1',0,'get_entity_state','{\"entity_id\":\"light.x\"}','ok')"
    )
    conn.execute(
        "INSERT INTO labels (request_id, ts, source, rating, correct_tool) "
        "VALUES ('r1','2026-09-17T00:01:00Z','ui',-1,'list_entities')"
    )
    conn.commit()
    conn.close()

    out = tmp_path / "cases.yaml"
    export_evals(db, str(out), labelled_only=True)

    # Must parse with the same loader tests/evals/run.py uses.
    cases = yaml.safe_load(out.read_text())
    assert isinstance(cases, list) and len(cases) == 1
    case = cases[0]
    assert case["prompt"] == "what lights do I have"
    assert case["expect_tool"] == "list_entities"  # correction wins over actual call
    assert "id" in case
