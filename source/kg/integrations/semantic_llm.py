from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_SEMANTIC_DIFF_MODEL = "gpt-5.4-mini"

_LITELLM_UNAVAILABLE: Exception | None = None


class SemanticDiffLlmClient:
    """litellm-based client for semantic contract-diff hypothesis generation."""

    def __init__(self, model: str | None = None) -> None:
        self.model = (
            model
            or os.getenv("SUPERCONTEXT_SEMANTIC_DIFF_MODEL")
            or DEFAULT_SEMANTIC_DIFF_MODEL
        )

    def complete_json(self, prompt: str) -> Any:
        """Call the LLM; return parsed JSON or None on any failure."""
        try:
            import litellm  # lazy: core package works without litellm installed
        except ImportError as exc:
            # Propagate ImportError so callers can distinguish unavailable from failure.
            raise ImportError(f"litellm not installed: {exc}") from exc

        try:
            response = litellm.completion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                timeout=30,
                max_tokens=512,
            )
            raw = response.choices[0].message.content or ""
        except Exception:  # noqa: BLE001
            return None

        return _extract_json(raw)


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
