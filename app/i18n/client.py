"""Async client for the lang-mt translation service.

Every failure — timeout, connection refused, bad status, unparsable body —
becomes a LangMTError with a short code. The adapter turns that into
fail-open behaviour and a telemetry attribute; nothing here retries.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class LangMTError(Exception):
    """A lang-mt call did not produce a usable translation."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(slots=True)
class MTResult:
    text: str
    src: str
    confidence: float
    translated: bool


class LangMTClient:
    def __init__(
        self,
        base_url: str,
        timeout_s: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def translate(
        self,
        text: str,
        src: str,
        tgt: str,
        allowed: list[str],
        prior: str | None = None,
    ) -> MTResult:
        payload = {"text": text, "src": src, "tgt": tgt, "allowed": list(allowed)}
        if prior:
            payload["prior"] = prior

        try:
            resp = await self._client.post(
                f"{self._base_url}/v1/translate",
                json=payload,
                timeout=self._timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise LangMTError("mt_timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LangMTError("mt_unavailable", str(exc)) from exc

        if resp.status_code == 422:
            raise LangMTError("mt_placeholder_lost", resp.text)
        if resp.status_code != 200:
            raise LangMTError("mt_error", f"HTTP {resp.status_code}")

        try:
            body = resp.json()
            return MTResult(
                text=body["text"],
                src=body["src"],
                confidence=float(body.get("confidence", 0.0)),
                translated=bool(body.get("translated", False)),
            )
        except Exception as exc:
            raise LangMTError("mt_error", f"bad response body: {exc}") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
