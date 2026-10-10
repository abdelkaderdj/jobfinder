import os
import unittest

os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_BOT_CHAT_ID"] = "12345"

from telegram_control import TelegramJobBot, event_key


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
    def build_bot(self, processor=None):
        bot = TelegramJobBot({}, processor or FakeProcessor(), lambda _reason: None)
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


if __name__ == "__main__":
    unittest.main()
