from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from decision_audit import read_encrypted_decision_audit

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "cloud_private" / "decision_audit.enc"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required secret is missing: {name}")
    return value


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cell(value: Any, limit: int = 320) -> str:
    text = " ".join(str(value or "").split())
    return text.replace("|", "\\|")[:limit]


_AI_STAGE_LABELS = {
    "offer_analysis": "تحليل الإعلان",
    "vacancy_rescue": "استخراج إعلان التوظيف",
    "manual_apply_analysis": "تحليل التقديم اليدوي",
    "independent_verification": "التحقق المستقل",
}
_AI_OUTCOME_LABELS = {
    "success": "نجح",
    "rate_limited": "حصة محدودة",
    "overloaded": "ضغط مؤقت",
    "configuration_error": "خطأ إعداد",
    "invalid_json": "JSON غير صالح",
    "provider_error": "خطأ مزود",
}


def _ai_call_labels(record: dict[str, Any]) -> str:
    items = record.get("ai_call_trace") or []
    labels = []
    for item in items[:12]:
        if not isinstance(item, dict):
            continue
        stage = _AI_STAGE_LABELS.get(str(item.get("stage") or ""), str(item.get("stage") or ""))
        provider = str(item.get("provider") or "").upper()
        model = str(item.get("model") or "")
        outcome = _AI_OUTCOME_LABELS.get(str(item.get("outcome") or ""), str(item.get("outcome") or ""))
        json_valid = item.get("json_valid")
        json_label = "JSON سليم" if json_valid is True else "JSON غير سليم" if json_valid is False else "JSON غير متاح"
        elapsed = item.get("elapsed_ms")
        elapsed_label = f"{elapsed}ms" if isinstance(elapsed, int) and not isinstance(elapsed, bool) else ""
        attempt = item.get("attempt")
        labels.append(
            f"{stage}: {provider}/{model} (محاولة {attempt}؛ {outcome}؛ {json_label}؛ {elapsed_label})"
        )
    if len(items) > 12:
        labels.append(f"+{len(items) - 12} محاولات أخرى")
    return " | ".join(labels)


_GATE_LABELS = {
    "candidate_fit": "ملاءمة المنصب",
    "manual_apply_title_validation": "التحقق من العنوان المختار يدويًا",
    "independent_verification": "التحقق المستقل",
    "location": "شرط الموقع",
    "contact_scope": "ربط البريد بالمنصب",
    "pre_send_guard": "حارس سجل الرفض",
    "telegram_rejection_guard": "حارس تيليغرام",
}
_DELIVERY_LABELS = {
    "sent": "تم الإرسال",
    "blocked_missing_cv": "السيرة الذاتية غير متوفرة",
    "blocked_missing_email": "لا يوجد بريد مرتبط بالمنصب",
    "duplicate_prevented": "مُنع الإرسال المكرر",
    "send_failed": "فشل الإرسال",
    "blocked_rejection_history": "ممنوع بسبب سجل رفض سابق",
    "not_attempted": "لم تُنفذ محاولة الإرسال",
    "not_applicable_non_apply": "لا ينطبق؛ القرار ليس APPLY",
    "not_recorded": "غير مسجل في السجلات القديمة",
}


def _gate_labels(record: dict[str, Any]) -> str:
    stages = record.get("decision_override_stages") or []
    return ", ".join(_GATE_LABELS.get(str(stage), str(stage)) for stage in stages if stage)


def _delivery_label(record: dict[str, Any]) -> str:
    status = str(record.get("delivery_status") or "not_recorded")
    return _DELIVERY_LABELS.get(status, status)


def make_report(records: list[dict[str, Any]], days: int, now: datetime | None = None) -> tuple[str, dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    cutoff = current - timedelta(days=max(1, min(14, days)))

    selected = []
    for record in records:
        when = _parse_time(record.get("observed_at_utc"))
        if when and cutoff <= when <= current:
            selected.append((when, record))
    selected.sort(key=lambda item: (item[0], str(item[1].get("source") or ""), str(item[1].get("message_id") or "")))

    # The same offer can be retried. For the headline accuracy table use its
    # latest recorded decision; retain the complete attempt list in the report.
    latest: dict[tuple[str, str, str], tuple[datetime, dict[str, Any]]] = {}
    for when, record in selected:
        key = (
            str(record.get("source") or ""),
            str(record.get("message_id") or ""),
            str(record.get("application_job_title") or record.get("job_title") or ""),
        )
        if key not in latest or when >= latest[key][0]:
            latest[key] = (when, record)

    final_records = [item[1] for item in sorted(latest.values(), key=lambda item: item[0])]
    final_counts = Counter(str(r.get("final_decision") or "UNKNOWN").upper() for r in final_records)
    original_counts = Counter(str(r.get("original_ai_decision") or "UNKNOWN").upper() for r in final_records)
    cv_scores = [
        int(r["cv_match_score"])
        for r in final_records
        if isinstance(r.get("cv_match_score"), (int, float))
        and not isinstance(r.get("cv_match_score"), bool)
        and 0 <= r["cv_match_score"] <= 100
    ]
    cv_score_bands = {
        "0-19": sum(0 <= score <= 19 for score in cv_scores),
        "20-29": sum(20 <= score <= 29 for score in cv_scores),
        "30-49": sum(30 <= score <= 49 for score in cv_scores),
        "50-69": sum(50 <= score <= 69 for score in cv_scores),
        "70-89": sum(70 <= score <= 89 for score in cv_scores),
        "90-100": sum(90 <= score <= 100 for score in cv_scores),
    }
    transitions = sum(
        1 for r in final_records
        if str(r.get("original_ai_decision") or "").upper()
        and str(r.get("original_ai_decision") or "").upper() != str(r.get("final_decision") or "").upper()
    )
    override_stage_counts: Counter[str] = Counter()
    for record in final_records:
        override_stage_counts.update(
            stage for stage in record.get("decision_override_stages", [])
            if isinstance(stage, str)
        )
    delivery_status_counts = Counter(
        str(record.get("delivery_status") or "not_recorded")
        for record in final_records
    )
    unique_ai_calls: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in final_records:
        source = str(record.get("source") or "")
        message_id = str(record.get("message_id") or "")
        for call in record.get("ai_call_trace") or []:
            if not isinstance(call, dict):
                continue
            key = (
                source, message_id, call.get("request"), call.get("stage"),
                call.get("provider"), call.get("model"), call.get("attempt"),
            )
            unique_ai_calls[key] = call
    ai_calls = list(unique_ai_calls.values())
    ai_model_usage = Counter(
        f"{call.get('provider')}/{call.get('model')}"
        for call in ai_calls
    )
    ai_successful_calls = sum(call.get("outcome") == "success" for call in ai_calls)
    ai_invalid_json_calls = sum(call.get("json_valid") is False for call in ai_calls)

    summary = {
        "window_start_utc": cutoff.isoformat(timespec="seconds"),
        "window_end_utc": current.isoformat(timespec="seconds"),
        "decision_attempts": len(selected),
        "unique_post_positions": len(final_records),
        "original_ai_decisions": dict(original_counts),
        "final_bot_decisions": dict(final_counts),
        "cv_match_score_bands": cv_score_bands,
        "cv_match_scores_available": len(cv_scores),
        "decision_changed_by_rules_or_guard": transitions,
        "decision_changes_by_gate": dict(override_stage_counts),
        "delivery_outcomes": dict(delivery_status_counts),
        "ai_invocation_attempts": len(ai_calls),
        "ai_model_usage": dict(ai_model_usage),
        "ai_successful_invocations": ai_successful_calls,
        "ai_invalid_json_attempts": ai_invalid_json_calls,
        "sent": sum(bool(r.get("sent")) for r in final_records),
    }

    lines = [
        "# JobFinder — سجل قرارات الوظائف",
        "",
        f"- الفترة حسب وقت المعالجة UTC: {summary['window_start_utc']} إلى {summary['window_end_utc']}",
        f"- محاولات التقييم المسجّلة: {summary['decision_attempts']}",
        f"- العروض/المناصب الفريدة بعد إزالة تكرار المحاولات: {summary['unique_post_positions']}",
        f"- قرار النموذج الأصلي: {json.dumps(summary['original_ai_decisions'], ensure_ascii=False)}",
        f"- قرار البوت النهائي: {json.dumps(summary['final_bot_decisions'], ensure_ascii=False)}",
        f"- فئات مؤشر تطابق كلمات السيرة (ليس احتمال قبول): {json.dumps(summary['cv_match_score_bands'], ensure_ascii=False)}",
        f"- عدد المناصب ذات مؤشر تطابق محفوظ: {summary['cv_match_scores_available']}",
        f"- مرات اختلاف قرار النموذج عن القرار النهائي بعد القواعد/الحواجز: {transitions}",
        f"- بوابات تغيير القرار: {json.dumps(summary['decision_changes_by_gate'], ensure_ascii=False)}",
        f"- نتائج الإرسال: {json.dumps(summary['delivery_outcomes'], ensure_ascii=False)}",
        f"- محاولات استدعاء AI الفعلية: {summary['ai_invocation_attempts']}",
        f"- النماذج المستعملة فعليًا: {json.dumps(summary['ai_model_usage'], ensure_ascii=False)}",
        f"- محاولات AI الناجحة: {summary['ai_successful_invocations']} | محاولات JSON غير الصالح: {summary['ai_invalid_json_attempts']}",
        f"- طلبات إرسال مسجلة ضمن آخر قرار لكل منصب: {summary['sent']}",
        "",
        "**تنبيه منهجي:** هذا سجل القرارات التي جُمعت بعد تفعيل دفتر التدقيق المشفّر فقط؛ لا يعيد بناء قرارات تاريخية لم تكن مسجلة عند حدوثها. القرار النهائي ليس حكمًا على جودة القرار؛ يحتاج كل إعلان إلى مراجعة مقابلة مستقلة.",
        "",
        "## آخر قرار مسجّل لكل إعلان/منصب",
        "",
        "| تاريخ المعالجة UTC | الإعلان الأصلي | المنصب | الشركة | آخر حالة للمسار | استدعاءات AI الفعلية | قرار AI | قرار البوت | درجة AI | مؤشر CV | مهارات CV المطابقة | مراحل تغيير القرار | حالة الإرسال | السبب المسجّل |",
        "|---|---|---|---|---|---|---|---|---:|---:|---|---|---|---|",
    ]
    for record in final_records:
        observed = _parse_time(record.get("observed_at_utc"))
        timestamp = observed.isoformat(timespec="minutes") if observed else ""
        source_url = str(record.get("source_url") or "")
        title = _cell(record.get("application_job_title") or record.get("job_title"), 140)
        if source_url and re.fullmatch(r"https://t\.me/[A-Za-z0-9_]{1,80}/\d+", source_url):
            post_link = f"[{_cell(record.get('source'))}/{_cell(record.get('message_id'))}]({source_url})"
        else:
            post_link = _cell(f"{record.get('source')}/{record.get('message_id')}")
        score = record.get("score")
        score_text = str(score) if score is not None else "—"
        lines.append(
            "| " + " | ".join([
                timestamp,
                post_link,
                title,
                _cell(record.get("company"), 100),
                _cell((str(record.get("provider") or "") + "/" + str(record.get("model") or "")).strip("/"), 100),
                _cell(_ai_call_labels(record), 420),
                _cell(record.get("original_ai_decision"), 16),
                _cell(record.get("final_decision"), 16),
                score_text,
                str(record.get("cv_match_score")) if record.get("cv_match_score") is not None else "—",
                _cell(", ".join(record.get("cv_matched_skill_groups") or []), 120),
                _cell(_gate_labels(record), 120),
                _cell(_delivery_label(record), 80),
                _cell(record.get("reason"), 280),
            ]) + " |"
        )

    lines.extend(["", "## سجل المحاولات", "", "| وقت UTC | الإعلان | المنصب | AI الأصلي | القرار النهائي | استدعاءات AI الفعلية | درجة AI | مؤشر CV | المزوّد | مراحل التغيير | الإرسال |", "|---|---|---|---|---|---|---:|---:|---|---|---|"])
    for when, record in selected:
        source_url = str(record.get("source_url") or "")
        if source_url and re.fullmatch(r"https://t\.me/[A-Za-z0-9_]{1,80}/\d+", source_url):
            post_link = f"[{_cell(record.get('source'))}/{_cell(record.get('message_id'))}]({source_url})"
        else:
            post_link = _cell(f"{record.get('source')}/{record.get('message_id')}")
        score_text = str(record.get("score")) if record.get("score") is not None else "—"
        lines.append("| " + " | ".join([
            when.isoformat(timespec="minutes"),
            post_link,
            _cell(record.get("application_job_title") or record.get("job_title"), 140),
            _cell(record.get("original_ai_decision"), 16),
            _cell(record.get("final_decision"), 16),
            _cell(_ai_call_labels(record), 420),
            score_text,
            str(record.get("cv_match_score")) if record.get("cv_match_score") is not None else "—",
            _cell(str(record.get("provider") or "") + "/" + str(record.get("model") or ""), 100),
            _cell(", ".join(record.get("decision_override_stages") or []), 100),
            _cell(record.get("delivery_status"), 40),
        ]) + " |")

    return "\n".join(lines) + "\n", summary


def send_private_report(markdown: str, summary: dict[str, Any]) -> None:
    token = required_env("TELEGRAM_BOT_TOKEN")
    chat_id = required_env("TELEGRAM_BOT_CHAT_ID")
    caption = (
        "📊 تقرير سجل قرارات JobFinder\n"
        f"محاولات التقييم: {summary['decision_attempts']} | "
        f"عروض/مناصب فريدة: {summary['unique_post_positions']}\n"
        "التقرير مُرسل إلى المحادثة الخاصة فقط؛ لا يحتوي على البريد أو السيرة الذاتية."
    )
    boundary = "----JobFinderDecisionAudit" + uuid.uuid4().hex
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    add_field("chat_id", chat_id)
    add_field("caption", caption)
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        b'Content-Disposition: form-data; name="document"; filename="JobFinder_Decision_Audit.md"\r\n'
    )
    parts.append(b"Content-Type: text/markdown; charset=utf-8\r\n\r\n")
    parts.append(markdown.encode("utf-8"))
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not send the decision audit report to Telegram") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("Telegram rejected the decision audit report")


def main() -> int:
    try:
        key = required_env("JOBFINDER_FERNET_KEY").encode("ascii")
        fernet = Fernet(key)
        rows = read_encrypted_decision_audit(AUDIT_PATH, fernet)
        days_raw = os.getenv("JOBFINDER_AUDIT_DAYS", "2").strip()
        try:
            days = int(days_raw)
        except ValueError:
            days = 2
        days = max(1, min(14, days))
        report, summary = make_report(rows, days)
        send_private_report(report, summary)
    except Exception as exc:
        print(json.dumps({"decision_audit_export_failed": True, "error_type": type(exc).__name__}))
        return 1

    print(json.dumps(summary, ensure_ascii=False))
    print("DECISION AUDIT REPORT SENT PRIVATELY; no plaintext report was uploaded as a public artifact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
