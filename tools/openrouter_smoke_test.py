"""One-request OpenRouter free-route connectivity check; never touches JobFinder state."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Executing a file under tools/ sets sys.path[0] to tools/, not the repo root.
# Add the root so this standalone workflow script can import the provider module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# auto_jobfinder_cloud validates these app-only settings during import. The probe
# never uses them and never reads candidate files, state, or sends applications.
os.environ.setdefault("GROQ_API_KEY", "smoke-test-unused")
os.environ.setdefault("GMAIL_APP_PASSWORD", "smoke-test-unused")
os.environ.setdefault("GMAIL_ADDRESS", "smoke-test@example.invalid")
os.environ.setdefault(
    "JOBFINDER_CANDIDATE_PROFILE",
    "Provider smoke test only; no real candidate data.",
)
os.environ.setdefault("JOBFINDER_CANDIDATE_NAME", "Provider Smoke Test")

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
        max_completion_tokens=512,
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
        print(
            "AI OPENROUTER SMOKE TEST FAILED: "
            + processor._safe_ai_provider_error(exc)
        )
        sys.exit(1)
