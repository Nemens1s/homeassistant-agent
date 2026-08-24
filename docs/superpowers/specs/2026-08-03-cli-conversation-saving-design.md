# CLI Conversation Saving — Design Spec

**Date:** 2026-08-03  
**Status:** Approved

## Summary

Add an opt-in `--save-conversations` flag to the CLI REPL that writes a plain-text
transcript of each session to a gitignored `.conversations/` directory at the repo
root. Intended for debugging and iterative tooling improvement.

## Invocation

```
python -m app.cli --save-conversations
```

A minimal `argparse` block (3–4 lines) is parsed before `asyncio.run(main())`.
The flag is passed into `main(save_conversations: bool)`.

## File layout

```
.conversations/
    2026-08-03T14-32-11.txt
    2026-08-03T15-01-44.txt
```

- Directory: `.conversations/` at the repo root.
- Created automatically on first use (`mkdir -p`).
- One file per session, named from the session start timestamp: `YYYY-MM-DDTHH-MM-SS.txt`.
- `.conversations/` appended to `.gitignore`.

## File format

```
[2026-08-03 14:32:11] You: which lights are on in the kitchen?
[2026-08-03 14:32:11] <think> I need to look up the current state of kitchen lights... </think>
[2026-08-03 14:32:12] Tool: list_entities(area="kitchen") → {"entities":[...]}
[2026-08-03 14:32:14] Agent: The ceiling light and the counter strip are both on.
[2026-08-03 14:32:20] You: turn them off
[2026-08-03 14:32:22] Tool: call_service(domain="light", service="turn_off", ...) → {"ok":true}
[2026-08-03 14:32:23] Agent: Done — both lights turned off.
```

- Every line is prefixed with a wall-clock timestamp (`[YYYY-MM-DD HH:MM:SS]`, local time).
- **User turn:** `You: <text>`
- **Thinking:** `<think> ... </think>` — logged only when `settings.show_thinking` is true,
  content accumulated from `token.additional_kwargs["reasoning_content"]` chunks.
- **Tool call:** `Tool: <name>(<args>) → <result>` — name+args from accumulated
  `tool_call_chunks`; result from the `ToolMessage` that follows in the stream.
- **Agent reply:** `Agent: <text>`
- **Errors:** `[error: <message>]` matching terminal output.

## Implementation

All changes in `cli.py`:

1. Add `argparse` block above `asyncio.run(main())`.
2. `main()` gains `save_conversations: bool = False` parameter.
3. When flag is set: open file handle before the REPL loop, close in `finally`.
4. Inside the streaming loop, append to file alongside existing terminal writes:
   - Accumulate thinking chunks → write `<think>...</think>` line at turn end.
   - Accumulate tool call chunks → write `Tool: name(args) → result` when
     the corresponding `ToolMessage` arrives.
   - Write `Agent:` line after final content is flushed.
5. No new files, no new classes, no changes to `config.py` or other modules.

## Out of scope

- Config-file or `.env` option for always-on saving (flag is sufficient).
- Rotation or retention policy (directory is gitignored; user manages manually).
- JSON or structured formats (plain text is the goal).
