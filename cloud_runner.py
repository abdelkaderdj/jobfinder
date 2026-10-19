from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon.sessions import StringSession

BASE_DIR = Path(__file__).resolve().parent
PRIVATE_DIR = BASE_DIR / "cloud_private"
STATE_ENC = PRIVATE_DIR / "state.enc"
ENCRYPTED_CVS_DIR = PRIVATE_DIR / "cvs"
SENT_FILE = BASE_DIR / "sent_applications.jsonl"
CV_DIR = BASE_DIR / "cvs"
CHANNELS = ["rcrdz1", "Jobs_dz7", "china1644", "ajob58dz", "ridkh", "CVDZJOBS"]
RUNNER_VERSION = "2026-10-19-ai-provider-routing-v2"

# Keep each scheduled run comfortably below the 10-minute GitHub Actions limit.
# Both limits apply; hitting either one ends the batch cleanly and persists cursors.
MAX_MESSAGES_PER_RUN = 8
RUN_BUDGET_SECONDS = 180


def provider_cooldown_remaining_seconds(
    state: dict[str, Any],
    now: datetime | None = None,
) -> int:
    """Return the persisted AI-provider cooldown in seconds, without mutating state."""
    raw = state.get("ai_provider_retry_after_utc")
    if not raw:
        return 0
    try:
        retry_at = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return 0
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0, int((retry_at.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds()))


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required GitHub Secret is missing: {name}")
    return value


def get_fernet() -> Fernet:
    key = required_env("JOBFINDER_FERNET_KEY").encode("ascii")
    try:
        return Fernet(key)
    except Exception as exc:
        raise RuntimeError("JOBFINDER_FERNET_KEY is invalid") from exc


def read_encrypted_json(path: Path, fernet: Fernet) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"Encrypted data file is missing: {path.relative_to(BASE_DIR)}")
    try:
        raw = fernet.decrypt(path.read_bytes())
        result = json.loads(raw.decode("utf-8"))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not decrypt {path.name}; check JOBFINDER_FERNET_KEY") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Encrypted JSON file has an invalid structure: {path.name}")
    return result


def write_encrypted_json(path: Path, payload: dict[str, Any], fernet: Fernet) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fernet.encrypt(raw))


def restore_private_files(fernet: Fernet, state: dict[str, Any]) -> None:
    # Decrypt candidate details from a private encrypted blob; never log the values.
    profile = read_encrypted_json(PRIVATE_DIR / "profile.enc", fernet)
    for field in ("candidate_profile", "candidate_name", "gmail_address"):
        if not isinstance(profile.get(field), str) or not profile[field].strip():
            raise RuntimeError("The encrypted private profile is incomplete")
    os.environ["JOBFINDER_CANDIDATE_PROFILE"] = profile["candidate_profile"]
    os.environ["JOBFINDER_CANDIDATE_NAME"] = profile["candidate_name"]
    os.environ["GMAIL_ADDRESS"] = profile["gmail_address"]

    CV_DIR.mkdir(parents=True, exist_ok=True)
    expected = {"cv_ats_en.pdf", "cv_ats_fr.pdf", "cv_normal_en.pdf", "cv_normal_fr.pdf"}
    found = set()
    for encrypted_path in ENCRYPTED_CVS_DIR.glob("*.pdf.fernet"):
        try:
            plaintext = fernet.decrypt(encrypted_path.read_bytes())
        except InvalidToken as exc:
            raise RuntimeError(f"Could not decrypt CV file: {encrypted_path.name}") from exc
        filename = encrypted_path.name[:-len(".fernet")]
        (CV_DIR / filename).write_bytes(plaintext)
        found.add(filename)
    missing = expected - found
    if missing:
        raise RuntimeError("Encrypted CV files missing: " + ", ".join(sorted(missing)))

    SENT_FILE.write_text(str(state.get("sent_applications_jsonl", "")), encoding="utf-8")


def _serialized_state(state: dict[str, Any]) -> bytes:
    return json.dumps(
        state, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def persist_checkpoint(
    state: dict[str, Any],
    fernet: Fernet,
    reason: str,
    checkpoint_cache: dict[str, bytes] | None = None,
) -> None:
    """Persist only encrypted cursor state and encrypted sent-history payload."""
    if SENT_FILE.exists():
        state["sent_applications_jsonl"] = SENT_FILE.read_text(encoding="utf-8")
    payload = _serialized_state(state)
    if checkpoint_cache is not None and checkpoint_cache.get("payload") == payload:
        print(f"Checkpoint unchanged ({reason})")
        return
    write_encrypted_json(STATE_ENC, state, fernet)
    if checkpoint_cache is not None:
        checkpoint_cache["payload"] = payload
    print(f"Encrypted checkpoint saved ({reason})")


async def poll_and_process(state: dict[str, Any], fernet: Fernet) -> None:
    # Import only after secrets and encrypted private profile have been loaded.
    import auto_jobfinder_cloud as processor

    api_id = int(required_env("TELEGRAM_API_ID"))
    api_hash = required_env("TELEGRAM_API_HASH")
    session = required_env("TELEGRAM_SESSION")
    last_ids = state.setdefault("last_message_ids", {})
    if not isinstance(last_ids, dict):
        raise RuntimeError("Encrypted Telegram cursor state is invalid")
    if SENT_FILE.exists():
        state["sent_applications_jsonl"] = SENT_FILE.read_text(encoding="utf-8")
    checkpoint_cache: dict[str, bytes] = {"payload": _serialized_state(state)}

    # Telegram control bot is independent from the Telethon account session.
    # Commands and review callbacks are polled at the start of each cloud run.
    from telegram_control import TelegramJobBot

    telegram_only_mode = os.getenv("JOBFINDER_TELEGRAM_ONLY", "").strip().lower() in {"1", "true", "yes"}
    telegram_webhook_mode = os.getenv("TELEGRAM_WEBHOOK_MODE", "").strip().lower() in {"1", "true", "yes"}
    webhook_event_mode = os.getenv("JOBFINDER_TELEGRAM_WEBHOOK_EVENT", "").strip().lower() in {"1", "true", "yes"}
    bot = TelegramJobBot(
        state,
        processor,
        lambda reason: persist_checkpoint(state, fernet, reason, checkpoint_cache),
        allow_review_actions=not telegram_only_mode,
    )

    if not os.getenv("GEMINI_API_KEY", "").strip():
        bot._enqueue(
            "ai-config-gemini-key-missing-v1",
            "⚠️ تنبيه إعداد الذكاء الاصطناعي في JobFinder\n\n"
            "مفتاح Gemini غير مضاف إلى GitHub Secrets؛ لذلك يعمل البوت حاليًا عبر Groq فقط، "
            "وقد يتوقف مؤقتًا عند بلوغ حصته. أضف مفتاحًا باسم GEMINI_API_KEY من إعدادات "
            "المستودع لتفعيل مزوّد مستقل. المنشورات غير المكتملة تبقى محفوظة لإعادة المحاولة.",
        )
    def persist_ai_provider_cooldown(retry_after_seconds: int, provider: str) -> None:
        seconds = max(60, min(86400, int(retry_after_seconds)))
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=seconds + 15)
        state["ai_provider_retry_after_utc"] = retry_at.isoformat(timespec="seconds")
        state["ai_provider_cooldown_reason"] = str(provider or "AI providers temporarily unavailable")[:80]
        bot._enqueue(
            "ai-provider-cooldown:" + retry_at.isoformat(timespec="seconds"),
            "⏸️ أوقف JobFinder تحليل المنشورات مؤقتًا لأن مزوّدات الذكاء الاصطناعي غير متاحة "
            "(حدود استخدام أو ضغط مؤقت).\n\n"
            f"سيُعاد التحليل بعد {retry_at.strftime('%Y-%m-%d %H:%M UTC')}. "
            "لم يُحرّك البوت مؤشر قراءة المنشور المتعثر، وستبقى المنشورات اللاحقة في الانتظار.\n"
            f"الحالة الفنية: {str(provider or 'quota')[:120]}",
        )
        print("AI PROVIDER COOLDOWN SAVED: " + str(provider or "unavailable")[:120] + "; retry after " + state["ai_provider_retry_after_utc"] + " UTC")
        persist_checkpoint(state, fernet, "AI provider cooldown saved", checkpoint_cache)

    processor.set_job_event_callback(bot.on_job_event)
    processor.set_job_pre_send_guard(bot.pre_send_guard_for_job)
    processor.set_job_ai_rate_limit_callback(persist_ai_provider_cooldown)

    # Correct the single false-positive review created by the old forced-recovery
    # exception. Match both channel and message ID; never reject it as a job.
    bot.dismiss_non_job_reviews_for_post(
        "ajob58dz",
        7654,
        "⚠️ تصحيح من JobFinder: أُرسل إشعار مراجعة لهذا المنشور بالخطأ. "
        "بعد التحقق، تبيّن أنه منشور اجتماعي وليس إعلان توظيف. "
        "تم استبعاده من المراجعات المعلقة ولن يُرسل أي طلب بخصوصه.",
    )


    # Automatically retire stale manual-review cards that the current
    # deterministic classifier now recognizes as CV/career-service marketing.
    # Group by source/message so multiple roles from one post reconcile counters once.
    pending_reviews = state.get("pending_reviews")
    service_promotion_posts: dict[tuple[str, str], None] = {}
    future_job_teaser_posts: dict[tuple[str, str], None] = {}
    if isinstance(pending_reviews, dict):
        for item in pending_reviews.values():
            if not isinstance(item, dict):
                continue
            post = item.get("post") or {}
            post_text = str(post.get("text") or item.get("advertisement") or "")
            source = str(post.get("source") or post.get("channel") or "").strip()
            message_id = str(post.get("message_id") or "").strip()
            if not source or not message_id:
                continue
            if processor.looks_like_non_job_job_teaser(post_text):
                future_job_teaser_posts[(source, message_id)] = None
            elif processor.looks_like_non_job_service_promotion(post_text):
                service_promotion_posts[(source, message_id)] = None

    dismissed_service_reviews = 0
    for source, message_id in service_promotion_posts:
        dismissed_service_reviews += bot.dismiss_non_job_reviews_for_post(
            source,
            message_id,
            "⚠️ تصحيح من JobFinder: كانت هذه البطاقة معلّقة كمراجعة توظيف، "
            "لكن التحقق من نص المنشور أظهر أنه إعلان ترويجي لخدمات إعداد السيرة الذاتية "
            "وليس إعلانًا يطلب موظفين. أُزيلت من المراجعات المعلقة ولن يُرسل أي طلب بخصوصها.",
        )
    if dismissed_service_reviews:
        print(
            f"NON-JOB SERVICE PROMOTION CLEANUP: "
            f"{dismissed_service_reviews} stale review card(s) dismissed"
        )

    dismissed_teaser_reviews = 0
    for source, message_id in future_job_teaser_posts:
        dismissed_teaser_reviews += bot.dismiss_non_job_reviews_for_post(
            source,
            message_id,
            "⚠️ تصحيح من JobFinder: أُرسلت بطاقة مراجعة لهذا المنشور بالخطأ. "
            "نص المنشور يعلن أن عروض العمل ستُنشر لاحقًا ولا يعرض وظيفة متاحة الآن. "
            "أُزيل من المراجعات المعلّقة، ولن يُرسل أي طلب بخصوصه.",
        )
    if dismissed_teaser_reviews:
        print(
            f"NON-JOB FUTURE-POST TEASER CLEANUP: "
            f"{dismissed_teaser_reviews} stale review card(s) dismissed"
        )

    if webhook_event_mode:
        event = {
            "update_type": os.getenv("JOBFINDER_TELEGRAM_UPDATE_TYPE", ""),
            "command": os.getenv("JOBFINDER_TELEGRAM_COMMAND", ""),
            "action": os.getenv("JOBFINDER_TELEGRAM_ACTION", ""),
            "pending_id": os.getenv("JOBFINDER_TELEGRAM_PENDING_ID", ""),
            "chat_id": os.getenv("JOBFINDER_TELEGRAM_CHAT_ID", ""),
            "user_id": os.getenv("JOBFINDER_TELEGRAM_USER_ID", ""),
            "message_id": os.getenv("JOBFINDER_TELEGRAM_MESSAGE_ID", ""),
        }
        bot.handle_webhook_event(event)
        state["last_run_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        persist_checkpoint(state, fernet, "Telegram webhook event processed", checkpoint_cache)
        print("Telegram webhook event mode: event handled; job channels were not scanned.")
        return

    if telegram_webhook_mode:
        print("Telegram webhook mode active; getUpdates polling is disabled")
    else:
        bot.poll_updates()
    bot.flush_outbox()
    bot.deliver_pending_reviews()
    state["last_run_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    persist_checkpoint(state, fernet, "Telegram controls checked", checkpoint_cache)

    if telegram_only_mode:
        print(
            "Telegram-only test mode: commands and notifications processed; "
            "job channels were not scanned and no application emails could be sent."
        )
        return

    if state.get("paused"):
        print("JobFinder is paused by Telegram command; Telegram controls remain active")
        return

    cooldown_remaining = provider_cooldown_remaining_seconds(state)
    if cooldown_remaining > 0:
        minutes, seconds = divmod(cooldown_remaining, 60)
        print(
            "AI PROVIDER COOLDOWN ACTIVE: Telegram commands remain available; "
            f"job processing will retry in {minutes}m {seconds}s. "
            "All unprocessed message cursors are preserved."
        )
        return
    expired_cooldown_at = state.pop("ai_provider_retry_after_utc", None)
    if expired_cooldown_at is not None:
        state.pop("ai_provider_cooldown_reason", None)
        bot._enqueue(
            "ai-provider-resumed:" + str(expired_cooldown_at),
            "✅ انتهت فترة انتظار مزوّد الذكاء الاصطناعي. استأنف JobFinder فحص المنشورات "
            "المحفوظة وإعادة محاولة التحليل.",
        )
        persist_checkpoint(state, fernet, "AI provider cooldown expired", checkpoint_cache)
        bot.flush_outbox()
        print("AI provider cooldown expired; resuming queued Telegram messages.")

    started = time.monotonic()
    messages_seen = 0
    if not CHANNELS:
        print("No Telegram channels configured")
        return

    # Rotate the starting channel each run so one busy channel cannot starve others.
    try:
        start_index = int(state.get("next_channel_index", 0)) % len(CHANNELS)
    except (TypeError, ValueError):
        start_index = 0
    ordered = [(start_index + offset) % len(CHANNELS) for offset in range(len(CHANNELS))]

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
            raise RuntimeError("Telegram session is not authorized; renew TELEGRAM_SESSION")

        for channel_index in ordered:
            channel = CHANNELS[channel_index]
            # The next run starts after the last channel that made progress.
            if messages_seen >= MAX_MESSAGES_PER_RUN or time.monotonic() - started >= RUN_BUDGET_SECONDS:
                if messages_seen == 0:
                    state["next_channel_index"] = (channel_index + 1) % len(CHANNELS)
                persist_checkpoint(state, fernet, "batch limit reached before channel", checkpoint_cache)
                print(
                    f"Batch stopped safely: {messages_seen} new message(s) examined; "
                    "remaining messages are queued for the next scheduled run"
                )
                return

            try:
                entity = await client.get_entity(channel)
                current_cursor = last_ids.get(channel)

                # One-time read-only diagnostic: compare the persisted cursor with
                # Telegram's latest message ID/date. This distinguishes genuinely
                # quiet channels from stale/ahead cursors that can silently yield
                # zero messages. No advertisement text is logged or stored here.
                cursor_diagnostics = state.setdefault("channel_cursor_diagnostics_v2", {})
                if not cursor_diagnostics.get(channel):
                    latest_messages = await client.get_messages(entity, limit=1)
                    latest_message = latest_messages[0] if latest_messages else None
                    latest_id = int(latest_message.id) if latest_message else 0
                    latest_date = (
                        latest_message.date.isoformat()
                        if latest_message is not None and latest_message.date
                        else "unavailable"
                    )
                    try:
                        cursor_number = int(current_cursor) if current_cursor is not None else None
                    except (TypeError, ValueError):
                        cursor_number = None
                    newer_available = (
                        latest_id > cursor_number if cursor_number is not None else None
                    )
                    id_gap = latest_id - cursor_number if cursor_number is not None else None
                    print(
                        f"[{channel}] CURSOR DIAGNOSTIC: stored_cursor={cursor_number}; "
                        f"latest_message_id={latest_id}; latest_message_date={latest_date}; "
                        f"latest_is_newer={newer_available}; id_gap={id_gap}"
                    )
                    cursor_diagnostics[channel] = {
                        "checked_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "stored_cursor": cursor_number,
                        "latest_message_id": latest_id,
                        "latest_message_date": latest_date,
                        "latest_is_newer": newer_available,
                    }
                    state["channel_cursor_diagnostics_v2"] = cursor_diagnostics
                    persist_checkpoint(
                        state, fernet, f"one-time cursor diagnostic for {channel}", checkpoint_cache
                    )

                if current_cursor is None:
                    latest = await client.get_messages(entity, limit=1)
                    latest_id = int(latest[0].id) if latest else 0
                    last_ids[channel] = latest_id
                    state["next_channel_index"] = (channel_index + 1) % len(CHANNELS)
                    persist_checkpoint(state, fernet, f"initial baseline for {channel}", checkpoint_cache)
                    print(f"[{channel}] initial baseline set; existing posts skipped")
                    continue

                cursor = int(current_cursor)
                # reverse=True processes oldest-to-newest. min_id selects posts newer than cursor.
                async for message in client.iter_messages(entity, min_id=cursor, reverse=True):
                    elapsed = time.monotonic() - started
                    if messages_seen >= MAX_MESSAGES_PER_RUN or elapsed >= RUN_BUDGET_SECONDS:
                        if messages_seen == 0:
                            state["next_channel_index"] = (channel_index + 1) % len(CHANNELS)
                        persist_checkpoint(state, fernet, "batch limit reached while scanning", checkpoint_cache)
                        print(
                            f"Batch stopped safely: {messages_seen} new message(s) examined; "
                            "remaining messages are queued for the next scheduled run"
                        )
                        return

                    message_id = int(message.id)
                    if message_id <= cursor:
                        continue
                    messages_seen += 1
                    text = (getattr(message, "message", None) or "").strip()
                    if not text:
                        cursor = message_id
                        last_ids[channel] = cursor
                        state["next_channel_index"] = (channel_index + 1) % len(CHANNELS)
                        persist_checkpoint(state, fernet, f"empty message cursor for {channel}", checkpoint_cache)
                        continue

                    post = {
                        "source": channel,
                        "channel": channel,
                        "message_id": message_id,
                        "date": message.date.isoformat() if message.date else "",
                        "text": text,
                    }
                    print(f"[{channel}] new post id={message_id}; analyzing")
                    try:
                        ok = processor.process_post(post)
                    except Exception as exc:
                        print(f"POST PROCESS ERROR: {type(exc).__name__}; post will retry")
                        ok = False

                    if ok is False:
                        # Hold this channel's cursor, but checkpoint sent-history from any
                        # partial success so the next run won't repeat already-sent requests.
                        state["next_channel_index"] = (channel_index + 1) % len(CHANNELS)
                        persist_checkpoint(state, fernet, f"failed post held for retry in {channel}", checkpoint_cache)
                        print(f"[{channel}] cursor held at {cursor}; failed post will retry")
                        if state.get("ai_provider_retry_after_utc"):
                            print(
                                "AI provider quota reached; ending this batch early. "
                                "The failed post and all later posts remain queued."
                            )
                            return
                        break

                    cursor = message_id
                    last_ids[channel] = cursor
                    state["next_channel_index"] = (channel_index + 1) % len(CHANNELS)
                    # Checkpoint each completed post so timeout/cancellation near the end
                    # of a run does not discard already-processed cursor progress.
                    persist_checkpoint(state, fernet, f"processed post in {channel}", checkpoint_cache)

                # Advance rotation even if this channel had no new posts. The finalizer
                # saves this rotation at the end; successful messages already checkpointed.
                state["next_channel_index"] = (channel_index + 1) % len(CHANNELS)

            except Exception as exc:
                # A channel failure keeps its cursor unchanged. Continue to other channels,
                # checkpoint the state, and retry the failed channel on a later run.
                state["next_channel_index"] = (channel_index + 1) % len(CHANNELS)
                print(f"[{channel}] Telegram polling error: {type(exc).__name__}; cursor preserved")
                persist_checkpoint(state, fernet, f"Telegram error for {channel}", checkpoint_cache)
                await asyncio.sleep(1)
                continue

        print(
            f"Batch complete: examined {messages_seen} new Telegram message(s) "
            f"across {len(CHANNELS)} channel(s) in {int(time.monotonic() - started)}s"
        )
    finally:
        await client.disconnect()


def main() -> int:
    print(f"JobFinder cloud runner version: {RUNNER_VERSION}")
    if os.getenv("GEMINI_API_KEY", "").strip():
        print("AI PROVIDER ROUTING: Gemini primary; Groq multi-model fallback enabled.")
    else:
        print(
            "AI PROVIDER ROUTING: Groq only. Independent Gemini fallback is inactive "
            "until the GitHub Actions secret GEMINI_API_KEY is populated."
        )
    print(
        f"Batch limits: max {MAX_MESSAGES_PER_RUN} message(s), "
        f"{RUN_BUDGET_SECONDS}s processing budget"
    )
    # Fail fast if required application credentials are absent.
    required_env("GROQ_API_KEY")
    required_env("GMAIL_APP_PASSWORD")
    fernet = get_fernet()
    state = read_encrypted_json(STATE_ENC, fernet)
    original_state = copy.deepcopy(state)

    try:
        restore_private_files(fernet, state)
        asyncio.run(poll_and_process(state, fernet))
    except Exception as exc:
        print(f"JOBFINDER RUN ERROR: {type(exc).__name__}: {str(exc)[:180]}")
        result_code = 1
    else:
        result_code = 0
    finally:
        # Persist Telegram cursors and sent-application deduplication history only as ciphertext.
        if SENT_FILE.exists():
            state["sent_applications_jsonl"] = SENT_FILE.read_text(encoding="utf-8")
        if state != original_state:
            write_encrypted_json(STATE_ENC, state, fernet)
            print("Encrypted state updated")
        else:
            print("No state changes")

    return result_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # No traceback to prevent accidental disclosure in public workflow logs.
        print(f"FATAL: {type(exc).__name__}: {str(exc)[:180]}")
        sys.exit(1)
