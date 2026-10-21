from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession

# This one-off reconstruction reads public Actions logs and Telegram messages only.
# It never imports the JobFinder processor, reads encrypted app state, restores a
# profile/CV, writes a cursor, invokes an AI provider, or sends an application.

REPOSITORY = os.getenv("GITHUB_REPOSITORY", "abdelkaderdj/jobfinder")
MONITOR_WORKFLOW_ID = 379675910
CHANNELS = ["rcrdz1", "Jobs_dz7", "china1644", "ajob58dz", "ridkh", "CVDZJOBS"]
SCAN_LIMIT_PER_CHANNEL = 2000
API_ROOT = "https://api.github.com"
ALGIERS = timezone(timedelta(hours=1), name="Africa/Algiers")
LOCAL_START = datetime(2026, 10, 20, 0, 0, 0, tzinfo=ALGIERS)
LOCAL_END = datetime(2026, 10, 21, 23, 59, 59, tzinfo=ALGIERS)
START_UTC = LOCAL_START.astimezone(timezone.utc)
END_UTC = LOCAL_END.astimezone(timezone.utc)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
NEW_POST_RE = re.compile(r"^\[([A-Za-z0-9_]+)\] new post id=(\d+); analyzing")
SECTION_RE = re.compile(r"^Detected (\d+) recruitment section")
FOUND_RE = re.compile(r"^Found (\d+) job position")
DECISION_RE = re.compile(r"^DECISION:\s*(APPLY|REVIEW|REJECT)\b", re.I)
ORIGINAL_DECISION_RE = re.compile(r"^ORIGINAL AI DECISION:\s*(APPLY|REVIEW|REJECT)\b", re.I)
SCORE_RE = re.compile(r"^SCORE:\s*(-?\d+(?:\.\d+)?)\b")
LOCATION_RE = re.compile(r"^LOCATION ELIGIBILITY:\s*(.+)")
PROVIDER_RE = re.compile(r"^AI PROVIDER:\s*(Gemini|Groq|OpenRouter)\s*(?:\(([^)]*)\))?\s+succeeded", re.I)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required secret is missing: {name}")
    return value


def api_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "JobFinder-read-only-history-audit",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("GitHub API returned an unexpected response")
    return result


def list_monitor_runs(token: str) -> list[dict[str, Any]]:
    base = f"{API_ROOT}/repos/{REPOSITORY}/actions/workflows/{MONITOR_WORKFLOW_ID}/runs"
    created_range = (
        START_UTC.strftime("%Y-%m-%dT%H:%M:%SZ")
        + ".."
        + END_UTC.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    runs: list[dict[str, Any]] = []
    for page in range(1, 20):
        query = urllib.parse.urlencode({
            "branch": "master",
            "per_page": 100,
            "page": page,
            "created": created_range,
        })
        response = api_json(f"{base}?{query}", token)
        page_runs = response.get("workflow_runs")
        if not isinstance(page_runs, list):
            raise RuntimeError("GitHub did not return a workflow-run list")
        runs.extend(
            run for run in page_runs
            if isinstance(run, dict)
            and run.get("head_branch") == "master"
            and START_UTC <= parse_time(run.get("created_at")) <= END_UTC
        )
        if len(page_runs) < 100:
            break
    return sorted(runs, key=lambda run: str(run.get("created_at") or ""))


def parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_run_log(run: dict[str, Any], token: str) -> tuple[list[dict[str, Any]], str | None]:
    run_id = int(run["id"])
    url = f"{API_ROOT}/repos/{REPOSITORY}/actions/runs/{run_id}/logs"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "JobFinder-read-only-history-audit",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            archive = response.read()
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            text_files = [name for name in bundle.namelist() if name.lower().endswith(".txt")]
            parsed: list[dict[str, Any]] = []
            for name in text_files:
                raw = bundle.read(name).decode("utf-8", errors="replace")
                parsed.extend(parse_monitor_log(raw, run))
        return parsed, None
    except Exception as exc:
        # Public Actions output will contain aggregate failures only, not post content.
        return [], type(exc).__name__


def strip_log_prefix(line: str) -> str:
    value = ANSI_RE.sub("", line).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}T[^ ]+Z ", value):
        value = value.split("Z ", 1)[1]
    return value.strip()


def parse_monitor_log(log_text: str, run: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    active_decision: dict[str, Any] | None = None

    def finish_decision() -> None:
        nonlocal active_decision
        if current is not None and active_decision is not None:
            current["decisions"].append(active_decision)
        active_decision = None

    def finish_post() -> None:
        nonlocal current
        if current is None:
            return
        finish_decision()
        if current.get("status") == "unseen_in_log":
            current["status"] = "processed_without_decision_block" if current.get("decisions") == [] else "decision_logged"
        if current.get("fallback_review") and not current["decisions"]:
            current["decisions"].append({
                "position_index": 1,
                "original_ai_decision": "REVIEW",
                "final_decision": "REVIEW",
                "score": None,
                "location_status": "unknown",
                "source": "synthetic extraction-fallback review card",
            })
        current["run_id"] = run_id_text()
        current["run_number"] = run.get("run_number")
        current["run_created_at_utc"] = str(run.get("created_at") or "")
        current["run_url"] = str(run.get("html_url") or "")
        output.append(current)
        current = None

    def run_id_text() -> str:
        return str(run.get("id") or "")

    for raw_line in log_text.splitlines():
        line = strip_log_prefix(raw_line)
        match = NEW_POST_RE.match(line)
        if match:
            finish_post()
            current = {
                "source": match.group(1),
                "message_id": int(match.group(2)),
                "run_id": run_id_text(),
                "run_number": run.get("run_number"),
                "run_created_at_utc": str(run.get("created_at") or ""),
                "run_url": str(run.get("html_url") or ""),
                "sections_detected": None,
                "positions_found": None,
                "decisions": [],
                "provider": "",
                "model": "",
                "status": "unseen_in_log",
                "fallback_review": False,
                "errors": [],
            }
            active_decision = None
            continue
        if current is None:
            continue

        if "NON-JOB SERVICE PROMOTION FILTER:" in line:
            current["status"] = "ignored_service_promotion"
        elif "NON-JOB FUTURE-POST TEASER FILTER:" in line:
            current["status"] = "ignored_future_post_teaser"
        elif "NON-JOB SOCIAL POST FILTER:" in line:
            current["status"] = "ignored_social_post"
        elif "NON-JOB" in line and "FILTER:" in line:
            current["status"] = "ignored_non_job"
        elif "VACANCY EXTRACTION FALLBACK: queued for manual review" in line:
            current["fallback_review"] = True
            current["status"] = "manual_review_extraction_fallback"
        elif line.startswith("AI EMPTY EXTRACTION:"):
            current["status"] = "vacancy_signalled_but_extraction_empty"
        elif line.startswith("AI PROVIDER UNAVAILABLE:") or "preserving this post for a later retry" in line:
            current["status"] = "deferred_provider_unavailable"
            current["errors"].append("provider_unavailable")
        elif line.startswith("AI ERROR:") or line.startswith("POST PROCESS ERROR:"):
            current["status"] = "processing_error"
            current["errors"].append(line.split(":", 1)[0])
        elif "cursor held at" in line or "post will retry" in line:
            if current["status"] == "unseen_in_log":
                current["status"] = "retry_pending"

        section_match = SECTION_RE.match(line)
        if section_match:
            current["sections_detected"] = int(section_match.group(1))
            if current["status"] == "unseen_in_log":
                current["status"] = "analyzed"
        found_match = FOUND_RE.match(line)
        if found_match:
            current["positions_found"] = int(found_match.group(1))

        provider_match = PROVIDER_RE.match(line)
        if provider_match:
            current["provider"] = provider_match.group(1).lower()
            current["model"] = provider_match.group(2) or ""

        if line.startswith("JOB RESULT:"):
            finish_decision()
            active_decision = {
                "position_index": len(current["decisions"]) + 1,
                "original_ai_decision": "UNKNOWN",
                "final_decision": "UNKNOWN",
                "score": None,
                "location_status": "",
                "sent": None,
                "job_duplicate": None,
                "provider": current.get("provider") or "",
                "model": current.get("model") or "",
            }
            current["status"] = "decision_logged"

        if active_decision is not None:
            decision_match = DECISION_RE.match(line)
            if decision_match:
                active_decision["final_decision"] = decision_match.group(1).upper()
            original_match = ORIGINAL_DECISION_RE.match(line)
            if original_match:
                active_decision["original_ai_decision"] = original_match.group(1).upper()
            score_match = SCORE_RE.match(line)
            if score_match:
                try:
                    score_value = float(score_match.group(1))
                    active_decision["score"] = int(score_value) if score_value.is_integer() else score_value
                except ValueError:
                    active_decision["score"] = None
            location_match = LOCATION_RE.match(line)
            if location_match:
                active_decision["location_status"] = location_match.group(1).strip()[:100]
            if line.startswith("SENT:"):
                active_decision["sent"] = line.split(":", 1)[1].strip().lower() == "true"
            if line.startswith("JOB DUPLICATE:"):
                active_decision["job_duplicate"] = line.split(":", 1)[1].strip().lower() == "true"

    finish_post()
    return output


async def collect_telegram_posts() -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    api_id = int(required_env("TELEGRAM_API_ID"))
    api_hash = required_env("TELEGRAM_API_HASH")
    session = required_env("TELEGRAM_SESSION")
    client = TelegramClient(
        StringSession(session),
        api_id,
        api_hash,
        connection_retries=3,
        retry_delay=2,
        request_retries=3,
        auto_reconnect=True,
    )
    posts: list[dict[str, Any]] = []
    channel_counts: dict[str, int] = {}
    scan_limited: list[str] = []
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized")
        for channel in CHANNELS:
            channel_posts = 0
            try:
                entity = await client.get_entity(channel)
                seen_in_range = 0
                async for message in client.iter_messages(entity, limit=SCAN_LIMIT_PER_CHANNEL):
                    message_time = parse_time(getattr(message, "date", None))
                    if message_time == datetime.min.replace(tzinfo=timezone.utc):
                        continue
                    if message_time < START_UTC:
                        break
                    if message_time > END_UTC:
                        continue
                    text = str(getattr(message, "message", None) or "").strip()
                    has_media = getattr(message, "media", None) is not None
                    # Include text posts and media-only messages, since an image-only vacancy
                    # can be a missed offer even though the text-only bot cannot analyze it.
                    if not text and not has_media:
                        continue
                    posts.append({
                        "source": channel,
                        "message_id": int(message.id),
                        "posted_at_utc": message_time.isoformat(timespec="seconds"),
                        "posted_at_algiers": message_time.astimezone(ALGIERS).strftime("%Y-%m-%d %H:%M:%S"),
                        "url": f"https://t.me/{channel}/{int(message.id)}",
                        "text": text,
                        "media_only": bool(has_media and not text),
                    })
                    channel_posts += 1
                    seen_in_range += 1
                if seen_in_range >= SCAN_LIMIT_PER_CHANNEL:
                    scan_limited.append(channel)
                channel_counts[channel] = channel_posts
            except Exception as exc:
                # Do not write the exception message to the public workflow log.
                channel_counts[channel] = channel_posts
                scan_limited.append(f"{channel}:READ_ERROR_{type(exc).__name__}")
    finally:
        await client.disconnect()
    posts.sort(key=lambda item: (item["posted_at_utc"], item["source"], item["message_id"]))
    return posts, channel_counts, scan_limited


def format_decision(decision: dict[str, Any]) -> str:
    final = decision.get("final_decision") or "UNKNOWN"
    original = decision.get("original_ai_decision") or "UNKNOWN"
    score = decision.get("score")
    score_text = str(score) if score is not None else "not logged"
    extra = []
    if decision.get("location_status"):
        extra.append("location=" + str(decision["location_status"]))
    if decision.get("provider"):
        extra.append("route=" + str(decision["provider"]) + ("/" + str(decision.get("model")) if decision.get("model") else ""))
    if decision.get("sent") is not None:
        extra.append("sent=" + str(decision["sent"]))
    if decision.get("job_duplicate") is not None:
        extra.append("duplicate=" + str(decision["job_duplicate"]))
    suffix = "; " + "; ".join(extra) if extra else ""
    return f"position #{decision.get('position_index', '?')}: AI={original} → final bot={final}; score={score_text}{suffix}"


def build_report(
    posts: list[dict[str, Any]],
    monitor_attempts: list[dict[str, Any]],
    channel_counts: dict[str, int],
    scan_limited: list[str],
    run_count: int,
    log_failures: int,
) -> tuple[str, dict[str, Any]]:
    attempt_map: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for attempt in monitor_attempts:
        attempt_map[(str(attempt["source"]), int(attempt["message_id"]))].append(attempt)
    for key in attempt_map:
        attempt_map[key].sort(key=lambda item: str(item.get("run_created_at_utc") or ""))

    latest_decisions: list[dict[str, Any]] = []
    posts_with_logs = 0
    posts_with_no_text = sum(bool(post["media_only"]) for post in posts)
    decisions_by_status: Counter[str] = Counter()
    post_rows = []

    for post in posts:
        key = (str(post["source"]), int(post["message_id"]))
        attempts = attempt_map.get(key, [])
        if attempts:
            posts_with_logs += 1
            decision_attempt = next((a for a in reversed(attempts) if a.get("decisions")), None)
            if decision_attempt is None:
                decision_attempt = attempts[-1]
            latest = decision_attempt
            status = str(latest.get("status") or "logged_without_final_decision")
            for decision in latest.get("decisions", []):
                latest_decisions.append({
                    **decision,
                    "source": post["source"],
                    "message_id": post["message_id"],
                    "post_url": post["url"],
                    "posted_at_algiers": post["posted_at_algiers"],
                    "run_created_at_utc": latest.get("run_created_at_utc"),
                })
                decisions_by_status[str(decision.get("final_decision") or "UNKNOWN").upper()] += 1
        else:
            status = "NO_MONITOR_OUTPUT_FOUND"
            latest = None

        post_rows.append({
            **post,
            "attempts": attempts,
            "latest_attempt": latest,
            "status": status,
        })

    summary = {
        "local_window_start": LOCAL_START.isoformat(),
        "local_window_end": LOCAL_END.isoformat(),
        "monitor_runs_found": run_count,
        "run_logs_unavailable": log_failures,
        "source_posts_collected": len(posts),
        "per_channel_posts": channel_counts,
        "posts_with_monitor_logs": posts_with_logs,
        "posts_without_matching_monitor_log": len(posts) - posts_with_logs,
        "media_only_posts_without_text": posts_with_no_text,
        "positions_in_latest_logged_decisions": len(latest_decisions),
        "final_decisions": dict(decisions_by_status),
        "channels_scan_limited_or_unreadable": scan_limited,
    }

    lines = [
        "# تدقيق تاريخي لـ JobFinder — 20 و21 أكتوبر 2026",
        "",
        f"- النافذة حسب توقيت الجزائر: {LOCAL_START.isoformat()} إلى {LOCAL_END.isoformat()}",
        f"- تشغيلات المراقب في GitHub Actions: {run_count}",
        f"- سجلات تشغيل تعذر قراءتها: {log_failures}",
        f"- منشورات المصدر التي جُمعت من القنوات الست: {len(posts)}",
        f"- منشورات لها أثر واضح في سجل تشغيل البوت: {posts_with_logs}",
        f"- منشورات لا يوجد لها أثر مطابق في السجل: {len(posts) - posts_with_logs}",
        f"- منشورات وسائط بلا نص/تعليق يمكن تقييمه نصيًا: {posts_with_no_text}",
        f"- المناصب التي ظهر لها قرار في أحدث محاولة مسجلة لكل منشور: {len(latest_decisions)}",
        f"- قرارات البوت النهائية المستخرجة: {json.dumps(dict(decisions_by_status), ensure_ascii=False)}",
        "",
        "## حدود الإثبات",
        "",
        "- السجلات العامة تخفي أسماء المناصب وأسباب الملاءمة لحماية البيانات؛ لذلك تظهر قرارات كل منشور برقم ترتيبها (#1، #2...) كما أخرجها البوت. اربط الرقم بقسم الإعلان حسب ترتيب المناصب. إذا كان الإعلان متعدد المناصب ولم يمكن ربط القسم على نحو موثوق، سجّله كغير محسوم ولا تخمّن.",
        "- «لا يوجد أثر مطابق» لا يثبت وحده أن البوت فوّت الإعلان؛ قد يكون لم يصل إليه بسبب مؤشر القراءة أو قد يكون منشورًا قديمًا. لكنه مرشح للتحقيق، خاصةً إذا كان يحتوي على وظيفة فعلية.",
        "- التقرير قراءة فقط: لم يشغّل محلل الوظائف مجددًا، ولم يستخدم أي مزوّد AI، ولم يقرأ/يفك تشفير حالة البوت أو السيرة الذاتية، ولم يغيّر المؤشرات، ولم يرسل أي طلبات توظيف.",
        "",
        "## عدد المنشورات بحسب القناة",
        "",
    ]
    for channel, count in channel_counts.items():
        lines.append(f"- {channel}: {count}")
    if scan_limited:
        lines.extend(["", "**تنبيه تغطية القنوات:** " + ", ".join(scan_limited)])
    lines.extend(["", "## كل منشور في النافذة الزمنية", ""])

    for index, post in enumerate(post_rows, start=1):
        lines.append(
            f"### {index}. {post['posted_at_algiers']} — "
            f"[{post['source']}/{post['message_id']}]({post['url']})"
        )
        lines.append(f"- حالة المقارنة: **{post['status']}**")
        if post["media_only"]:
            lines.append("- تنبيه: منشور وسائط بلا نص/تعليق؛ لم يكن لدى المعالج النصي محتوى كافٍ.")
        attempts = post["attempts"]
        if not attempts:
            lines.append("- القرار التاريخي: لم أجد كتلة تشغيل مطابقة لهذا المنشور في السجلات المسترجعة.")
        else:
            latest = post["latest_attempt"]
            for attempt_index, attempt in enumerate(attempts, start=1):
                lines.append(
                    f"- محاولة بوت #{attempt_index}: تشغيل [#{attempt.get('run_number')}]"
                    f"({attempt.get('run_created_at_utc')}) — {attempt.get('status')}"
                )
                if attempt.get("sections_detected") is not None:
                    lines.append(f"  - أقسام التوظيف المكتشفة: {attempt['sections_detected']}")
                if attempt.get("positions_found") is not None:
                    lines.append(f"  - المناصب المستخرجة: {attempt['positions_found']}")
                if attempt.get("decisions"):
                    for decision in attempt["decisions"]:
                        lines.append("  - " + format_decision(decision))
                elif attempt.get("errors"):
                    lines.append("  - خطأ/تأجيل فني: " + ", ".join(attempt["errors"]))
        lines.append("")
        text = post.get("text") or ""
        if text:
            lines.append("**نص الإعلان الأصلي:**")
            lines.append("")
            lines.append(text)
        else:
            lines.append("**نص الإعلان الأصلي:** لا يوجد نص أو تعليق؛ يلزم فتح الرابط لرؤية الوسائط.")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.extend([
        "",
        "## ملخص آلي فقط — ليس نسبة الدقة",
        "",
        "الأرقام أعلاه هي تغطية السجلات وقرارات البوت المستخرجة. لم تُحسب نسبة دقة نهائية بعد؛ يلزم تقييم مستقل لكل إعلان ومطابقة كل قرار بالمنصب المقابل. عند وجود أي غموض في مطابقة رقم المنصب بقسم الإعلان، يجب استبعاده من نسبة الدقة حتى يتحقق يدويًا.",
    ])
    return "\n".join(lines) + "\n", summary


def send_private_report(markdown: str, summary: dict[str, Any]) -> None:
    token = required_env("TELEGRAM_BOT_TOKEN")
    chat_id = required_env("TELEGRAM_BOT_CHAT_ID")
    caption = (
        "📊 انتهى تجميع السجلات التاريخية لـ JobFinder يومي 20 و21 أكتوبر.\n"
        f"منشورات القنوات: {summary['source_posts_collected']} | "
        f"لها سجلات بوت: {summary['posts_with_monitor_logs']} | "
        f"قرارات مستخرجة: {summary['positions_in_latest_logged_decisions']}\n"
        "هذا تقرير قراءة فقط؛ لم تُرسل طلبات توظيف ولم تُغيّر حالة البوت."
    )
    boundary = "----JobFinderHistoricalAudit" + uuid.uuid4().hex
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
        b'Content-Disposition: form-data; name="document"; filename="JobFinder_Historical_Audit_2026-10-20_21.md"\r\n'
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
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not deliver the historical audit privately") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("Telegram rejected the historical audit report")


async def main_async() -> int:
    token = required_env("GITHUB_TOKEN")
    print("Historical audit: listing monitor runs for the local-date window.")
    runs = list_monitor_runs(token)
    print(f"Historical audit: found {len(runs)} monitor run(s); downloading read-only logs.")

    attempts: list[dict[str, Any]] = []
    log_failures = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_run_log, run, token): run for run in runs}
        completed = 0
        for future in as_completed(futures):
            parsed, error = future.result()
            attempts.extend(parsed)
            if error:
                log_failures += 1
            completed += 1
            if completed % 100 == 0:
                print(f"Historical audit: inspected {completed}/{len(runs)} run logs.")

    print("Historical audit: fetching original public Telegram posts (read-only).")
    posts, channel_counts, scan_limited = await collect_telegram_posts()
    report, summary = build_report(
        posts, attempts, channel_counts, scan_limited, len(runs), log_failures
    )
    send_private_report(report, summary)
    print(json.dumps(summary, ensure_ascii=False))
    print("HISTORICAL AUDIT SENT PRIVATELY; no AI analysis or applications were run.")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except Exception as exc:
        # Only report exception type to public logs; never print posts or source texts.
        print(json.dumps({"historical_audit_failed": True, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
