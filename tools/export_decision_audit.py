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
    transitions = sum(
        1 for r in final_records
        if str(r.get("original_ai_decision") or "").upper()
        and str(r.get("original_ai_decision") or "").upper() != str(r.get("final_decision") or "").upper()
    )
    summary = {
        "window_start_utc": cutoff.isoformat(timespec="seconds"),
        "window_end_utc": current.isoformat(timespec="seconds"),
        "decision_attempts": len(selected),
        "unique_post_positions": len(final_records),
        "original_ai_decisions": dict(original_counts),
        "final_bot_decisions": dict(final_counts),
        "decision_changed_by_rules_or_guard": transitions,
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
        f"- مرات اختلاف قرار النموذج عن القرار النهائي بعد القواعد/الحواجز: {transitions}",
        f"- طلبات إرسال مسجلة ضمن آخر قرار لكل منصب: {summary['sent']}",
        "",
        "**تنبيه منهجي:** هذا سجل القرارات التي جُمعت بعد تفعيل دفتر التدقيق المشفّر فقط؛ لا يعيد بناء قرارات تاريخية لم تكن مسجلة عند حدوثها. القرار النهائي ليس حكمًا على جودة القرار؛ يحتاج كل إعلان إلى مراجعة مقابلة مستقلة.",
        "",
        "## آخر قرار مسجّل لكل إعلان/منصب",
        "",
        "| تاريخ المعالجة UTC | الإعلان الأصلي | المنصب | الشركة | النموذج | قرار AI | قرار البوت | الدرجة | السبب المسجّل |",
        "|---|---|---|---|---|---|---|---:|---|",
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
                _cell(record.get("original_ai_decision"), 16),
                _cell(record.get("final_decision"), 16),
                score_text,
                _cell(record.get("reason"), 280),
            ]) + " |"
        )

    lines.extend(["", "## سجل المحاولات", "", "| وقت UTC | الإعلان | المنصب | AI الأصلي | القرار النهائي | الدرجة | المزوّد |", "|---|---|---|---|---|---:|---|"])
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
            score_text,
            _cell(str(record.get("provider") or "") + "/" + str(record.get("model") or ""), 100),
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
        # Exact Algeria-local date window: 20 Oct 00:00 onward, through this run's cutoff.
        window_start = datetime(2026, 10, 19, 23, 0, tzinfo=timezone.utc)
        audit_cutoff = datetime.now(timezone.utc)
        rows = [
            row for row in rows
            if (when := _parse_time(row.get("observed_at_utc"))) is not None
            and window_start <= when <= audit_cutoff
        ]
        report, summary = make_report(rows, 2, now=audit_cutoff)
        summary["window_start_utc"] = window_start.isoformat(timespec="seconds").replace("+00:00", "Z")
        summary["audit_as_of_utc"] = audit_cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
        summary["audit_as_of_algeria"] = audit_cutoff.astimezone(timezone(timedelta(hours=1))).isoformat(timespec="seconds")
        markdown, summary = make_report(rows, 2, now=audit_cutoff)
        markdown = markdown.replace(
            "# JobFinder — سجل قرارات الوظائف",
            "# JobFinder — دفتر قرارات 20–21 أكتوبر 2026 (حتى وقت التدقيق)\\n\\n"
            f"- بداية النافذة: {summary['window_start_utc']}\\n"
            f"- وقت التدقيق الجزائر: {audit_cutoff.astimezone(timezone(timedelta(hours=1))).isoformat(timespec='seconds')}\\n"
        )
        (ROOT / "jobfinder_decision_audit_report.md").write_text(markdown, encoding="utf-8")
        (ROOT / "jobfinder_decision_audit_report.json").write_text(
            json.dumps({"summary": summary, "records": [
                row for row in rows
                if (_parse_time(row.get("observed_at_utc")) is not None
                    and window_start <= _parse_time(row.get("observed_at_utc")) <= audit_cutoff)
            ]}, ensure_ascii=False, indent=2) + "\\n",
            encoding="utf-8",
        )
        send_private_report(markdown, summary)
    except Exception as exc:
        print(json.dumps({"decision_audit_export_failed": True, "error_type": type(exc).__name__}))
        return 1

    print(json.dumps(summary, ensure_ascii=False))
    print("DECISION AUDIT REPORT SENT PRIVATELY; no plaintext report was uploaded as a public artifact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
