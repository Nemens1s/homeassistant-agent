**Status:** Draft v3 · **Owner:** Ilja **Supersedes:** v2 (external gateway proxy) and v1 ("Multilingual Voice Gateway Microservice")

## 1. Summary

The household speaks English, Russian and Estonian. The agent core of `homeassistant-agent` (LangGraph graph, Needle fast path, tools, prompts, memory) understands English only and should stay that way.

This spec adds an optional **language adapter** at the app's HTTP edge. It is enabled by config and wraps `/api/chat` and `/api/chat/stream`: it detects the language, translates the input to English, runs the agent unchanged, and translates the reply back. The models do not run in the app; they run in a stateless **MT service** on the ASUS box.

Voice keeps using HA's Assist pipeline, with two small Wyoming wrappers for language-aware STT and TTS.

**Components**

| Component                      | Where                                         | New / changed                                 |
| :----------------------------- | :-------------------------------------------- | :-------------------------------------------- |
| Language adapter (`app/i18n/`) | `homeassistant-agent`, HA App on the Mac Mini | New module, off by default                    |
| `lang-mt`                      | ASUS box                                      | New service: detection and translation        |
| `wyoming-stt-whitelist`        | ASUS box                                      | New, replaces the `wyoming-whisper` container |
| `wyoming-tts-router`           | ASUS box                                      | New, sits in front of Piper                   |
| Chat frontend                  | `homeassistant-agent`                         | Small change to `done` handling               |
| `hass-agent-gosling`           | HA                                            | No change                                     |

## 2. Why the HTTP edge (and not an external gateway)

v2 put a proxy in front of the agent. The repo made that a poor fit:

- The chat frontend is served by the app through HA ingress and calls the relative path `api/chat/stream`. It only works same-origin behind the ingress prefix, so it cannot be pointed at another host without giving up ingress auth and the sidebar panel.
- The frontend consumes an SSE (Server-Sent Events) stream of `thinking`, `token`, `tool_call`, `tool_result`, `done` and `error` events, which a proxy would have to re-implement.
- Both clients already meet at the app's HTTP edge: gosling calls `/api/chat` and the frontend calls `/api/chat/stream`. One adapter there covers voice and chat.
- A request no longer bounces HA → ASUS → HA → ASUS. The app makes one call to `lang-mt` on the way in and one on the way out.

**What "English-only" now means.** The agent core never sees non-English text: the graph, Needle, tools, prompts, the checkpointer's stored messages and LLM calls all stay English. Only the API edge is language-aware, and it is a separate module that can be switched off.

## 3. Goals and non-goals

**Goals**

- A single Assist pipeline accepts en, ru and et and replies in the language it was spoken to in.
- The chat UI gets the same behaviour, through the same adapter as voice.
- The adapter is controlled by one config flag. When it is off, behaviour is identical to today and `lang-mt` is never called.
- No extra LLM calls. Detection and translation use small dedicated models on CPU.
- If `lang-mt` is down or slow, the agent still answers (fail open).
- Everything stays on the LAN.

**Non-goals (for now)**

- Code-switching inside a single utterance. Each message gets exactly one language.
- Languages beyond en/ru/et.
- Natural-sounding Estonian TTS. Intelligible is enough (see section 7.3 for the upgrade path).

## 4. Architecture

```
Satellite ──audio──▶ HA Assist pipeline (one pipeline, labelled "English")
                       │
                       ├─ STT  ─▶ wyoming-stt-whitelist (ASUS) ──▶ native text
                       │
                       ├─ Conv ─▶ hass-agent-gosling ─▶ POST /api/chat ────────┐
                       │                                                        │
                       │   Chat UI (ingress) ─────────▶ POST /api/chat/stream ──┤
                       │                                                        ▼
                       │                         homeassistant-agent (HA App, Mac Mini)
                       │                         ┌───────────────────────────────────┐
                       │                         │ language adapter (if enabled) ◀──┼──▶ lang-mt (ASUS)
                       │                         │        │ English text            │    detect + translate
                       │                         │        ▼                          │
                       │                         │ agent core (English only,         │
                       │                         │ FastPathMiddleware / Needle) ─────┼──▶ LLM (ASUS)
                       │                         └───────────────────────────────────┘
                       │
                       └─ TTS  ─▶ wyoming-tts-router (ASUS) ──▶ Piper (en, ru) / ET engine
```

**Design principle.** Translation happens in exactly one place: the adapter, via `lang-mt`. STT and TTS are only language-aware and never translate.

## 5. Language adapter (`app/i18n/`)

### 5.1 Placement in the code

The adapter is **not** a LangGraph middleware. It wraps the HTTP handlers, before `agent.ainvoke` / `stream_events` and after them. That keeps it outside the graph, so the fast path, tool subsetting and history capping all see English exactly as today.

```
app/i18n/
  __init__.py
  adapter.py      # LanguageAdapter: inbound(), outbound()
  client.py       # async HTTP client for lang-mt, timeouts, fail-open
  glossary.py     # entity protection (placeholders)
  store.py        # per-thread language + native-text overlay (phase 5)
```

```python
@dataclass
class Inbound:
    english_text: str
    language: str            # "en" | "ru" | "et"
    confidence: float
    original_text: str
    translated: bool

class LanguageAdapter:
    async def inbound(self, text: str, thread_id: str) -> Inbound: ...
    async def outbound(self, reply_en: str, inbound: Inbound) -> str: ...
```

When the flag is off, a `NoopLanguageAdapter` returns the input unchanged. Handlers always call the adapter and never branch on config themselves.

### 5.2 Inbound flow

1. **Protect entities.** Glossary terms in the text are replaced with placeholders such as `⟦E1⟧` (section 5.5).
2. **Detect and translate** in one call: `POST lang-mt /v1/translate` with `src: "auto"`, `tgt: "en"`, `allowed: [en, ru, et]` and `prior` set to the thread's last language.
3. **Decide.** If the detected language is `en`, the service returns the text unchanged and no translation is done. If confidence is below `min_confidence`, use the thread's last language; if there is none, use `default_language`.
4. **Restore entities** as their English canonical names.
5. **Remember** the thread's language (in memory; persisted in phase 5).

**Why the thread prior.** One- or two-word commands ("свет", "tuled") are hard to detect. The language the user used in the previous message is the best tiebreaker.

### 5.3 Outbound flow

- If the message language is `en`, return the reply unchanged.
- Otherwise protect entities in the reply, translate `en → lang`, and restore entities as native names.
- On failure or timeout, return the English reply. The user gets an answer in English rather than no answer.

### 5.4 Endpoint behaviour

**`/api/chat` (gosling, voice)**

- `ChatRequest.message` goes through `inbound()` and `ChatResponse.reply` through `outbound()`.
- The response gains optional fields `language` and `reply_en`. Gosling ignores unknown fields, so nothing changes there.

**`/api/chat/stream` (chat UI)**

- `inbound()` runs before `stream_events`.
- `thinking`, `tool_call`, `tool_result` and `token` events stream in English as today. Tokens cannot be translated incrementally, and the English stream still shows progress.
- The `done` event carries the translated `reply`, plus `reply_en` and `language`.
- **Frontend change:** on `done`, if `language !== "en"`, replace the bubble text with `evt.reply`. The user sees the English answer stream in, then switch to their language. A later option is a toggle to hide English tokens for non-English messages and show only the loading word until `done`.

### 5.5 Entity glossary

Translation models mangle proper nouns and device names. The "Конкорд" LEGO lights may come back as "Concord" or be translated literally. The glossary maps each entity to its forms in every language:

```yaml
# /config/gosling/glossary.yaml
- id: concorde_lights
  en: Concorde
  ru: [Конкорд, Конкорда, Конкорду]     # include inflected forms
  et: [Concorde, Concorde'i]
- id: living_room
  en: living room
  ru: [гостиная, гостиной, гостиную]
  et: [elutuba, elutoas, elutuppa]
```

**Rules**

- Matching is case-insensitive and longest-match-first.
- Inbound: a match becomes a placeholder, then is restored as the English canonical form.
- Outbound: the English form becomes a placeholder, then is restored as the first native form (the nominative).
- Russian and Estonian are heavily inflected, so the inbound forms list matters. Nominative-only restoration on output can produce slightly off grammar. That is acceptable for v1.

**Why in the app, not in `lang-mt`.** The app already talks to HA. A later step can generate room entries from HA areas, and `lang-mt` stays a generic translation service.

### 5.6 Config

New options in `config.yaml` / `Settings`:

```yaml
language_layer_enabled: false
lang_mt_url: "http://192.168.1.4:8765"
languages: [en, ru, et]
default_language: en
min_confidence: 0.5
lang_mt_timeout_s: 3.0
glossary_path: "/config/gosling/glossary.yaml"
```

Startup does not fail if `lang-mt` is unreachable. It logs a warning, and every request fails open.

### 5.7 Telemetry

The attributes are added to the existing root span (`invoke_agent gosling`) under the `gosling.lang.*` namespace, next to `gosling.input.text` and `gosling.output.text`:

|Attribute|Example|
|:--|:--|
|`gosling.lang.enabled`|`true`|
|`gosling.lang.detected` / `.confidence` / `.source`|`ru`, `0.94`, `detector` \| `thread_prior` \| `default`|
|`gosling.lang.original_text`|`выключи свет в спальне`|
|`gosling.lang.reply_native`|`Выключил свет в спальне.`|
|`gosling.lang.glossary_hits`|`["bedroom"]`|
|`gosling.lang.error`|`mt_timeout`, `mt_unavailable`, …|

**Meaning of the existing attributes.** `gosling.input.text` and `gosling.output.text` hold the **English** text the agent actually saw and produced. The Needle labelling data therefore stays English with no change to the existing tooling.

Child spans `lang.inbound` and `lang.outbound` carry the timings, so they show up in Grafana next to `fast_path.classify` and `chat`.

### 5.8 History (phase 5)

The checkpointer stores English messages, so `/api/history` returns English turns after a page reload. A small SQLite overlay table in `store.py` fixes that:

```
lang_overlay(thread_id, turn_index, language, original_text, reply_native)
```

`/api/history` substitutes the native texts where an overlay row exists. The agent's own memory stays English.

## 6. `lang-mt` service (ASUS)

A stateless FastAPI service. It knows nothing about the agent, HA or entities.

### `POST /v1/translate`

**Request**

```json
{
  "text": "выключи свет в ⟦E1⟧",
  "src": "auto",
  "tgt": "en",
  "allowed": ["en", "ru", "et"],
  "prior": "ru"
}
```

**Response `200`**

```json
{
  "text": "turn off the light in ⟦E1⟧",
  "src": "ru",
  "confidence": 0.94,
  "translated": true,
  "timings_ms": { "detect": 2, "translate": 180 }
}
```

- `src: "auto"` runs lingua restricted to `allowed`. When `src` resolves to `tgt`, the text is returned unchanged with `translated: false`.
- Placeholders must survive translation. The service checks that every `⟦En⟧` in the input also appears in the output. If one is lost, it retries once with constrained decoding or returns `422`, and the adapter fails open.

### `POST /v1/detect`

Detection only. Used by `wyoming-tts-router` and for debugging.

### `GET /healthz`

Reports loaded models per language pair.

**Models.** CTranslate2 (int8, CPU), chosen per language pair by the eval (section 9). The candidates are `nllb-200-distilled-600M` and TartuNLP models for ET. They sit behind one interface, so the model can differ per pair:

```python
class Translator(Protocol):
    def translate(self, text: str, src: str, tgt: str) -> str: ...
```

## 7. Voice path

### 7.1 `wyoming-stt-whitelist`

A Wyoming STT server wrapping faster-whisper. It replaces the `rhasspy/wyoming-whisper` container.

**Per utterance**

1. Run `model.detect_language(audio)`.
2. Keep only en/ru/et and renormalise. If the top probability is below `min_confidence`, use `fallback_language`.
3. Run `model.transcribe(audio, language=lang, task="transcribe")`. The output is native text, with no Whisper translation.

It ignores the language HA passes in, and advertises en, ru and et so HA offers it for the pipeline.

**Why the whitelist.** Short Estonian commands are easily detected as Finnish. Restricting the candidates to the household languages removes that failure mode.

```yaml
model: small-int8          # evaluate medium-int8 if ET accuracy is poor
allowed_languages: [en, ru, et]
min_confidence: 0.5
fallback_language: en
beam_size: 5
```

`small` is weak for Estonian. If the eval shows poor ET accuracy, try `medium-int8` or an Estonian fine-tuned Whisper converted to CTranslate2.

### 7.2 `wyoming-tts-router`

A Wyoming TTS server in front of the real engines.

1. Receive the text to synthesise.
2. Detect its language in-process with the shared detector from `speech_common` (section 11.1). No call to `lang-mt` is needed.
3. Forward to the mapped voice and stream the audio back.

It advertises a single voice, **"auto"**, under en, ru and et, so HA offers it whatever the pipeline language is.

```yaml
voices:
  en: piper:en_US-<voice>
  ru: piper:ru_RU-<voice>
  et: <engine>:<voice>
default: en
```

**Estonian voice** (funny-sounding is acceptable), in order of preference:

1. A dedicated Estonian model (for example an MMS-TTS Estonian checkpoint), if it runs acceptably on CPU.
2. A Piper Finnish voice reading Estonian text: close phonetically, and zero extra dependencies.

**Why detect on the reply text.** HA fixes the TTS voice per pipeline and does not carry the conversation's language through to TTS. Detecting on the text keeps the router stateless.

### 7.3 Custom voices (later)

Piper voices can be fine-tuned from an existing checkpoint with roughly one to two hours of clean recordings per language. espeak-ng supports Estonian, so an Estonian voice can be fine-tuned from a Finnish checkpoint. Recording the same speaker in all three languages gives one consistent voice.

Training needs a real GPU, so rent one or use Colab rather than the GTX 1050. Inference is ordinary Piper on CPU. A custom voice is just a new entry in the router's voice map; nothing else changes.

### 7.4 Assist pipeline configuration

HA allows one language per pipeline. With this design that language becomes a label:

|Setting|Value|Why|
|:--|:--|:--|
|Language|English|Label only. STT ignores it, gosling accepts all languages (`MATCH_ALL`), the TTS router serves every language.|
|STT|`wyoming-stt-whitelist`|Detects en/ru/et itself|
|Conversation agent|Gosling|Unchanged|
|Prefer handling commands locally|**Off**|Otherwise HA's built-in English intents catch some commands before gosling, bypassing the adapter and telemetry|
|TTS|`wyoming-tts-router`, voice "auto"|Picks the voice from the reply text|

The pipeline language is always "en", so gosling passes no language hint. The adapter relies on text detection plus the thread prior.

The current "Gosling / Russian" pipeline, which uses Whisper translate, stays as the working setup until phase 3 and is then replaced by this single pipeline.

## 8. Failure modes

|Failure|Behaviour|
|:--|:--|
|`language_layer_enabled: false`|Identical to today; `lang-mt` never called|
|`lang-mt` unreachable or timeout|Inbound: original text goes to the agent. Outbound: English reply. `gosling.lang.error` is set.|
|Placeholder lost in translation|`lang-mt` returns 422; the adapter falls back to the untranslated text for that direction|
|Low detection confidence|Thread prior, then `default_language`|
|TTS router cannot detect|`default` voice (en)|
|ASUS box down|Voice STT/TTS and the LLM are already down in this case. Chat without the adapter behaves as today.|

## 9. Evaluation plan (before choosing models)

**Dataset.** 30–50 real household commands and questions per language (ru, et, en), including:

- glossary entities (device names such as the Concorde lights, rooms)
- short commands (2–4 words), the hardest case for detection
- questions whose answers include numbers or entity names

**Voice set.** The same phrases recorded as audio on real satellites and the phone, spoken by each household member.

**Metrics**

- STT: language detection accuracy with the whitelist; WER per language.
- Text detection: lingua accuracy on transcripts and chat text, with and without the thread prior.
- MT: does the translated English lead to the same Needle/agent tool call as the reference English command? This matters more than BLEU.
- Latency: p50/p95 for `lang.inbound`, `lang.outbound` and end to end.

**Decisions this eval makes**

- Whisper model size (small or medium, possibly an ET fine-tune).
- MT model per language pair (NLLB vs TartuNLP for ET).
- Whether outbound MT fits the voice latency budget. If not, ET voice replies could fall back to English while chat stays translated.

## 10. Build phases

|Phase|Scope|Done when|
|:--|:--|:--|
|1|`lang-mt` service; adapter + glossary on `/api/chat`; config flag; telemetry|ru/et text to `/api/chat` (curl, gosling text input) triggers the correct tools and replies natively|
|2|Adapter on `/api/chat/stream`; frontend `done` replacement|Chat UI works in all three languages|
|3|`wyoming-stt-whitelist` replaces the translate container|Native transcription; detection accuracy acceptable in the eval|
|4|`wyoming-tts-router` with ET voice; single pipeline per section 7.4|Replies are spoken in the language they were asked in|
|5|History overlay|Reloaded chat shows native text|
|Later|Custom Piper voices|—|

Each phase ships on its own. Phase 1 already works with today's voice setup: the Russian pipeline sends Whisper-translated English, which the adapter detects as `en` and passes through untouched.

## 11. Deployment

### 11.1 Repository layout

The three ASUS services live in one repo (working name `gosling-speech`) as separate packages with a shared core, deployed with one Docker Compose file. They stay **separate containers**: one repo does not mean one process. A crash or restart in the TTS router must not take down STT.

```
gosling-speech/
  packages/
    speech_common/        # shared: language detection (lingua, whitelist),
                          # config loading, CT2 model cache, logging/OTel setup
    lang_mt/              # FastAPI: /v1/translate, /v1/detect, /healthz
    stt_whitelist/        # Wyoming STT server (faster-whisper)
    tts_router/           # Wyoming TTS server -> Piper / ET engine
  docker/
    Dockerfile            # one base image; the service is chosen by command
  config/
    lang-mt.yaml
    stt.yaml
    voices.yaml
  compose.yaml
  eval/                   # section 9 datasets and scripts
  pyproject.toml          # uv workspace
```

**What the shared core buys**

- **One detection implementation.** `tts_router` imports the detector from `speech_common` and runs it in-process, so it needs no HTTP call to `lang-mt`. Detection behaves identically in STT, MT and TTS.
- **One image.** faster-whisper and the MT models share the CTranslate2 runtime, so a single image with one dependency lock serves all three; each service is picked by its `command`.
- **One model cache volume**, and the eval code lives next to the services it tests.

The agent-side adapter (`app/i18n/`) stays in `homeassistant-agent`. It only talks HTTP to `lang-mt` and must not depend on this repo's packages.

### 11.2 Compose

```yaml
services:
  lang-mt:
    image: gosling-speech:latest
    command: ["lang-mt", "--config", "/config/lang-mt.yaml"]
    ports: ["8765:8765"]
    volumes: ["./config:/config:ro", "models:/models"]
    restart: unless-stopped

  stt:
    image: gosling-speech:latest
    command: ["stt-whitelist", "--config", "/config/stt.yaml"]
    ports: ["10300:10300"]
    volumes: ["./config:/config:ro", "models:/models"]
    restart: unless-stopped

  tts:
    image: gosling-speech:latest
    command: ["tts-router", "--config", "/config/voices.yaml"]
    ports: ["10200:10200"]
    volumes: ["./config:/config:ro", "models:/models"]
    restart: unless-stopped

volumes:
  models:
```

Everything is CPU only, so the GPU stays reserved for the LLM. The ports follow the Wyoming defaults, so the HA Wyoming integration entries only need their host changed.

### 11.3 Mac Mini (HA)

- The adapter ships inside the `homeassistant-agent` App. It adds only an HTTP client and a YAML parser, and no model dependencies.
- The glossary is a file under `/config`, editable without rebuilding the App.

|Service|Runtime|Est. RAM|
|:--|:--|:--|
|`lang-mt`|FastAPI + lingua + CT2 MT|~1 GB|
|`stt`|faster-whisper `small-int8`|~0.5 GB|
|`tts`|router + Piper voices (+ ET engine)|~0.3–0.8 GB|

The RAM figures are estimates to verify in phase 1.

## 12. Risks and open questions

- **Estonian quality end to end.** Weak STT, MT and TTS for Estonian all compound. The eval in section 9 shows it before phase 3.
- **Short-utterance detection.** One- or two-word commands may be ambiguous. Mitigations: STT whitelist, thread prior, confidence fallback.
- **Glossary inflection.** Missing forms mean missed matches. Comparing `gosling.lang.glossary_hits` with agent failures will show the gaps.
- **Placeholder robustness.** Some MT models reorder or drop unusual tokens. The placeholder format needs testing per model in the eval.
- **Streaming UX.** English tokens followed by a switch to the native reply may feel odd. Revisit after phase 2 with real use.
- **Latency.** Two MT passes on CPU plus two LAN hops (Mac Mini ↔ ASUS). Measure before committing to outbound MT on the voice path.
- **Mixed-language text.** Out of scope for v1. The dominant language wins.