from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession

from cloud_runner import CHANNELS, STATE_ENC, get_fernet, read_encrypted_json, required_env

# Read only a bounded number of messages per channel. If the limit is reached,
# report a lower bound instead of claiming the backlog count is exact.
MAX_PENDING_SAMPLE = 200
CLIENT_TIMEOUT_SECONDS = 12


def utc_iso(value: datetime | None) -> str:
    if value is None:
        return "غير متوفر"
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="minutes")


def one_line_error(exc: Exception) -> str:
    # Keep logs useful without printing message contents, session data, or secrets.
    return type(exc).__name__


async def check_channels() -> dict[str, Any]:
    api_id = int(required_env("TELEGRAM_API_ID"))
    api_hash = required_env("TELEGRAM_API_HASH")
    session = required_env("TELEGRAM_SESSION")
    fernet = get_fernet()
    state = read_encrypted_json(STATE_ENC, fernet)

    cursors = state.get("last_message_ids", {})
    if not isinstance(cursors, dict):
        raise RuntimeError("Encrypted channel cursor map is invalid")

    client = TelegramClient(
        StringSession(session),
        api_id,
        api_hash,
        connection_retries=2,
        retry_delay=2,
        request_retries=2,
        timeout=CLIENT_TIMEOUT_SECONDS,
        auto_reconnect=False,
    )

    channels: list[dict[str, Any]] = []
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram user session is not authorized")

        for channel in CHANNELS:
            item: dict[str, Any] = {
                "channel": channel,
                "readable": False,
                "status": "UNKNOWN",
                "stored_cursor": None,
                "latest_message_id": None,
                "latest_message_date_utc": None,
                "pending_messages": None,
                "pending_count_exact": False,
                "text_messages_in_sample": 0,
                "empty_messages_in_sample": 0,
                "oldest_pending_id": None,
                "oldest_pending_date_utc": None,
                "error": None,
            }
            try:
                entity = await client.get_entity(channel)
                latest_items = await client.get_messages(entity, limit=1)
                latest = latest_items[0] if latest_items else None
                latest_id = int(latest.id) if latest is not None else 0
                item["readable"] = True
                item["latest_message_id"] = latest_id
                item["latest_message_date_utc"] = (
                    utc_iso(latest.date) if latest is not None else None
                )

                raw_cursor = cursors.get(channel)
                if raw_cursor is None:
                    item["status"] = "CURSOR_MISSING"
                    item["error"] = (
                        "لا يوجد مؤشر محفوظ؛ التشغيل العادي قد يضع خط أساس عند آخر منشور "
                        "ويتجاوز المنشورات السابقة."
                    )
                    channels.append(item)
                    continue

                try:
                    cursor = int(raw_cursor)
                    if cursor < 0:
                        raise ValueError("negative cursor")
                except (TypeError, ValueError):
                    item["status"] = "CURSOR_INVALID"
                    item["error"] = "مؤشر القراءة المحفوظ ليس رقمًا صالحًا."
                    channels.append(item)
                    continue

                item["stored_cursor"] = cursor
                if cursor > latest_id:
                    item["status"] = "CURSOR_AHEAD"
                    item["error"] = (
                        "المؤشر المحفوظ أكبر من آخر رقم رسالة مرئي؛ "
                        "قد يمنع ذلك قراءة الرسائل الجديدة حتى تتجاوز هذا الرقم."
                    )
                    channels.append(item)
                    continue

                if cursor == latest_id or latest_id == 0:
                    item["pending_messages"] = 0
                    item["pending_count_exact"] = True
                    item["status"] = "READABLE_NO_BACKLOG"
                    channels.append(item)
                    continue

                pending_count = 0
                text_count = 0
                empty_count = 0
                oldest_pending = None

                async for message in client.iter_messages(
                    entity,
                    min_id=cursor,
                    reverse=True,
                    limit=MAX_PENDING_SAMPLE + 1,
                ):
                    message_id = int(message.id)
                    if message_id <= cursor:
                        continue
                    pending_count += 1
                    if oldest_pending is None:
                        oldest_pending = message
                    if str(getattr(message, "message", None) or "").strip():
                        text_count += 1
                    else:
                        empty_count += 1

                item["text_messages_in_sample"] = text_count
                item["empty_messages_in_sample"] = empty_count
                if oldest_pending is not None:
                    item["oldest_pending_id"] = int(oldest_pending.id)
                    item["oldest_pending_date_utc"] = utc_iso(oldest_pending.date)

                if pending_count > MAX_PENDING_SAMPLE:
                    item["pending_messages"] = f"at least {MAX_PENDING_SAMPLE + 1}"
                    item["pending_count_exact"] = False
                    item["status"] = "READABLE_BACKLOG_OVER_SAMPLE_LIMIT"
                else:
                    item["pending_messages"] = pending_count
                    item["pending_count_exact"] = True
                    item["status"] = (
                        "READABLE_WITH_BACKLOG" if pending_count else "READABLE_NO_BACKLOG"
                    )
            except Exception as exc:
                item["status"] = "READ_ERROR"
                item["error"] = one_line_error(exc)
            channels.append(item)
    finally:
        await client.disconnect()

    unreadable = [x for x in channels if not x["readable"]]
    cursor_problems = [
        x for x in channels
        if x["status"] in {"CURSOR_MISSING", "CURSOR_INVALID", "CURSOR_AHEAD"}
    ]
    backlog_channels = [
        x for x in channels
        if x["status"] in {"READABLE_WITH_BACKLOG", "READABLE_BACKLOG_OVER_SAMPLE_LIMIT"}
    ]
    known_pending = sum(
        int(x["pending_messages"])
        for x in channels
        if x["pending_count_exact"] and isinstance(x["pending_messages"], int)
    )
    lower_bound_pending = known_pending + sum(
        MAX_PENDING_SAMPLE + 1
        for x in channels
        if x["status"] == "READABLE_BACKLOG_OVER_SAMPLE_LIMIT"
    )

    return {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channels_configured": len(CHANNELS),
        "channels_readable": len(CHANNELS) - len(unreadable),
        "unreadable_channels": [x["channel"] for x in unreadable],
        "cursor_problem_channels": [x["channel"] for x in cursor_problems],
        "backlog_channels": [x["channel"] for x in backlog_channels],
        "known_pending_messages": known_pending,
        "minimum_pending_messages": lower_bound_pending,
        "channels": channels,
        "read_only": True,
    }


def make_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# JobFinder — فحص قراءة قنوات تيليغرام",
        "",
        f"- وقت الفحص UTC: {report['checked_at_utc']}",
        f"- القنوات التي أمكن قراءتها: {report['channels_readable']}/{report['channels_configured']}",
        f"- القنوات التي بها مشكلة في مؤشر القراءة: {len(report['cursor_problem_channels'])}",
        f"- القنوات التي لديها رسائل تنتظر المعالجة: {len(report['backlog_channels'])}",
        f"- أقل عدد مؤكد من الرسائل المنتظرة: {report['minimum_pending_messages']}",
        "",
        "**فحص قراءة فقط:** لم تُعالج إعلانات، ولم تُرسل رسائل بريد، ولم تُعدّل حالة البوت أو مؤشرات القراءة.",
        "",
        "| القناة | النتيجة | مؤشر القراءة | آخر رسالة | تاريخ آخر رسالة UTC | الرسائل المنتظرة | أقدم رسالة منتظرة UTC |",
        "|---|---|---:|---:|---|---:|---|",
    ]
    for ch in report["channels"]:
        pending = ch["pending_messages"]
        if pending is None:
            pending = "غير معروف"
        latest_date = ch["latest_message_date_utc"] or "غير متوفر"
        oldest_date = ch["oldest_pending_date_utc"] or "—"
        cursor = ch["stored_cursor"]
        latest_id = ch["latest_message_id"]
        lines.append(
            f"| @{ch['channel']} | {ch['status']} | {cursor if cursor is not None else 'مفقود'} "
            f"| {latest_id if latest_id is not None else '—'} | {latest_date} "
            f"| {pending} | {oldest_date} |"
        )
        if ch["error"]:
            lines.append("")
            lines.append(f"- **@{ch['channel']} — توضيح:** {ch['error']}")
    lines.extend([
        "",
        "## كيف نقرأ النتيجة؟",
        "- READABLE_NO_BACKLOG: القناة قابلة للقراءة ولا توجد رسائل بعد المؤشر.",
        "- READABLE_WITH_BACKLOG: القناة قابلة للقراءة وهناك رسائل لم تصل إليها المعالجة بعد.",
        f"- READABLE_BACKLOG_OVER_SAMPLE_LIMIT: يوجد على الأقل {MAX_PENDING_SAMPLE + 1} رسالة جديدة؛ أوقف الفحص العدّ عند الحد الآمن.",
        "- READ_ERROR: فشل فتح القناة أو قراءة رسائلها.",
        "- CURSOR_MISSING أو CURSOR_INVALID أو CURSOR_AHEAD: يلزم فحص مؤشر القراءة قبل اعتبار القناة سليمة.",
        "",
    ])
    return "\n".join(lines)


async def main() -> int:
    report = await check_channels()
    markdown = make_markdown(report)
    print(markdown)

    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write(markdown + "\n")

    if report["unreadable_channels"] or report["cursor_problem_channels"]:
        print(
            "::error title=JobFinder channel health::"
            "Some channels are unreadable or have an unsafe cursor; see the report above."
        )
        return 1

    if report["backlog_channels"]:
        print(
            "::warning title=JobFinder channel backlog::"
            "Some channels have messages waiting to be processed; see the report above."
        )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        # Never print environment values, session data, or advertisement contents.
        print(f"CHANNEL HEALTH AUDIT FAILED: {type(exc).__name__}")
        sys.exit(1)
