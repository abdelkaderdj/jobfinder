"""Encrypted per-offer decision history for private JobFinder quality audits.

Only this new sidecar is read/written here. Existing encrypted cursor state,
private profile and CV ciphertext files are not touched by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAX_DECISION_AUDIT_RECORDS = 5000
DECISION_AUDIT_RETENTION_DAYS = 14
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_]{1,80}$")


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _safe_text(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


_ALLOWED_DECISIONS = {"APPLY", "REVIEW", "REJECT"}
_ALLOWED_TRACE_GATES = {
    "candidate_fit", "manual_apply_title_validation", "independent_verification",
    "location", "contact_scope", "pre_send_guard", "telegram_rejection_guard",
}
_SAFE_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_SAFE_AI_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")
_ALLOWED_AI_STAGES = {
    "offer_analysis", "vacancy_rescue", "manual_apply_analysis",
    "independent_verification",
}
_ALLOWED_AI_PROVIDERS = {"gemini", "groq", "openrouter"}
_ALLOWED_AI_OUTCOMES = {
    "success", "rate_limited", "overloaded", "configuration_error",
    "invalid_json", "provider_error",
}


def _safe_ai_call_trace(value: Any) -> list[dict[str, Any]]:
    """Retain model-attempt telemetry only; drop prompts, responses and arbitrary fields."""
    if not isinstance(value, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in value[:40]:
        if not isinstance(item, dict):
            continue
        stage = _safe_code(item.get("stage"), "")
        provider = _safe_code(item.get("provider"), "")
        model = str(item.get("model") or "").strip()
        outcome = _safe_code(item.get("outcome"), "")
        request = item.get("request")
        attempt = item.get("attempt")
        elapsed_ms = item.get("elapsed_ms")
        json_valid = item.get("json_valid")
        if stage not in _ALLOWED_AI_STAGES or provider not in _ALLOWED_AI_PROVIDERS:
            continue
        if outcome not in _ALLOWED_AI_OUTCOMES or not _SAFE_AI_MODEL_RE.fullmatch(model):
            continue
        if (
            not isinstance(request, int) or isinstance(request, bool) or not 1 <= request <= 100
            or not isinstance(attempt, int) or isinstance(attempt, bool) or not 1 <= attempt <= 100
            or not isinstance(elapsed_ms, int) or isinstance(elapsed_ms, bool) or not 0 <= elapsed_ms <= 600000
            or (json_valid is not None and not isinstance(json_valid, bool))
        ):
            continue
        cleaned.append({
            "request": request,
            "stage": stage,
            "provider": provider,
            "model": model,
            "attempt": attempt,
            "json_valid": json_valid,
            "elapsed_ms": elapsed_ms,
            "outcome": outcome,
        })
    return cleaned


def _safe_code(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    return text if _SAFE_CODE_RE.fullmatch(text) else default


def _safe_decision(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in _ALLOWED_DECISIONS else "UNKNOWN"


def _safe_decision_trace(value: Any) -> list[dict[str, Any]]:
    """Keep only compact gate transitions; never copy free text or source evidence."""
    if not isinstance(value, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in value[:16]:
        if not isinstance(item, dict):
            continue
        gate = _safe_code(item.get("gate"), "")
        if gate not in _ALLOWED_TRACE_GATES:
            continue
        before = _safe_decision(item.get("before"))
        after = _safe_decision(item.get("after"))
        outcome = _safe_code(item.get("outcome"))
        cleaned.append({
            "gate": gate,
            "before": before,
            "after": after,
            "outcome": outcome,
            "changed": before != after,
        })
    return cleaned


def make_decision_audit_record(
    event: dict[str, Any],
    *,
    run_id: str = "",
    run_attempt: str = "",
    provider_status: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a compact record that deliberately excludes ads, emails, CVs and profile data."""
    record = event.get("record") if isinstance(event, dict) else {}
    post = event.get("post") if isinstance(event, dict) else {}
    record = record if isinstance(record, dict) else {}
    post = post if isinstance(post, dict) else {}

    source = _safe_text(record.get("source") or post.get("source") or post.get("channel"), 80)
    if not _SOURCE_RE.fullmatch(source):
        source = ""
    message_id = _safe_text(record.get("message_id") or post.get("message_id"), 40)
    title = _safe_text(
        record.get("application_job_title") or record.get("job_title") or "UNPARSED",
        200,
    )
    company = _safe_text(record.get("company"), 160)
    run_id = _safe_text(run_id, 80)
    attempt = _safe_text(run_attempt, 20) or "1"
    observed = _utc_now(observed_at).isoformat(timespec="seconds")
    unique = "|".join((run_id, attempt, source, message_id, title, company))
    record_id = hashlib.sha256(unique.encode("utf-8")).hexdigest()

    provider = provider_status if isinstance(provider_status, dict) else {}
    source_url = (
        f"https://t.me/{source}/{message_id}"
        if source and message_id.isdigit()
        else ""
    )

    score = record.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        score = None

    cv_match_score = record.get("cv_match_score")
    if (
        not isinstance(cv_match_score, (int, float))
        or isinstance(cv_match_score, bool)
        or not 0 <= cv_match_score <= 100
    ):
        cv_match_score = None
    matched_groups = record.get("cv_matched_skill_groups")
    if not isinstance(matched_groups, list):
        matched_groups = []
    matched_groups = [
        _safe_text(item, 60)
        for item in matched_groups
        if isinstance(item, str) and item.strip()
    ][:12]

    return {
        "record_id": record_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "observed_at_utc": observed,
        "post_date": _safe_text(record.get("date") or post.get("date"), 40),
        "source": source,
        "message_id": message_id,
        "source_url": source_url,
        "job_title": _safe_text(record.get("job_title") or title, 200),
        "application_job_title": title,
        "company": company,
        "location": _safe_text(record.get("location"), 160),
        "original_ai_decision": _safe_text(record.get("original_ai_decision"), 20).upper(),
        "final_decision": _safe_text(record.get("decision"), 20).upper(),
        "decision_trace": _safe_decision_trace(record.get("decision_trace")),
        "decision_override_stages": [
            item["gate"] for item in _safe_decision_trace(record.get("decision_trace")) if item["changed"]
        ],
        "delivery_status": _safe_code(record.get("delivery_status"), "not_recorded"),
        "manual_apply": bool(record.get("manual_apply")),
        "ai_verification_status": _safe_code(record.get("ai_verification_status"), "not_recorded"),
        "ai_call_trace": _safe_ai_call_trace(record.get("ai_call_trace")),
        "score": score,
        "cv_match_score": cv_match_score,
        "cv_matched_skill_groups": matched_groups,
        "reason": _safe_text(record.get("reason"), 700),
        "location_status": _safe_text(record.get("location_status"), 80),
        "contact_scope_matched": bool(record.get("contact_scope_matched")),
        "sent": bool(record.get("sent")),
        "job_duplicate": bool(record.get("job_duplicate")),
        "analysis_fallback": bool(record.get("analysis_fallback")),
        "provider": _safe_text(provider.get("provider"), 32),
        "model": _safe_text(provider.get("model"), 120),
    }


def read_encrypted_decision_audit(path: Path, fernet: Any) -> list[dict[str, Any]]:
    """Read only the new encrypted decision-audit sidecar; never reads state/profile/CV files."""
    if not path.exists():
        return []
    try:
        decrypted = fernet.decrypt(path.read_bytes())
        payload = json.loads(decrypted.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Could not read encrypted decision-audit history") from exc
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise RuntimeError("Encrypted decision-audit history has an invalid structure")
    return payload


def append_encrypted_decision_audit(
    path: Path,
    record: dict[str, Any],
    fernet: Any,
    *,
    now: datetime | None = None,
) -> int:
    """Append/deduplicate one record in a Fernet-encrypted sidecar, retaining 14 days/5000 rows."""
    if not isinstance(record, dict) or not record.get("record_id"):
        raise ValueError("Decision audit record is invalid")

    current = _utc_now(now)
    cutoff = current - timedelta(days=DECISION_AUDIT_RETENTION_DAYS)
    # Fail closed: never silently overwrite a corrupt private audit history.
    rows = read_encrypted_decision_audit(path, fernet)

    kept: list[dict[str, Any]] = []
    for item in rows:
        try:
            timestamp = datetime.fromisoformat(str(item.get("observed_at_utc") or "").replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if timestamp >= cutoff:
            kept.append(item)

    # A retry of the same record within one workflow attempt is idempotent.
    by_id = {str(item.get("record_id")): item for item in kept}
    by_id[str(record["record_id"])] = record
    retained = sorted(
        by_id.values(),
        key=lambda item: str(item.get("observed_at_utc") or ""),
    )[-MAX_DECISION_AUDIT_RECORDS:]

    ciphertext = fernet.encrypt(
        json.dumps(retained, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(ciphertext)
    temporary.replace(path)
    return len(retained)
