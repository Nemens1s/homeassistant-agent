"""The read-only eval results server (tests/evals/view.py)."""

import json
import socket
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from tests.evals.view import list_runs, main, make_server, parse_filename, safe_run_path, summarize_run

NEW = "2026-10-24T20-15-55_gemma-4-E4B-thinking_llamacpp.json"
OLD = "2026-09-22T10-26-50_openbmb-MiniCPM5-2B_llamacpp.json"
PARTIAL = "2026-10-25T08-00-00_partial_llamacpp.json"


@pytest.fixture
def results(tmp_path):
    new_run = {
        "model": "gemma-4-E4B",
        "provider": "llamacpp",
        "started_at": "2026-10-24T20:15:55",
        "score": "2/3",
        "elapsed": 30.0,
        "cases": {
            "a": {"passed": True, "prompt": "p", "duration_s": 1.2},
            "b": {"passed": False, "reason": "no tool call", "prompt": "p", "duration_s": 3.4},
            "c": {"passed": True, "tool_errors": ["feature_disabled"], "prompt": "p", "duration_s": 5.6},
        },
    }
    old_run = {
        "model": "MiniCPM5",
        "provider": "llamacpp",
        "score": "1/1",
        "elapsed": 99.0,
        "cases": {"a": {"passed": True}},
    }
    (tmp_path / NEW).write_text(json.dumps(new_run))
    (tmp_path / OLD).write_text(json.dumps(old_run))
    (tmp_path / PARTIAL).write_text('{"model": "x", "cases": {')
    (tmp_path / "notes.txt").write_text("not a run")
    return tmp_path


@pytest.fixture
def server(results):
    httpd = make_server(results, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_parse_filename_timestamp_and_label():
    assert parse_filename(NEW) == {"started_at": "2026-10-24T20:15:55", "label": "gemma-4-E4B-thinking"}


def test_parse_filename_model_with_underscores():
    assert parse_filename("2026-10-24T20-15-55_a_b_c_llamacpp.json")["label"] == "a_b_c"


def test_parse_filename_without_timestamp_falls_back_to_stem():
    assert parse_filename("mine.json") == {"started_at": None, "label": "mine"}


def test_list_runs_newest_first_and_skips_non_json(results):
    files = []
    for run in list_runs(results):
        files.append(run["file"])
    assert files == [PARTIAL, NEW, OLD]


def test_summary_counts(results):
    summary = summarize_run(results / NEW)
    assert (summary["cases"], summary["failed"], summary["passed_with_tool_errors"]) == (3, 1, 1)
    assert "error" not in summary


def test_old_file_summarizes_with_none_for_new_fields(results):
    summary = summarize_run(results / OLD)
    assert summary["started_at"] == "2026-09-22T10:26:50"
    assert summary["sampling_overrides"] is None


def test_truncated_file_is_flagged_not_fatal(results):
    summary = summarize_run(results / PARTIAL)
    assert summary["error"] == "invalid result file"
    assert summary["label"] == "partial"


def test_missing_dir_lists_nothing(tmp_path):
    assert list_runs(tmp_path / "nope") == []


@pytest.mark.parametrize(
    "name",
    ["../secret.json", "..%2Fsecret.json", ".hidden.json", "notes.txt", "sub/x.json", "missing.json"],
)
def test_safe_run_path_rejects(results, name):
    assert safe_run_path(results, name) is None


def test_safe_run_path_accepts_existing_run(results):
    assert safe_run_path(results, NEW) == results / NEW


def test_http_index_runs_and_run(server):
    assert b"<title>" in urlopen(server + "/").read()
    runs = json.loads(urlopen(server + "/api/runs").read())
    assert len(runs) == 3
    body = json.loads(urlopen(server + "/api/runs/" + NEW).read())
    assert len(body["cases"]) == 3


@pytest.mark.parametrize("path", ["/api/runs/..%2F..%2Fpyproject.toml", "/api/runs/notes.txt", "/nope"])
def test_http_404s(server, path):
    with pytest.raises(HTTPError) as err:
        urlopen(server + path)
    assert err.value.code == 404


def test_new_file_appears_without_restart(server, results):
    (results / "2026-10-26T09-00-00_new_llamacpp.json").write_text('{"cases": {}}')
    assert len(json.loads(urlopen(server + "/api/runs").read())) == 4


def test_main_port_in_use(capsys):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    try:
        assert main(["--port", str(taken.getsockname()[1]), "--no-open"]) == 1
    finally:
        taken.close()
    assert "in use" in capsys.readouterr().err
