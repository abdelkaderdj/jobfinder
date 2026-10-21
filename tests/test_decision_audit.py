import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from decision_audit import (
    append_encrypted_decision_audit,
    make_decision_audit_record,
)
from tools.export_decision_audit import make_report


class DecisionAuditTests(unittest.TestCase):
    def test_record_contains_offer_decision_but_excludes_contact_and_candidate_files(self):
        event = {
            "post": {
                "source": "rcrdz1",
                "message_id": 22716,
                "date": "2026-10-21T10:00:00+01:00",
                "text": "PRIVATE FULL AD TEXT MUST NOT BE COPIED",
            },
            "advertisement": "PRIVATE FULL AD TEXT MUST NOT BE COPIED",
            "record": {
                "source": "rcrdz1",
                "message_id": 22716,
                "date": "2026-10-21T10:00:00+01:00",
                "job_title": "Opérateur de Machine CNC",
                "application_job_title": "Opérateur de Machine CNC",
                "company": "Example Industry",
                "location": "Oran",
                "original_ai_decision": "REJECT",
                "decision": "REVIEW",
                "score": 30,
                "reason": "Mandatory experience could not be verified.",
                "emails": ["private@example.org"],
                "cv": "/runner/cvs/private.pdf",
                "candidate_profile": "MUST NOT BE COPIED",
                "location_status": "allowed_local",
                "contact_scope_matched": True,
                "sent": False,
            },
        }

        result = make_decision_audit_record(
            event,
            run_id="98765",
            run_attempt="2",
            provider_status={"provider": "gemini", "model": "gemini-flash"},
            observed_at=datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result["source_url"], "https://t.me/rcrdz1/22716")
        self.assertEqual(result["original_ai_decision"], "REJECT")
        self.assertEqual(result["final_decision"], "REVIEW")
        self.assertEqual(result["score"], 30)
        self.assertEqual(result["provider"], "gemini")
        for forbidden in ("text", "advertisement", "emails", "cv", "candidate_profile"):
            self.assertNotIn(forbidden, result)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("private@example.org", serialized)
        self.assertNotIn("MUST NOT BE COPIED", serialized)
        self.assertNotIn("PRIVATE FULL AD TEXT", serialized)
        self.assertNotIn("/runner/cvs/private.pdf", serialized)

    def test_sidecar_is_encrypted_and_appends_distinct_decisions(self):
        fernet = Fernet(Fernet.generate_key())
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        event1 = {
            "post": {"source": "rcrdz1", "message_id": 100, "date": now.isoformat()},
            "record": {
                "source": "rcrdz1",
                "message_id": 100,
                "job_title": "Technicien de maintenance",
                "application_job_title": "Technicien de maintenance",
                "decision": "REVIEW",
                "original_ai_decision": "REJECT",
                "score": 35,
                "reason": "Relevant technical role; experience needs confirmation.",
            },
        }
        event2 = {
            "post": {"source": "Jobs_dz7", "message_id": 200, "date": now.isoformat()},
            "record": {
                "source": "Jobs_dz7",
                "message_id": 200,
                "job_title": "Commercial",
                "application_job_title": "Commercial",
                "decision": "REJECT",
                "original_ai_decision": "REJECT",
                "score": 5,
                "reason": "Role unrelated to the candidate's technical background.",
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision_audit.enc"
            rec1 = make_decision_audit_record(event1, run_id="1", observed_at=now)
            rec2 = make_decision_audit_record(event2, run_id="2", observed_at=now)

            self.assertEqual(append_encrypted_decision_audit(path, rec1, fernet, now=now), 1)
            self.assertEqual(append_encrypted_decision_audit(path, rec2, fernet, now=now), 2)

            ciphertext = path.read_bytes()
            self.assertNotIn(b"Technicien de maintenance", ciphertext)
            self.assertNotIn(b"Relevant technical role", ciphertext)
            decoded = json.loads(fernet.decrypt(ciphertext).decode("utf-8"))
            self.assertEqual(len(decoded), 2)
            self.assertEqual(
                {item["final_decision"] for item in decoded},
                {"REVIEW", "REJECT"},
            )

    def test_expired_records_are_pruned_and_same_record_id_is_idempotent(self):
        fernet = Fernet(Fernet.generate_key())
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        old = {
            "record_id": "old",
            "observed_at_utc": (now - timedelta(days=15)).isoformat(),
            "final_decision": "REJECT",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision_audit.enc"
            path.write_bytes(fernet.encrypt(json.dumps([old]).encode("utf-8")))
            current = {
                "record_id": "current",
                "observed_at_utc": now.isoformat(),
                "final_decision": "REVIEW",
            }
            self.assertEqual(append_encrypted_decision_audit(path, current, fernet, now=now), 1)
            self.assertEqual(append_encrypted_decision_audit(path, current, fernet, now=now), 1)
            decoded = json.loads(fernet.decrypt(path.read_bytes()).decode("utf-8"))
            self.assertEqual([item["record_id"] for item in decoded], ["current"])


    def test_report_uses_latest_attempt_per_position_but_retains_attempt_count(self):
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        event = {
            "post": {"source": "rcrdz1", "message_id": 123, "date": "2026-10-21T10:00:00Z"},
            "record": {
                "source": "rcrdz1",
                "message_id": 123,
                "job_title": "Technicien de maintenance",
                "application_job_title": "Technicien de maintenance",
                "company": "Industry Co",
                "original_ai_decision": "REJECT",
                "decision": "REVIEW",
                "score": 35,
                "reason": "Relevant technical role; verify experience.",
            },
        }
        first = make_decision_audit_record(
            event, run_id="run1", observed_at=now - timedelta(hours=1)
        )
        second = make_decision_audit_record(
            event, run_id="run2", observed_at=now
        )
        second["final_decision"] = "APPLY"
        second["sent"] = True

        report, summary = make_report([first, second], 2, now=now)

        self.assertEqual(summary["decision_attempts"], 2)
        self.assertEqual(summary["unique_post_positions"], 1)
        self.assertEqual(summary["final_bot_decisions"], {"APPLY": 1})
        self.assertEqual(summary["sent"], 1)
        self.assertIn("https://t.me/rcrdz1/123", report)
        self.assertIn("قرار البوت النهائي", report)
        self.assertIn("Relevant technical role; verify experience.", report)

    def test_report_excludes_records_outside_the_selected_window(self):
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        record = {
            "record_id": "too-old",
            "observed_at_utc": (now - timedelta(days=4)).isoformat(),
            "source": "rcrdz1",
            "message_id": "1",
            "final_decision": "REJECT",
            "original_ai_decision": "REJECT",
            "application_job_title": "Old Role",
        }

        report, summary = make_report([record], 2, now=now)

        self.assertEqual(summary["decision_attempts"], 0)
        self.assertEqual(summary["unique_post_positions"], 0)
        self.assertNotIn("Old Role", report)


if __name__ == "__main__":
    unittest.main()
