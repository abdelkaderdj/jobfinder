from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Make the repository root importable when running this file by path from tools/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient
from telethon.sessions import StringSession

from cloud_runner import (
    BASE_DIR,
    CHANNELS,
    STATE_ENC,
    get_fernet,
    read_encrypted_json,
    required_env,
    restore_private_files,
)

WINDOW_HOURS = 48
PER_CHANNEL_SCAN_LIMIT = 180


def _bounded_int_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


# Audits use real AI quota even though email sending and persistence are disabled.
# Keep the default sample small, with a hard cap to avoid accidental quota bursts.
MAX_AUDIT_POSTS = _bounded_int_setting("JOBFINDER_AUDIT_MAX_POSTS", 12, 1, 40)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def safe_job_record(event: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
    record = event.get("record") or {}
    title = str(
        record.get("application_job_title")
        or record.get("job_title")
        or "عنوان غير مستخرج"
    ).strip()
    decision = str(record.get("decision") or "UNKNOWN").upper()
    return {
        "title": title,
        "company": str(record.get("company") or "").strip(),
        "location": str(record.get("location") or "").strip(),
        "decision": decision,
        "original_ai_decision": str(record.get("original_ai_decision") or ""),
        "score": record.get("score"),
        "reason": str(record.get("reason") or "").strip(),
        "location_status": str(record.get("location_status") or ""),
        "contact_scope_matched": bool(record.get("contact_scope_matched")),
        # This is an audit simulation only: no email is ever sent by this script.
        "would_apply_in_live_run": decision == "APPLY",
        "telegram_post_url": (
            f"https://t.me/{post['source']}/{post['message_id']}"
            if post.get("source") and post.get("message_id")
            else ""
        ),
    }


def make_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# JobFinder — تدقيق تجريبي لآخر 48 ساعة",
        "",
        f"- الفترة UTC: {summary['window_start_utc']} إلى {summary['window_end_utc']}",
        f"- الرسائل النصية في النافذة الزمنية: {summary['posts_collected_in_window']}",
        f"- الرسائل التي أُدخلت إلى التدقيق: {summary['posts_processed']}",
        f"- المصادر المفحوصة: {len(summary['channels'])}",
        f"- المناصب التي قيّمها النظام: {summary['positions_evaluated']}",
        f"- APPLY: {summary['decisions'].get('APPLY', 0)}",
        f"- REVIEW: {summary['decisions'].get('REVIEW', 0)}",
        f"- REJECT: {summary['decisions'].get('REJECT', 0)}",
        f"- منشورات اجتماعية واضحة تم تجاوزها: {summary['social_posts_ignored']}",
        f"- إعلانات خدمات CV التي تم تجاوزها: {summary['service_promotions_ignored']}",
        f"- منشورات لم يُستخرج منها منصب: {summary['no_position_output']}",
        f"- أخطاء/تحليلات غير مكتملة: {summary['processing_incomplete']}",
        f"- منشورات أُجّل تحليلها بسبب حدود Groq: {summary['provider_rate_limited_posts']}",
        f"- محاولات إرسال بريد تم منعها في الاختبار: {summary['email_attempts_suppressed']}",
        "",
        "**هذه محاكاة قراءة فقط. لم يُرسل أي بريد، ولم تُعدّل مؤشرات القنوات أو حالة البوت.**",
        "",
        "## الرسائل التي جُمعت حسب القناة",
        "",
    ]
    for channel, count in summary["channels"].items():
        lines.append(f"- {channel}: {count} رسالة")
    lines.extend([
        "",
        "## الوظائف التي قيّمها النظام",
        "",
    ])
    for post in report["posts"]:
        lines.append(
            f"### {post['source']} / {post['message_id']} — "
            f"{post['date_utc']} ({post['status']})"
        )
        lines.append(f"الرابط: {post['url']}")
        if post.get("excerpt"):
            lines.append(f"مقتطف: {post['excerpt']}")
        for job in post.get("jobs", []):
            score = job.get("score")
            score_text = f"{score}/100" if score is not None else "غير متوفر"
            lines.append(
                f"- **{job['decision']}** — {job['title']} | "
                f"{job.get('company') or 'الشركة غير محددة'} | "
                f"{job.get('location') or 'الموقع غير محدد'} | {score_text}"
            )
            if job.get("reason"):
                lines.append(f"  - السبب: {job['reason']}")
            if job.get("location_status"):
                lines.append(f"  - أهلية الموقع: {job['location_status']}")
            if job.get("would_apply_in_live_run"):
                lines.append("  - **كان سيصل إلى مسار APPLY في التشغيل الفعلي، لكن لم يُرسل بريد في هذا التدقيق.**")
        lines.append("")
    if report.get("channel_errors"):
        lines.extend(["## أخطاء الوصول للقنوات", ""])
        for channel, error in report["channel_errors"].items():
            lines.append(f"- {channel}: {error}")
    if report.get("truncated"):
        lines.extend([
            "",
            "**تنبيه:** وصل التدقيق إلى حد الرسائل المحدد؛ النتائج تخص أحدث الرسائل ضمن الحد وليست مراجعة كاملة لكل إعلان في الفترة.",
        ])
    return "\n".join(lines) + "\n"


async def run_audit() -> dict[str, Any]:
    required_env("GROQ_API_KEY")
    required_env("GMAIL_APP_PASSWORD")
    api_id = int(required_env("TELEGRAM_API_ID"))
    api_hash = required_env("TELEGRAM_API_HASH")
    session = required_env("TELEGRAM_SESSION")

    fernet = get_fernet()
    state = read_encrypted_json(STATE_ENC, fernet)
    restore_private_files(fernet, state)

    # Import only after encrypted profile data has been restored into this
    # ephemeral runner. Email-related functions are replaced before analysis.
    import auto_jobfinder_cloud as processor
    from telegram_control import TelegramJobBot

    bot = TelegramJobBot(
        state,
        processor,
        lambda _reason: None,
        allow_review_actions=False,
    )

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    audit_posts: list[dict[str, Any]] = []
    channel_counts: dict[str, int] = {}
    channel_errors: dict[str, str] = {}
    channel_limited: list[str] = []

    client = TelegramClient(
        StringSession(session),
        api_id,
        api_hash,
        connection_retries=4,
        retry_delay=3,
        request_retries=4,
        auto_reconnect=True,
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized")

        for channel in CHANNELS:
            count = 0
            try:
                entity = await client.get_entity(channel)
                async for message in client.iter_messages(
                    entity,
                    limit=PER_CHANNEL_SCAN_LIMIT,
                ):
                    message_date = as_utc(getattr(message, "date", None))
                    if message_date is None or message_date < cutoff:
                        break
                    text = str(getattr(message, "message", None) or "").strip()
                    if not text:
                        continue
                    audit_posts.append({
                        "source": channel,
                        "message_id": int(message.id),
                        "date_utc": message_date.isoformat(timespec="seconds"),
                        "text": text,
                        "url": f"https://t.me/{channel}/{int(message.id)}",
                    })
                    count += 1
                if count >= PER_CHANNEL_SCAN_LIMIT:
                    channel_limited.append(channel)
                channel_counts[channel] = count
            except Exception as exc:
                channel_errors[channel] = type(exc).__name__
                channel_counts[channel] = count
    finally:
        await client.disconnect()

    audit_posts.sort(key=lambda item: item["date_utc"], reverse=True)
    total_posts_collected = len(audit_posts)
    truncated = total_posts_collected > MAX_AUDIT_POSTS
    if truncated:
        # Prioritize the newest actionable offers in a rate-limited audit window.
        audit_posts = audit_posts[:MAX_AUDIT_POSTS]

    # Hard safety barrier: replace all persistence and mail-sending hooks for
    # this run. No callback will update bot counters or queue Telegram cards.
    original_send = processor.send_application
    original_save_sent = processor.save_sent
    original_save_result = processor.save_result
    original_event_callback = processor.JOB_EVENT_CALLBACK
    original_pre_send_guard = processor.JOB_PRE_SEND_GUARD
    email_attempts_suppressed = 0

    def suppress_email(*_args: Any, **_kwargs: Any) -> None:
        nonlocal email_attempts_suppressed
        email_attempts_suppressed += 1
        return None

    processor.send_application = suppress_email
    processor.save_sent = lambda *_args, **_kwargs: None
    processor.save_result = lambda *_args, **_kwargs: None
    processor.JOB_PRE_SEND_GUARD = bot.pre_send_guard_for_job

    output_posts: list[dict[str, Any]] = []
    decisions: Counter[str] = Counter()
    positions_evaluated = 0
    ignored_social = 0
    service_promotions_ignored = 0
    no_position_output = 0
    processing_incomplete = 0
    provider_rate_limited_posts = 0

    try:
        for post_index, post in enumerate(audit_posts):
            current_events: list[dict[str, Any]] = []
            processor.JOB_EVENT_CALLBACK = current_events.append

            base_result: dict[str, Any] = {
                "source": post["source"],
                "message_id": post["message_id"],
                "date_utc": post["date_utc"],
                "url": post["url"],
                "jobs": [],
            }

            if processor.looks_like_non_job_service_promotion(post["text"]):
                base_result["status"] = "ignored_non_job_service_promotion"
                service_promotions_ignored += 1
                output_posts.append(base_result)
                continue

            if processor.looks_like_non_job_social_post(post["text"]):
                base_result["status"] = "ignored_clear_social_post"
                ignored_social += 1
                output_posts.append(base_result)
                continue

            processor_result: bool | None = None
            try:
                processor_result = processor.process_post({
                    "source": post["source"],
                    "channel": post["source"],
                    "message_id": post["message_id"],
                    "date": post["date_utc"],
                    "text": post["text"],
                })
            except Exception as exc:
                base_result["status"] = "processing_exception"
                base_result["error_type"] = type(exc).__name__
                processing_incomplete += 1
                output_posts.append(base_result)
                continue

            groq_routes = set(getattr(processor, "GROQ_MODEL_FALLBACKS", ()))
            unavailable_routes = (
                set(getattr(processor, "_RATE_LIMITED_GROQ_MODELS", set()))
                | set(getattr(processor, "_UNAVAILABLE_GROQ_MODELS", set()))
            )
            all_models_unavailable = bool(groq_routes) and groq_routes.issubset(
                unavailable_routes
            )

            if processor_result is False and all_models_unavailable:
                # Stop immediately instead of emitting the same no-model-available
                # error for every remaining post. Preserve their deferred status.
                base_result["status"] = "deferred_rate_limit"
                base_result["error_type"] = "GroqRateLimit"
                base_result["excerpt"] = post["text"][:600]
                processing_incomplete += 1
                provider_rate_limited_posts += 1
                output_posts.append(base_result)

                for deferred in audit_posts[post_index + 1:]:
                    output_posts.append({
                        "source": deferred["source"],
                        "message_id": deferred["message_id"],
                        "date_utc": deferred["date_utc"],
                        "url": deferred["url"],
                        "jobs": [],
                        "status": "deferred_rate_limit",
                    })
                    processing_incomplete += 1
                    provider_rate_limited_posts += 1
                break

            if current_events:
                base_result["status"] = "evaluated"
                for event in current_events:
                    safe_job = safe_job_record(event, post)
                    base_result["jobs"].append(safe_job)
                    decisions[safe_job["decision"]] += 1
                    positions_evaluated += 1
            elif processor_result is False:
                base_result["status"] = "processing_incomplete"
                base_result["excerpt"] = post["text"][:600]
                processing_incomplete += 1
            else:
                base_result["status"] = "no_position_output"
                base_result["vacancy_signal_detected"] = bool(
                    processor.looks_like_vacancy_ad(post["text"])
                )
                base_result["excerpt"] = post["text"][:600]
                no_position_output += 1

            output_posts.append(base_result)
    finally:
        processor.send_application = original_send
        processor.save_sent = original_save_sent
        processor.save_result = original_save_result
        processor.JOB_EVENT_CALLBACK = original_event_callback
        processor.JOB_PRE_SEND_GUARD = original_pre_send_guard

    report = {
        "audit_mode": "read-only dry run",
        "email_sending_enabled": False,
        "state_or_cursor_modified": False,
        "window_hours": WINDOW_HOURS,
        "summary": {
            "window_start_utc": cutoff.isoformat(timespec="seconds"),
            "window_end_utc": now.isoformat(timespec="seconds"),
            "posts_collected_in_window": total_posts_collected,
            "posts_processed": len(output_posts),
            "positions_evaluated": positions_evaluated,
            "decisions": dict(decisions),
            "social_posts_ignored": ignored_social,
            "service_promotions_ignored": service_promotions_ignored,
            "no_position_output": no_position_output,
            "processing_incomplete": processing_incomplete,
            "provider_rate_limited_posts": provider_rate_limited_posts,
            "email_attempts_suppressed": email_attempts_suppressed,
            "channels": channel_counts,
            "channels_at_scan_limit": channel_limited,
        },
        "channel_errors": channel_errors,
        "truncated": truncated or bool(channel_limited),
        "posts": output_posts,
    }
    return report


def send_private_report_to_telegram(markdown: str, summary: dict[str, Any]) -> None:
    """Send the detailed report only to the configured owner's private Telegram chat."""
    token = required_env("TELEGRAM_BOT_TOKEN")
    chat_id = required_env("TELEGRAM_BOT_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Private Telegram report destination is not configured")

    caption = (
        "📊 انتهى تدقيق JobFinder لآخر 48 ساعة.\n"
        f"الرسائل: {summary['posts_processed']} | "
        f"الوظائف المقيمة: {summary['positions_evaluated']}\n"
        f"APPLY: {summary['decisions'].get('APPLY', 0)} | "
        f"REVIEW: {summary['decisions'].get('REVIEW', 0)} | "
        f"REJECT: {summary['decisions'].get('REJECT', 0)}\n"
        "هذا اختبار قراءة فقط؛ لم يُرسل أي بريد توظيف ولم تُغيّر مؤشرات القنوات."
    )
    boundary = "----JobFinderAudit" + uuid.uuid4().hex
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    add_field("chat_id", chat_id)
    add_field("caption", caption)
    add_field("parse_mode", "HTML")

    document = markdown.encode("utf-8")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        b'Content-Disposition: form-data; name="document"; filename="JobFinder_Audit_48h.md"\r\n'
    )
    parts.append(b"Content-Type: text/markdown; charset=utf-8\r\n\r\n")
    parts.append(document)
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
        raise RuntimeError("Could not deliver the private audit report to Telegram") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("Telegram rejected the private audit report")


def main() -> int:
    try:
        report = asyncio.run(run_audit())
        summary = report["summary"]
        markdown = make_markdown(report)
        # Save report before Telegram delivery so it remains accessible in GitHub Actions
        # even if the configured private chat cannot receive the document.
        (BASE_DIR / "jobfinder_audit_report.md").write_text(markdown, encoding="utf-8")
        (BASE_DIR / "jobfinder_audit_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        send_private_report_to_telegram(markdown, summary)
    except Exception as exc:
        # Keep public Actions output aggregate-only; never print post text or decision reasons.
        print(json.dumps({"audit_failed": True, "error_type": type(exc).__name__}))
        return 1

    print(json.dumps(summary, ensure_ascii=False))
    print(
        "READ-ONLY AUDIT COMPLETE: detailed report delivered only to the configured "
        "private Telegram chat; no job emails sent and no cursor/state checkpoint written."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
