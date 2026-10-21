from __future__ import annotations

import asyncio
import csv
import io
import json
import mimetypes
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession

# This audit reads public Telegram posts and completed monitor logs only.
# It does NOT restore/decrypt state.enc, profile.enc, or encrypted CV files.
ALGERIA = timezone(timedelta(hours=1))
WINDOW_START_LOCAL = datetime(2026, 10, 20, 0, 0, tzinfo=ALGERIA)
WINDOW_END_LOCAL = datetime(2026, 10, 22, 0, 0, tzinfo=ALGERIA)
WINDOW_START_UTC = WINDOW_START_LOCAL.astimezone(timezone.utc)
WINDOW_END_UTC = WINDOW_END_LOCAL.astimezone(timezone.utc)

CHANNELS = ["rcrdz1", "Jobs_dz7", "china1644", "ajob58dz", "ridkh", "CVDZJOBS"]
MONITOR_WORKFLOW_ID = "379675910"
MAX_MESSAGES_SCANNED_PER_CHANNEL = 5000
MAX_PAGES_OF_RUNS = 20
LOG_FETCH_WORKERS = 8
MAX_MEDIA_DOWNLOADS = 300
MAX_MEDIA_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_MEDIA_BYTES = 80 * 1024 * 1024
REPORT_DIR = Path("historical_jobfinder_audit")
REPORT_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR = REPORT_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
NEW_POST_RE = re.compile(
    r"\[(?P<source>rcrdz1|Jobs_dz7|china1644|ajob58dz|ridkh|CVDZJOBS)\]\s+"
    r"new post id=(?P<message_id>\d+);\s*analyzing",
    re.IGNORECASE,
)
DECISION_RE = re.compile(r"\bDECISION:\s*(APPLY|REVIEW|REJECT)\b", re.IGNORECASE)
ORIGINAL_DECISION_RE = re.compile(
    r"\bORIGINAL AI DECISION:\s*(APPLY|REVIEW|REJECT)\b", re.IGNORECASE
)
SCORE_RE = re.compile(r"\bSCORE:\s*(\d+(?:\.\d+)?|None|null)\b", re.IGNORECASE)
LOCATION_RE = re.compile(r"\bLOCATION ELIGIBILITY:\s*(.*?)\s*$", re.IGNORECASE)
SENT_RE = re.compile(r"\bSENT:\s*(True|False)\b", re.IGNORECASE)
DUPLICATE_RE = re.compile(r"\bJOB DUPLICATE:\s*(True|False)\b", re.IGNORECASE)

FILTER_MARKERS = (
    ("NON-JOB SERVICE PROMOTION FILTER", "فلتر إعلان خدمة للسيرة الذاتية"),
    ("NON-JOB FUTURE-POST TEASER FILTER", "فلتر إعلان تشويقي بلا شاغر حالي"),
    ("NON-JOB SOCIAL POST FILTER", "فلتر منشور اجتماعي غير وظيفي"),
    ("AI EMPTY EXTRACTION", "إشارة لاستخراج فارغ"),
    ("AI ERROR:", "خطأ ذكاء اصطناعي"),
    ("POST PROCESS ERROR:", "خطأ معالجة منشور"),
    ("POST PROCESS FAILED", "فشلت معالجة المنشور"),
)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required GitHub secret/environment is missing: {name}")
    return value


def github_request(url: str, token: str, timeout: int = 45) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "JobFinder-read-only-historical-audit",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def list_monitor_runs(repository: str, token: str, audit_cutoff: datetime) -> list[dict[str, Any]]:
    start_query = WINDOW_START_UTC.strftime("%Y-%m-%dT%H:%M:%SZ")
    runs: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES_OF_RUNS + 1):
        query = urllib.parse.urlencode({
            "branch": "master",
            "event": "workflow_dispatch",
            "per_page": 100,
            "page": page,
            "created": f">={start_query}",
        })
        url = (
            f"https://api.github.com/repos/{repository}/actions/workflows/"
            f"{MONITOR_WORKFLOW_ID}/runs?{query}"
        )
        payload = json.loads(github_request(url, token).decode("utf-8"))
        page_runs = payload.get("workflow_runs") or []
        if not page_runs:
            break
        for run in page_runs:
            created = parse_iso_datetime(run.get("created_at"))
            if created is None or created < WINDOW_START_UTC or created > audit_cutoff:
                continue
            if run.get("head_branch") != "master":
                continue
            if run.get("status") != "completed":
                continue
            runs.append({
                "id": int(run["id"]),
                "run_number": run.get("run_number"),
                "created_at_utc": run.get("created_at"),
                "conclusion": run.get("conclusion"),
                "url": run.get("html_url", ""),
            })
        if len(page_runs) < 100:
            break
    unique = {int(run["id"]): run for run in runs}
    return sorted(unique.values(), key=lambda item: str(item.get("created_at_utc") or ""))


def download_run_log_texts(repository: str, token: str, run: dict[str, Any]) -> tuple[int, list[str], str]:
    url = f"https://api.github.com/repos/{repository}/actions/runs/{run['id']}/logs"
    last_error = "unknown error"
    for attempt in range(3):
        try:
            archive_bytes = github_request(url, token, timeout=90)
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                text_files = [
                    name for name in archive.namelist()
                    if name.lower().endswith(".txt") and not name.endswith("/")
                ]
                contents = [
                    archive.read(name).decode("utf-8", errors="replace")
                    for name in text_files
                ]
            return int(run["id"]), contents, ""
        except Exception as exc:
            last_error = type(exc).__name__
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    return int(run["id"]), [], last_error


def parse_decision_from_line(line: str) -> dict[str, Any] | None:
    match = DECISION_RE.search(line)
    if not match:
        return None
    return {
        "sequence": 0,
        "decision": match.group(1).upper(),
        "original_ai_decision": None,
        "score": None,
        "location_eligibility": None,
        "sent": None,
        "duplicate": None,
    }


def correlate_run_logs(
    runs: list[dict[str, Any]],
    downloaded_logs: list[tuple[int, list[str], str]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    run_by_id = {int(run["id"]): run for run in runs}
    history: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, Any]] = []

    for run_id, text_files, error in downloaded_logs:
        run = run_by_id.get(run_id, {"id": run_id})
        if error:
            failures.append({"run_id": run_id, "error_type": error})
            continue

        for log_text in text_files:
            active: dict[str, Any] | None = None
            current_decision: dict[str, Any] | None = None
            for raw_line in log_text.splitlines():
                line = ANSI_RE.sub("", raw_line).strip()
                new_post = NEW_POST_RE.search(line)
                if new_post:
                    source = new_post.group("source")
                    message_id = int(new_post.group("message_id"))
                    active = {
                        "source": source,
                        "message_id": message_id,
                        "run_id": run_id,
                        "run_number": run.get("run_number"),
                        "run_created_at_utc": run.get("created_at_utc"),
                        "run_conclusion": run.get("conclusion"),
                        "run_url": run.get("url", ""),
                        "markers": [],
                        "job_decisions": [],
                    }
                    key = f"{source}/{message_id}"
                    history.setdefault(key, []).append(active)
                    current_decision = None
                    continue

                if active is None:
                    continue

                for needle, label in FILTER_MARKERS:
                    if needle in line and label not in active["markers"]:
                        active["markers"].append(label)

                if "Analyzing new job post..." in line:
                    if "بدأ تحليل المنشور" not in active["markers"]:
                        active["markers"].append("بدأ تحليل المنشور")

                decision = parse_decision_from_line(line)
                if decision is not None:
                    decision["sequence"] = len(active["job_decisions"]) + 1
                    active["job_decisions"].append(decision)
                    current_decision = decision
                    continue

                if current_decision is not None:
                    original = ORIGINAL_DECISION_RE.search(line)
                    if original:
                        current_decision["original_ai_decision"] = original.group(1).upper()
                        continue
                    score = SCORE_RE.search(line)
                    if score:
                        raw_score = score.group(1)
                        if raw_score.lower() not in {"none", "null"}:
                            try:
                                numeric = float(raw_score)
                                current_decision["score"] = int(numeric) if numeric.is_integer() else numeric
                            except ValueError:
                                pass
                        continue
                    location = LOCATION_RE.search(line)
                    if location:
                        current_decision["location_eligibility"] = location.group(1)[:100]
                        continue
                    sent = SENT_RE.search(line)
                    if sent:
                        current_decision["sent"] = sent.group(1).lower() == "true"
                        continue
                    duplicate = DUPLICATE_RE.search(line)
                    if duplicate:
                        current_decision["duplicate"] = duplicate.group(1).lower() == "true"
                        continue

    for key in history:
        history[key].sort(key=lambda item: str(item.get("run_created_at_utc") or ""))
    return history, failures


async def collect_telegram_posts() -> tuple[list[dict[str, Any]], dict[str, int], dict[str, str], list[str]]:
    api_id = int(required_env("TELEGRAM_API_ID"))
    api_hash = required_env("TELEGRAM_API_HASH")
    session = required_env("TELEGRAM_SESSION")
    client = TelegramClient(
        StringSession(session),
        api_id,
        api_hash,
        connection_retries=4,
        retry_delay=3,
        request_retries=4,
        auto_reconnect=True,
    )

    posts: list[dict[str, Any]] = []
    channel_counts: dict[str, int] = {}
    channel_errors: dict[str, str] = {}
    channels_at_limit: list[str] = []
    media_download_count = 0
    media_bytes_downloaded = 0
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized")
        for source in CHANNELS:
            count = 0
            reached_window_start = False
            scanned = 0
            try:
                entity = await client.get_entity(source)
                async for message in client.iter_messages(
                    entity,
                    limit=MAX_MESSAGES_SCANNED_PER_CHANNEL,
                ):
                    scanned += 1
                    message_date = getattr(message, "date", None)
                    if not isinstance(message_date, datetime):
                        continue
                    if message_date.tzinfo is None:
                        message_date = message_date.replace(tzinfo=timezone.utc)
                    message_utc = message_date.astimezone(timezone.utc)
                    if message_utc >= WINDOW_END_UTC:
                        continue
                    if message_utc < WINDOW_START_UTC:
                        reached_window_start = True
                        break
                    message_text = str(getattr(message, "message", None) or "").strip()
                    has_media = bool(getattr(message, "media", None))
                    if not message_text and not has_media:
                        continue
                    message_id = int(message.id)
                    media_info = None
                    if has_media:
                        file_obj = getattr(message, "file", None)
                        mime_type = str(getattr(file_obj, "mime_type", "") or "")
                        original_name = str(getattr(file_obj, "name", "") or "")
                        is_photo = bool(getattr(message, "photo", None))
                        kind = "photo" if is_photo else ("document" if file_obj else "other_media")
                        estimated_size = getattr(file_obj, "size", None) if file_obj else None
                        if is_photo:
                            photo_sizes = []
                            for photo_size in getattr(getattr(message, "photo", None), "sizes", []) or []:
                                direct_size = getattr(photo_size, "size", None)
                                if isinstance(direct_size, int) and direct_size > 0:
                                    photo_sizes.append(direct_size)
                                progressive_sizes = getattr(photo_size, "sizes", None)
                                if isinstance(progressive_sizes, (list, tuple)) and progressive_sizes:
                                    numeric_sizes = [value for value in progressive_sizes if isinstance(value, int)]
                                    if numeric_sizes:
                                        photo_sizes.append(max(numeric_sizes))
                            if photo_sizes:
                                estimated_size = max(photo_sizes)
                            mime_type = mime_type or "image/jpeg"

                        media_info = {
                            "kind": kind,
                            "mime_type": mime_type or None,
                            "original_name": original_name or None,
                            "estimated_size_bytes": estimated_size if isinstance(estimated_size, int) else None,
                            "download_status": "not_downloaded_unsupported_type",
                            "zip_path": None,
                        }
                        supported_media = is_photo or mime_type.startswith("image/") or mime_type == "application/pdf"
                        if supported_media:
                            if not isinstance(estimated_size, int) or estimated_size <= 0:
                                media_info["download_status"] = "not_downloaded_size_unknown"
                            elif estimated_size > MAX_MEDIA_FILE_BYTES:
                                media_info["download_status"] = "not_downloaded_file_too_large"
                            elif media_download_count >= MAX_MEDIA_DOWNLOADS:
                                media_info["download_status"] = "not_downloaded_item_limit"
                            elif media_bytes_downloaded + estimated_size > MAX_TOTAL_MEDIA_BYTES:
                                media_info["download_status"] = "not_downloaded_total_size_limit"
                            else:
                                suffix = (
                                    ".jpg" if is_photo
                                    else (Path(original_name).suffix.lower() or mimetypes.guess_extension(mime_type) or ".bin")
                                )
                                if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
                                    suffix = mimetypes.guess_extension(mime_type) or ".bin"
                                target = MEDIA_DIR / f"{source}_{message_id}{suffix}"
                                try:
                                    downloaded_path = await client.download_media(message, file=str(target))
                                    if downloaded_path and Path(downloaded_path).is_file():
                                        actual_path = Path(downloaded_path)
                                        actual_size = actual_path.stat().st_size
                                        if actual_size > MAX_MEDIA_FILE_BYTES:
                                            actual_path.unlink(missing_ok=True)
                                            media_info["download_status"] = "not_downloaded_actual_file_too_large"
                                        else:
                                            media_download_count += 1
                                            media_bytes_downloaded += actual_size
                                            media_info["download_status"] = "downloaded"
                                            media_info["zip_path"] = f"media/{actual_path.name}"
                                            media_info["downloaded_size_bytes"] = actual_size
                                    else:
                                        media_info["download_status"] = "download_failed"
                                except Exception as media_exc:
                                    target.unlink(missing_ok=True)
                                    media_info["download_status"] = f"download_failed_{type(media_exc).__name__}"

                    posts.append({
                        "source": source,
                        "message_id": message_id,
                        "published_at_utc": message_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
                        "published_at_algeria": message_utc.astimezone(ALGERIA).isoformat(timespec="seconds"),
                        "url": f"https://t.me/{source}/{message_id}",
                        "text": message_text,
                        "has_media": has_media,
                        "media_only": bool(has_media and not message_text),
                        "media": media_info,
                    })
                    count += 1
                if not reached_window_start and scanned >= MAX_MESSAGES_SCANNED_PER_CHANNEL:
                    channels_at_limit.append(source)
                channel_counts[source] = count
            except Exception as exc:
                channel_errors[source] = type(exc).__name__
                channel_counts[source] = count
    finally:
        await client.disconnect()

    unique = {f"{post['source']}/{post['message_id']}": post for post in posts}
    ordered = sorted(
        unique.values(),
        key=lambda item: (str(item.get("published_at_utc") or ""), item["source"], item["message_id"]),
    )
    return ordered, channel_counts, channel_errors, channels_at_limit


def decision_attempt_summary(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_number": attempt.get("run_number"),
        "run_created_at_utc": attempt.get("run_created_at_utc"),
        "run_url": attempt.get("run_url", ""),
        "run_conclusion": attempt.get("run_conclusion"),
        "markers": attempt.get("markers") or [],
        "job_decisions": attempt.get("job_decisions") or [],
    }


def final_attempt_for_post(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not attempts:
        return None
    with_decisions = [attempt for attempt in attempts if attempt.get("job_decisions")]
    return with_decisions[-1] if with_decisions else attempts[-1]


def summarize(
    posts: list[dict[str, Any]],
    history: dict[str, list[dict[str, Any]]],
    channel_counts: dict[str, int],
    channel_errors: dict[str, str],
    channels_at_limit: list[str],
    log_failures: list[dict[str, Any]],
    monitor_runs_examined: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    enriched: list[dict[str, Any]] = []
    aggregate_decisions = {"APPLY": 0, "REVIEW": 0, "REJECT": 0}
    posts_with_trace = 0
    posts_with_job_decisions = 0
    posts_without_job_decisions = 0

    for post in posts:
        key = f"{post['source']}/{post['message_id']}"
        attempts = history.get(key, [])
        final_attempt = final_attempt_for_post(attempts)
        if attempts:
            posts_with_trace += 1
        decisions = list((final_attempt or {}).get("job_decisions") or [])
        if decisions:
            posts_with_job_decisions += 1
            for item in decisions:
                value = str(item.get("decision") or "").upper()
                if value in aggregate_decisions:
                    aggregate_decisions[value] += 1
        elif attempts:
            posts_without_job_decisions += 1

        item = dict(post)
        item["bot_history_status"] = (
            "media_only_not_processed_by_text_poller"
            if post.get("media_only")
            else "decision_logged"
            if decisions
            else "processing_trace_without_job_decision"
            if attempts
            else "no_matching_bot_log_found"
        )
        item["bot_history_attempts"] = [decision_attempt_summary(attempt) for attempt in attempts]
        item["latest_logged_bot_decisions"] = decisions
        item["independent_review"] = {
            "status": "pending_chatgpt_review",
            "note": "سيُملأ عند المراجعة المستقلة؛ لا يُستنتج القرار الأصلي من عدّادات إجمالية.",
        }
        enriched.append(item)

    coverage_complete = not channel_errors and not channels_at_limit and not log_failures
    summary = {
        "report": "JobFinder historical decision evidence export",
        "timezone": "Africa/Algiers (UTC+01:00)",
        "window_start_local": WINDOW_START_LOCAL.isoformat(),
        "window_end_local_exclusive": WINDOW_END_LOCAL.isoformat(),
        "window_start_utc": WINDOW_START_UTC.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "window_end_utc_exclusive": WINDOW_END_UTC.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "posts_collected": len(enriched),
        "channel_counts": channel_counts,
        "monitor_runs_examined": monitor_runs_examined,
        "monitor_log_download_failures": len(log_failures),
        "posts_with_matching_bot_trace": posts_with_trace,
        "posts_with_logged_job_decisions": posts_with_job_decisions,
        "posts_with_trace_but_no_job_decision": posts_without_job_decisions,
        "posts_without_matching_bot_trace": len(enriched) - posts_with_trace,
        "posts_with_media": sum(1 for item in enriched if item.get("has_media")),
        "media_only_posts_not_sent_to_text_analyzer": sum(1 for item in enriched if item.get("media_only")),
        "media_files_downloaded": sum(1 for item in enriched if (item.get("media") or {}).get("download_status") == "downloaded"),
        "media_files_not_downloaded_or_unsupported": sum(1 for item in enriched if item.get("media") and (item.get("media") or {}).get("download_status") != "downloaded"),
        "logged_job_decision_counts_latest_attempt_per_post": aggregate_decisions,
        "channel_read_errors": channel_errors,
        "channels_at_scan_limit": channels_at_limit,
        "coverage_complete_for_sources_and_logs": coverage_complete,
        "important_limit": (
            "A post with no matching monitor log is not automatically a bot false negative: "
            "it may not have been polled during the window, could have been baselined, or may "
            "have been handled outside the retained logs. Decision logs intentionally redact job "
            "titles; for multi-position posts, a reviewer must verify the sequence-to-title mapping "
            "from the original text. Aggregate counters are never used to fabricate per-ad decisions."
        ),
    }
    return summary, enriched


def render_markdown(summary: dict[str, Any], posts: list[dict[str, Any]], log_failures: list[dict[str, Any]]) -> str:
    lines = [
        "# JobFinder — الدليل التاريخي لقرارات 20 و21 أكتوبر 2026",
        "",
        f"- التوقيت: الجزائر (UTC+01:00).",
        f"- النافذة: {summary['window_start_local']} إلى {summary['window_end_local_exclusive']} (النهاية غير مشمولة).",
        f"- المنشورات النصية المجمّعة: **{summary['posts_collected']}**.",
        f"- المنشورات التي ظهر لها أثر في سجل المراقب: **{summary['posts_with_matching_bot_trace']}**.",
        f"- المنشورات ذات قرارات وظائف مسجّلة: **{summary['posts_with_logged_job_decisions']}**.",
        f"- المنشورات ذات مرفقات: **{summary['posts_with_media']}**؛ منها بلا نص: **{summary['media_only_posts_not_sent_to_text_analyzer']}**.",
        f"- ملفات الصور/PDF المضمّنة في الأرشيف: **{summary['media_files_downloaded']}**؛ مرفقات أخرى لم تُحمّل أو لم تدعمها حدود التدقيق: **{summary['media_files_not_downloaded_or_unsupported']}**.",
        f"- قرارات المناصب من آخر محاولة مسجّلة لكل منشور: APPLY={summary['logged_job_decision_counts_latest_attempt_per_post']['APPLY']}, REVIEW={summary['logged_job_decision_counts_latest_attempt_per_post']['REVIEW']}, REJECT={summary['logged_job_decision_counts_latest_attempt_per_post']['REJECT']}.",
        f"- سجلات تشغيل تعذّر تنزيلها: **{summary['monitor_log_download_failures']}**.",
        f"- اكتمال تغطية القنوات والسجلات: **{summary['coverage_complete_for_sources_and_logs']}**.",
        "",
        "**هذا تصدير قراءة فقط:** لا يستدعي نموذج ذكاء اصطناعي، ولا يقرأ/يفك تشفير ملفات الحالة أو الملف الشخصي أو ملفات CV، ولا يغيّر المؤشرات، ولا يرسل طلبات توظيف.",
        "",
        "## طريقة تفسير المطابقة",
        "",
        "قرارات البوت أدناه مأخوذة من سجلات GitHub Actions الفعلية. أسماء المناصب غير مطبوعة في السجلات العامة، لذلك تحفظ القرارات بالترتيب الذي ظهرت به. عندما يحتوي المنشور عدة مناصب، يجب التحقق من مطابقة رقم القرار مع المسمى في النص؛ أي حالة ملتبسة تُصنّف «غير محسومة» ولا تدخل نسبة الدقة.",
        "المنشور الذي لا يملك أثرًا في سجل التشغيل لا يُعدّ تلقائيًا خطأ من البوت. وقد يعني أن المراقب لم يقرأه خلال الفترة، أو تجاوزه في مرحلة baseline، أو أن سجلاته غير متاحة.",
        "",
        "## جميع المنشورات في النافذة الزمنية",
        "",
    ]
    for index, post in enumerate(posts, start=1):
        lines.extend([
            f"### {index}. {post['source']}/{post['message_id']} — {post['published_at_algeria']}",
            f"- الرابط: {post['url']}",
            f"- الأثر التاريخي: {post['bot_history_status']}",
        ])
        attempts = post.get("bot_history_attempts") or []
        if not attempts:
            lines.append("- القرار التاريخي: لا يوجد أثر قرار مطابق في سجلات GitHub المتاحة.")
        else:
            for attempt_index, attempt in enumerate(attempts, start=1):
                lines.append(
                    f"- محاولة المراقب {attempt_index}: "
                    f"run #{attempt.get('run_number')} — {attempt.get('run_conclusion')} — "
                    f"{attempt.get('run_url') or 'رابط غير متاح'}"
                )
                if attempt.get("markers"):
                    lines.append("  - مؤشرات السجل: " + "، ".join(attempt["markers"]))
                for decision in attempt.get("job_decisions") or []:
                    lines.append(
                        f"  - المنصب رقم {decision.get('sequence')}: "
                        f"قرار البوت **{decision.get('decision')}**؛ "
                        f"قرار النموذج الأصلي {decision.get('original_ai_decision') or 'غير مسجل'}؛ "
                        f"درجة المطابقة {decision.get('score') if decision.get('score') is not None else 'غير مسجلة'}؛ "
                        f"أهلية الموقع {decision.get('location_eligibility') or 'غير مسجلة'}؛ "
                        f"أُرسل {decision.get('sent') if decision.get('sent') is not None else 'غير مسجل'}؛ "
                        f"مكرر {decision.get('duplicate') if decision.get('duplicate') is not None else 'غير مسجل'}."
                    )
        media = post.get("media") or {}
        if post.get("has_media"):
            lines.append(
                "- المرفق: "
                f"{media.get('kind') or 'غير محدد'} / {media.get('mime_type') or 'نوع غير محدد'}؛ "
                f"حالة التنزيل: {media.get('download_status') or 'غير مسجلة'}؛ "
                f"مسار الأرشيف: {media.get('zip_path') or 'لا يوجد'}."
            )
        if post.get("media_only"):
            lines.append("- ملاحظة مهمة: هذا منشور وسائط بلا نص. المراقب الحالي يتجاهل الرسائل الخالية من النص قبل التحليل؛ تُراجع الصورة/PDF إن كان متاحًا.")
        lines.extend(["", "**نص المنشور الأصلي:**", "", post["text"] or "(لا يوجد نص؛ افحص المرفق داخل ZIP)", ""])
    if log_failures:
        lines.extend(["## سجلات تعذّر الوصول إليها", ""])
        lines.extend(
            f"- تشغيل #{item['run_id']}: {item['error_type']}" for item in log_failures
        )
    lines.extend([
        "",
        "## ما لم يُستنتج",
        "",
        "لا توجد هنا نسبة دقة نهائية بعد. تُحتسب النسبة فقط بعد مراجعة مستقلة لكل منصب ومقارنته بقرار البوت التاريخي عندما يكون القرار موثقًا ومطابقته بالمسمى مؤكدة. الحالات الناقصة أو الملتبسة تبقى خارج مقام النسبة.",
    ])
    return "\n".join(lines) + "\n"


def create_csv(posts: list[dict[str, Any]], output_path: Path) -> None:
    fields = [
        "published_at_algeria", "source", "message_id", "url", "bot_history_status",
        "has_media", "media_only", "media", "latest_logged_bot_decisions",
        "bot_history_attempts", "text",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for post in posts:
            writer.writerow({
                **{key: post.get(key, "") for key in ("published_at_algeria", "source", "message_id", "url", "bot_history_status", "has_media", "media_only", "text")},
                "media": json.dumps(post.get("media") or {}, ensure_ascii=False),
                "latest_logged_bot_decisions": json.dumps(post.get("latest_logged_bot_decisions") or [], ensure_ascii=False),
                "bot_history_attempts": json.dumps(post.get("bot_history_attempts") or [], ensure_ascii=False),
            })


def send_private_report(path: Path, summary: dict[str, Any]) -> None:
    token = required_env("TELEGRAM_BOT_TOKEN")
    chat_id = required_env("TELEGRAM_BOT_CHAT_ID")
    caption = (
        "📊 ملف تدقيق JobFinder التاريخي للفترة 20–21 أكتوبر 2026 (توقيت الجزائر).\n"
        f"المنشورات: {summary['posts_collected']} | "
        f"قرارات المناصب المسجلة: "
        f"{sum(summary['logged_job_decision_counts_latest_attempt_per_post'].values())}\n"
        "التقرير يضم نصوص الإعلانات وآثار قرارات البوت من سجلات التشغيل. "
        "قراءة فقط؛ لا إرسال طلبات ولا تغيير حالة البوت."
    )
    boundary = "----JobFinderHistoricalAudit" + str(int(time.time() * 1000))
    pieces: list[bytes] = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="chat_id"\r\n\r\n',
        chat_id.encode("utf-8"),
        b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="caption"\r\n\r\n',
        caption.encode("utf-8"),
        b"\r\n",
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="document"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: application/zip\r\n\r\n",
        path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=b"".join(pieces),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("Telegram did not accept the private historical audit report")


def main() -> int:
    now = datetime.now(timezone.utc)
    if now < WINDOW_END_UTC:
        print(
            "HISTORICAL AUDIT SKIPPED: the 21 October Algeria-local window has not ended; "
            "rerun after 00:00 Algeria time on 22 October."
        )
        return 0
    if now > WINDOW_END_UTC + timedelta(days=7):
        print(
            "HISTORICAL AUDIT SKIPPED: this one-off report is only enabled for the week "
            "after the target window."
        )
        return 0

    repository = os.getenv("GITHUB_REPOSITORY", "abdelkaderdj/jobfinder").strip()
    token = required_env("GITHUB_TOKEN")
    posts, channel_counts, channel_errors, channels_at_limit = asyncio.run(collect_telegram_posts())

    runs = list_monitor_runs(repository, token, now)
    downloaded: list[tuple[int, list[str], str]] = []
    with ThreadPoolExecutor(max_workers=LOG_FETCH_WORKERS) as executor:
        futures = {
            executor.submit(download_run_log_texts, repository, token, run): run
            for run in runs
        }
        for future in as_completed(futures):
            downloaded.append(future.result())

    history, log_failures = correlate_run_logs(runs, downloaded)
    summary, enriched_posts = summarize(
        posts, history, channel_counts, channel_errors, channels_at_limit,
        log_failures, len(runs),
    )
    report = {
        "summary": summary,
        "posts": enriched_posts,
        "monitor_log_fetch_failures": log_failures,
        "privacy_and_safety": {
            "read_only_telegram_history": True,
            "used_ai_provider": False,
            "read_or_decrypted_state_enc": False,
            "read_or_decrypted_profile_enc": False,
            "read_or_decrypted_cv_files": False,
            "changed_channel_cursors_or_state": False,
            "sent_job_applications_or_emails": False,
            "report_destination": "configured private Telegram chat only; no public artifact uploaded",
        },
    }

    json_path = REPORT_DIR / "jobfinder_oct20_oct21_historical_audit.json"
    md_path = REPORT_DIR / "jobfinder_oct20_oct21_historical_audit.md"
    csv_path = REPORT_DIR / "jobfinder_oct20_oct21_historical_audit.csv"
    zip_path = Path("jobfinder_oct20_oct21_historical_audit.zip")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(summary, enriched_posts, log_failures), encoding="utf-8")
    create_csv(enriched_posts, csv_path)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(json_path, arcname=json_path.name)
        archive.write(md_path, arcname=md_path.name)
        archive.write(csv_path, arcname=csv_path.name)
        for media_file in sorted(MEDIA_DIR.rglob("*")):
            if media_file.is_file():
                archive.write(media_file, arcname=f"media/{media_file.name}")

    send_private_report(zip_path, summary)
    print(json.dumps({
        "audit_complete": True,
        "summary": summary,
        "report_sent_to_private_telegram": True,
        "public_artifact_uploaded": False,
        "no_ai_calls_or_job_applications": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
