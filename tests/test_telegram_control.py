import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GROQ_API_KEY", "validation-dummy-key")
os.environ.setdefault("GMAIL_APP_PASSWORD", "validation-dummy-password")
os.environ.setdefault("GMAIL_ADDRESS", "validation@example.org")
os.environ.setdefault("JOBFINDER_CANDIDATE_PROFILE", "validation profile")
os.environ.setdefault("JOBFINDER_CANDIDATE_NAME", "Validation User")

os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_BOT_CHAT_ID"] = "12345"

from telegram_control import TelegramJobBot, event_key
import auto_jobfinder_cloud as processor


class FakeProcessor:
    def __init__(self, status="sent"):
        self.status = status
        self.calls = []

    def send_approved_review(self, item):
        self.calls.append(item)
        return {"status": self.status}


def sample_event(decision="REVIEW"):
    record = {
        "source": "rcrdz1",
        "message_id": 77,
        "channel": "rcrdz1",
        "job_title": "Technicien de Maintenance",
        "application_job_title": "Technicien de Maintenance",
        "company": "Test Company",
        "location": "Oran",
        "decision": decision,
        "score": 75,
        "reason": "Test reason",
        "emails": ["jobs@example.org"],
        "cv": "/tmp/cv_ats_fr.pdf",
        "location_status": "allowed_local",
        "contact_scope_matched": True,
        "sent": False,
        "job_duplicate": False,
    }
    return {
        "post": {
            "source": "rcrdz1",
            "message_id": 77,
            "text": "Technicien de Maintenance — Oran — jobs@example.org",
        },
        "record": record,
        "job": {"job_title": "Technicien de Maintenance", "company": "Test Company"},
        "advertisement": "Technicien de Maintenance — Oran — jobs@example.org",
    }


class TelegramControlTests(unittest.TestCase):
    def build_bot(self, processor=None, allow_review_actions=True):
        bot = TelegramJobBot(
            {}, processor or FakeProcessor(), lambda _reason: None,
            allow_review_actions=allow_review_actions,
        )
        sent_messages = []
        bot._send_message = lambda chat, text, keyboard=None: (
            sent_messages.append((str(chat), text, keyboard)) or {"message_id": 42}
        )
        bot._answer_callback = lambda *args, **kwargs: None
        bot._edit_message = lambda *args, **kwargs: None
        return bot, sent_messages

    def test_review_is_queued_and_shows_controls(self):
        bot, sent = self.build_bot()
        bot.on_job_event(sample_event())
        self.assertEqual(len(bot.state["pending_reviews"]), 1)
        self.assertEqual(len(sent), 1)
        keyboard = sent[0][2]["inline_keyboard"][0]
        self.assertIn("jf:approve:", keyboard[0]["callback_data"])
        self.assertIn("jf:reject:", keyboard[1]["callback_data"])

    def test_reject_callback_resolves_review(self):
        bot, _ = self.build_bot()
        bot.on_job_event(sample_event())
        pending_id = next(iter(bot.state["pending_reviews"]))
        bot._handle_callback({
            "id": "callback-1",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 42},
            "data": f"jf:reject:{pending_id}",
        })
        self.assertNotIn(pending_id, bot.state["pending_reviews"])
        self.assertEqual(bot.state["review_history"][pending_id]["status"], "rejected")

    def test_unauthorized_user_cannot_approve(self):
        processor = FakeProcessor()
        bot, _ = self.build_bot(processor)
        bot.on_job_event(sample_event())
        pending_id = next(iter(bot.state["pending_reviews"]))
        bot._handle_callback({
            "id": "callback-2",
            "from": {"id": 99999},
            "message": {"chat": {"id": 12345}, "message_id": 42},
            "data": f"jf:approve:{pending_id}",
        })
        self.assertEqual(processor.calls, [])
        self.assertIn(pending_id, bot.state["pending_reviews"])

    def test_approved_application_is_processed_once_and_resolved(self):
        processor = FakeProcessor("sent")
        bot, _ = self.build_bot(processor)
        bot.on_job_event(sample_event())
        pending_id = next(iter(bot.state["pending_reviews"]))
        bot._handle_callback({
            "id": "callback-3",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 42},
            "data": f"jf:approve:{pending_id}",
        })
        self.assertEqual(len(processor.calls), 1)
        self.assertNotIn(pending_id, bot.state["pending_reviews"])
        self.assertEqual(bot.state["review_history"][pending_id]["status"], "sent")

    def test_event_key_is_stable(self):
        record = sample_event()["record"]
        self.assertEqual(event_key(record), event_key(dict(record)))

    def test_webhook_test_button_edits_message_without_real_job_action(self):
        processor = FakeProcessor()
        bot, _ = self.build_bot(processor)
        edits = []
        bot._edit_message = lambda query, text: edits.append((query, text))
        bot.handle_webhook_event({
            "update_type": "callback",
            "action": "test",
            "pending_id": "accept",
            "chat_id": "12345",
            "user_id": "12345",
            "message_id": "42",
        })
        self.assertEqual(processor.calls, [])
        self.assertTrue(any("نجح اختبار زر القبول" in text for _, text in edits))

    def test_webhook_status_command_replies_to_owner(self):
        bot, sent = self.build_bot()
        bot.handle_webhook_event({
            "update_type": "command",
            "command": "/status",
            "chat_id": "12345",
            "user_id": "12345",
        })
        self.assertTrue(any("حالة JobFinder" in text for _, text, _ in sent))

    def test_webhook_event_rejects_unauthorized_user(self):
        bot, sent = self.build_bot()
        bot.handle_webhook_event({
            "update_type": "command",
            "command": "/status",
            "chat_id": "12345",
            "user_id": "99999",
        })
        self.assertEqual(sent, [])

    def test_empty_callback_id_is_not_answered_twice(self):
        bot, _ = self.build_bot()
        with patch.object(bot, "_api") as api:
            TelegramJobBot._answer_callback(bot, "", "already acknowledged")
        api.assert_not_called()

    def test_test_accept_button_never_calls_email_sender(self):
        processor = FakeProcessor()
        bot, _ = self.build_bot(processor, allow_review_actions=False)
        bot._handle_callback({
            "id": "test-callback-accept",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 42},
            "data": "jf:test:accept",
        })
        self.assertEqual(processor.calls, [])
        self.assertEqual(bot.state["pending_reviews"], {})

    def test_test_reject_button_never_changes_real_review_state(self):
        processor = FakeProcessor()
        bot, _ = self.build_bot(processor, allow_review_actions=False)
        bot._handle_callback({
            "id": "test-callback-reject",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 43},
            "data": "jf:test:reject",
        })
        self.assertEqual(processor.calls, [])
        self.assertEqual(bot.state["pending_reviews"], {})
        self.assertEqual(bot.state["review_history"], {})

    def test_real_approval_is_blocked_in_test_only_mode(self):
        processor = FakeProcessor()
        bot, _ = self.build_bot(processor, allow_review_actions=False)
        bot.state["pending_reviews"]["real-review"] = {
            "id": "real-review",
            "can_auto_send": True,
            "record": {"application_job_title": "Test"},
        }
        bot._handle_callback({
            "id": "test-callback-real",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 44},
            "data": "jf:approve:real-review",
        })
        self.assertEqual(processor.calls, [])
        self.assertIn("real-review", bot.state["pending_reviews"])

    def sample_review_item(self, can_auto_send=True):
        event = sample_event()
        return {
            "id": "review123",
            "can_auto_send": can_auto_send,
            "record": event["record"],
            "job": event["job"],
            "post": event["post"],
            "advertisement": event["advertisement"],
        }

    def test_processor_blocks_unsafe_review_before_any_send(self):
        with self.assertRaisesRegex(RuntimeError, "not safe"):
            processor.send_approved_review(self.sample_review_item(False))

    def test_processor_skips_already_sent_duplicate(self):
        item = self.sample_review_item(True)
        with (
            patch.object(processor.os.path, "isfile", return_value=True),
            patch.object(processor, "already_sent_same_job", return_value=(True, {})),
            patch.object(processor, "already_sent", return_value=False),
            patch.object(processor, "send_application") as send,
            patch.object(processor, "save_sent"),
            patch.object(processor, "save_result"),
        ):
            result = processor.send_approved_review(item)
        self.assertEqual(result["status"], "duplicate")
        send.assert_not_called()

    def test_processor_sends_only_after_validated_approval(self):
        item = self.sample_review_item(True)
        with (
            patch.object(processor.os.path, "isfile", return_value=True),
            patch.object(processor, "already_sent_same_job", return_value=(False, None)),
            patch.object(processor, "already_sent", return_value=False),
            patch.object(processor, "send_application") as send,
            patch.object(processor, "save_sent") as save_sent,
            patch.object(processor, "save_result"),
        ):
            result = processor.send_approved_review(item)
        self.assertEqual(result["status"], "sent")
        send.assert_called_once()
        save_sent.assert_called_once()



if __name__ == "__main__":
    unittest.main()
