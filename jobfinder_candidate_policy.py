"""Canonical candidate experience policy shared by prompts and deterministic gates.

The model must extract the employer's stated requirement. Python owns the final
threshold decision so that the screening prompt and post-processing cannot drift.
"""

from __future__ import annotations

import math
from typing import Any

MAX_EXPERIENCE_YEARS_WAIVED = 3.0
MAX_EXPERIENCE_YEARS_MANUAL_REVIEW = 4.0


def classify_experience_requirement(years: Any) -> str | None:
    """Map an explicitly extracted minimum experience threshold to JobFinder policy."""
    if years is None or isinstance(years, bool):
        return None
    try:
        value = float(years)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if value <= MAX_EXPERIENCE_YEARS_WAIVED:
        return "waive_experience"
    if value <= MAX_EXPERIENCE_YEARS_MANUAL_REVIEW:
        return "manual_review"
    return "reject"


def experience_policy_prompt_text() -> str:
    """Render the same thresholds into every model prompt that needs this policy."""
    waived = f"{MAX_EXPERIENCE_YEARS_WAIVED:g}"
    review_max = f"{MAX_EXPERIENCE_YEARS_MANUAL_REVIEW:g}"
    return (
        "EXPERIENCE ELIGIBILITY POLICY — AUTHORITATIVE; THE CODE ENFORCES THESE THRESHOLDS\n"
        "- Do not invent a minimum experience requirement when the advertisement does not state one.\n"
        f"- Up to and including {waived} years: experience alone is NOT a reason to reject; evaluate every other explicit requirement normally.\n"
        f"- More than {waived} and up to and including {review_max} years: REVIEW only; never send an automatic application.\n"
        f"- More than {review_max} years: REJECT on experience grounds.\n"
        "- For a stated range, preserve the extracted minimum/lower endpoint; the deterministic policy layer applies the threshold.\n"
        "- This policy applies to experience ONLY. It does not override a mismatched discipline, mandatory degree/certificate/licence, seniority, location, or any other eligibility rule.\n"
        "- Extract and report the employer's actual experience wording. Do not independently override the thresholds above based on intuition."
    )
