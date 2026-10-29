#!/usr/bin/env python3
"""Run the labelled AI benchmark with the encrypted candidate profile loaded.

This workflow intentionally emits only aggregate metrics and sanitized provider
success traces. It never sends applications, touches bot state, uploads reports,
or writes decrypted profile/CV files to the repository.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_PROFILE_FILE = ROOT / "cloud_private" / "profile.enc"
PROFILE_ENV_KEYS = ("JOBFINDER_CANDIDATE_PROFILE", "JOBFINDER_CANDIDATE_NAME")
AI_PROVIDER_KEYS = ("OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY")

_PROVIDER_SUCCESS = re.compile(
    r"^AI PROVIDER: (OpenAI|Gemini|Groq|OpenRouter) "
    r"\(([^()\r\n]{1,100})\) succeeded(?: after JSON-mode retry)?$"
)
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.:-]")


def load_encrypted_candidate_profile(
    profile_path: Path,
    fernet: Fernet,
) -> dict[str, str]:
    """Decrypt only the two fields needed by analysis; never return email/CV data."""
    if not profile_path.is_file():
        raise RuntimeError("encrypted candidate profile is missing")
    try:
        payload = json.loads(fernet.decrypt(profile_path.read_bytes()).decode("utf-8"))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("encrypted candidate profile could not be decrypted or parsed") from None
    if not isinstance(payload, dict):
        raise RuntimeError("encrypted candidate profile has an invalid structure")
    profile: dict[str, str] = {}
    for field in PROFILE_ENV_KEYS:
        source_field = "candidate_profile" if field.endswith("PROFILE") else "candidate_name"
        value = payload.get(source_field)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("encrypted candidate profile is incomplete")
        profile[field] = value
    return profile


def _safe_id(value: Any) -> str:
    return _SAFE_ID.sub("", str(value or ""))[:100]


def _successful_provider_routes(trace: Any) -> list[dict[str, str]]:
    """Keep only exact successful-provider records; discard all error/log text."""
    routes: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if not isinstance(trace, list):
        return routes
    for line in trace:
        if not isinstance(line, str):
            continue
        match = _PROVIDER_SUCCESS.fullmatch(line.strip())
        if not match:
            continue
        provider, model = match.groups()
        safe_model = "".join(
            char for char in model
            if char.isalnum() or char in "._:/- "
        ).strip()[:100]
        if not safe_model:
            continue
        route = (provider, safe_model)
        if route in seen:
            continue
        seen.add(route)
        routes.append({"provider": provider, "model": safe_model})
    return routes


def safe_summary_from_report(report: dict[str, Any]) -> dict[str, Any]:
    """Whitelist aggregate fields only; do not leak titles, reasons, or exceptions."""
    raw_summary = report.get("summary")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    metric_names = (
        "cases",
        "cases_passed",
        "post_type_accuracy",
        "title_precision",
        "title_recall",
        "evidence_errors",
        "forbidden_title_hits",
        "non_vacancy_with_jobs",
        "decision_checks",
        "raw_model_decision_accuracy",
        "decision_accuracy_after_candidate_gate",
    )
    metrics: dict[str, int | float | None] = {}
    for name in metric_names:
        value = summary.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) or value is None:
            metrics[name] = value

    failed_ids = summary.get("failed_case_ids", [])
    if isinstance(failed_ids, list):
        failed_ids = [_safe_id(item) for item in failed_ids[:100]]
    else:
        failed_ids = []

    case_runs: list[dict[str, Any]] = []
    raw_results = report.get("results")
    if isinstance(raw_results, list):
        for item in raw_results[:100]:
            if not isinstance(item, dict):
                continue
            status = "error" if item.get("status") == "error" else "evaluated"
            case_run = {
                "case_id": _safe_id(item.get("case_id")),
                "status": status,
                "passed": bool(item.get("passed", False)),
                "successful_provider_routes": _successful_provider_routes(
                    item.get("provider_trace")
                ),
            }
            # Safe diagnostics explain which layer failed without emitting ad text,
            # titles, aliases, reasons, profile fields, or raw provider errors.
            if isinstance(item.get("post_type_passed"), bool):
                case_run["post_type_passed"] = item["post_type_passed"]
            if item.get("expected_post_type") in {
                "actual_vacancy", "unconfirmed_rumour", "career_advice",
                "service_promotion", "future_teaser", "other",
            }:
                case_run["expected_post_type"] = item["expected_post_type"]
            if item.get("predicted_post_type") in {
                "actual_vacancy", "unconfirmed_rumour", "career_advice",
                "service_promotion", "future_teaser", "other", "missing",
            }:
                case_run["predicted_post_type"] = item["predicted_post_type"]
            for list_field, output_field in (
                ("matched_titles", "matched_title_count"),
                ("missing_titles", "missing_title_count"),
                ("unexpected_titles", "unexpected_title_count"),
                ("evidence_errors", "evidence_error_count"),
                ("forbidden_title_hits", "forbidden_title_hit_count"),
            ):
                field_value = item.get(list_field)
                if isinstance(field_value, list):
                    case_run[output_field] = len(field_value)
            if isinstance(item.get("non_vacancy_with_jobs"), bool):
                case_run["non_vacancy_with_jobs"] = item["non_vacancy_with_jobs"]
            checks = item.get("decision_checks")
            if isinstance(checks, list):
                case_run["decision_checks"] = [
                    {
                        key: check[key]
                        for key in ("expected", "raw", "after_candidate_gate", "passed")
                        if key in check and (
                            isinstance(check[key], bool)
                            or check[key] in {"APPLY", "REVIEW", "REJECT", "MISSING"}
                        )
                    }
                    for check in checks[:30]
                    if isinstance(check, dict)
                ]
            # Preserve only the exception class, never request text or messages.
            if status == "error":
                raw_error = item.get("error")
                if isinstance(raw_error, str):
                    match = re.match(r"^([A-Za-z][A-Za-z0-9_]{0,79})(?::|$)", raw_error)
                    if match:
                        case_run["error_type"] = match.group(1)
            case_runs.append(case_run)

    return {
        "metrics": metrics,
        "failed_case_ids": failed_ids,
        "case_runs": case_runs,
        "privacy": {
            "encrypted_profile_loaded": True,
            "only_aggregate_results_printed": True,
            "email_sending": False,
            "telegram_api_used": False,
            "bot_state_modified": False,
            "decrypted_cv_files_created": False,
            "raw_report_artifact_uploaded": False,
        },
    }


def build_evaluation_args(output_path: Path, limit: int, strict: bool) -> list[str]:
    """Build evaluator CLI arguments without fragile list-index assignments."""
    eval_args = ["--output", str(output_path)]
    if limit:
        eval_args.extend(["--limit", str(limit)])
    if strict:
        eval_args.append("--strict")
    return eval_args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-file", type=Path, default=DEFAULT_PROFILE_FILE)
    parser.add_argument("--limit", type=int, default=0, help="Evaluate only the first N labelled cases")
    parser.add_argument("--strict", action="store_true", help="Fail when any labelled case fails")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be zero or a positive integer")

    stage = "provider_configuration"
    tracked_env_keys = (*PROFILE_ENV_KEYS, "JOBFINDER_EVAL_LIVE_PROGRESS")
    original_env = {key: os.environ.get(key) for key in tracked_env_keys}
    try:
        if not any(os.getenv(key, "").strip() for key in AI_PROVIDER_KEYS):
            raise RuntimeError("no AI provider is configured")

        stage = "profile_decryption"
        from cloud_runner import get_fernet

        profile = load_encrypted_candidate_profile(args.profile_file, get_fernet())
        for key, value in profile.items():
            os.environ[key] = value

        # analyze_offer reads the candidate profile at module import time.
        sys.modules.pop("auto_jobfinder_cloud", None)
        from tools.evaluate_ai_quality import main as run_evaluation

        stage = "model_evaluation"
        os.environ["JOBFINDER_EVAL_LIVE_PROGRESS"] = "1"
        with tempfile.TemporaryDirectory(prefix="jobfinder-private-eval-") as directory:
            raw_report_path = Path(directory) / "report.json"
            eval_args = build_evaluation_args(raw_report_path, args.limit, args.strict)

            captured_stdout = io.StringIO()
            captured_stderr = io.StringIO()
            try:
                with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                    exit_code = run_evaluation(eval_args)
            except SystemExit as exc:
                exit_code = int(exc.code) if isinstance(exc.code, int) else 2

            if not raw_report_path.is_file():
                print(json.dumps({
                    "status": "failed",
                    "stage": stage,
                    "error_type": "EvaluationReportMissing",
                }, ensure_ascii=False))
                return 2
            try:
                report = json.loads(raw_report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                print(json.dumps({
                    "status": "failed",
                    "stage": stage,
                    "error_type": "EvaluationReportInvalid",
                }, ensure_ascii=False))
                return 2

            safe_report = safe_summary_from_report(report)
            safe_report["status"] = "completed" if exit_code == 0 else "regression_cases_failed"
            safe_report["git_commit"] = _safe_id(os.getenv("GITHUB_SHA", "unknown"))
            # Do not print captured stdout/stderr or the full private evaluation report.
            print(json.dumps(safe_report, ensure_ascii=False, indent=2))
            return int(exit_code or 0)
    except Exception as exc:
        # Exception messages may include request/prompt fragments. Emit only class and stage.
        print(json.dumps({
            "status": "failed",
            "stage": stage,
            "error_type": type(exc).__name__,
        }, ensure_ascii=False))
        return 2
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.modules.pop("auto_jobfinder_cloud", None)


if __name__ == "__main__":
    raise SystemExit(main())
