from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the repository root importable when this file is run directly by Actions.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# These import-time settings are placeholders only. This tool calls the
# OpenRouter helper directly and never invokes the job processor or email code.
os.environ.setdefault("GROQ_API_KEY", "smoke-test-import-only")
os.environ.setdefault("GMAIL_APP_PASSWORD", "smoke-test-unused")
os.environ.setdefault("GMAIL_ADDRESS", "smoke-test@example.invalid")
os.environ.setdefault("JOBFINDER_CANDIDATE_PROFILE", "OpenRouter smoke test only; no real candidate data.")
os.environ.setdefault("JOBFINDER_CANDIDATE_NAME", "OpenRouter Smoke Test")

import auto_jobfinder_cloud as processor


def main() -> int:
    if not os.getenv("OPENROUTER_API_KEY", "").strip():
        print(
            "OPENROUTER FREE SMOKE TEST BLOCKED: OPENROUTER_API_KEY is not configured; "
            "no API request was sent."
        )
        return 2

    try:
        result = processor._openrouter_json_request(
            [
                {
                    "role": "system",
                    "content": (
                        "This is an API connectivity test. Do not analyze a job, "
                        "do not create an application, and return only valid JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": 'Return exactly this JSON object: {"provider_smoke_test":"ok"}.',
                },
            ],
            max_completion_tokens=80,
        )
    except Exception as exc:
        diagnostic = processor._safe_ai_provider_error(exc)
        print(
            "OPENROUTER FREE SMOKE TEST FAILED: "
            f"{diagnostic}; no email or job application was attempted."
        )
        return 1

    if not isinstance(result, dict) or result.get("provider_smoke_test") != "ok":
        print(
            "OPENROUTER FREE SMOKE TEST FAILED: provider returned unexpected JSON; "
            "no email or job application was attempted."
        )
        return 1

    model = processor._configured_openrouter_model()
    print(
        f"OPENROUTER FREE SMOKE TEST PASSED: {model} returned valid JSON. "
        "One API request was used; no email or job application was attempted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
