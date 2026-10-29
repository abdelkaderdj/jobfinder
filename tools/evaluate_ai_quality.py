#!/usr/bin/env python3
"""Run a read-only, labelled evaluation set against the configured JobFinder model route.

This invokes analyze_offer only. It does not call process_post, save bot state,
enqueue Telegram events, or send email.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import inspect
import io
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "data" / "ai_quality_eval_cases.jsonl"


def emit_live_progress(
    event: str,
    index: int = 0,
    total: int = 0,
    case_id: str = "",
    status: str = "",
    passed: bool | None = None,
) -> None:
    """Emit only whitelisted progress metadata outside the encrypted wrapper's capture."""
    if os.getenv("JOBFINDER_EVAL_LIVE_PROGRESS") != "1":
        return
    if event not in {"evaluation_started", "case_started", "case_finished"}:
        return
    safe_case_id = re.sub(r"[^A-Za-z0-9_.:-]", "", str(case_id or ""))[:100]
    payload: dict[str, Any] = {
        "evaluation_progress": {
            "event": event,
            "index": max(0, int(index)),
            "total": max(0, int(total)),
        }
    }
    if safe_case_id:
        payload["evaluation_progress"]["case_id"] = safe_case_id
    if status in {"evaluated", "error"}:
        payload["evaluation_progress"]["status"] = status
    if isinstance(passed, bool):
        payload["evaluation_progress"]["passed"] = passed
    stream = getattr(sys, "__stdout__", None)
    if stream is None:
        return
    try:
        stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\\n")
        stream.flush()
    except Exception:
        # Progress display must never change the evaluation result.
        return


def normalize_for_match(value: Any) -> str:
    """Normalize accents/punctuation while preserving Arabic and Latin letters."""
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = "".join(char if char.isalnum() else " " for char in text)
    return re.sub(r"\s+", " ", text).strip()


def job_title(job: dict[str, Any]) -> str:
    for key in ("job_title", "application_title_fr", "application_title_en"):
        value = str(job.get(key) or "").strip()
        if value:
            return value
    return ""


def title_matches_aliases(title: str, aliases: list[str]) -> bool:
    normalized = normalize_for_match(title)
    return bool(normalized) and any(
        normalized == normalize_for_match(alias)
        for alias in aliases
        if normalize_for_match(alias)
    )


def source_quote_is_grounded(quote: Any, source_text: str) -> bool:
    normalized_quote = normalize_for_match(quote)
    normalized_source = normalize_for_match(source_text)
    return len(normalized_quote) >= 8 and normalized_quote in normalized_source


def load_cases(dataset_path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with dataset_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{dataset_path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(item, dict) or not item.get("id") or not item.get("source_text"):
                raise ValueError(f"{dataset_path}:{line_number}: case needs id and source_text")
            if not item.get("expected_post_type"):
                raise ValueError(f"{dataset_path}:{line_number}: case needs expected_post_type")
            for field in ("expected_titles", "expected_decisions"):
                if not isinstance(item.get(field, []), list):
                    raise ValueError(f"{dataset_path}:{line_number}: {field} must be a list")
            cases.append(item)
    if not cases:
        raise ValueError(f"No evaluation cases found in {dataset_path}")
    return cases


def _match_expected_titles(
    expected_titles: list[dict[str, Any]],
    predicted_jobs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """One-to-one match expected title labels to predictions to avoid double counting."""
    unmatched_indices = set(range(len(predicted_jobs)))
    matched: list[dict[str, Any]] = []
    missing: list[str] = []
    for expected in expected_titles:
        aliases = [str(value) for value in expected.get("aliases", [])]
        match_index = next(
            (
                index for index in sorted(unmatched_indices)
                if title_matches_aliases(job_title(predicted_jobs[index]), aliases)
            ),
            None,
        )
        if match_index is None:
            missing.append(aliases[0] if aliases else "<missing alias>")
            continue
        unmatched_indices.remove(match_index)
        matched.append({
            "expected_aliases": aliases,
            "predicted_title": job_title(predicted_jobs[match_index]),
            "job_index": match_index,
        })
    unexpected = [job_title(predicted_jobs[index]) for index in sorted(unmatched_indices)]
    return matched, missing, unexpected


def _default_decision_transform(
    job: dict[str, Any],
    source_text: str,
    processor: Any,
) -> tuple[str, str]:
    cv_score, _groups = processor.calculate_candidate_cv_match(job, source_text)
    final_decision, reason = processor.enforce_candidate_fit_rule(
        dict(job),
        str(job.get("decision") or "REVIEW"),
        source_text,
        cv_match_score=cv_score,
    )
    return str(final_decision or "UNKNOWN").upper(), str(reason or "")


def evaluate_case(
    case: dict[str, Any],
    result: Any,
    decision_transform: Callable[[dict[str, Any], str], tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Compare one model response against manually specified expected behaviour."""
    source_text = str(case.get("source_text") or "")
    expected_post_type = str(case.get("expected_post_type") or "").strip()
    if not isinstance(result, dict):
        result = {}
    predicted_jobs_raw = result.get("jobs")
    predicted_jobs = [
        item for item in predicted_jobs_raw
        if isinstance(item, dict)
    ] if isinstance(predicted_jobs_raw, list) else []
    actual_post_type = str(result.get("post_type") or "missing").strip()

    expected_titles = case.get("expected_titles") or []
    matched_titles, missing_titles, unexpected_titles = _match_expected_titles(
        expected_titles, predicted_jobs
    )
    title_errors = missing_titles + unexpected_titles
    forbidden_aliases = [str(value) for value in (case.get("forbidden_titles") or [])]
    forbidden_hits = [
        job_title(job) for job in predicted_jobs
        if title_matches_aliases(job_title(job), forbidden_aliases)
    ]

    evidence_errors: list[str] = []
    type_evidence = result.get("post_type_evidence")
    if not isinstance(type_evidence, list) or not type_evidence:
        evidence_errors.append("missing post_type_evidence")
    else:
        for index, quote in enumerate(type_evidence):
            if not source_quote_is_grounded(quote, source_text):
                evidence_errors.append(f"ungrounded post_type_evidence[{index}]")

    for index, job in enumerate(predicted_jobs):
        quote = job.get("title_evidence")
        if not source_quote_is_grounded(quote, source_text):
            evidence_errors.append(f"ungrounded title_evidence for job {index}: {job_title(job)}")

    non_vacancy_with_jobs = actual_post_type != "actual_vacancy" and bool(predicted_jobs)
    decision_checks: list[dict[str, Any]] = []
    expected_decisions = case.get("expected_decisions") or []
    for expected in expected_decisions:
        aliases = [str(value) for value in expected.get("title_aliases", [])]
        expected_decision = str(expected.get("expected") or "").strip().upper()
        matching_job = next(
            (
                job for job in predicted_jobs
                if title_matches_aliases(job_title(job), aliases)
            ),
            None,
        )
        if matching_job is None:
            decision_checks.append({
                "expected_aliases": aliases,
                "expected": expected_decision,
                "raw": "MISSING",
                "final": "MISSING",
                "passed": False,
                "reason": "expected job title was not extracted",
            })
            continue
        raw_decision = str(matching_job.get("decision") or "MISSING").strip().upper()
        final_decision, reason = (
            decision_transform(matching_job, source_text)
            if decision_transform is not None
            else (raw_decision, str(matching_job.get("reason") or ""))
        )
        decision_checks.append({
            "expected_aliases": aliases,
            "predicted_title": job_title(matching_job),
            "expected": expected_decision,
            "raw": raw_decision,
            "after_candidate_gate": str(final_decision or "MISSING").upper(),
            "passed": str(final_decision or "MISSING").upper() == expected_decision,
            "reason": str(reason or "")[:300],
        })

    expected_count = len(expected_titles)
    predicted_count = len(predicted_jobs)
    matched_count = len(matched_titles)
    precision = (
        matched_count / predicted_count
        if predicted_count else (1.0 if expected_count == 0 else 0.0)
    )
    recall = matched_count / expected_count if expected_count else 1.0
    return {
        "case_id": str(case.get("id") or ""),
        "provenance": str(case.get("provenance") or ""),
        "expected_post_type": expected_post_type,
        "predicted_post_type": actual_post_type,
        "post_type_passed": actual_post_type == expected_post_type,
        "matched_titles": matched_titles,
        "missing_titles": missing_titles,
        "unexpected_titles": unexpected_titles,
        "title_errors": title_errors,
        "predicted_title_count": predicted_count,
        "title_precision": precision,
        "title_recall": recall,
        "forbidden_title_hits": forbidden_hits,
        "evidence_errors": evidence_errors,
        "non_vacancy_with_jobs": non_vacancy_with_jobs,
        "decision_checks": decision_checks,
        "passed": (
            actual_post_type == expected_post_type
            and not title_errors
            and not forbidden_hits
            and not evidence_errors
            and not non_vacancy_with_jobs
            and all(item["passed"] for item in decision_checks)
        ),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    case_count = len(results)
    expected_title_count = sum(
        len(item.get("matched_titles", [])) + len(item.get("missing_titles", []))
        for item in results
    )
    predicted_title_count = sum(
        int(item.get("predicted_title_count", 0))
        for item in results
    )
    matched_title_count = sum(len(item.get("matched_titles", [])) for item in results)
    decision_checks = [
        decision
        for item in results
        for decision in item.get("decision_checks", [])
    ]
    evidence_error_count = sum(len(item.get("evidence_errors", [])) for item in results)
    return {
        "cases": case_count,
        "cases_passed": sum(1 for item in results if item.get("passed")),
        "post_type_accuracy": round(
            sum(1 for item in results if item.get("post_type_passed")) / case_count, 4
        ) if case_count else 0.0,
        "title_precision": round(
            matched_title_count / predicted_title_count, 4
        ) if predicted_title_count else (1.0 if expected_title_count == 0 else 0.0),
        "title_recall": round(
            matched_title_count / expected_title_count, 4
        ) if expected_title_count else 1.0,
        "evidence_errors": evidence_error_count,
        "forbidden_title_hits": sum(len(item.get("forbidden_title_hits", [])) for item in results),
        "non_vacancy_with_jobs": sum(1 for item in results if item.get("non_vacancy_with_jobs")),
        "decision_checks": len(decision_checks),
        "raw_model_decision_accuracy": round(
            sum(1 for item in decision_checks if item.get("raw") == item.get("expected"))
            / len(decision_checks), 4
        ) if decision_checks else None,
        "decision_accuracy_after_candidate_gate": round(
            sum(1 for item in decision_checks if item.get("passed")) / len(decision_checks), 4
        ) if decision_checks else None,
        "failed_case_ids": [item["case_id"] for item in results if not item.get("passed")],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON report path")
    parser.add_argument("--limit", type=int, default=0, help="Run only the first N cases")
    parser.add_argument("--strict", action="store_true", help="Return exit code 1 if any case fails")
    args = parser.parse_args(argv)

    try:
        cases = load_cases(args.dataset)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.limit > 0:
        cases = cases[:args.limit]

    if not any(os.getenv(name, "").strip() for name in (
        "OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"
    )):
        parser.error("configure an AI provider key before running model evaluations")

    sys.path.insert(0, str(ROOT))
    try:
        import auto_jobfinder_cloud as processor
        import jobfinder_candidate_policy
    except Exception as exc:
        parser.error(f"cannot import JobFinder analysis code: {type(exc).__name__}: {exc}")

    prompt_fingerprint = hashlib.sha256(
        (
            inspect.getsource(processor.analyze_offer)
            + inspect.getsource(jobfinder_candidate_policy.experience_policy_prompt_text)
        ).encode("utf-8")
    ).hexdigest()
    decision_code_fingerprint = hashlib.sha256(
        (
            inspect.getsource(processor.enforce_candidate_fit_rule)
            + inspect.getsource(processor.calculate_candidate_cv_match)
        ).encode("utf-8")
    ).hexdigest()
    outputs: list[dict[str, Any]] = []
    emit_live_progress("evaluation_started", total=len(cases))
    for index, case in enumerate(cases, start=1):
        emit_live_progress(
            "case_started", index=index, total=len(cases), case_id=str(case.get("id") or "")
        )
        print(f"[{index}/{len(cases)}] Evaluating {case['id']}")
        provider_output = io.StringIO()
        try:
            with redirect_stdout(provider_output):
                response = processor.analyze_offer(str(case["source_text"]))
            provider_lines = provider_output.getvalue().splitlines()
            for line in provider_lines:
                print(line)
            provider_trace = [
                line for line in provider_lines
                if line.startswith((
                    "AI PROVIDER:", "AI OPENAI", "AI GEMINI", "AI GROQ",
                    "AI OPENROUTER", "AI RATE LIMIT", "AI MODEL RETRY",
                ))
            ]
            if not isinstance(response, dict):
                response = {}
            evaluation = evaluate_case(
                case,
                response,
                decision_transform=lambda job, source: _default_decision_transform(
                    job, source, processor
                ),
            )
            evaluation["status"] = "evaluated"
            evaluation["provider_trace"] = provider_trace
            evaluation["predicted_titles"] = [job_title(job) for job in (response.get("jobs") or []) if isinstance(job, dict)]
        except Exception as exc:
            provider_lines = provider_output.getvalue().splitlines()
            for line in provider_lines:
                print(line)
            evaluation = {
                "case_id": case["id"],
                "provenance": case.get("provenance", ""),
                "provider_trace": [
                    line for line in provider_lines
                    if line.startswith((
                        "AI PROVIDER:", "AI OPENAI", "AI GEMINI", "AI GROQ",
                        "AI OPENROUTER", "AI RATE LIMIT", "AI MODEL RETRY",
                    ))
                ],
                "status": "error",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "passed": False,
            }
        outputs.append(evaluation)
        emit_live_progress(
            "case_finished",
            index=index,
            total=len(cases),
            case_id=str(case.get("id") or ""),
            status="error" if evaluation.get("status") == "error" else "evaluated",
            passed=bool(evaluation.get("passed", False)),
        )
        print(
            f"  type={evaluation.get('predicted_post_type', 'ERROR')} "
            f"passed={evaluation.get('passed', False)}"
        )

    summary = summarize([
        item for item in outputs if item.get("status") != "error"
    ])
    error_count = sum(1 for item in outputs if item.get("status") == "error")
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": os.getenv("GITHUB_SHA", "unknown"),
        "prompt_fingerprint": prompt_fingerprint,
        "decision_code_fingerprint": decision_code_fingerprint,
        "configured_model_route": {
            "OPENAI_MODEL": os.getenv("OPENAI_MODEL", ""),
            "GEMINI_MODEL": os.getenv("GEMINI_MODEL", ""),
            "OPENROUTER_MODEL": os.getenv("OPENROUTER_MODEL", ""),
            "route_note": "Provider routing may fail over; use the run logs to identify the actual provider per case.",
        },
        "summary": summary,
        "execution_errors": error_count,
        "results": outputs,
        "side_effects": "analyze_offer only; no email sends, state changes, or job persistence",
    }

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Report written to {args.output}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if args.strict and (
        error_count or summary.get("cases_passed", 0) != summary.get("cases", 0)
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
