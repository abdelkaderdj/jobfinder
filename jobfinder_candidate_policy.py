"""Canonical owner-defined JobFinder candidate policy.

This module is the single source for experience thresholds and the policy wording
shared by the extraction prompt, independent APPLY reviewer, and deterministic
decision gate. Changes to candidate scope must be reflected here and in the
regression tests before a workflow can be approved.
"""

from __future__ import annotations

import math
from typing import Any


# The owner explicitly asked JobFinder to consider applications requiring up to
# and including four years of experience. Experience alone is not a rejection
# reason at or below this limit.
MAX_EXPERIENCE_YEARS_APPLY = 4.0


# Human review is used when the employer explicitly requires experience but
# gives no usable number. The candidate's eligibility cannot be safely inferred.
UNQUANTIFIED_MANDATORY_EXPERIENCE_DECISION = "review"


def classify_experience_requirement(years: Any) -> str | None:
    """Classify an explicit minimum/lower-bound experience requirement."""
    if years is None or isinstance(years, bool):
        return None
    try:
        value = float(years)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if value <= MAX_EXPERIENCE_YEARS_APPLY:
        return "waive_experience"
    return "reject"


def experience_policy_prompt_text() -> str:
    """Render the same thresholds into every model prompt that uses this policy."""
    limit = f"{MAX_EXPERIENCE_YEARS_APPLY:g}"
    return (
        "EXPERIENCE ELIGIBILITY POLICY — AUTHORITATIVE; THE CODE ENFORCES THESE THRESHOLDS\n"
        "- Do not invent a minimum experience requirement when the advertisement does not state one.\n"
        f"- An explicit minimum of up to and including {limit} years: experience alone MUST NOT cause REJECT or REVIEW. If the role otherwise fits and no independent blocker applies, choose APPLY.\n"
        "- Do not treat absence of professional experience as a second reason to reject or review when it is the same waived minimum-years condition.\n"
        f"- More than {limit} years as an explicit minimum: REJECT on experience grounds.\n"
        "- For a stated range, preserve the lower/minimum endpoint; the deterministic policy layer applies the threshold.\n"
        "- An optional/preferred experience value is not a mandatory minimum and must not trigger the experience rejection rule.\n"
        "- An upper bound such as 'up to 4 years', '4 years maximum', or '4 ans maximum' is not a minimum requirement.\n"
        "- If professional experience is explicitly mandatory but no number or usable lower bound is stated, choose REVIEW; do not invent a threshold or reject automatically.\n"
        "- This policy applies to experience ONLY. It does not override a mismatched discipline, mandatory degree/certificate/licence, seniority, location, or any other eligibility rule.\n"
        "- Extract and report the employer's actual experience wording. Do not independently override the thresholds above based on intuition."
    )


def candidate_role_policy_prompt_text() -> str:
    """Shared candidate-scope rules for extraction and independent APPLY review."""
    return (
        "CANDIDATE ROLE-SCOPE POLICY — OWNER-DEFINED; APPLY TO EACH POSITION SEPARATELY\n"
        "- Candidate qualification: Licence/Bac+3 in Mechanical and Energy Engineering (2021); not an engineering diploma. The candidate has no professional engineering work experience. Knowledge, coursework, and training are not professional experience.\n"
        "- Primary target lanes: mechanical maintenance; industrial/mechanical equipment; refrigeration, froid, frigoriste, HVAC and thermal systems; thermodynamics and heat transfer; centrifugal pumps and fluid systems; junior mechanical design, CAD, SolidWorks, and technical drawings.\n"
        "- Closely adjacent junior technical/operator roles may be considered only when the duties in THAT position's own section show a genuine connection to the target lanes. A company's industry or a shared word elsewhere in the post is not evidence.\n"
        "- Hard exclusions: sales/commercial, marketing, teaching/training, administration, accounting, HR, laboratory/biology/chemistry, electrical-only, electromechanical, instrumentation/automation, civil engineering, HSE, aviation/aircraft, warehouse/inventory/logistics/supply-chain, security, and other unrelated disciplines.\n"
        "- Any position titled Ingénieur, Engineer, or مهندس is REJECT unless THAT position's own advertisement explicitly accepts Licence/Bac+3 as sufficient and does not make a higher engineering degree mandatory. A generic mention of Licence elsewhere in a multi-position post is not enough.\n"
        "- Production technician/production operator positions are REVIEW only; never auto-send solely because the title is technical or the employer is industrial.\n"
        "- Broad generalist/polyvalent roles are REVIEW only when local and when their own duties do not establish a clear target-lane match. Never APPLY based only on a factory/industrial employer.\n"
        "- Never borrow tasks, qualifications, experience, education, location, accommodation, or skills from a neighbouring position. Shared conditions may apply only when the advertisement clearly states they apply to all positions.\n"
        "- A keyword-overlap score is an explainable screening aid, NOT a probability of being accepted and NOT a substitute for title, position-specific duties, or hard eligibility rules."
    )


def candidate_location_policy_prompt_text() -> str:
    """Shared location policy used by model prompts."""
    return (
        "LOCATION ELIGIBILITY POLICY — OWNER-DEFINED\n"
        "- Oran, Mostaganem, and the explicitly configured nearby local areas are eligible.\n"
        "- Southern/Saharan Algerian locations are eligible.\n"
        "- Other known Algerian locations require explicit employer-provided accommodation for THAT position; residency, transport pickup points, or a company address are not accommodation.\n"
        "- Unknown or ambiguous work location is REVIEW, never an automatic APPLY.\n"
        "- Do not infer accommodation, residence, transport, or worksite from unrelated lines or another position."
    )
