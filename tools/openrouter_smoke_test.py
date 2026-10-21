"""One-request OpenRouter free-route connectivity check; never touches JobFinder state."""

from __future__ import annotations

import os
import sys

import auto_jobfinder_cloud as processor


def main() -> int:
    if not os.getenv("OPENROUTER_API_KEY", "").strip():
        print(
            "AI OPENROUTER SMOKE TEST SKIPPED: OPENROUTER_API_KEY is not configured "
            "for this workflow."
        )
        return 0

    model = processor._configured_openrouter_model()
    result = processor._openrouter_json_request(
        messages=[
            {
                "role": "system",
                "content": "Return exactly one valid JSON object. Do not add prose.",
            },
            {
                "role": "user",
                "content": (
                    'Return {"ok": true, "probe": "jobfinder-openrouter-free-smoke"} '
                    "and nothing else."
                ),
            },
        ],
        temperature=0,
        max_completion_tokens=80,
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("OpenRouter did not return the expected JSON probe result.")

    print(
        "AI OPENROUTER SMOKE TEST PASSED: "
        f"{model} returned valid JSON. No email or job application was attempted."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Provider helper sanitizes URLs, tokens, and API keys from diagnostics.
        print(
            "AI OPENROUTER SMOKE TEST FAILED: "
            + processor._safe_ai_provider_error(exc)
        )
        sys.exit(1)
