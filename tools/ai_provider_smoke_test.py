from __future__ import annotations

import os
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

# Running a script from tools/ puts tools/ on sys.path; add the repo root
# so the provider module can be imported consistently in GitHub Actions.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    provider_log = StringIO()
    try:
        # Preserve the router's sanitized diagnostics so the smoke test reveals
        # whether Gemini itself worked or the run survived through Groq fallback.
        with redirect_stdout(provider_log):
            result = processor.groq_json_request(messages, max_completion_tokens=80)
    except processor.AIProviderRateLimited as exc:
        print(provider_log.getvalue(), end="")
        retry_after = getattr(exc, "retry_after_seconds", 0)
        print(
            "AI PROVIDER SMOKE TEST BLOCKED BY QUOTA: "
            f"all available routes were rate-limited; retry_after_seconds={retry_after}. "
            "No email or job application was attempted."
        )
        return 2
    except processor.AIProviderUnavailable as exc:
        print(provider_log.getvalue(), end="")
        retry_after = getattr(exc, "retry_after_seconds", 0)
        reason = str(getattr(exc, "reason", "") or "AI provider routes unavailable")
        print(
            "AI PROVIDER SMOKE TEST BLOCKED: all configured routes are unavailable or rejected "
            "the request; "
            f"reason={reason}; retry_after_seconds={retry_after}; "
            "no email or job application was attempted."
        )
        return 3
    except Exception as exc:
        print(provider_log.getvalue(), end="")
        print(
            "AI PROVIDER SMOKE TEST FAILED: "
            f"{processor._safe_ai_provider_error(exc)}. "
            "No email or job application was attempted."
        )
        return 1

    route_log = provider_log.getvalue()
    print(route_log, end="")
    if not isinstance(result, dict) or result.get("provider_smoke_test") != "ok":
        print(
            "AI PROVIDER SMOKE TEST FAILED: provider returned unexpected JSON. "
            "No email or job application was attempted."
        )
        return 1

    if "AI PROVIDER: Gemini (" in route_log:
        route_summary = "Gemini succeeded"
    elif "AI PROVIDER: Groq (" in route_log and not os.getenv("GEMINI_API_KEY", "").strip():
        route_summary = (
            "Groq succeeded; GEMINI_API_KEY is absent, so Gemini was not tested"
        )
    elif "AI PROVIDER: Groq (" in route_log:
        route_summary = (
            "Groq fallback succeeded; no Gemini model returned a response in this run"
        )
    else:
        route_summary = "router returned valid JSON; inspect the provider diagnostics above"

    print(
        f"AI PROVIDER SMOKE TEST PASSED: {route_summary}. "
        "No email or job application was attempted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
