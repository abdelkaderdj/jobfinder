from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


LOCAL_ZONE = ZoneInfo("Africa/Algiers")
MAX_OUTBOX_PER_RUN = 10


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

    def __init__(self, state: dict[str, Any], processor, persist_callback):
        self.state = state
        self.processor = processor
        self.persist_callback = persist_callback
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
                "send_failed": 0,
            }
            self.state["daily_stats"] = stats
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
        elif decision == "APPLY" and not record.get("job_duplicate"):
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

    def on_job_event(self, event: dict[str, Any]) -> None:
        record = event.get("record") or {}
        if not isinstance(record, dict):
            return
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
            return
        for update in updates[:100]:
            if not isinstance(update, dict) or "update_id" not in update:
                continue
            update_id = int(update["update_id"])
            try:
                if isinstance(update.get("message"), dict):
                    self._handle_message(update["message"])
                elif isinstance(update.get("callback_query"), dict):
                    self._handle_callback(update["callback_query"])
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
        if not text.startswith("/"):
            return
        command = text.split()[0].split("@")[0].lower()

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

        if command in {"/start", "/help"}:
            self._send_owner(
                "مرحبًا بك في JobFinder.\n\n"
                "/status — حالة النظام\n"
                "/today — ملخص اليوم\n"
                "/pending — الوظائف التي تنتظر قرارك\n"
                "/pause — إيقاف معالجة الإعلانات الجديدة\n"
                "/resume — استئناف المعالجة"
            )
        elif command == "/status":
            pending_count = len(self.state.get("pending_reviews") or {})
            paused = bool(self.state.get("paused"))
            last_run = safe_text(self.state.get("last_run_at"), 100) if self.state.get("last_run_at") else "غير متوفر"
            self._send_owner(
                "📡 حالة JobFinder\n\n"
                f"الحالة: {'متوقف مؤقتًا' if paused else 'نشط'}\n"
                f"مراجعات معلقة: {pending_count}\n"
                f"آخر تشغيل: {last_run}"
            )
        elif command == "/today":
            stats = self._today_stats()
            self._save("Daily stats checked")
            self._send_owner(
                f"📊 ملخص JobFinder — {stats.get('date')}\n\n"
                f"الإعلانات المحللة: {stats.get('analyzed', 0)}\n"
                f"الطلبات المرسلة: {stats.get('sent', 0)}\n"
                f"تحتاج إلى مراجعة: {stats.get('review', 0)}\n"
                f"المرفوضة: {stats.get('rejected', 0)}\n"
                f"محاولات الإرسال التي لم تتأكد: {stats.get('send_failed', 0)}"
            )
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

    def _handle_callback(self, query: dict[str, Any]) -> None:
        query_id = str(query.get("id") or "")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        sender = query.get("from") or {}
        if not self._authorized(chat.get("id"), sender.get("id")):
            if query_id:
                self._answer_callback(query_id, "غير مصرح لك باستخدام هذا البوت.", True)
            return

        data = str(query.get("data") or "")
        pieces = data.split(":")
        if len(pieces) != 3 or pieces[0] != "jf":
            self._answer_callback(query_id, "زر غير صالح.", True)
            return
        _, action, pending_id = pieces
        pending = self.state.setdefault("pending_reviews", {})
        item = pending.get(pending_id)
        if not isinstance(item, dict):
            self._answer_callback(query_id, "تمت معالجة هذه الوظيفة أو لم تعد متاحة.", True)
            return

        if action == "reject":
            self._resolve_review(pending_id, "rejected")
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
        self.state.setdefault("pending_reviews", {}).pop(pending_id, None)
        history = self.state.setdefault("review_history", {})
        history[pending_id] = {"status": status, "at": iso_now()}
        if len(history) > 500:
            for old_id in list(history.keys())[:-400]:
                history.pop(old_id, None)
        self._save(f"Review resolved: {status}")


def re_safe_channel(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char == "_" for char in value)
