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

## Multilingual strategy: translate *in*, at the STT stage

**Do NOT fine-tune Needle for Russian.** Its tokenizer is 8,192 tokens,
English-centric, with **zero Cyrillic** — Russian falls back to byte encoding and
explodes in the 256-token window. Estonian (Latin) is only marginally better.
Needle is an English model; making it multilingual would need a new tokenizer +
base, not a fine-tune.

Instead, solve language at STT — nearly free, because **Whisper has a built-in
`translate` task** (any-language audio → English text in one pass):

```
RU/ET audio → Whisper(translate) → English text → Needle / English agent → action
```

- No separate translation model, no extra latency for the action path.
- Everything downstream stays English: English automations, English Needle, even
  HA's free English template intents keep working unchanged.
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

- Whisper `small` vs `base` on **real Russian short commands** — latency and
  accuracy on the i5-8250U CPU.
- Whisper **`translate` task quality** on terse RU commands (proper nouns like
  "Concorde" surviving translation).
- Where the **app (Needle + agent) runs** in the voice setup — currently the Mac;
  for always-on it'd move to a box that's always up (ties into the deferred
  deployment question).
- Response localization: LLM answering directly in RU vs translate-back for TTS.
