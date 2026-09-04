# Deployment

Three ways to run the agent, from most to least common.

## 1. As a Home Assistant add-on (the normal way)

The Supervisor builds the image itself — you never run `docker build` for
this path.

1. Copy the repo to your HA host at `/addons/local/local_ha_agent/`
   (Samba add-on, SSH, or the Studio Code Server add-on). Only the addon
   files matter: `app/`, `frontend/`, `config.yaml`, `build.yaml`,
   `Dockerfile`, `run.sh`, `requirements.txt` — `venv/`, `tests/`, `docs/`
   are ignored by the build but harmless to copy.
2. Settings → Add-ons → Add-on Store → ⋮ (top right) → **Check for updates**.
   The add-on appears under **Local add-ons**.
3. Install, then on the **Configuration** tab set at minimum:
   - `llm_url`: `http://<laptop-ip>:11434` (Ollama) or your llama.cpp base URL
   - `llm_model`: a model pulled on that server (e.g. `qwen3.5:4b`)
   Everything else has sane defaults; the Supervisor writes your choices to
   `/data/options.json`, which the app reads at startup.
4. Start it. The **Log** tab shows startup + every tool call
   (`agent.tools` lines). The sidebar panel ("HA Agent") opens the chat UI
   through ingress.
5. Health: the add-on exposes `/api/health` and `config.yaml` wires it to
   the Supervisor watchdog — if HA or Ollama become unreachable the addon
   reports degraded rather than dying.

**Updating:** copy the new files over, bump nothing by hand — the version in
`config.yaml` changes with the repo. In the UI: ⋮ → Check for updates →
Update (Supervisor rebuilds the image). Add-on data (`/data`, which will
hold `audit.db` and `conversations.db` from iterations 2–3) survives
updates and is included in HA backups.

## 2. Standalone docker build (CI / smoke-testing the image)

`Dockerfile` starts with `ARG BUILD_FROM` because the HA builder injects the
per-architecture base image from `build.yaml`. Building outside HA you must
supply it yourself — this is why a bare `docker build -t ollama-agent .`
fails with "base name ($BUILD_FROM) should not be blank":

```bash
docker build -t ollama-agent \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19 .
```

(aarch64: `ghcr.io/home-assistant/aarch64-base:3.19` — same tags as
`build.yaml`.)

Running the standalone image is only useful for smoke tests: `run.sh` uses
`with-contenv bashio` (present in the HA base image) and expects
`SUPERVISOR_TOKEN` + `http://supervisor/core`, which don't exist outside HA.
To smoke-test the server against a real HA instance instead:

```bash
docker run --rm -p 8099:8099 \
  -e HA_BASE_URL=http://<ha-ip>:8123 \
  -e SUPERVISOR_TOKEN=<long-lived-access-token> \
  -e LLM_URL=http://<laptop-ip>:11434 \
  -e LLM_MODEL=qwen3.5:4b \
  --entrypoint sh ollama-agent \
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
gitignored; the legacy `OLLAMA_MODEL` key is ignored by the app — use
`LLM_MODEL`.
