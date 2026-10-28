"""Small, dependency-free safeguards for JobFinder's reasoning and review gates."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def normalize_reasoning_level(value: Any, default: str = "low") -> str:
    """Normalize provider reasoning effort to a supported value."""
    allowed = {"low", "medium", "high"}
    level = str(value or "").strip().lower()
    if level in allowed:
        return level
    fallback = str(default or "").strip().lower()
    return fallback if fallback in allowed else "low"


def _title_key(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def apply_verification_result(
    jobs: list[dict[str, Any]],
    review_result: Any,
    evidence_is_grounded: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """Apply an independent reviewer only as a fail-closed gate for APPLY jobs.

    A reviewer can preserve an existing APPLY only with a matching job index,
    matching title, an explicit safe verdict, and source-grounded evidence.
    It can never turn REVIEW/REJECT into APPLY or modify the underlying decision.
    """
    copied_jobs = [dict(job) if isinstance(job, dict) else job for job in (jobs or [])]
    apply_indexes = [
        index
        for index, job in enumerate(copied_jobs)
        if isinstance(job, dict)
        and str(job.get("decision") or "").strip().upper() == "APPLY"
    ]
    if not apply_indexes:
        return copied_jobs

    reviews = review_result.get("reviews", []) if isinstance(review_result, dict) else []
    if not isinstance(reviews, list):
        reviews = []

    reviews_by_index: dict[int, dict[str, Any]] = {}
    duplicate_indexes: set[int] = set()
    for review in reviews:
        if not isinstance(review, dict):
            continue
        index = review.get("job_index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if index in reviews_by_index:
            duplicate_indexes.add(index)
        else:
            reviews_by_index[index] = review

    for review_index, job_index in enumerate(apply_indexes):
        job = copied_jobs[job_index]
        review = reviews_by_index.get(review_index)
        title_matches = (
            isinstance(review, dict)
            and _title_key(review.get("job_title")) == _title_key(job.get("job_title"))
        )
        quotes = review.get("evidence_quotes", []) if isinstance(review, dict) else []
        if isinstance(quotes, str):
            quotes = [quotes]
        if not isinstance(quotes, list):
            quotes = []
        grounded_evidence = False
        for quote in quotes:
            if not isinstance(quote, str) or not quote.strip():
                continue
            try:
                if evidence_is_grounded(quote):
                    grounded_evidence = True
                    break
            except Exception:
                continue

        verdict = (
            str(review.get("verdict") or "").strip().lower()
            if isinstance(review, dict) else ""
        )
        reason = (
            " ".join(str(review.get("reason") or "").split())[:500]
            if isinstance(review, dict) else ""
        )
        can_keep_apply = (
            review_index not in duplicate_indexes
            and title_matches
            and verdict == "safe_to_keep_apply"
            and grounded_evidence
        )

        if can_keep_apply:
            job["verification_gate"] = "passed"
            job["verification_reason"] = reason or (
                "Independent AI review found no source-grounded disqualifier."
            )
        else:
            job["verification_gate"] = "forced_review"
            job["verification_reason"] = reason or (
                "لم يجتز قرار التقديم التحقق المستقل؛ يلزم فحص بشري قبل أي إرسال."
            )

    return copied_jobs
