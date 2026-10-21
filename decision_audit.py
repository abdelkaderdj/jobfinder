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
