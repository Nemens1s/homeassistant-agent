"""Export the local telemetry dataset to files usable for fine-tuning and evals.

Two subcommands:

    python -m app.telemetry.export fast-path [--labelled-only] --out F.jsonl
    python -m app.telemetry.export evals --labelled-only --out F.yaml

`fast-path` writes one JSON object per classified request (utterance, the menu
that was offered, the prediction, and the label if present) — the training set
for the fast-path classifier.  `evals` emits cases in the `tests/evals/cases.yaml`
format built from labelled requests, so a labelling session turns straight into
regression cases.
"""

from __future__ import annotations

import argparse
import json

import yaml

from app.telemetry.store import open_store


def _menu_items(conn, menu_hash):
    """Resolve a menu hash to its stored item list, or [] when unknown."""
    if not menu_hash:
        return []
    row = conn.execute(
        "SELECT items_json FROM menu_snapshots WHERE hash = ?", (menu_hash,)
    ).fetchone()
    if row is None or row[0] is None:
        return []
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return []


def export_fast_path(db: str, out: str, labelled_only: bool) -> int:
    """Write one JSONL line per classified request. Returns the row count."""
    conn = open_store(db)
    try:
        rows = conn.execute(
            "SELECT request_id, utterance, menu_hash, prediction, confidence, "
            "       accepted, latest_label_rating, agent_reference "
            "FROM fast_path_dataset "
            "ORDER BY ts_start"
        ).fetchall()

        written = 0
        with open(out, "w", encoding="utf-8") as handle:
            for row in rows:
                (request_id, utterance, menu_hash, prediction, confidence,
                 accepted, label_rating, agent_reference) = row
                if labelled_only and label_rating is None:
                    continue
                record = {
                    "request_id": request_id,
                    "utterance": utterance,
                    "menu": _menu_items(conn, menu_hash),
                    "prediction": prediction,
                    "confidence": confidence,
                    "accepted": bool(accepted),
                }
                if label_rating is not None:
                    record["label"] = label_rating
                if agent_reference is not None:
                    record["agent_reference"] = agent_reference
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
        return written
    finally:
        conn.close()


def _first_tool_call(conn, request_id):
    """The tool the agent actually called first, or (None, None)."""
    row = conn.execute(
        "SELECT tool, args_json FROM tool_calls "
        "WHERE request_id = ? ORDER BY seq LIMIT 1",
        (request_id,),
    ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _entity_id_from_args(args_json):
    if not args_json:
        return None
    try:
        args = json.loads(args_json)
    except (ValueError, TypeError):
        return None
    if isinstance(args, dict):
        return args.get("entity_id")
    return None


def _eval_case(conn, request_id, input_text, correct_tool, correct_entity_id):
    """Build one eval case, or None when there is nothing to assert.

    A correction (from a thumbs-down label) wins over what the agent actually
    did; otherwise the actual first tool call becomes the expectation.
    """
    actual_tool, actual_args = _first_tool_call(conn, request_id)
    expect_tool = correct_tool or actual_tool
    if expect_tool is None:
        return None

    expect_entity = correct_entity_id or _entity_id_from_args(actual_args)
    case = {
        "id": request_id,
        "prompt": input_text,
        "expect_tool": expect_tool,
    }
    if expect_entity:
        case["expect_params"] = {"entity_id": expect_entity}
    return case


def export_evals(db: str, out: str, labelled_only: bool) -> int:
    """Write eval cases (cases.yaml format) from labelled requests.

    Returns the case count. `labelled_only` is honoured for symmetry with the
    fast-path exporter; evals are only meaningful for labelled requests, so a
    False value still restricts to requests that carry a label.
    """
    conn = open_store(db)
    try:
        rows = conn.execute(
            "SELECT r.request_id, r.input_text, "
            "       l.rating, l.correct_tool, l.correct_entity_id "
            "FROM requests r "
            "JOIN labels l ON l.request_id = r.request_id "
            "GROUP BY r.request_id "
            "HAVING l.id = MAX(l.id) "
            "ORDER BY r.ts_start"
        ).fetchall()

        cases = []
        for row in rows:
            request_id, input_text, rating, correct_tool, correct_entity_id = row
            # Thumbs-down without a correction tells us the answer was wrong but
            # not what it should have been — nothing to assert.
            if rating is not None and rating < 0 and not correct_tool:
                continue
            case = _eval_case(conn, request_id, input_text, correct_tool, correct_entity_id)
            if case is not None:
                cases.append(case)

        with open(out, "w", encoding="utf-8") as handle:
            yaml.safe_dump(cases, handle, sort_keys=False, allow_unicode=True)
        return len(cases)
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.telemetry.export")
    parser.add_argument("--db", default=None, help="telemetry SQLite path (default: settings.telemetry_db_path)")
    sub = parser.add_subparsers(dest="command", required=True)

    fp = sub.add_parser("fast-path", help="export the fast-path training set as JSONL")
    fp.add_argument("--labelled-only", action="store_true")
    fp.add_argument("--out", required=True)

    ev = sub.add_parser("evals", help="export eval cases (cases.yaml format) from labels")
    ev.add_argument("--labelled-only", action="store_true")
    ev.add_argument("--out", required=True)

    args = parser.parse_args(argv)

    db = args.db
    if db is None:
        from app.config import load_settings

        db = load_settings().telemetry_db_path

    if args.command == "fast-path":
        count = export_fast_path(db, args.out, args.labelled_only)
    else:
        count = export_evals(db, args.out, args.labelled_only)
    print(f"wrote {count} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
