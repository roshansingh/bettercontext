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


def _strip_markdown_fence(text: str) -> str:
    """Strip markdown code fences (```json ... ``` or ``` ... ```) from text.

    Uses str ops only. Returns text with the fence wrapper removed when found,
    otherwise returns the original text unchanged.
    """
    stripped = text.strip()
    # Match opening fence: ```json or ``` (with optional trailing whitespace)
    for fence_open in ("```json", "```"):
        if not stripped.startswith(fence_open):
            continue
        after_open = stripped[len(fence_open):]
        # The character immediately after the fence tag must be whitespace or newline
        if after_open and after_open[0] not in (" ", "\t", "\r", "\n"):
            continue
        # Find the closing ```
        close_idx = after_open.rfind("```")
        if close_idx == -1:
            continue
        return after_open[:close_idx].strip()
    return text


def _unwrap_single_key_object(value: Any) -> Any:
    """If value is a dict with exactly one key whose value is a list, return that list.

    Handles responses shaped like {"changes": [...]} or {"claims": [...]}.
    Returns value unchanged if it is not a single-key dict wrapping a list.
    """
    if isinstance(value, dict) and len(value) == 1:
        inner = next(iter(value.values()))
        if isinstance(inner, list):
            return inner
    return value


def _extract_json(text: str) -> Any:
    """Find and parse the first JSON array/object in text using str ops (no regex).

    Handles:
      (a) markdown-fenced blocks (```json ... ``` or bare ```)
      (b) top-level JSON object wrapping the array under a single key
      (c) leading/trailing prose (bracket-scan with proper string/escape tracking)
    """
    # Step 1: try stripping markdown fence first
    defenced = _strip_markdown_fence(text)
    if defenced != text:
        # Fence found — try parsing the defenced content directly
        try:
            parsed = json.loads(defenced)
            return _unwrap_single_key_object(parsed)
        except json.JSONDecodeError:
            pass
        # Fall through to bracket-scan on defenced content
        text = defenced

    # Step 2: bracket-scan for first JSON array or object
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = text.find(start_char)
        if start == -1:
            continue
        # Walk forward to find the matching close; track string context to handle
        # nested brackets inside string values correctly.
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
                        parsed = json.loads(candidate)
                        return _unwrap_single_key_object(parsed)
                    except json.JSONDecodeError:
                        break
    return None
