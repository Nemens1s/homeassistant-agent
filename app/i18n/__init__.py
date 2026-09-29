"""Language adapter at the HTTP edge: the agent core never sees non-English
text, only this package does. Design notes live in
docs/superpowers/specs/2026-09-29-multilingual-language-layer-design.md."""

from __future__ import annotations

import json
import logging

from app.i18n.adapter import Inbound, LanguageAdapter, NoopLanguageAdapter
from app.i18n.client import LangMTClient
from app.i18n.glossary import Glossary
from app.telemetry import conventions as C

log = logging.getLogger("agent.i18n")

__all__ = [
    "Inbound",
    "set_language_attributes",
    "LanguageAdapter",
    "NoopLanguageAdapter",
    "build_language_adapter",
]


def build_language_adapter(settings):
    """Build the adapter for *settings*; a no-op one when the layer is off.

    Startup never fails here: an unreachable lang-mt or a missing glossary is
    a warning, and every request then fails open.
    """
    if not settings.language_layer_enabled:
        return NoopLanguageAdapter()

    glossary = Glossary.load(settings.glossary_path)
    if len(glossary) == 0:
        log.warning("lang: no glossary entries loaded from %s", settings.glossary_path)

    client = LangMTClient(settings.lang_mt_url, timeout_s=settings.lang_mt_timeout_s)
    log.info(
        "lang: layer enabled — mt=%s languages=%s glossary=%d entries",
        settings.lang_mt_url, settings.languages, len(glossary),
    )
    return LanguageAdapter(
        client=client,
        glossary=glossary,
        languages=settings.languages,
        default_language=settings.default_language,
        min_confidence=settings.min_confidence,
    )


def set_language_attributes(span, adapter, inbound=None, reply_native=None) -> None:
    """Put the gosling.lang.* attributes on the request's root span.

    gosling.input.text / gosling.output.text stay English; these attributes are
    what the user actually said and heard. Never raises — telemetry must not
    break a request.
    """
    try:
        enabled = bool(getattr(adapter, "enabled", False))
        span.set_attribute(C.GOSLING_LANG_ENABLED, enabled)
        if not enabled or inbound is None:
            return
        span.set_attribute(C.GOSLING_LANG_DETECTED, inbound.language)
        span.set_attribute(C.GOSLING_LANG_CONFIDENCE, inbound.confidence)
        span.set_attribute(C.GOSLING_LANG_SOURCE, inbound.source)
        span.set_attribute(C.GOSLING_LANG_ORIGINAL_TEXT, inbound.original_text)
        if inbound.glossary_hits:
            span.set_attribute(
                C.GOSLING_LANG_GLOSSARY_HITS, json.dumps(inbound.glossary_hits)
            )
        if reply_native is not None:
            span.set_attribute(C.GOSLING_LANG_REPLY_NATIVE, reply_native)
        if inbound.error:
            span.set_attribute(C.GOSLING_LANG_ERROR, inbound.error)
    except Exception:
        pass
