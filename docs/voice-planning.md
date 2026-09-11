# Voice control planning (multilingual, local-first)

Planning notes for adding voice to the local HA agent. Not implemented yet —
captured so the design decisions and hardware constraints aren't lost. Related:
`docs/needle-finetuning.md`, `docs/needle-confidence.md`.

## Context / constraints

- Household speaks mostly **Russian**, some Estonian; the HA config, automations,
  and the Needle fast path are all **English**.
- **Russian is the priority; Estonian is not critical** (it's Whisper's weak
  language and would force bigger models — deferred).
- Local-first: keep it on-prem, no cloud round-trip, matching the rest of the
  project.
- Hardware today: HA host = mid-2011 Mac Mini (Sandy Bridge, **no AVX2**, weak);
  "Ollama box" = laptop, **Intel i5-8250U** (4c/8t, **has AVX2**), 16 GB RAM,
  2 GB VRAM GPU; dev/app currently runs on an Apple-Silicon Mac.

## The pipeline

```
[satellite: mic + wake word] → HA → STT → conversation agent → TTS → [satellite: speaker]
                                      │           │
                                      │           ├─ fast path: Needle → trigger automation.ai_*   [no LLM]
                                      │           └─ fallback: Ollama LLM (read queries)            [GPU]
```

Where language matters: **STT** (must handle RU/ET audio) and **the response**
(should come back in the user's language). Intent routing and action execution
are language-agnostic once we have English text.

## Architecture: how the pieces connect

**HA orchestrates the voice pipeline — our app does not.** The satellite streams
audio to HA; HA's **Assist pipeline** (Settings → Voice assistants) calls STT,
then the conversation agent, then TTS. Neither the satellite nor our app is the
conductor.

- **Whisper is a separate service that HA calls**, running side-by-side with
  Ollama on the i5 laptop (`wyoming-faster-whisper` container, Wyoming protocol).
  Ollama and Whisper don't know about each other — HA talks to both. **Our app
  never touches audio.**
- **Our app is HA's *conversation agent*** — it receives already-transcribed
  **text**, runs Needle/agent, returns a reply which HA speaks via TTS (Piper).

So our app has **two text entry points**, and STT is HA's job in both:
- UI chat: browser → `/api/chat` directly.
- Voice: satellite → HA → (STT) → our app → reply → (TTS) → satellite.

**Two pieces of glue that don't exist yet (future work):**

1. **A small custom HA integration to make our app the conversation agent.** HA
   has no built-in "call an arbitrary HTTP endpoint" agent (the stock ones point
   at Ollama/OpenAI/Anthropic directly, bypassing Needle). We'd write a thin
   `ConversationEntity` that forwards the text to `/api/chat` and returns the
   reply, then select it in the Assist pipeline.
2. **Deciding where translation happens** — see below.

Do **not** invert this and make our app receive audio / drive satellites — that
re-implements HA's satellite + wake-word + pipeline machinery for no gain.

## Multilingual strategy: translate *in*, at the STT stage

**Do NOT fine-tune Needle for Russian.** Its tokenizer is 8,192 tokens,
English-centric, with **zero Cyrillic** — Russian falls back to byte encoding and
explodes in the 256-token window. Estonian (Latin) is only marginally better.
Needle is an English model; making it multilingual would need a new tokenizer +
base, not a fine-tune.

Instead, get **English text** to the fast path. The Whisper *model* has a
`translate` task (any-language audio → English in one pass), which would be ideal
— but **the stock HA `wyoming-faster-whisper` add-on transcribes in the native
language and does not surface the translate task.** So translation placement is a
real decision, not free:

```
RU/ET audio → [ translate somewhere ] → English text → Needle / English agent → action
```

- **(a) Translate at STT (best for the fast path):** run a translate-capable
  Whisper wrapper so HA hands our app **English**. Keeps the trigger path fast and
  our app simple. Cost: a custom/configured STT (not the stock add-on as-is) —
  verify what `wyoming-faster-whisper` actually exposes before relying on this.
- **(b) Translate in our app:** app receives native Russian, translates → English
  before Needle. Simpler to deploy, but adds a translation step to the **fast
  path** — the very latency we optimized. Only acceptable on the read path.

Everything downstream stays English regardless: English automations, English
Needle, even HA's free English template intents keep working.

- **Responses:**
  - Fast-path triggers → confirmation is a **canned string localized per
    language** ("Готово" / "Tehtud" / "Done"). No translation needed.
  - Read queries → either have the LLM **answer in the user's language**
    (instruct it to), or translate the English answer back for TTS. Latency
    isn't critical on this path.

## STT sizing & placement

Use **`faster-whisper`** (CTranslate2; the Wyoming-protocol standard), **not**
PyTorch Whisper. Multilingual models only (not `.en`).

| Model | Params | ~VRAM int8 | Russian | Estonian |
|-------|--------|-----------|---------|----------|
| base | 74M | ~1 GB | decent | weak |
| **small** | 244M | ~1–1.5 GB | **good** | marginal |
| medium | 769M | ~2.5–3 GB | very good | decent |
| large-v3 | 1.5B | ~4–5 GB | best | best |

**Decision: `faster-whisper small` (int8) on the i5-8250U CPU.**

- The i5-8250U has **AVX2** (the Mac Mini does not), so `faster-whisper` is
  efficient on CPU: ~1–2 s for a typical 2–3 s command. Snappy enough.
- Running on **CPU leaves the 2 GB GPU entirely for Ollama** — no VRAM
  contention. And fast-path (trigger) turns never invoke the LLM anyway, so the
  common case is just STT (CPU) → Needle → HA.
- **Not on the Mac Mini** — no AVX2, weak, already loaded; `small` there would be
  several seconds per command.
- If `small` feels slow, try **`base`** (~2–3× faster, still decent RU for short
  commands) and A/B. GPU is a fallback if you want `small` faster.

Deploy as a **`wyoming-faster-whisper`** container on the Ollama box
(`--model small`, `--language ru` or auto-detect, int8, **model kept warm**). HA
points its STT at it over the network — STT need not live on the HA host.

**Estonian caveat:** good ET wants `medium`/`large`, which won't fit the 2 GB GPU
(→ CPU, slower) — deferred until it matters or better hardware exists.

## Front-end: the Assist satellite (mic in, speaker out)

Two input modes:

- **Push-to-talk** (tap, then speak) — no wake word. Simplest, works today.
- **Always-listening** ("Hey …") — needs on-device wake-word detection; that's
  what dedicated satellites provide.

The wake word is **language-agnostic** (a trained sound pattern) — use an
English-ish wake word and speak Russian commands; STT handles the language.

**Start free, today — the phone.** The HA Companion app (iOS/Android) has Assist
built in: tap the mic, speak Russian, run the whole pipeline. Ideal test rig to
validate RU-audio → Whisper → Needle → action **before buying hardware**. Also
works from Apple Watch (Companion/Siri Shortcut) and desktop browser.

**For hands-free room use (later):**

| Option | What | Notes |
|--------|------|-------|
| **HA Voice PE** | HA's official ~$59 satellite | Easiest turnkey, fully local, on-device wake word. Default pick. |
| **ESP32** (Atom Echo ~$13 / ESP32-S3-BOX-3 ~$50) | DIY ESPHome satellite | Cheap; S3-BOX mic ≫ Atom Echo. |
| **Wyoming satellite** | Raspberry Pi + USB mic/speaker | Most flexible; reuse an old Pi. |

All of these are just ears + mouth + wake word; STT/Needle/TTS stay on the i5 box
and the app.

**TTS:** Piper (local) has Russian and Estonian voices.

## Suggested path

1. **Now (no spend):** phone Companion app (push-to-talk) → prove RU → Whisper
   `small` → Needle → Concorde works and feels fast.
2. **Then:** one HA Voice PE (or an ESP32) for the main room.
3. **New apartment:** add satellites per room; revisit Estonian / a bigger GPU if
   wanted.

## Open questions / to test

- **Does `wyoming-faster-whisper` expose the `translate` task?** Decides
  translation placement (a) vs (b) above. Check its options before relying on
  translate-at-STT.
- **Custom conversation-agent integration** (glue #1): a thin HA `ConversationEntity`
  forwarding transcribed text to `/api/chat`. Needed for voice→our-app; doesn't
  exist yet.
- Whisper `small` vs `base` on **real Russian short commands** — latency and
  accuracy on the i5-8250U CPU.
- Whisper **`translate` quality** (if using path (a)) on terse RU commands (proper
  nouns like "Concorde" surviving translation).
- Where the **agent app runs** in the voice setup. Resolved since this was
  written: the app is packaged as an HA **App** on the HA host, and **Needle is
  decoupled** — it runs as a `remote` backend (`needle_backend: remote`,
  `needle_remote_url`) on a separate always-on machine to minimise latency. So
  no heavy inference sits on the HA host: the LLM goes to Ollama and Needle to
  its remote sidecar, both off-host.
- Response localization: LLM answering directly in RU vs translate-back for TTS.

## Decisions made (2026-08-28)

Resolved during review of the pipeline plan against HA's actual voice
plumbing.

### STT: standalone container, not the HA App

Do **not** install the Whisper App (add-on) in HA. It runs on the HA host
(Proxmox VM on the Mac Mini — Sandy Bridge, no AVX2) and will be slow.

Instead, run **`wyoming-faster-whisper` as a standalone Docker container on
the ASUS box** (i5-8250U, AVX2, CPU, int8). HA connects to it via the
**Wyoming Protocol integration** (Settings → Devices & Services → Add →
Wyoming Protocol → `192.168.1.4:10300`).

```bash
docker run -d --restart always \
  --name wyoming-whisper \
  -p 10300:10300 \
  -v whisper-data:/data \
  rhasspy/wyoming-whisper \
  --model small-int8 --language ru --whisper-task translate
```

Same approach for **Piper TTS** — standalone `wyoming-piper` container on
the ASUS box, Russian voice, added to HA the same way.

### Whisper variant: wyoming-faster-whisper, not WhisperLive

**`wyoming-faster-whisper`** (OHF-Voice) speaks the Wyoming protocol that
HA natively expects. WhisperLive (Collabora) uses its own WebSocket
protocol — HA can't talk to it without a custom STT integration bridge.
No reason to build that.

Other variants noted for reference:
- **whisper.cpp** — has a Wyoming wrapper (`wyoming-whisper-cpp`), could
  benchmark against faster-whisper on this CPU later.
- Original PyTorch Whisper, whisper-jax, distil-whisper — no Wyoming
  wrappers, no advantage here.

### Conversation agent: custom `ConversationEntity`

The Assist conversation agent is a **custom HA `ConversationEntity`
integration** shipped in `custom_components/local_ha_agent/`. It is
configured with the App's base URL and forwards already-transcribed text to
the App's **`/api/chat`** endpoint, returning the reply for HA to speak via
TTS. This is the path of record — it slots directly into the Assist
pipeline as the conversation agent, and does **not** use HA's built-in
OpenAI Conversation integration.

> **Removed this iteration:** an earlier POC exposed an OpenAI-compatible
> `/v1/chat/completions` (+ `/v1/models`) shim so HA's built-in **OpenAI
> Conversation** integration could point at the App for development. That
> shim has been **removed** — the custom `ConversationEntity` → `/api/chat`
> is now the only supported wiring. `/api/chat` (and `/api/chat/stream`)
> are the App's only chat surfaces.

Design notes for the component:
- **Ignore HA's message history** — the agent manages its own context via
  the checkpointer / `thread_id`. Forward only the current user turn's
  text to `/api/chat`.
- Use a distinct `thread_id` for voice to separate voice conversations from
  the UI chat thread (`"default"`).
- For dev, the App runs on the MacBook. `HA_BASE_URL` must point at HA's
  actual URL (not `http://supervisor/core`) with a long-lived access token
  in `HA_TOKEN` / `SUPERVISOR_TOKEN`.

### Pipeline assembly

Once all three pieces are running, create an **Assist pipeline** in HA:

Settings → Voice assistants → Add pipeline:
- **STT:** Wyoming Whisper (ASUS box)
- **Conversation agent:** the custom `Local HA Agent` `ConversationEntity`
  (from `custom_components/local_ha_agent/`, configured with the App's base
  URL → `/api/chat`)
- **TTS:** Wyoming Piper (ASUS box)

Test with the **phone Companion app** (push-to-talk) before buying any
satellite hardware.