from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_SEMANTIC_DIFF_MODEL = "gpt-5.4-mini"

_LITELLM_UNAVAILABLE: Exception | None = None

# Auth error substrings (checked on lowercased exception message).
_AUTH_SUBSTRINGS = ("auth", "unauthorized", "401", "api_key", "apikey")

# Sentinel so None can be a valid parsed JSON value.
_MISSING = object()


class LlmResult:
    """Typed result from SemanticDiffLlmClient.complete_json.

    Discriminated by .kind:
      "" (empty string)  — parsed_ok; .value holds the parsed JSON
      "parse_miss"       — call succeeded but no parseable JSON in response
      "no_api_key"       — auth/API-key failure from the provider
      "llm_error"        — provider error (timeout, 5xx, non-auth)
    """

    __slots__ = ("kind", "_value")

    def __init__(self, kind: str, value: Any = _MISSING) -> None:
        self.kind = kind
        self._value = value

    @classmethod
    def parsed(cls, value: Any) -> "LlmResult":
        return cls("", value)

    @classmethod
    def parse_miss(cls) -> "LlmResult":
        return cls("parse_miss")

    @classmethod
    def call_failure(cls, kind: str) -> "LlmResult":
        return cls(kind)

    def is_ok(self) -> bool:
        return self.kind == ""

    @property
    def value(self) -> Any:
        if self._value is _MISSING:
            raise AttributeError(f"LlmResult.value not set (kind={self.kind!r})")
        return self._value


class SemanticDiffLlmClient:
    """litellm-based client for semantic contract-diff hypothesis generation."""

    def __init__(self, model: str | None = None) -> None:
        self.model = (
            model
            or os.getenv("SUPERCONTEXT_SEMANTIC_DIFF_MODEL")
            or DEFAULT_SEMANTIC_DIFF_MODEL
        )

    def complete_json(self, prompt: str) -> LlmResult:
        """Call the LLM; return a typed LlmResult.

        Returns:
          LlmResult.parsed(value)               — JSON parsed from response
          LlmResult.parse_miss()                — call ok, no parseable JSON
          LlmResult.call_failure("no_api_key")  — auth/API-key error
          LlmResult.call_failure("llm_error")   — provider error (timeout, 5xx, etc.)

        Raises:
          ImportError — litellm not installed; propagated so caller routes to
                        "unavailable" status (never caught here).
        """
        import litellm  # lazy: propagated ImportError → "unavailable" in caller

        try:
            response = litellm.completion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                timeout=30,
                max_tokens=512,
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if any(sub in msg for sub in _AUTH_SUBSTRINGS):
                return LlmResult.call_failure("no_api_key")
            return LlmResult.call_failure("llm_error")

        value = _extract_json(raw)
        if value is None:
            return LlmResult.parse_miss()
        return LlmResult.parsed(value)


def _extract_json(text: str) -> Any:
    """Find and parse the first JSON array/object in text using str ops (no regex)."""
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = text.find(start_char)
        if start == -1:
            continue
        # Walk forward to find the matching close
        depth = 0
        in_str = False
        escape_next = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_str:
                escape_next = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    return None
