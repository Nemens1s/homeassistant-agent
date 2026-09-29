"""The language adapter.

It wraps the HTTP handlers, not the graph: inbound() turns whatever the user
said into English before the agent runs, outbound() turns the English reply
back into the user's language. The agent core, its prompts, tools and stored
messages stay English.

Every failure is fail-open: the agent still answers, in English.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.i18n.client import LangMTClient, LangMTError
from app.i18n.glossary import Glossary
from app.telemetry import conventions as C

log = logging.getLogger("agent.i18n")


@dataclass
class Inbound:
    """What the adapter learned about one user message.

    Carried from inbound() to outbound() and to the telemetry attributes.
    ``error`` is set by whichever direction failed, so outbound() may fill it
    in after inbound() already returned.
    """

    english_text: str
    language: str
    confidence: float
    original_text: str
    translated: bool
    source: str = "detector"  # detector | thread_prior | default
    glossary_hits: list[str] = field(default_factory=list)
    error: str | None = None


class NoopLanguageAdapter:
    """Used when the language layer is off: lang-mt is never called."""

    enabled = False

    async def inbound(self, text: str, thread_id: str) -> Inbound:
        return Inbound(
            english_text=text,
            language="en",
            confidence=1.0,
            original_text=text,
            translated=False,
            source="default",
        )

    async def outbound(self, reply_en: str, inbound: Inbound) -> str:
        return reply_en

    async def aclose(self) -> None:
        return None


class LanguageAdapter:
    enabled = True

    def __init__(
        self,
        client: LangMTClient,
        glossary: Glossary,
        languages: list[str],
        default_language: str = "en",
        min_confidence: float = 0.5,
        tracer=None,
    ) -> None:
        self._tracer = tracer
        self._client = client
        self._glossary = glossary
        self._languages = list(languages)
        self._default_language = default_language
        self._min_confidence = min_confidence
        self._priors: dict[str, str] = {}

    def _span(self, name: str):
        """Child span for one MT hop; the timings are what Grafana plots."""
        tracer = self._tracer
        if tracer is None:
            from app.telemetry.setup import get_tracer
            tracer = get_tracer()
        return tracer.start_as_current_span(name)

    # ---------- inbound ----------
    async def inbound(self, text: str, thread_id: str) -> Inbound:
        with self._span(C.SPAN_LANG_INBOUND) as span:
            result = await self._inbound(text, thread_id)
            try:
                span.set_attribute(C.GOSLING_LANG_DETECTED, result.language)
                span.set_attribute(C.GOSLING_LANG_CONFIDENCE, result.confidence)
                span.set_attribute(C.GOSLING_LANG_SOURCE, result.source)
                if result.error:
                    span.set_attribute(C.GOSLING_LANG_ERROR, result.error)
            except Exception:
                pass
            return result

    async def _inbound(self, text: str, thread_id: str) -> Inbound:
        protected = self._glossary.protect(text)
        prior = self._priors.get(thread_id)

        try:
            result = await self._client.translate(
                protected.text,
                src="auto",
                tgt="en",
                allowed=self._languages,
                prior=prior,
            )
        except LangMTError as exc:
            log.warning("lang: inbound failed (%s) — passing the original text through", exc.code)
            return Inbound(
                english_text=text,
                language=prior or self._default_language,
                confidence=0.0,
                original_text=text,
                translated=False,
                source="thread_prior" if prior else "default",
                glossary_hits=protected.hits,
                error=exc.code,
            )

        language = result.src
        source = "detector"
        english = result.text
        translated = result.translated
        error = None

        if result.confidence < self._min_confidence:
            language = prior or self._default_language
            source = "thread_prior" if prior else "default"
            if language != result.src:
                # The detection we translated from was a guess; redo it with the
                # language we actually believe in.
                english, translated, error = await self._retranslate(protected.text, language)

        if language == "en":
            english = text
            translated = False
        else:
            english = self._glossary.restore(english, protected, "en")

        self._priors[thread_id] = language

        return Inbound(
            english_text=english,
            language=language,
            confidence=result.confidence,
            original_text=text,
            translated=translated,
            source=source,
            glossary_hits=protected.hits,
            error=error,
        )

    async def _retranslate(self, protected_text: str, language: str) -> tuple[str, bool, str | None]:
        if language == "en":
            return protected_text, False, None
        try:
            retry = await self._client.translate(
                protected_text, src=language, tgt="en", allowed=self._languages
            )
        except LangMTError as exc:
            log.warning("lang: inbound retry failed (%s)", exc.code)
            return protected_text, False, exc.code
        return retry.text, retry.translated, None

    # ---------- outbound ----------
    async def outbound(self, reply_en: str, inbound: Inbound) -> str:
        if inbound.language == "en" or not reply_en:
            return reply_en

        with self._span(C.SPAN_LANG_OUTBOUND) as span:
            reply = await self._outbound(reply_en, inbound)
            try:
                span.set_attribute(C.GOSLING_LANG_DETECTED, inbound.language)
                if inbound.error:
                    span.set_attribute(C.GOSLING_LANG_ERROR, inbound.error)
            except Exception:
                pass
            return reply

    async def _outbound(self, reply_en: str, inbound: Inbound) -> str:
        protected = self._glossary.protect(reply_en, languages=["en"])
        try:
            result = await self._client.translate(
                protected.text,
                src="en",
                tgt=inbound.language,
                allowed=self._languages,
            )
        except LangMTError as exc:
            log.warning("lang: outbound failed (%s) — replying in English", exc.code)
            inbound.error = exc.code
            return reply_en

        return self._glossary.restore(result.text, protected, inbound.language)

    async def aclose(self) -> None:
        await self._client.aclose()
