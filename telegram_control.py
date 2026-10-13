from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


LOCAL_ZONE = ZoneInfo("Africa/Algiers")
MAX_OUTBOX_PER_RUN = 10

MAIN_MENU_BUTTONS = {
    "🏠 الرئيسية": "/start",
    "📥 المراجعات المعلقة": "/pending",
    "📨 طلبات اليوم": "/sent",
    "📊 ملخص اليوم": "/today",
    "📡 حالة النظام": "/status",
    "⏯ تشغيل/إيقاف": "/toggle",
    "❓ المساعدة": "/help",
}


def main_menu_keyboard(paused: bool = False) -> dict[str, Any]:
    """Persistent Arabic navigation; one state-aware toggle avoids stale duplicate controls."""
    return {
        "keyboard": [
            [{"text": "🏠 الرئيسية"}, {"text": "📥 المراجعات المعلقة"}],
            [{"text": "📨 طلبات اليوم"}, {"text": "📊 ملخص اليوم"}],
            [{"text": "📡 حالة النظام"}, {"text": "⏯ تشغيل/إيقاف"}],
            [{"text": "❓ المساعدة"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "اختر من القائمة…",
    }


def now_local() -> datetime:
    return datetime.now(LOCAL_ZONE)


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def safe_text(value: Any, limit: int = 300) -> str:
    text = str(value or "").strip()
    return text[:limit] if text else "غير محدد"


def event_key(record: dict[str, Any]) -> str:
    raw = "|".join(
        str(record.get(key) or "")
        for key in (
            "source",
            "message_id",
            "application_job_title",
            "company",
            "location",
        )
    )
    raw += "|" + "|".join(str(item) for item in (record.get("emails") or []))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class TelegramJobBot:
    """Telegram Bot API helper for notifications and owner-only review controls."""

    def __init__(self, state: dict[str, Any], processor, persist_callback, allow_review_actions: bool = True):
        self.state = state
        self.processor = processor
        self.persist_callback = persist_callback
        self.allow_review_actions = allow_review_actions
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.owner_chat_id = os.getenv("TELEGRAM_BOT_CHAT_ID", "").strip()
        self.state.setdefault("pending_reviews", {})
        self.state.setdefault("telegram_outbox", [])
        self.state.setdefault("telegram_sent_notifications", {})
        self.state.setdefault("review_history", {})
        self.state.setdefault("counted_job_events", [])

    @property
    def token_configured(self) -> bool:
        return bool(self.token)

    @property
    def owner_configured(self) -> bool:
        return bool(self.token and self.owner_chat_id)

    def _save(self, reason: str) -> None:
        try:
            self.persist_callback(reason)
        except Exception as exc:
            print(f"Telegram state checkpoint error: {type(exc).__name__}")

    def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("Telegram bot token is not configured")
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("Telegram Bot API request failed") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError("Telegram Bot API rejected the request")
        return result.get("result") or {}

    def _send_message(
        self,
        chat_id: str | int,
        text: str,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        return self._api("sendMessage", payload)

    def _send_owner(self, text: str, keyboard: dict[str, Any] | None = None) -> bool:
        if not self.owner_configured:
            return False
        if keyboard is None:
            keyboard = main_menu_keyboard(paused=bool(self.state.get("paused")))
        try:
            self._send_message(self.owner_chat_id, text, keyboard)
            return True
        except Exception as exc:
            print(f"Telegram notification send failed: {type(exc).__name__}")
            return False

    def _answer_callback(
        self,
        callback_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        # In webhook mode Telegram has already acknowledged the callback immediately.
        # Do not attempt to answer it a second time from the delayed GitHub runner.
        if not callback_id:
            return
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:180]
        if show_alert:
            payload["show_alert"] = True
        try:
            self._api("answerCallbackQuery", payload)
        except Exception as exc:
            print(f"Telegram callback answer failed: {type(exc).__name__}")

    def _edit_message(self, query: dict[str, Any], text: str) -> None:
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        message_id = message.get("message_id")
        if not chat.get("id") or not message_id:
            return
        try:
            self._api(
                "editMessageText",
                {
                    "chat_id": chat["id"],
                    "message_id": message_id,
                    "text": text[:4000],
                    "disable_web_page_preview": True,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        except Exception as exc:
            print(f"Telegram message edit failed: {type(exc).__name__}")

    def _authorized(self, chat_id: Any, user_id: Any = None) -> bool:
        if not self.owner_configured or str(chat_id) != self.owner_chat_id:
            return False
        # The intended control channel is the owner's private chat, not a group.
        return user_id is None or str(user_id) == self.owner_chat_id

    def _parse_local_datetime(self, value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_ZONE)
            return parsed.astimezone(LOCAL_ZONE)
        except (TypeError, ValueError, OverflowError):
            return None

    def _sent_records_without_timestamp(self) -> list[dict[str, Any]]:
        raw = self.state.get("sent_applications_jsonl", "")
        if not isinstance(raw, str):
            return []
        records: list[dict[str, Any]] = []
        for line in raw.splitlines():
            try:
                record = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and self._parse_local_datetime(record.get("sent_at")) is None:
                records.append(record)
        return records

    def _sent_records_today(self) -> list[dict[str, Any]]:
        today = now_local().date()
        raw = self.state.get("sent_applications_jsonl", "")
        if not isinstance(raw, str):
            return []
        records: list[dict[str, Any]] = []
        for line in raw.splitlines():
            try:
                record = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            sent_at = self._parse_local_datetime(record.get("sent_at"))
            if sent_at and sent_at.date() == today:
                records.append(record)
        records.sort(key=lambda item: str(item.get("sent_at") or ""), reverse=True)
        return records

    def _last_run_display(self) -> str:
        raw = self.state.get("last_run_at")
        parsed = self._parse_local_datetime(raw)
        if parsed:
            return parsed.strftime("%Y-%m-%d %H:%M")
        return safe_text(raw, 100) if raw else "غير متوفر"

    def _dashboard_text(self) -> str:
        stats = self._today_stats()
        pending_count = len(self.state.get("pending_reviews") or {})
        sent_count = len(self._sent_records_today())
        undated_sent_count = len(self._sent_records_without_timestamp())
        paused = bool(self.state.get("paused"))
        return (
            "📡 حالة JobFinder — لوحة التحكم\n\n"
            f"الحالة: {'⏸ متوقف مؤقتًا' if paused else '🟢 نشط'}\n"
            f"آخر تشغيل: {self._last_run_display()} (توقيت الجزائر)\n\n"
            f"📥 المراجعات المعلقة: {pending_count}\n"
            f"📨 طلبات مسجلة اليوم: {sent_count}\n"
            f"📚 سجلات إرسال قديمة بلا توقيت: {undated_sent_count}\n"
            f"🔎 الوظائف التي جرى تقييمها اليوم: {stats.get('analyzed', 0)}\n"
            f"❌ المرفوضة تلقائيًا اليوم: {stats.get('rejected', 0)}\n"
            f"🛑 مراجعات رُفضت يدويًا اليوم: {stats.get('manual_rejected', 0)}\n"
            f"♻️ وظائف مكررة تم تجاوزها: {stats.get('duplicates', 0)}\n"
            f"⚠️ محاولات الإرسال غير المؤكدة: {stats.get('send_failed', 0)}\n\n"
            "استخدم الأزرار أسفل الشاشة للتنقل."
        )

    def _manual_rejections_today(self) -> int:
        """Count reviews manually rejected today, using the resolution timestamp."""
        today = now_local().date()
        history = self.state.get("review_history", {})
        if not isinstance(history, dict):
            return 0
        count = 0
        for item in history.values():
            if not isinstance(item, dict) or item.get("status") != "rejected":
                continue
            resolved_at = self._parse_local_datetime(item.get("at"))
            if resolved_at and resolved_at.date() == today:
                count += 1
        return count

    def _today_stats(self) -> dict[str, Any]:
        today = now_local().date().isoformat()
        stats = self.state.get("daily_stats")
        if not isinstance(stats, dict) or stats.get("date") != today:
            stats = {
                "date": today,
                "analyzed": 0,
                "sent": 0,
                "review": 0,
                "rejected": 0,
                "manual_rejected": 0,
                "duplicates": 0,
                "send_failed": 0,
                # Fresh counters already use the corrected definitions.
                "manual_rejections_reconciled": True,
            }
            self.state["daily_stats"] = stats
        else:
            # The old version counted a manual review rejection as an automatic
            # rejection too. Split those historical counts exactly once after
            # deployment, based on review_history timestamps.
            if not stats.get("manual_rejections_reconciled"):
                manual_rejections = self._manual_rejections_today()
                old_rejected = max(0, int(stats.get("rejected", 0)))
                reconciled = min(old_rejected, manual_rejections)
                stats["rejected"] = old_rejected - reconciled
                stats["manual_rejected"] = max(
                    int(stats.get("manual_rejected", 0)),
                    manual_rejections,
                )
                stats["manual_rejections_reconciled"] = True

            stats.setdefault("manual_rejected", 0)
            # Migrate same-day legacy counters. Infer only duplicate APPLY
            # events that the old schema did not count as a separate category.
            if "duplicates" not in stats:
                analyzed = max(0, int(stats.get("analyzed", 0)))
                classified = (
                    max(0, int(stats.get("sent", 0)))
                    + max(0, int(stats.get("review", 0)))
                    + max(0, int(stats.get("rejected", 0)))
                    + max(0, int(stats.get("send_failed", 0)))
                )
                stats["duplicates"] = max(0, analyzed - classified)

        # Normalize new fields and tolerate old/partial state without crashing
        # the Telegram command handler.
        for key in ("analyzed", "sent", "review", "rejected", "manual_rejected", "duplicates", "send_failed"):
            try:
                stats[key] = max(0, int(stats.get(key, 0)))
            except (TypeError, ValueError):
                stats[key] = 0
        return stats

    def _count_event_once(self, record: dict[str, Any]) -> None:
        key = event_key(record)
        seen = self.state.setdefault("counted_job_events", [])
        if key in seen:
            return
        seen.append(key)
        del seen[:-500]
        stats = self._today_stats()
        stats["analyzed"] = int(stats.get("analyzed", 0)) + 1
        decision = str(record.get("decision") or "").upper()
        if decision == "REVIEW":
            stats["review"] = int(stats.get("review", 0)) + 1
        elif decision == "REJECT":
            stats["rejected"] = int(stats.get("rejected", 0)) + 1
        elif decision == "APPLY" and record.get("sent"):
            stats["sent"] = int(stats.get("sent", 0)) + 1
        elif decision == "APPLY" and record.get("job_duplicate"):
            stats["duplicates"] = int(stats.get("duplicates", 0)) + 1
        elif decision == "APPLY":
            stats["send_failed"] = int(stats.get("send_failed", 0)) + 1

    def _enqueue(self, key: str, text: str) -> None:
        sent = self.state.setdefault("telegram_sent_notifications", {})
        if key in sent:
            return
        outbox = self.state.setdefault("telegram_outbox", [])
        if any(item.get("key") == key for item in outbox if isinstance(item, dict)):
            return
        outbox.append({"key": key, "text": text, "created_at": iso_now()})
        self._save("Telegram notification queued")

    def flush_outbox(self, limit: int = MAX_OUTBOX_PER_RUN) -> None:
        if not self.owner_configured:
            return
        outbox = self.state.setdefault("telegram_outbox", [])
        sent = self.state.setdefault("telegram_sent_notifications", {})
        processed = 0
        while outbox and processed < limit:
            item = outbox[0]
            if not isinstance(item, dict) or not item.get("key"):
                outbox.pop(0)
                continue
            key = str(item["key"])
            if key in sent:
                outbox.pop(0)
                continue
            try:
                result = self._send_message(self.owner_chat_id, str(item.get("text") or ""))
            except Exception as exc:
                print(f"Telegram outbox delivery delayed: {type(exc).__name__}")
                self._save("Telegram outbox retained for retry")
                return
            sent[key] = {
                "message_id": result.get("message_id"),
                "sent_at": iso_now(),
            }
            outbox.pop(0)
            if len(sent) > 500:
                oldest = list(sent.keys())[:-400]
                for old_key in oldest:
                    sent.pop(old_key, None)
            self._save("Telegram outbox delivered")
            processed += 1

    def _review_text(self, item: dict[str, Any]) -> str:
        record = item.get("record") or {}
        title = safe_text(record.get("application_job_title") or record.get("job_title"), 200)
        company = safe_text(record.get("company"), 180)
        location = safe_text(record.get("location"), 180)
        score = record.get("score")
        score_text = f"{score}/100" if score is not None else "غير متوفر"
        emails = record.get("emails") or []
        email_text = ", ".join(str(email) for email in emails) if emails else "لا يوجد بريد واضح"
        reason = safe_text(record.get("reason"), 600)
        section = str(item.get("advertisement") or "").strip()
        excerpt = section[:1700]
        source = str(item.get("post", {}).get("source") or "")
        message_id = item.get("post", {}).get("message_id")
        source_link = ""
        if source and message_id and re_safe_channel(source):
            source_link = f"\n🔗 الإعلان الأصلي: https://t.me/{source}/{message_id}"
        if item.get("can_auto_send"):
            action_note = "✅ القبول سيرسل طلبًا بالبريد بعد إعادة فحص التكرار."
        else:
            action_note = (
                "⚠️ القبول هنا يعني قبولًا للمراجعة اليدوية فقط؛ لن يُرسل بريد تلقائيًا "
                "لأن وسيلة التقديم أو بيانات الاتصال غير مؤكدة."
            )
        return (
            "🟡 وظيفة تحتاج إلى مراجعتك\n\n"
            f"💼 الوظيفة: {title}\n"
            f"🏢 الشركة: {company}\n"
            f"📍 الموقع: {location}\n"
            f"📊 التقييم: {score_text}\n"
            f"📧 جهة التقديم: {email_text}\n"
            f"🧠 السبب: {reason}\n\n"
            f"{action_note}\n\n"
            f"📄 مقتطف الإعلان:\n{excerpt}{source_link}"
        )[:3900]

    def _review_keyboard(self, item: dict[str, Any]) -> dict[str, Any]:
        pending_id = str(item.get("id") or "")
        accept_action = "approve" if item.get("can_auto_send") else "manual"
        accept_label = "✅ قبول وإرسال" if item.get("can_auto_send") else "✅ قبول يدوي"
        return {
            "inline_keyboard": [[
                {"text": accept_label, "callback_data": f"jf:{accept_action}:{pending_id}"},
                {"text": "❌ رفض", "callback_data": f"jf:reject:{pending_id}"},
            ]]
        }

    def _deliver_review(self, item: dict[str, Any]) -> bool:
        if not self.owner_configured:
            return False
        try:
            result = self._send_message(
                self.owner_chat_id,
                self._review_text(item),
                self._review_keyboard(item),
            )
        except Exception as exc:
            print(f"Telegram review delivery delayed: {type(exc).__name__}")
            return False
        item["notified"] = True
        item["telegram_message_id"] = result.get("message_id")
        item["notified_at"] = iso_now()
        self._save("Telegram review delivered")
        return True

    def deliver_pending_reviews(self, limit: int = 10) -> None:
        if not self.owner_configured:
            return
        count = 0
        for item in list(self.state.setdefault("pending_reviews", {}).values()):
            if count >= limit:
                break
            if isinstance(item, dict) and not item.get("notified"):
                if self._deliver_review(item):
                    count += 1

    def pre_send_guard_for_job(
        self,
        job: dict[str, Any],
        application_title: str,
        emails: list[str] | None,
        post: dict[str, Any] | None = None,
    ) -> str:
        """Block a job that matches a prior owner rejection, including reposts."""
        if not isinstance(job, dict) or not str(application_title or "").strip():
            return ""
        rejected = self.state.get("rejected_job_history", [])
        if not isinstance(rejected, list):
            return ""
        current_post = post if isinstance(post, dict) else {}
        current_source = str(current_post.get("source") or "")
        current_message_id = str(current_post.get("message_id") or "")
        current_emails = [str(email).strip() for email in (emails or []) if str(email).strip()]

        for old_record in reversed(rejected[-500:]):
            if not isinstance(old_record, dict):
                continue
            old_title = str(
                old_record.get("application_job_title")
                or old_record.get("job_title")
                or ""
            ).strip()
            same_post = (
                current_source
                and current_message_id
                and current_source == str(old_record.get("source") or "")
                and current_message_id == str(old_record.get("message_id") or "")
            )

            # Prefer the processor's domain-aware identity matcher.
            similarity_fn = getattr(self.processor, "title_similarity", None)
            if callable(similarity_fn):
                similarity = similarity_fn(application_title, old_title)
            else:
                # A deterministic fallback keeps the guard fail-safe in tests
                # and degraded modes where the processor helper is unavailable.
                from difflib import SequenceMatcher
                normalize = lambda value: " ".join(str(value or "").casefold().split())
                similarity = SequenceMatcher(
                    None, normalize(application_title), normalize(old_title)
                ).ratio()

            old_emails = old_record.get("emails") or []
            old_email = str(old_record.get("email") or "").strip()
            old_email_set = {str(email).strip().casefold() for email in old_emails if str(email).strip()}
            if old_email:
                old_email_set.add(old_email.casefold())
            same_email = bool(
                {email.casefold() for email in current_emails}.intersection(old_email_set)
            )

            normalize_company = getattr(self.processor, "normalize_company", None)
            normalize_location = getattr(self.processor, "normalize_location", None)
            current_company_raw = str(job.get("company") or "").strip()
            old_company_raw = str(old_record.get("company") or "").strip()
            current_location_raw = str(job.get("location") or "").strip()
            old_location_raw = str(old_record.get("location") or "").strip()
            current_company = (
                normalize_company(current_company_raw) if callable(normalize_company)
                else current_company_raw.casefold()
            )
            old_company = (
                normalize_company(old_company_raw) if callable(normalize_company)
                else old_company_raw.casefold()
            )
            current_location = (
                normalize_location(current_location_raw) if callable(normalize_location)
                else current_location_raw.casefold()
            )
            old_location = (
                normalize_location(old_location_raw) if callable(normalize_location)
                else old_location_raw.casefold()
            )
            location_compatible = (
                not current_location or not old_location
                or current_location == old_location
                or current_location in old_location
                or old_location in current_location
            )
            same_company = bool(current_company and old_company and current_company == old_company)

            if similarity >= 0.82 and (
                same_post
                or (same_email and location_compatible)
                or (same_company and location_compatible)
            ):
                return "تم منع تكرار وظيفة سبق رفضها يدويًا."

            identity_fn = getattr(self.processor, "same_job_identity", None)
            if callable(identity_fn):
                for email in current_emails or [""]:
                    try:
                        if identity_fn(job, application_title, email, old_record):
                            return "تم منع تكرار وظيفة سبق رفضها يدويًا."
                    except Exception as exc:
                        print("PREVIOUS REJECTION MATCH ERROR:", type(exc).__name__)
                        raise
        return ""

    def on_job_event(self, event: dict[str, Any]) -> None:
        record = event.get("record") or {}
        if not isinstance(record, dict):
            return
        decision = str(record.get("decision") or "").upper()
        if decision in {"APPLY", "REVIEW"}:
            job = event.get("job") or {}
            post = event.get("post") or {}
            reason = self.pre_send_guard_for_job(
                job if isinstance(job, dict) else {},
                str(record.get("application_job_title") or record.get("job_title") or ""),
                record.get("emails") or [],
                post if isinstance(post, dict) else {},
            )
            if reason:
                record["decision"] = "REJECT"
                record["reason"] = (
                    (str(record.get("reason") or "").strip() + " ") + reason
                ).strip()
        self._count_event_once(record)
        self._save("Job event counters updated")
        decision = str(record.get("decision") or "").upper()
        key = event_key(record)

        if decision == "REVIEW":
            pending_id = key[:16]
            pending = self.state.setdefault("pending_reviews", {})
            history = self.state.setdefault("review_history", {})
            if pending_id not in pending and pending_id not in history:
                job = event.get("job") or {}
                emails = record.get("emails") or []
                location_status = str(record.get("location_status") or "")
                can_auto_send = bool(
                    record.get("contact_scope_matched") is True
                    and len(emails) == 1
                    and str(record.get("cv") or "").strip()
                    and location_status.startswith("allowed_")
                )
                item = {
                    "id": pending_id,
                    "created_at": iso_now(),
                    "notified": False,
                    "can_auto_send": can_auto_send,
                    "record": record,
                    "job": job if isinstance(job, dict) else {},
                    "advertisement": str(event.get("advertisement") or "")[:8000],
                    "post": event.get("post") or {},
                }
                pending[pending_id] = item
                self._save("New job review queued")
                self._deliver_review(item)
            return

        if decision != "APPLY":
            return

        title = safe_text(record.get("application_job_title") or record.get("job_title"), 200)
        company = safe_text(record.get("company"), 160)
        location = safe_text(record.get("location"), 160)
        source = str(record.get("source") or "")
        message_id = record.get("message_id")
        source_link = (
            f"\n🔗 https://t.me/{source}/{message_id}"
            if source and message_id and re_safe_channel(source)
            else ""
        )
        if record.get("sent"):
            email = (record.get("emails") or [""])[0]
            cv = os.path.basename(str(record.get("cv") or "")) or "غير محددة"
            text = (
                "✅ تم إرسال طلب التوظيف\n\n"
                f"💼 الوظيفة: {title}\n"
                f"🏢 الشركة: {company}\n"
                f"📍 الموقع: {location}\n"
                f"📧 البريد: {email}\n"
                f"📄 السيرة الذاتية: {cv}\n\n"
                "ℹ️ خادم البريد قبل الرسالة؛ هذا لا يضمن وصولها إلى صندوق الوارد."
                f"{source_link}"
            )
            self._enqueue(f"job-sent:{key}", text)
        elif record.get("job_duplicate"):
            self._enqueue(
                f"job-duplicate:{key}",
                (
                    "ℹ️ لم يُرسل طلب مكرر\n\n"
                    f"💼 الوظيفة: {title}\n"
                    f"🏢 الشركة: {company}\n"
                    "وجد النظام طلبًا سابقًا للوظيفة نفسها."
                    f"{source_link}"
                ),
            )
        else:
            reason = safe_text(record.get("reason"), 250)
            self._enqueue(
                f"job-send-failed:{key}",
                (
                    "⚠️ تعذّر تأكيد إرسال طلب التوظيف\n\n"
                    f"💼 الوظيفة: {title}\n"
                    f"🏢 الشركة: {company}\n"
                    f"📍 الموقع: {location}\n"
                    f"السبب: {reason}\n"
                    "سيحتفظ النظام بسجل الحالة؛ راجع GitHub إذا استمر الخطأ."
                    f"{source_link}"
                ),
            )
        self.flush_outbox()

    def handle_webhook_event(self, event: dict[str, Any]) -> None:
        """Process a minimal, owner-validated Telegram event dispatched by the webhook."""
        if not isinstance(event, dict):
            raise RuntimeError("Telegram webhook event is invalid")

        update_type = str(event.get("update_type") or "")
        chat_id = event.get("chat_id")
        user_id = event.get("user_id")
        try:
            chat_id = int(chat_id)
            user_id = int(user_id)
        except (TypeError, ValueError):
            print("Telegram webhook event rejected: invalid chat or user ID")
            return

        if not self._authorized(chat_id, user_id):
            print("Telegram webhook event rejected: owner authorization did not match")
            return

        if update_type == "command":
            command = str(event.get("command") or "").strip()
            if command.split("@")[0].lower() not in {
                "/start", "/help", "/status", "/today", "/sent", "/pending", "/pause", "/resume", "/toggle"
            }:
                print("Telegram webhook command ignored: unsupported command")
                return
            self._handle_message({
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": user_id},
                "text": command,
            })
            print(f"Telegram webhook command completed: {command.split()[0][:24]}")
        elif update_type == "callback":
            action = str(event.get("action") or "")
            pending_id = str(event.get("pending_id") or "")
            allowed_actions = {"approve", "manual", "reject", "test"}
            if action not in allowed_actions or not pending_id or len(pending_id) > 32:
                print("Telegram webhook callback rejected: invalid action payload")
                return
            try:
                message_id = int(event.get("message_id"))
            except (TypeError, ValueError):
                print("Telegram webhook callback rejected: invalid message ID")
                return
            self._handle_callback({
                # Empty callback ID means the webhook already acknowledged it.
                "id": "",
                "from": {"id": user_id},
                "message": {
                    "chat": {"id": chat_id, "type": "private"},
                    "message_id": message_id,
                },
                "data": f"jf:{action}:{pending_id}",
            })
            print(f"Telegram webhook callback completed: {action}")
        else:
            print("Telegram webhook event ignored: unsupported update type")
            return

        self.flush_outbox()
        self.deliver_pending_reviews()

    def poll_updates(self) -> None:
        if not self.token_configured:
            return
        try:
            offset = int(self.state.get("telegram_bot_update_offset", 0))
            updates = self._api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 0,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
        except Exception as exc:
            print(f"Telegram bot update polling failed: {type(exc).__name__}")
            return
        if not isinstance(updates, list):
            print("Telegram poll result was not a list")
            return
        print(f"Telegram updates received by this run: {len(updates)}")
        for update in updates[:100]:
            if not isinstance(update, dict) or "update_id" not in update:
                continue
            update_id = int(update["update_id"])
            try:
                if isinstance(update.get("message"), dict):
                    message = update["message"]
                    text_value = str(message.get("text") or "").strip()
                    if text_value.startswith("/"):
                        print(f"Telegram command received: {text_value.split()[0].split('@')[0][:24]}")
                    self._handle_message(message)
                elif isinstance(update.get("callback_query"), dict):
                    callback = update["callback_query"]
                    data_value = str(callback.get("data") or "")
                    action_value = data_value.split(":")[1] if data_value.startswith("jf:") and len(data_value.split(":")) > 1 else "unknown"
                    print(f"Telegram callback received: {action_value[:20]}")
                    self._handle_callback(callback)
            except Exception as exc:
                print(f"Telegram update handling failed: {type(exc).__name__}")
            self.state["telegram_bot_update_offset"] = update_id + 1
            self._save("Telegram update processed")
        self.flush_outbox()
        self.deliver_pending_reviews()

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = sender.get("id")
        text = str(message.get("text") or "").strip()
        if not text:
            return
        if text in MAIN_MENU_BUTTONS:
            command = MAIN_MENU_BUTTONS[text]
        elif text.startswith("/"):
            command = text.split()[0].split("@")[0].lower()
        else:
            return

        if not self.owner_chat_id:
            if chat.get("type") != "private":
                return
            if command in {"/start", "/id"} and chat_id is not None:
                self._send_message(
                    chat_id,
                    (
                        f"معرّف محادثتك هو: {chat_id}\n\n"
                        "أضف هذا الرقم إلى GitHub Secrets باسم TELEGRAM_BOT_CHAT_ID، "
                        "ثم أعد تشغيل JobFinder. لن تعمل أوامر التحكم قبل ضبط هذا السر."
                    ),
                )
            return

        if not self._authorized(chat_id, user_id):
            return

        if command == "/start":
            self._send_owner("👋 مرحبًا بك في JobFinder\n\n" + self._dashboard_text())
        elif command == "/help":
            self._send_owner(
                "🤖 مساعدة JobFinder\n\n"
                "🏠 الرئيسية — لوحة التحكم\n"
                "📥 المراجعات المعلقة — عرض الوظائف التي تنتظر قرارك\n"
                "📊 ملخص اليوم — إحصائيات اليوم\n"
                "📡 حالة النظام — الحالة وآخر تشغيل\n"
                "⏸ إيقاف مؤقت — إيقاف فحص الإعلانات الجديدة\n"
                "▶️ استئناف — إعادة الفحص\n"
                "📨 طلبات اليوم — الوظائف التي سُجّل إرسالها اليوم\n"
                "⏯ تشغيل/إيقاف — تبديل الحالة حسب وضع النظام الحالي\n\n"
                "أزرار قبول/رفض الوظيفة تظهر تحت رسالة الوظيفة نفسها."
            )
        elif command == "/status":
            self._send_owner(self._dashboard_text())
        elif command == "/today":
            stats = self._today_stats()
            sent_count = len(self._sent_records_today())
            self._save("Daily stats checked")
            self._send_owner(
                f"📊 ملخص JobFinder — {stats.get('date')} (توقيت الجزائر)\n\n"
                f"الوظائف التي جرى تقييمها اليوم: {stats.get('analyzed', 0)}\n"
                f"طلبات مسجلة الإرسال اليوم: {sent_count}\n"
                f"مراجعات جديدة اليوم: {stats.get('review', 0)}\n"
                f"المرفوضة تلقائيًا اليوم: {stats.get('rejected', 0)}\n"
                f"مراجعات رُفضت يدويًا اليوم: {stats.get('manual_rejected', 0)}\n"
                f"وظائف مكررة تم تجاوزها: {stats.get('duplicates', 0)}\n"
                f"محاولات الإرسال غير المؤكدة: {stats.get('send_failed', 0)}\n\n"
                "ملاحظة: «مراجعات جديدة» تعني وظائف صُنّفت للمراجعة أولًا؛ الرفض اليدوي يُحسب منفصلًا ولا يُضاف إلى الرفض التلقائي.\n"
                "تُحتسب الطلبات حسب توقيت الإرسال المسجل؛ السجلات القديمة بلا توقيت لا تُنسب إلى اليوم تخمينًا."
            )
        elif command == "/sent":
            records = self._sent_records_today()
            today = now_local().date().isoformat()
            if not records:
                undated_records = self._sent_records_without_timestamp()
                lines = [
                    f"📨 طلبات التوظيف — {today}",
                    "",
                    "لا توجد طلبات يمكن تأكيد توقيتها لهذا اليوم.",
                ]
                if undated_records:
                    lines.extend([
                        "",
                        f"📚 توجد {len(undated_records)} سجلات محفوظة في سجل الإرسال القديم بلا توقيت.",
                        "الإصدار السابق لم يكن يحفظ وقت الإرسال؛ هذه السجلات لا تخص طلبات اليوم، ولا يثبت السجل وحده وصول البريد إلى صندوق الوارد.",
                        "نحتفظ بها لمنع إعادة التقديم على الوظائف نفسها.",
                        "",
                        "أول سجلات قديمة (ليست طلبات اليوم):",
                    ])
                    for index, record in enumerate(undated_records[:5], start=1):
                        title = safe_text(record.get("application_job_title") or record.get("job_title"), 180)
                        company = safe_text(record.get("company"), 140)
                        lines.append(f"{index}. 💼 {title}")
                        if company:
                            lines.append(f"🏢 {company}")
                    if len(undated_records) > 5:
                        lines.append(f"تُعرض أول 5 سجلات من أصل {len(undated_records)}.")
                else:
                    lines.extend([
                        "",
                        "ستظهر هنا الطلبات الجديدة التي تحمل توقيت إرسال محفوظًا.",
                    ])
                self._send_owner("\n".join(lines))
            else:
                lines = [
                    f"📨 طلبات التوظيف المسجلة اليوم — {today}",
                    f"عدد الطلبات المسجلة: {len(records)}",
                    "",
                ]
                for index, record in enumerate(records[:10], start=1):
                    sent_at = self._parse_local_datetime(record.get("sent_at"))
                    time_label = sent_at.strftime("%H:%M") if sent_at else "وقت غير متوفر"
                    title = safe_text(record.get("application_job_title") or record.get("job_title"), 180)
                    company = safe_text(record.get("company"), 140)
                    location = safe_text(record.get("location"), 140)
                    cv = os.path.basename(str(record.get("cv") or "")) or "غير محددة"
                    email = safe_text(record.get("email"), 180)
                    lines.extend([
                        f"{index}. 💼 {title}",
                        f"🏢 {company} | 📍 {location}",
                        f"🕒 {time_label} | 📄 {cv}",
                        f"📧 {email}",
                        "",
                    ])
                if len(records) > 10:
                    lines.append(f"يعرض آخر 10 طلبات من أصل {len(records)}.")
                undated_records = self._sent_records_without_timestamp()
                if undated_records:
                    lines.extend([
                        "",
                        f"ℹ️ توجد أيضًا {len(undated_records)} سجلات إرسال قديمة بلا توقيت محفوظ؛ ليست طلبات اليوم وتبقى محفوظة لمنع التكرار.",
                    ])
                self._send_owner("\n".join(lines))
        elif command == "/pending":
            pending_items = list((self.state.get("pending_reviews") or {}).values())
            if not pending_items:
                self._send_owner("لا توجد وظائف تنتظر المراجعة حاليًا.")
            else:
                for item in pending_items[:10]:
                    if isinstance(item, dict):
                        self._send_message(
                            self.owner_chat_id,
                            self._review_text(item),
                            self._review_keyboard(item),
                        )
                if len(pending_items) > 10:
                    self._send_owner(f"توجد {len(pending_items) - 10} مراجعة أخرى. أرسل /pending لاحقًا.")
        elif command == "/pause":
            self.state["paused"] = True
            self._save("JobFinder paused by owner")
            self._send_owner("⏸️ أوقفت معالجة الإعلانات الجديدة. ستظل أوامر البوت والمراجعات متاحة.")
        elif command == "/resume":
            self.state["paused"] = False
            self._save("JobFinder resumed by owner")
            self._send_owner("▶️ استؤنفت معالجة الإعلانات الجديدة.")
        elif command == "/toggle":
            paused = not bool(self.state.get("paused"))
            self.state["paused"] = paused
            self._save("JobFinder toggled by owner")
            if paused:
                self._send_owner("⏸️ أوقفت معالجة الإعلانات الجديدة. ستظل أوامر البوت والمراجعات متاحة.")
            else:
                self._send_owner("▶️ استؤنفت معالجة الإعلانات الجديدة.")

    def _handle_callback(self, query: dict[str, Any]) -> None:
        query_id = str(query.get("id") or "")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        sender = query.get("from") or {}
        if not self._authorized(chat.get("id"), sender.get("id")):
            print("Telegram callback rejected: owner authorization did not match")
            if query_id:
                self._answer_callback(query_id, "غير مصرح لك باستخدام هذا البوت.", True)
            return

        data = str(query.get("data") or "")
        pieces = data.split(":")
        if len(pieces) != 3 or pieces[0] != "jf":
            self._answer_callback(query_id, "زر غير صالح.", True)
            return
        _, action, pending_id = pieces

        # Test callbacks are deliberately no-op: they can never access real reviews
        # or invoke the email sender, even when runner is in Telegram-only mode.
        if action == "test" and pending_id in {"accept", "reject"}:
            self._answer_callback(query_id, "زر الاختبار يعمل.")
            if pending_id == "accept":
                result_text = (
                    "✅ نجح اختبار زر القبول.\n"
                    "لم تتغير حالة أي وظيفة ولم يُرسل أي بريد إلكتروني."
                )
            else:
                result_text = (
                    "✅ نجح اختبار زر الرفض.\n"
                    "لم تُرفض أي وظيفة حقيقية ولم يُرسل أي بريد إلكتروني."
                )
            self._edit_message(query, result_text)
            print(f"Telegram test callback completed: {pending_id}")
            return

        if not self.allow_review_actions:
            self._answer_callback(
                query_id,
                "وضع الاختبار فقط: لم يتم تنفيذ قرار الوظيفة.",
                True,
            )
            return

        pending = self.state.setdefault("pending_reviews", {})
        item = pending.get(pending_id)
        if not isinstance(item, dict):
            self._answer_callback(query_id, "تمت معالجة هذه الوظيفة أو لم تعد متاحة.", True)
            return

        if action == "reject":
            # Migrate old daily counters before adding the current rejection to history.
            stats = self._today_stats()
            self._resolve_review(pending_id, "rejected")
            stats["manual_rejected"] = int(stats.get("manual_rejected", 0)) + 1
            self._save("Manual review rejection counted")
            self._answer_callback(query_id, "تم رفض الوظيفة.")
            self._edit_message(
                query,
                "❌ تم رفض الوظيفة\n\n"
                + safe_text((item.get("record") or {}).get("application_job_title"), 200),
            )
            return

        if action == "manual":
            self._resolve_review(pending_id, "accepted_manual")
            self._answer_callback(query_id, "تم القبول للمراجعة اليدوية.")
            self._edit_message(
                query,
                "✅ تم قبول الوظيفة للمراجعة اليدوية.\n"
                "لم يتم إرسال بريد تلقائي لأن بيانات الاتصال أو طريقة التقديم غير مؤكدة.\n\n"
                + safe_text((item.get("record") or {}).get("application_job_title"), 200),
            )
            return

        if action != "approve" or not item.get("can_auto_send"):
            self._answer_callback(query_id, "لا يمكن إرسال هذه الوظيفة تلقائيًا.", True)
            return

        self._answer_callback(query_id, "جارٍ تنفيذ التقديم والتحقق من التكرار…")
        try:
            result = self.processor.send_approved_review(item)
        except Exception as exc:
            # Keep the review pending and its button active for a deliberate retry.
            self._send_owner(
                "⚠️ تعذّر إرسال الطلب بعد موافقتك.\n"
                f"الوظيفة: {safe_text((item.get('record') or {}).get('application_job_title'), 200)}\n"
                f"نوع الخطأ: {type(exc).__name__}\n"
                "بقيت الوظيفة معلقة؛ يمكنك إعادة المحاولة من زر الرسالة."
            )
            self._save("Approved application failed; review retained")
            return

        status = str(result.get("status") or "")
        title = safe_text((item.get("record") or {}).get("application_job_title"), 200)
        if status == "sent":
            self._resolve_review(pending_id, "sent")
            stats = self._today_stats()
            stats["sent"] = int(stats.get("sent", 0)) + 1
            self._answer_callback(query_id, "تم إرسال الطلب.")
            self._edit_message(
                query,
                "✅ تمت الموافقة وإرسال طلب التوظيف.\n"
                "خادم البريد قبل الرسالة؛ هذا لا يضمن وصولها إلى صندوق الوارد.\n\n"
                + title,
            )
        elif status == "duplicate":
            self._resolve_review(pending_id, "duplicate_skipped")
            self._answer_callback(query_id, "لم يُرسل طلب مكرر.")
            self._edit_message(
                query,
                "ℹ️ لم يُرسل طلب جديد لأن النظام وجد تقديمًا سابقًا للوظيفة نفسها.\n\n"
                + title,
            )
        else:
            self._answer_callback(query_id, "تعذر تنفيذ الموافقة. بقيت الوظيفة معلقة.", True)
        self._save("Review approval result processed")

    def _resolve_review(self, pending_id: str, status: str) -> None:
        pending = self.state.setdefault("pending_reviews", {})
        item = pending.get(pending_id)
        if status == "rejected" and isinstance(item, dict):
            record = item.get("record") or {}
            post = item.get("post") or {}
            emails = record.get("emails") or []
            rejected_record = {
                "status": "rejected",
                "at": iso_now(),
                "source": post.get("source"),
                "message_id": post.get("message_id"),
                "job_title": record.get("job_title"),
                "application_job_title": record.get("application_job_title") or record.get("job_title"),
                "company": record.get("company", ""),
                "location": record.get("location", ""),
                "email": str(emails[0]).strip() if emails else "",
                "emails": emails,
                "identity_key": record.get("identity_key", ""),
            }
            rejected_history = self.state.setdefault("rejected_job_history", [])
            if not isinstance(rejected_history, list):
                rejected_history = []
                self.state["rejected_job_history"] = rejected_history
            rejected_history.append(rejected_record)
            del rejected_history[:-500]

        pending.pop(pending_id, None)
        history = self.state.setdefault("review_history", {})
        history[pending_id] = {"status": status, "at": iso_now()}
        if len(history) > 500:
            for old_id in list(history.keys())[:-400]:
                history.pop(old_id, None)
        self._save(f"Review resolved: {status}")


def re_safe_channel(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char == "_" for char in value)
