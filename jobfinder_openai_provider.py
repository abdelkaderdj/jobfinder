"""Optional OpenAI Responses API route used for high-reasoning JobFinder tasks."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from jobfinder_ai_quality import normalize_reasoning_level


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_OPENAI_MODEL = "gpt-6.1-sol"


def build_openai_responses_payload(
    messages: list[dict[str, Any]],
    model_name: str | None = None,
    max_output_tokens: int = 8192,
    reasoning_level: str = "high",
) -> dict[str, Any]:
    """Build a Responses API request, keeping roles and requesting JSON output."""
    input_messages: list[dict[str, str]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").strip().lower()
        content = str(message.get("content") or "").strip()
        if role not in {"system", "developer", "user", "assistant"} or not content:
            continue
        input_messages.append({"role": role, "content": content})

    if not input_messages:
        raise ValueError("OpenAI request needs at least one non-empty input message")

    try:
        token_limit = int(max_output_tokens)
    except (TypeError, ValueError):
        token_limit = 8192
    token_limit = max(512, min(token_limit, 65536))

    chosen_model = str(
        model_name or os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_OPENAI_MODEL
    ).strip()
    if not chosen_model:
        raise ValueError("OpenAI model name is empty")

    return {
        "model": chosen_model,
        "input": input_messages,
        "reasoning": {"effort": normalize_reasoning_level(reasoning_level, default="high")},
        "max_output_tokens": token_limit,
        "text": {"format": {"type": "json_object"}},
        # Job ads and candidate details should not be stored by the provider API.
        "store": False,
    }


def extract_openai_output_text(body: Any) -> str:
    """Read text from either Responses API convenience output or the item list."""
    if not isinstance(body, dict):
        return ""
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    chunks: list[str] = []
    output = body.get("output", [])
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"}:
                text = part.get("text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    return "\n".join(chunks).strip()


def openai_json_request(
    messages: list[dict[str, Any]],
    *,
    api_key: str | None = None,
    model_name: str | None = None,
    max_output_tokens: int = 8192,
    reasoning_level: str = "high",
    parser: Callable[[str], Any] | None = None,
    timeout_seconds: int = 120,
) -> Any:
    """Call OpenAI Responses API and parse the JSON-only model output."""
    key = str(api_key or os.getenv("OPENAI_API_KEY", "")).strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    payload = build_openai_responses_payload(
        messages,
        model_name=model_name,
        max_output_tokens=max_output_tokens,
        reasoning_level=reasoning_level,
    )
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(10, int(timeout_seconds))) as response:
            body_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            error_text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            error_text = ""
        # Keep only provider diagnostics; callers sanitize this before public logs.
        error = RuntimeError(f"OpenAI HTTP {exc.code}: {error_text[:1200]}")
        error.status_code = exc.code  # type: ignore[attr-defined]
        error.headers = exc.headers  # type: ignore[attr-defined]
        raise error from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"OpenAI transport error: {type(exc).__name__}") from exc

    try:
        body = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise ValueError("OpenAI returned a non-JSON response envelope") from exc
    if not isinstance(body, dict):
        raise ValueError("OpenAI returned an invalid response envelope")
    if body.get("error"):
        error = body["error"]
        message = error.get("message", "OpenAI API returned an error") if isinstance(error, dict) else str(error)
        status = error.get("code") if isinstance(error, dict) else None
        raise RuntimeError(f"OpenAI response error {status or ''}: {message[:500]}".strip())

    status = str(body.get("status") or "").strip().lower()
    if status and status != "completed":
        details = body.get("incomplete_details")
        raise ValueError(f"OpenAI response was not completed (status={status}, details={details})")

    output_text = extract_openai_output_text(body)
    if not output_text:
        raise ValueError("OpenAI returned an empty response body")
    if parser is not None:
        return parser(output_text)
    try:
        result = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise ValueError("OpenAI response was not valid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("OpenAI response JSON must be an object")
    return result
