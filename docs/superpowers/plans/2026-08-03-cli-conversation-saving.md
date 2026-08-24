# CLI Conversation Saving Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--save-conversations` flag to `app/cli.py` that writes a timestamped plain-text transcript of each CLI session to `.conversations/`.

**Architecture:** All changes are isolated to `app/cli.py`. Two small helpers (`_ts`, `_open_conv_file`) are extracted so they can be unit-tested. The streaming loop gains file-write calls alongside its existing print calls.

**Tech Stack:** Python stdlib (`argparse`, `datetime`, `pathlib`, `json`); `langchain_core.messages.ToolMessage` (already a transitive dep).

## Global Constraints

- Python interpreter: `venv/bin/python` / `venv/bin/pytest` — never system python.
- Tests: `venv/bin/python -m pytest -q` must stay green with exactly 2 known third-party warnings.
- No new modules, no new classes — everything in `app/cli.py`.
- `langchain v1` API only; do not add new runtime dependencies.
- Tool output to file must not block or raise into the agent loop.

---

### Task 1: Helpers, flag, file lifecycle, and .gitignore

**Files:**
- Modify: `app/cli.py`
- Modify: `.gitignore`
- Create: `tests/test_cli_conversation.py`

**Interfaces:**
- Produces:
  - `_ts() -> str` — returns `"[YYYY-MM-DD HH:MM:SS]"` using local time
  - `_open_conv_file(base_dir: Path) -> IO[str]` — creates `base_dir` if absent, opens and returns a writable text file named `YYYY-MM-DDTHH-MM-SS.txt` from current local time
  - `main(save_conversations: bool = False) -> None` — existing function gains this parameter; when `True`, opens file before the REPL loop and closes it in `finally`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli_conversation.py
import re
from pathlib import Path
from app.cli import _ts, _open_conv_file


def test_ts_format():
    result = _ts()
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", result)


def test_open_conv_file_creates_dir(tmp_path):
    base = tmp_path / ".conversations"
    f = _open_conv_file(base)
    f.close()
    assert base.is_dir()


def test_open_conv_file_name_format(tmp_path):
    base = tmp_path / ".conversations"
    f = _open_conv_file(base)
    name = Path(f.name).name
    f.close()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.txt", name)


def test_open_conv_file_is_writable(tmp_path):
    f = _open_conv_file(tmp_path / ".conversations")
    f.write("hello\n")
    f.close()
```

- [ ] **Step 2: Run tests — expect failure**

```
venv/bin/python -m pytest tests/test_cli_conversation.py -v
```

Expected: `ImportError` or `AttributeError` — `_ts` and `_open_conv_file` don't exist yet.

- [ ] **Step 3: Add helpers to `app/cli.py`**

Add these imports at the top of `app/cli.py` (alongside existing ones):

```python
import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import IO
```

Add these two functions just above `async def main()`:

```python
def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def _open_conv_file(base_dir: Path) -> IO[str]:
    base_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("%Y-%m-%dT%H-%M-%S.txt")
    return open(base_dir / name, "w", encoding="utf-8")
```

- [ ] **Step 4: Add `save_conversations` parameter to `main()`**

Change the signature:
```python
async def main(save_conversations: bool = False) -> None:
```

Add file open/close around the REPL loop. The `conv_file` variable is `IO[str] | None`.

Immediately before `print(f"\nAgent ready ...")`:
```python
    conv_file: IO[str] | None = (
        _open_conv_file(Path(".conversations")) if save_conversations else None
    )
```

In the `finally` block, after `audit.close()`:
```python
        if conv_file is not None:
            conv_file.close()
```

- [ ] **Step 5: Add `argparse` block and wire `main()`**

Replace the `if __name__ == "__main__":` block with:

```python
if __name__ == "__main__":
    _parser = argparse.ArgumentParser(description="HA agent CLI")
    _parser.add_argument(
        "--save-conversations",
        action="store_true",
        help="Write a transcript of this session to .conversations/",
    )
    _args = _parser.parse_args()
    asyncio.run(main(save_conversations=_args.save_conversations))
```

- [ ] **Step 6: Add `.conversations/` to `.gitignore`**

Append to `.gitignore`:
```
.conversations/
```

- [ ] **Step 7: Run tests — expect pass**

```
venv/bin/python -m pytest tests/test_cli_conversation.py -v
```

Expected: 4 tests pass.

- [ ] **Step 8: Run full suite — expect no regressions**

```
venv/bin/python -m pytest -q
```

Expected: all existing tests pass, exactly 2 third-party warnings.

- [ ] **Step 9: Commit**

```bash
git add app/cli.py tests/test_cli_conversation.py .gitignore
git commit -m "feat: add --save-conversations flag scaffold to CLI"
```

---

### Task 2: Streaming loop — log user turns, agent replies, errors, thinking, and tool calls

**Files:**
- Modify: `app/cli.py:60-92` (the streaming loop inside the `while True` block)

**Interfaces:**
- Consumes: `_ts() -> str`, `conv_file: IO[str] | None` (from Task 1)
- Consumes: `ToolMessage` from `langchain_core.messages`

**Transcript line formats:**
- User turn: `{_ts()} You: {user_input}\n`
- Thinking: `{_ts()} <think> {accumulated_thinking} </think>\n` — only when `settings.show_thinking` is True
- Tool call: `{_ts()} Tool: {name}({args}) → {result}\n`
- Agent reply: `{_ts()} Agent: {accumulated_content}\n`
- Recursion error: `{_ts()} [stopped: hit the N-step limit without finishing]\n`
- Other error: `{_ts()} [error: {exc}]\n`

- [ ] **Step 1: Add `ToolMessage` import**

In `app/cli.py`, change the existing langchain import line from:
```python
from langchain_core.messages import AIMessageChunk
```
to:
```python
from langchain_core.messages import AIMessageChunk, ToolMessage
```

- [ ] **Step 2: Replace the streaming loop body**

Find the block starting with `if not user_input:` and replace the entire `print("Agent: " ...)` through the `print(f"\n[{elapsed:.1f}s]\n")` lines with the following. The new code adds four things: (a) log the user turn, (b) accumulate think_buf and pending_tool_calls alongside existing buffers, (c) log tool results when ToolMessage arrives, (d) log agent reply and errors after the loop.

```python
            if not user_input:
                continue
            if conv_file:
                conv_file.write(f"{_ts()} You: {user_input}\n")
                conv_file.flush()
            print("Agent: ", end="", flush=True)
            t0 = time.monotonic()
            try:
                content_buf: list[str] = []
                think_buf: list[str] = []
                pending_tool_calls: dict[int, dict] = {}  # index → {name, args, id}
                has_tool_calls = False
                async for token, _meta in agent.astream(
                    {"messages": [{"role": "user", "content": user_input}]},
                    config=config,
                    stream_mode="messages",
                ):
                    if not isinstance(token, AIMessageChunk):
                        if content_buf and not has_tool_calls:
                            print("".join(content_buf), end="", flush=True)
                        content_buf = []
                        think_buf = []
                        has_tool_calls = False
                        if conv_file and isinstance(token, ToolMessage):
                            raw = token.content
                            result = raw if isinstance(raw, str) else json.dumps(raw)
                            for call in pending_tool_calls.values():
                                if call.get("id") == token.tool_call_id:
                                    conv_file.write(
                                        f"{_ts()} Tool: {call['name']}({call['args']}) → {result}\n"
                                    )
                                    conv_file.flush()
                                    break
                        continue
                    if settings.show_thinking:
                        thinking = token.additional_kwargs.get("reasoning_content", "")
                        if thinking:
                            print(f"\033[2m{thinking}\033[0m", end="", flush=True)
                            if conv_file:
                                think_buf.append(thinking)
                    if token.tool_call_chunks:
                        has_tool_calls = True
                        if conv_file:
                            for chunk in token.tool_call_chunks:
                                idx = chunk.get("index") or 0
                                if idx not in pending_tool_calls:
                                    pending_tool_calls[idx] = {"name": "", "args": "", "id": ""}
                                if chunk.get("name"):
                                    pending_tool_calls[idx]["name"] = chunk["name"]
                                if chunk.get("id"):
                                    pending_tool_calls[idx]["id"] = chunk["id"]
                                pending_tool_calls[idx]["args"] += chunk.get("args") or ""
                    if isinstance(token.content, str) and token.content:
                        content_buf.append(token.content)
                # Flush the final turn (no non-AIMessageChunk follows it).
                if content_buf and not has_tool_calls:
                    print("".join(content_buf), end="", flush=True)
                    if conv_file:
                        if think_buf:
                            conv_file.write(f"{_ts()} <think> {''.join(think_buf)} </think>\n")
                        conv_file.write(f"{_ts()} Agent: {''.join(content_buf)}\n")
                        conv_file.flush()
            except GraphRecursionError:
                msg = f"[stopped: hit the {settings.recursion_limit}-step limit without finishing]"
                print(f"\n{msg}", end="")
                if conv_file:
                    conv_file.write(f"{_ts()} {msg}\n")
                    conv_file.flush()
            except Exception as exc:
                print(f"\n[error: {exc}]", end="")
                if conv_file:
                    conv_file.write(f"{_ts()} [error: {exc}]\n")
                    conv_file.flush()
            elapsed = time.monotonic() - t0
            print(f"\n[{elapsed:.1f}s]\n")
```

- [ ] **Step 3: Run full test suite**

```
venv/bin/python -m pytest -q
```

Expected: all tests pass, exactly 2 third-party warnings.

- [ ] **Step 4: Manual smoke test**

```
venv/bin/python -m app.cli --save-conversations
```

Ask one question, exit with `Ctrl+C`. Then:

```bash
cat .conversations/*.txt
```

Expected output should look like:
```
[2026-08-03 14:32:11] You: what time is it?
[2026-08-03 14:32:13] Agent: It's currently 2:32 PM.
```

- [ ] **Step 5: Commit**

```bash
git add app/cli.py
git commit -m "feat: log user turns, agent replies, thinking, and tool calls to conversation file"
```
