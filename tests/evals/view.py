"""Read-only local viewer for eval results in .evals/. Usage:

    uv run python -m tests.evals.view [--port 8765] [--no-open] [--dir .evals]

Serves a single page plus two JSON endpoints, bound to 127.0.0.1 only.
Files are read fresh on every request, so a new run shows up on reload.
Dev tooling: nothing here ships in the add-on image.
"""

import argparse
import json
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

DEFAULT_DIR = Path(__file__).resolve().parent.parent.parent / ".evals"
VIEWER_HTML = Path(__file__).parent / "viewer" / "index.html"

RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.json$")
# run.py names files "<YYYY-MM-DDTHH-MM-SS>_<model>_<provider>.json".
FILENAME_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})$")


def parse_filename(name: str) -> dict:
    """The run's start time and label (the model segment) from its filename."""
    stem = name.removesuffix(".json")
    parts = stem.split("_")
    started_at = None
    match = FILENAME_TIMESTAMP.match(parts[0])
    if match:
        date, hour, minute, second = match.groups()
        started_at = f"{date}T{hour}:{minute}:{second}"
    if len(parts) < 3:
        label = stem
    else:
        label = "_".join(parts[1:-1])
    return {"started_at": started_at, "label": label}


def safe_run_path(results_dir: Path, name: str) -> Path | None:
    """The file for `name`, only if it's a plain .json file directly in results_dir."""
    if not RUN_NAME.match(name):
        return None
    path = results_dir / name
    if not path.is_file():
        return None
    return path


def summarize_run(path: Path) -> dict:
    """The run-picker row for one results file. Never raises on a bad file."""
    names = parse_filename(path.name)
    summary: dict = {"file": path.name, "label": names["label"], "started_at": names["started_at"]}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("cases"), dict):
        summary["error"] = "invalid result file"
        return summary

    failed = 0
    passed_with_tool_errors = 0
    for case in data["cases"].values():
        if not isinstance(case, dict):
            continue
        if case.get("passed"):
            if case.get("tool_errors"):
                passed_with_tool_errors += 1
        else:
            failed += 1

    if data.get("started_at"):
        summary["started_at"] = data["started_at"]
    summary["model"] = data.get("model")
    summary["provider"] = data.get("provider")
    summary["score"] = data.get("score")
    summary["elapsed"] = data.get("elapsed")
    summary["sampling_overrides"] = data.get("sampling_overrides")
    summary["cases"] = len(data["cases"])
    summary["failed"] = failed
    summary["passed_with_tool_errors"] = passed_with_tool_errors
    return summary


def _newest_first_key(summary: dict) -> tuple[str, str]:
    return (summary["started_at"] or "", summary["file"])


def list_runs(results_dir: Path) -> list[dict]:
    """Summaries of every results file, newest first."""
    if not results_dir.is_dir():
        return []
    runs = []
    for path in results_dir.glob("*.json"):
        if safe_run_path(results_dir, path.name) is None:
            continue
        runs.append(summarize_run(path))
    runs.sort(key=_newest_first_key, reverse=True)
    return runs


def make_server(results_dir: Path, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/":
                self._send(200, "text/html; charset=utf-8", VIEWER_HTML.read_bytes())
                return
            if path == "/api/runs":
                body = json.dumps(list_runs(results_dir)).encode()
                self._send(200, "application/json", body)
                return
            prefix = "/api/runs/"
            if path.startswith(prefix):
                run_path = safe_run_path(results_dir, unquote(path[len(prefix):]))
                if run_path is not None:
                    self._send(200, "application/json", run_path.read_bytes())
                    return
            self._send(404, "text/plain; charset=utf-8", b"not found")

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args) -> None:  # keep the terminal quiet
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only viewer for eval results.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Don't open a browser")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="Results directory")
    args = parser.parse_args(argv)

    try:
        httpd = make_server(args.dir, args.port)
    except OSError:
        print(f"port {args.port} is in use; pick another with --port", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"eval results viewer: {url}  (reading {args.dir}; Ctrl-C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
