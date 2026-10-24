# Eval Results Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only local page that compares 2–3 eval runs case by case, with failure filters, per-case traces and per-case durations.

**Architecture:** `tests/evals/run.py` records `duration_s`, `prompt` and `expected` on every case, plus `started_at` on the run. `tests/evals/view.py` is a stdlib `http.server` that lists and serves the JSON files in `.evals/`. `tests/evals/viewer/index.html` is a single vanilla-JS page (comparison grid, filters, detail panel) that fetches from it.

**Tech Stack:** Python 3.14 stdlib (`http.server`, `json`, `argparse`, `webbrowser`), plain HTML/CSS/JS, pytest.

**Spec:** `/Users/ilniko/Desktop/Obsidian/Big Beautiful Brain/Claude Specs/homeassistant-agent/2026-10-24-eval-results-viewer-design.md`

## Global Constraints

- Branch: `fix/eval-runner-parity` (already checked out). No worktrees. One PR together with the eval context fix.
- No new dependencies: stdlib on the server, no Node/build step, no CDN scripts on the page.
- Nothing under `app/`, `frontend/` or `run.sh` changes. The Dockerfile copies only those, so the viewer never ships.
- Read-only: the server only answers GET. It never writes, deletes or renames.
- Every field added by this work is optional when reading: old result files must still render.
- The server binds `127.0.0.1` only.
- Max 3 runs selected at once.
- Python style per CLAUDE.md: plain `for` loops over comprehensions, explicit steps. Run with `uv run`.
- `uv run pytest -q` stays green and warning-free; `uv run ruff check tests/` is clean.

## Review Focus

1. **Model output containing HTML** (`<b>`, `<script>`, `&lt;` in thinking, answers or args) must render as literal text. Every string goes into the DOM via `textContent`, never `innerHTML`. Manual check in Task 4.
2. **Old result files** (no `duration_s`, `prompt`, `expected` or `started_at`; non-verbose files with no `trace`) list and render with "—" for missing values. Test in Task 2, manual check in Task 3.
3. **A result file that is half-written or not JSON** (a run in progress, a stray file) appears in the list flagged as an error, and the other runs still load. Test in Task 2.
4. **Port already in use** gives a one-line message naming `--port`, not a traceback. Test in Task 2.
5. **Odd filenames** (no timestamp, extra underscores in the model, `..`, `%2F`, non-`.json`): listing never crashes, the label falls back to the stem, and unsafe names get 404. Test in Task 2.

---

### Task 1: Record per-case duration, prompt and expectation in `run.py`

**Files:**
- Modify: `tests/evals/run.py` (`_run_cases`, plus two new module-level functions)
- Test: `tests/test_eval_run_record.py`

**Interfaces:**
- Produces:
  - `case_expectation(case: dict) -> dict`: copies whichever of `expect_tool`, `expect_any_tool`, `expect_not_tool`, `expect_params` the case defines. No other keys.
  - `case_record(case: dict, ok: bool, reason: str, duration_s: float, tool_errors: list[str] | None = None, trace: list[dict] | None = None) -> dict`: key order `passed`, `reason` (only if non-empty), `tool_errors` (only if non-empty), `prompt`, `expected`, `duration_s` (rounded to 1 decimal), `trace` (only if not None).
  - Result file top level gains `started_at` (ISO, seconds precision, local time, taken when `_run_cases` starts).

- [ ] **Step 1: Write the failing tests**

```python
from tests.evals.run import case_expectation, case_record

CASE = {"id": "x", "prompt": "Is it on?", "expect_tool": "get_entity_state",
        "expect_params": {"entity_id": "light.a"}, "min_tier": 1}

def test_expectation_copies_only_expect_keys():
    assert case_expectation(CASE) == {"expect_tool": "get_entity_state",
                                      "expect_params": {"entity_id": "light.a"}}

def test_expectation_any_and_not_tool():
    case = {"prompt": "p", "expect_any_tool": ["a", "b"], "expect_not_tool": "c"}
    assert case_expectation(case) == {"expect_any_tool": ["a", "b"], "expect_not_tool": "c"}

def test_record_plain_pass():
    rec = case_record(CASE, True, "", 2.345)
    assert rec == {"passed": True, "prompt": "Is it on?",
                   "expected": case_expectation(CASE), "duration_s": 2.3}
    assert list(rec) == ["passed", "prompt", "expected", "duration_s"]

def test_record_fail_keeps_reason():
    rec = case_record(CASE, False, "no tool call", 1.0)
    assert rec["passed"] is False and rec["reason"] == "no tool call"

def test_record_verbose_with_tool_errors_and_trace():
    trace = [{"tool_result": "{}"}]
    rec = case_record(CASE, True, "", 14.04, tool_errors=["feature_disabled"], trace=trace)
    assert list(rec) == ["passed", "tool_errors", "prompt", "expected", "duration_s", "trace"]
    assert rec["tool_errors"] == ["feature_disabled"] and rec["trace"] is trace
    assert rec["duration_s"] == 14.0

def test_record_empty_tool_errors_omitted():
    assert "tool_errors" not in case_record(CASE, True, "", 1.0, tool_errors=[])
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_eval_run_record.py -q`
Expected: FAIL with `ImportError: cannot import name 'case_expectation'`

- [ ] **Step 3: Implement `case_expectation` and `case_record` in `tests/evals/run.py`, and use them in `_run_cases`**

- Measure `start = time.monotonic()` immediately before `_agent_trace(...)` / `bound.ainvoke(...)` and `duration = time.monotonic() - start` right after, so the case delay and skipped cases are excluded.
- Both branches build `case_data` with `case_record(...)`. Verbose passes `tool_errors` and `trace`; plain passes neither. Remove the hand-built dict code it replaces, including the separate `prompt` assignment.
- Keep the `passed_with_errors` counting and the console line as they are.
- Add `"started_at": datetime.now().isoformat(timespec="seconds")` to `output` when it's built.
- Update the module docstring usage lines to `uv run python -m tests.evals.run`.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_eval_run_record.py -q && uv run ruff check tests/evals/run.py`
Expected: 6 passed; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add tests/evals/run.py tests/test_eval_run_record.py
git commit -m "feat(evals): record duration, prompt and expectation per case"
```

---

### Task 2: Results server `tests/evals/view.py`

**Files:**
- Create: `tests/evals/view.py`
- Create: `tests/evals/viewer/index.html` (placeholder `<!doctype html><title>Eval results</title>`; Task 3 fills it)
- Test: `tests/test_eval_viewer.py`

**Interfaces:**
- Consumes: the result-file shape from Task 1 (all new fields optional).
- Produces:
  - `DEFAULT_DIR: Path` = repo root / `.evals`; `VIEWER_HTML: Path` = `Path(__file__).parent / "viewer" / "index.html"`.
  - `parse_filename(name: str) -> dict` with `started_at: str | None` (`"2026-10-24T20-15-55_x_llamacpp.json"` → `"2026-10-24T20:15:55"`) and `label: str` (the segment between the first and the last `_`; the stem when there are fewer than two `_`).
  - `safe_run_path(results_dir: Path, name: str) -> Path | None`: a path only when `name` matches `^[A-Za-z0-9][A-Za-z0-9._-]*\.json$` and is an existing regular file directly in `results_dir`.
  - `summarize_run(path: Path) -> dict`: keys `file`, `label`, `started_at` (the file's `started_at` if present, else from the filename), `model`, `provider`, `score`, `elapsed`, `sampling_overrides`, `cases` (count), `failed` (count of `passed: false`), `passed_with_tool_errors` (count of passed cases with non-empty `tool_errors`). Missing values are `None`. A file that fails `json.loads` or isn't a dict with a `cases` dict gives `file`, `label`, `started_at` plus `error: "invalid result file"`.
  - `list_runs(results_dir: Path) -> list[dict]`: summaries of `*.json` files, newest first by `started_at` (falling back to filename). A missing directory returns `[]`.
  - `make_server(results_dir: Path, port: int) -> ThreadingHTTPServer` on `127.0.0.1`. GET routes: `/` → `VIEWER_HTML` (`text/html; charset=utf-8`); `/api/runs` → `list_runs` JSON; `/api/runs/<name>` → the file's bytes as `application/json` after URL-unquoting `name` and passing `safe_run_path`. Everything else, and any unsafe name, is 404. Override `log_message` to stay quiet. Files are read fresh on every request.
  - `main(argv: list[str] | None = None) -> int`: flags `--port` (default `8765`), `--no-open`, `--dir` (default `DEFAULT_DIR`). Prints the URL, opens it with `webbrowser.open` unless `--no-open`, and serves until Ctrl-C (returns 0). If binding fails with `OSError`, prints `port <n> is in use; pick another with --port` to stderr and returns 1. `if __name__ == "__main__": sys.exit(main())`.

- [ ] **Step 1: Write the failing tests**

Fixtures: a `results` tmp dir holding
`2026-10-24T20-15-55_gemma-4-E4B-thinking_llamacpp.json` (new shape: `started_at`, three cases: a pass with `duration_s`, a fail, a pass with `tool_errors`),
`2026-09-22T10-26-50_openbmb-MiniCPM5-2B_llamacpp.json` (old shape: no `started_at`, cases have only `passed`/`reason`),
`2026-10-25T08-00-00_partial_llamacpp.json` containing `{"model": "x", "cases": {` (truncated), and
`notes.txt`.
A `server` fixture starts `make_server(results, 0)` in a daemon thread, yields its base URL, and calls `shutdown()`/`server_close()` on teardown.

```python
def test_parse_filename_timestamp_and_label():
    assert parse_filename("2026-10-24T20-15-55_gemma-4-E4B-thinking_llamacpp.json") == {
        "started_at": "2026-10-24T20:15:55", "label": "gemma-4-E4B-thinking"}

def test_parse_filename_model_with_underscores():
    assert parse_filename("2026-10-24T20-15-55_a_b_c_llamacpp.json")["label"] == "a_b_c"

def test_parse_filename_without_timestamp_falls_back_to_stem():
    assert parse_filename("mine.json") == {"started_at": None, "label": "mine"}

def test_list_runs_newest_first_and_skips_non_json(results):
    files = [r["file"] for r in list_runs(results)]
    assert files == ["2026-10-25T08-00-00_partial_llamacpp.json",
                     "2026-10-24T20-15-55_gemma-4-E4B-thinking_llamacpp.json",
                     "2026-09-22T10-26-50_openbmb-MiniCPM5-2B_llamacpp.json"]

def test_summary_counts(results):
    s = summarize_run(results / "2026-10-24T20-15-55_gemma-4-E4B-thinking_llamacpp.json")
    assert (s["cases"], s["failed"], s["passed_with_tool_errors"]) == (3, 1, 1)
    assert "error" not in s

def test_old_file_summarizes_with_none_for_new_fields(results):
    s = summarize_run(results / "2026-09-22T10-26-50_openbmb-MiniCPM5-2B_llamacpp.json")
    assert s["started_at"] == "2026-09-22T10:26:50" and s["sampling_overrides"] is None

def test_truncated_file_is_flagged_not_fatal(results):
    s = summarize_run(results / "2026-10-25T08-00-00_partial_llamacpp.json")
    assert s["error"] == "invalid result file" and s["label"] == "partial"

def test_missing_dir_lists_nothing(tmp_path):
    assert list_runs(tmp_path / "nope") == []

@pytest.mark.parametrize("name", ["../secret.json", "..%2Fsecret.json", ".hidden.json",
                                  "notes.txt", "sub/x.json", "missing.json"])
def test_safe_run_path_rejects(results, name):
    assert safe_run_path(results, name) is None

def test_http_index_runs_and_run(server):
    assert b"<title>" in urlopen(server + "/").read()
    runs = json.loads(urlopen(server + "/api/runs").read())
    assert len(runs) == 3
    body = json.loads(urlopen(server + "/api/runs/2026-10-24T20-15-55_gemma-4-E4B-thinking_llamacpp.json").read())
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
    taken = socket.socket(); taken.bind(("127.0.0.1", 0)); taken.listen()
    try:
        assert main(["--port", str(taken.getsockname()[1]), "--no-open"]) == 1
    finally:
        taken.close()
    assert "in use" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_eval_viewer.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.evals.view'`

- [ ] **Step 3: Implement `tests/evals/view.py` and the placeholder `index.html` per the Interfaces block**

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_eval_viewer.py -q && uv run ruff check tests/evals/view.py`
Expected: all passed, no warnings; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add tests/evals/view.py tests/evals/viewer/index.html tests/test_eval_viewer.py
git commit -m "feat(evals): local read-only server for eval results"
```

---

### Task 3: Page with run picker, comparison grid and filters

**Files:**
- Modify: `tests/evals/viewer/index.html` (inline `<style>` and `<script>`; a sibling `viewer.js` is allowed only if the file passes ~600 lines, and then `view.py` must serve it too)

**Interfaces:**
- Consumes: `GET /api/runs` (summary list from Task 2), `GET /api/runs/<file>` (full result JSON, shape from Task 1, new fields optional).
- Produces (JS, used by Task 4):
  - `cellStatus(caseData) -> "pass" | "warn" | "fail" | "missing"`: `undefined` → missing; `passed` with non-empty `tool_errors` → warn; `passed` → pass; else fail.
  - `state = { runs: [{file, label, data}], filter: "all"|"failed"|"disagree"|"warn", query: string, open: {caseId, file} | null, sortBy: file | null }`
  - `renderGrid()`: rebuilds the table from `state`.

- [ ] **Step 1: Build the run picker**

A list from `/api/runs`, one row per run showing the date/time (`started_at`), `label`, `provider`, score, `elapsed` formatted as seconds, and sampling overrides as `k=v` pairs. Runs with `error` show it and can't be selected. Checkbox selection, max 3: a 4th selection is refused with the note `Compare up to 3 runs at a time.` The selection is mirrored to `?runs=a.json,b.json` via `history.replaceState` and restored from the URL on load. Unknown files in the URL are dropped silently.

- [ ] **Step 2: Build the grid**

- Rows: case ids in the first selected run's order, then ids only later runs have, in their order.
- One column per run. The header shows the label, score and total time.
- A cell shows the symbol (`✓` pass, `✓*` warn, `✗` fail, `—` missing) plus `duration_s` as `12.3s` or `—`.
- Filters (one active at a time):
  - `All`
  - `Any failed`: any cell is fail
  - `Runs disagree`: the set of non-missing statuses has more than one member
  - `Passed with tool errors`: any warn
- Search is case-insensitive over case id, `prompt`, `reason`, and the tool names in `trace[*].calls[*].name` across selected runs.
- Default filter on load: `Any failed` if it matches at least one row, else `All`.
- Shows `N of M cases`.
- Every dynamic string goes in via `textContent` / `createElement`, never `innerHTML` with data.

- [ ] **Step 3: Theme**

Catppuccin Latte for light and Mocha for dark, as CSS custom properties on `:root`, switched with `@media (prefers-color-scheme: dark)`. Pass is green, warn is yellow/peach, fail is red, missing is overlay grey. The `body` background is set explicitly. Desktop layout only.

- [ ] **Step 4: Manual check against existing runs**

Run: `uv run python -m tests.evals.view`
Expected:
- The 6 existing runs are listed newest first.
- Selecting `2026-10-24T20-15-55…thinking`, `…20-09-17…no-thinking` and `2026-09-22T12-50-16…MiniCPM5` shows the grid.
- `Any failed` is active by default and includes `automation_clean_away`, `logbook_clean_away_today`, `deferred_vacuum_stop`, `battery_sofija_phone`.
- `deferred_vacuum_stop` shows `—` in the MiniCPM column.
- Durations show `—` (old files).
- Reloading keeps the selection.

- [ ] **Step 5: Commit**

```bash
git add tests/evals/viewer/index.html
git commit -m "feat(evals): comparison grid with filters in the results viewer"
```

---

### Task 4: Detail panel, time sort, docs

**Files:**
- Modify: `tests/evals/viewer/index.html`
- Modify: `CLAUDE.md` (Commands section)

**Interfaces:**
- Consumes: `state`, `cellStatus`, `renderGrid` from Task 3.

- [ ] **Step 1: Detail panel**

Clicking a non-missing cell sets `state.open = {caseId, file}` and renders a full-width row directly under that case row:
- Header: run label and case id.
- Prompt, or `Prompt not recorded`.
- `expected` rendered as `expect_tool: get_automations` lines, or `Expectation not recorded`.
- Status, `reason`, and `tool_errors`.
- Trace, one line per entry:
  - `think`: collapsed to its first 120 chars; click expands.
  - `call`: `name(args as compact JSON)`.
  - `result`: if it parses as JSON with `status`, show the status and `error.code`; the full text is collapsed. Error results get the fail colour. Non-JSON text shows as-is, collapsed.
  - `answer`: shown in full.
  - `error`: fail colour.
- No `trace` key: `No trace: run with -v to record one.`

Clicking another cell in the same row switches the panel to that run. Clicking the open cell again closes it. Filter or selection changes close it if its row is hidden.

- [ ] **Step 2: Time sort**

Clicking a column header's time toggles `state.sortBy` to that run's file: rows sort by that run's `duration_s` descending, with missing durations last. Clicking again returns to case order. The active header shows `↓`.

- [ ] **Step 3: Docs**

In `CLAUDE.md` → Commands, under the evals bullet, add:
`- Eval results viewer: uv run python -m tests.evals.view (read-only page over .evals/; compare up to 3 runs).`

- [ ] **Step 4: Manual check with a fresh run**

Run `uv run python -m tests.evals.run -v` (needs the LLM server), then `uv run python -m tests.evals.view`.
Expected:
- The new run is at the top with durations in every cell.
- Opening `time_reminder` shows the prompt, `expect_tool: schedule_at_time` and the full trace, with error results highlighted.
- Sorting by the new run's time puts the slowest case first.
- Opening a case from an old non-verbose run shows the no-trace message.

Review Focus 1, checked in devtools: run `state.runs[0].data.cases[Object.keys(state.runs[0].data.cases)[0]].prompt = "<b>x</b><script>alert(1)</script>"`, then call `renderGrid()` and open that cell. Expected: the literal text shows and no alert fires.

- [ ] **Step 5: Full suite and commit**

Run: `uv run pytest -q && uv run ruff check tests/`
Expected: all passed, no warnings; `All checks passed!`

```bash
git add tests/evals/viewer/index.html CLAUDE.md
git commit -m "feat(evals): case detail panel and time sort in the results viewer"
```
