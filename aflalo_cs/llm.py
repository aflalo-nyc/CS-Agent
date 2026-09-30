"""Anthropic client wrapper.

Three separate, single-purpose calls (classify / draft / verify) rather than one combined
call: each prompt stays small, the routing and Shopify gates run before any drafting cost is
spent, and any one stage can be swapped or tested alone.

All calls use schema-constrained structured output, so the response is guaranteed to match
the expected shape instead of occasionally arriving with a stray sentence around the JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

MODEL = "claude-opus-5"


class LLM(Protocol):
    def structured(
        self,
        *,
        system: list[dict[str, Any]],
        user: str,
        schema: dict[str, Any],
        effort: str = "medium",
        max_tokens: int = 4096,
    ) -> dict[str, Any]: ...


class LLMError(RuntimeError):
    """Any failure that should route the message to needs-human rather than crash the run."""


@dataclass
class AnthropicLLM:
    """Real client. Requires ANTHROPIC_API_KEY, or an `ant auth login` profile."""

    model: str = MODEL
    _client: Any = None

    def __post_init__(self) -> None:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise LLMError("anthropic SDK not installed — pip install anthropic") from exc
            self._client = anthropic.Anthropic()

    def structured(
        self,
        *,
        system: list[dict[str, Any]],
        user: str,
        schema: dict[str, Any],
        effort: str = "medium",
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        import anthropic

        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Anthropic connection error: {exc}") from exc

        # Opus 5 safety classifiers can decline with a normal HTTP 200. Check before content.
        if resp.stop_reason == "refusal":
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            raise LLMError(f"model declined the request (category={category})")
        if resp.stop_reason == "max_tokens":
            raise LLMError("response truncated at max_tokens")

        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            raise LLMError("no text block in response")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"structured output was not valid JSON: {exc}") from exc


def cached_system(*blocks: str) -> list[dict[str, Any]]:
    """Build a system prompt whose stable prefix is cached.

    Brand voice + policy digest are byte-identical on every call, so the breakpoint goes on
    the last block; the per-message content rides in the user turn, after the cached prefix.
    """
    out = [{"type": "text", "text": b} for b in blocks if b]
    if out:
        out[-1]["cache_control"] = {"type": "ephemeral"}
    return out


@dataclass
class FakeLLM:
    """Deterministic stand-in for unit tests.

    Lets tests force conditions on command — a specific classification, a hallucinated draft,
    or an API failure — without spending money or depending on model behaviour.
    """

    responses: list[dict[str, Any] | Exception]
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self.responses:
            raise LLMError("FakeLLM ran out of scripted responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
