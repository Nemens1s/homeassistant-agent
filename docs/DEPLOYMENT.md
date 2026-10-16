# Deployment

Three ways to run the agent, from most to least common.

## 1. As a Home Assistant App (the normal way)

The Supervisor builds the image itself — you never run `docker build` for
this path.

1. Copy the repo to your HA host at `/addons/local/agent_gosling/`
   (Samba App, SSH, or the Studio Code Server App). Only the App
   files matter: `app/`, `frontend/`, `config.yaml`, `Dockerfile`, `run.sh`,
   `pyproject.toml`, `uv.lock`, `.python-version` — `.venv/`, `tests/`,
   `docs/` are ignored by the build but harmless to copy.
2. Settings → Apps → App Store → ⋮ (top right) → **Check for updates**.
   The App appears under **Local Apps**.
3. Install, then on the **Configuration** tab set at minimum:
   - `llm_url`: your llama.cpp (llama-server) base URL, e.g. `http://<laptop-ip>:8080`
   - `llm_model`: the model id (llama-server ignores it; cloud endpoints need it)
   The Configuration tab only shows the minimal option set below; the
   Supervisor writes your choices to `/data/options.json`, which the app
   reads at startup.

   | Key | Default | Notes |
   |---|---|---|
   | `llm_url` | `` | llama.cpp (llama-server) or any OpenAI-compatible base URL |
   | `llm_model` | `` | Model id served by the LLM backend |
   | `api_key` | `` | Key for an OpenAI-compatible endpoint (blank for llama-server) |
   | `allow_actions` | `false` | `true` unlocks `trigger_action`/`notify_user`/scheduling |
   | `ai_actions_switch` | `input_boolean.ai_triggered_actions` | Home-level AI kill switch; empty disables it |
   | `notify_action` | `script.ai_action_notify` | The only script `notify_user` may call |
   | `extra_instructions` | `` | Household-specific text appended after the built-in base prompt |
   | `watched_entities` | `[]` | Entities watched for state-triggered notes; empty = off |
   | `clock_entity` | `` | World Clock sensor for time-triggered notes/scheduling; empty = off |
   | `event_confirmations_enabled` | `true` | Send a confirmation when a fired note acts |
   | `person_name_map` | `[]` | Rename people for the agent |
   | `language_layer_enabled` | `false` | Accept non-English messages via lang-mt |
   | `languages` | `[en]` | Languages detection may choose between; add `et`/`ru` etc. to enable them |

   The base system prompt is code-owned; `extra_instructions` is appended after it
   under a "HOUSEHOLD INSTRUCTIONS" heading. The full assembled prompt is viewable
   at `GET /api/prompt` and in a collapsible panel on the Metrics tab.

   Everything else — sampling knobs (`temperature`, `top_p`, `num_ctx`, ...),
   `llm_provider`, `recursion_limit`, `max_rows`,
   `max_history_messages`, `enable_tool_subsetting`, `fast_path_*`,
   `person_name_exclude`, the language-layer tuning knobs
   (`lang_mt_url`/`default_language`/`min_confidence`/`lang_mt_timeout_s`),
   `telemetry_enabled`, `telemetry_retention_days`, `thread_retention_days`
   (daily pruning, off by default; set a number of days to enable), and the note-scheduling knobs
   (`memory_note_default_ttl_hours`, `memory_note_max_ttl_hours`,
   `time_note_grace_minutes`) — has a code default and stays hidden from the
   Configuration tab until you add it explicitly. `checkpoint_db_path`,
   `telemetry_db_path`, and `glossary_path` are no longer options at all;
   `run.sh` fixes them via `CHECKPOINT_DB_PATH`/`TELEMETRY_DB_PATH`/
   `GLOSSARY_PATH` environment variables.

   If you run a multilingual model without the language layer turned on,
   set `enable_tool_subsetting: false` — the tool-subsetting heuristic is
   tuned on English tool descriptions and can mis-rank tools for
   non-English prompts.
4. Start it. The **Log** tab shows startup + every tool call
   (`agent.tools` lines). The sidebar panel ("HA Agent") opens the chat UI
   through ingress.
5. Health: the App exposes `/api/health` and `config.yaml` wires it to
   the Supervisor watchdog — if HA or the LLM server become unreachable the App
   reports degraded rather than dying.

**Updating:** copy the new files over, bump nothing by hand — the version in
`config.yaml` changes with the repo. In the UI: ⋮ → Check for updates →
Update (Supervisor rebuilds the image). App data (`/data`, which will
holds `audit.db`, `checkpoints.sqlite` and `telemetry.sqlite`) survives
updates and is included in HA backups (except telemetry, see
`backup_exclude`). Nothing is pruned by default; set
`telemetry_retention_days` / `thread_retention_days` to delete old telemetry
and idle conversations daily.

## 2. Standalone docker build (CI / smoke-testing the image)

`Dockerfile` takes `ARG BUILD_ARCH` (default `amd64`), which the HA builder
sets per architecture; it picks the Debian base image
`ghcr.io/home-assistant/<arch>-base-debian:bookworm`. uv installs the Python
pinned in `.python-version` and syncs `uv.lock`:

```bash
docker build -t ha-agent .                                  # amd64
docker build -t ha-agent --build-arg BUILD_ARCH=aarch64 .   # ARM
```

Running the standalone image is only useful for smoke tests: `run.sh` uses
`with-contenv bashio` (present in the HA base image) and expects
`SUPERVISOR_TOKEN` + `http://supervisor/core`, which don't exist outside HA.
To smoke-test the server against a real HA instance instead:

```bash
docker run --rm -p 8099:8099 \
  -e HA_BASE_URL=http://<ha-ip>:8123 \
  -e SUPERVISOR_TOKEN=<long-lived-access-token> \
  -e LLM_URL=http://<laptop-ip>:8080 \
  -e LLM_MODEL=qwen3.5:4b \
  --entrypoint sh ha-agent \
  -c "cd /app && uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8099"
```

Then `curl http://localhost:8099/api/health`.

Note: the base image is Alpine 3.19 (`apk` Python 3.11). The app targets
modern Python but uses no 3.12+ syntax; the standalone build is the check
that this stays true.

## 3. Dev mode (no container)

```bash
venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env   # or create .env: HA_BASE_URL, HA_TOKEN, LLM_URL, LLM_MODEL
venv/bin/python -m app.cli                      # capability-testing REPL
venv/bin/uvicorn app.main:create_app --factory --port 8099   # web UI at localhost:8099
venv/bin/python -m tests.evals.run              # tool-selection scoring
```

`HA_TOKEN` is a long-lived access token (HA profile → Security). `.env` is
gitignored.
