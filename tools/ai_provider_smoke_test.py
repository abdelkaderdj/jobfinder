from __future__ import annotations

import os
import sys

# Harmless placeholders; this process never calls any email/application function.
os.environ.setdefault("GROQ_API_KEY", "smoke-test-unused")
os.environ.setdefault("GMAIL_APP_PASSWORD", "smoke-test-unused")
os.environ.setdefault("GMAIL_ADDRESS", "smoke-test@example.invalid")
os.environ.setdefault("JOBFINDER_CANDIDATE_PROFILE", "Provider smoke test only; no real candidate data.")
os.environ.setdefault("JOBFINDER_CANDIDATE_NAME", "Provider Smoke Test")

import auto_jobfinder_cloud as processor


def main() -> int:
    messages = [
        {
            "role": "system",
            "content": (
                "This is an API connectivity test. Do not analyze a real job, "
                "do not create an application, and return only valid JSON."
            ),
        },
        {
            "role": "user",
            "content": 'Return exactly this JSON object: {"provider_smoke_test":"ok"}.',
        },
    ]
    try:
        result = processor.groq_json_request(messages, max_completion_tokens=80)
    except processor.AIProviderRateLimited as exc:
        retry_after = getattr(exc, "retry_after_seconds", 0)
        print(
            "AI PROVIDER SMOKE TEST BLOCKED BY QUOTA: "
            f"all available routes were rate-limited; retry_after_seconds={retry_after}. "
            "No email or job application was attempted."
        )
        return 2
    except Exception as exc:
        print(
            "AI PROVIDER SMOKE TEST FAILED: "
            f"{processor._safe_ai_provider_error(exc)}. "
            "No email or job application was attempted."
        )
        return 1

    if not isinstance(result, dict) or result.get("provider_smoke_test") != "ok":
        print(
            "AI PROVIDER SMOKE TEST FAILED: provider returned unexpected JSON. "
            "No email or job application was attempted."
        )
        return 1

    print(
        "AI PROVIDER SMOKE TEST PASSED: valid JSON returned through the configured "
        "Gemini/Groq router. No email or job application was attempted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
