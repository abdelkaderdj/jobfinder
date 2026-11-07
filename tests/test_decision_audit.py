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
                "cv_match_score": 55,
                "cv_matched_skill_groups": ["industrial_maintenance", "pumps_fluids_hydraulics"],
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
        self.assertEqual(result["cv_match_score"], 55)
        self.assertEqual(
            result["cv_matched_skill_groups"],
            ["industrial_maintenance", "pumps_fluids_hydraulics"],
        )
        self.assertEqual(result["provider"], "gemini")
        for forbidden in ("text", "advertisement", "emails", "cv", "candidate_profile"):
            self.assertNotIn(forbidden, result)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("private@example.org", serialized)
        self.assertNotIn("MUST NOT BE COPIED", serialized)
        self.assertNotIn("PRIVATE FULL AD TEXT", serialized)
        self.assertNotIn("/runner/cvs/private.pdf", serialized)

    def test_decision_trace_keeps_only_safe_gate_codes_and_records_overrides(self):
        event = {
            "post": {"source": "rcrdz1", "message_id": 123},
            "record": {
                "job_title": "Technicien maintenance",
                "decision": "REJECT",
                "original_ai_decision": "APPLY",
                "decision_trace": [
                    {
                        "gate": "candidate_fit",
                        "before": "APPLY",
                        "after": "REJECT",
                        "outcome": "overrode",
                        "reason": "PRIVATE ADVERTISEMENT TEXT MUST NOT BE COPIED",
                    },
                    {
                        "gate": "contact_scope",
                        "before": "APPLY",
                        "after": "REVIEW",
                        "outcome": "blocked_no_email",
                        "email": "private@example.org",
                    },
                    {
                        "gate": "unrecognized_gate",
                        "before": "APPLY",
                        "after": "APPLY",
                        "outcome": "ignore",
                    },
                ],
                "delivery_status": "blocked_missing_cv",
                "manual_apply": False,
                "ai_verification_status": "forced_review",
            },
        }
        result = make_decision_audit_record(event)
        self.assertEqual(
            result["decision_trace"],
            [
                {"gate": "candidate_fit", "before": "APPLY", "after": "REJECT", "outcome": "overrode", "changed": True},
                {"gate": "contact_scope", "before": "APPLY", "after": "REVIEW", "outcome": "blocked_no_email", "changed": True},
            ],
        )
        self.assertEqual(result["decision_override_stages"], ["candidate_fit", "contact_scope"])
        self.assertEqual(result["delivery_status"], "blocked_missing_cv")
        self.assertEqual(result["ai_verification_status"], "forced_review")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("PRIVATE ADVERTISEMENT TEXT", serialized)
        self.assertNotIn("private@example.org", serialized)
        self.assertNotIn("unrecognized_gate", serialized)

    def test_ai_invocation_trace_is_sanitized_and_rejects_prompt_fields(self):
        event = {
            "post": {"source": "rcrdz1", "message_id": 501},
            "record": {
                "job_title": "Technicien de maintenance",
                "decision": "REVIEW",
                "ai_call_trace": [
                    {
                        "request": 1,
                        "stage": "offer_analysis",
                        "provider": "gemini",
                        "model": "gemini-3.8-flash",
                        "attempt": 1,
                        "json_valid": False,
                        "elapsed_ms": 240,
                        "outcome": "invalid_json",
                        "prompt": "PRIVATE ADVERTISEMENT AND CANDIDATE PROFILE",
                        "response": "PRIVATE MODEL RESPONSE",
                    },
                    {
                        "request": 1,
                        "stage": "offer_analysis",
                        "provider": "groq",
                        "model": "openai/gpt-oss-20b",
                        "attempt": 1,
                        "json_valid": True,
                        "elapsed_ms": 410,
                        "outcome": "success",
                    },
                    {
                        "request": 2,
                        "stage": "unknown_stage",
                        "provider": "groq",
                        "model": "openai/gpt-oss-20b",
                        "attempt": 1,
                        "json_valid": True,
                        "elapsed_ms": 10,
                        "outcome": "success",
                    },
                    {
                        "request": 2,
                        "stage": "offer_analysis",
                        "provider": "gemini",
                        "model": "gemini-3.8-flash PRIVATE TEXT",
                        "attempt": 1,
                        "json_valid": True,
                        "elapsed_ms": 10,
                        "outcome": "success",
                    },
                ],
            },
        }
        result = make_decision_audit_record(event)
        self.assertEqual(len(result["ai_call_trace"]), 2)
        self.assertEqual(result["ai_call_trace"][0]["outcome"], "invalid_json")
        self.assertFalse(result["ai_call_trace"][0]["json_valid"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("PRIVATE ADVERTISEMENT", serialized)
        self.assertNotIn("PRIVATE MODEL RESPONSE", serialized)
        self.assertNotIn("PRIVATE TEXT", serialized)
        self.assertNotIn("prompt", serialized)
        self.assertNotIn("response", serialized)

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
                "cv_match_score": 55,
                "cv_matched_skill_groups": ["industrial_maintenance"],
                "decision_trace": [
                    {
                        "gate": "candidate_fit",
                        "before": "REJECT",
                        "after": "REVIEW",
                        "outcome": "overrode",
                    }
                ],
                "delivery_status": "blocked_missing_cv",
                "ai_call_trace": [
                    {
                        "request": 1,
                        "stage": "offer_analysis",
                        "provider": "gemini",
                        "model": "gemini-3.8-flash",
                        "attempt": 1,
                        "json_valid": False,
                        "elapsed_ms": 120,
                        "outcome": "invalid_json",
                    },
                    {
                        "request": 1,
                        "stage": "offer_analysis",
                        "provider": "groq",
                        "model": "openai/gpt-oss-20b",
                        "attempt": 1,
                        "json_valid": True,
                        "elapsed_ms": 320,
                        "outcome": "success",
                    },
                ],
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
        second["delivery_status"] = "sent"

        report, summary = make_report([first, second], 2, now=now)

        self.assertEqual(summary["decision_attempts"], 2)
        self.assertEqual(summary["unique_post_positions"], 1)
        self.assertEqual(summary["final_bot_decisions"], {"APPLY": 1})
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["cv_match_scores_available"], 1)
        self.assertEqual(summary["cv_match_score_bands"]["50-69"], 1)
        self.assertEqual(summary["decision_changes_by_gate"], {"candidate_fit": 1})
        self.assertEqual(summary["delivery_outcomes"], {"sent": 1})
        # Both workflow runs made their own provider calls. These must remain
        # visible in aggregate AI usage even though the decision table keeps
        # only the latest decision for this post/position.
        self.assertEqual(summary["ai_invocation_attempts"], 4)
        self.assertEqual(summary["ai_model_usage"], {
            "gemini/gemini-3.8-flash": 2,
            "groq/openai/gpt-oss-20b": 2,
        })
        self.assertEqual(summary["ai_successful_invocations"], 2)
        self.assertEqual(summary["ai_invalid_json_attempts"], 2)
        self.assertIn("استدعاءات AI الفعلية", report)
        self.assertIn("gemini-3.8-flash", report)
        self.assertIn("openai/gpt-oss-20b", report)
        self.assertIn("مراحل تغيير القرار", report)
        self.assertIn("ملاءمة المنصب", report)
        self.assertIn("تم الإرسال", report)
        self.assertIn("مؤشر CV", report)
        self.assertIn("55", report)
        self.assertIn("https://t.me/rcrdz1/123", report)
        self.assertIn("قرار البوت النهائي", report)
        self.assertIn("Relevant technical role; verify experience.", report)

    def test_ai_usage_counts_retries_once_and_deduplicates_roles_within_a_run(self):
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        event = {
            "post": {"source": "rcrdz1", "message_id": 321},
            "record": {
                "source": "rcrdz1",
                "message_id": 321,
                "job_title": "Technicien de maintenance",
                "application_job_title": "Technicien de maintenance",
                "company": "Industry Co",
                "original_ai_decision": "APPLY",
                "decision": "REVIEW",
                "ai_call_trace": [
                    {
                        "request": 1,
                        "stage": "offer_analysis",
                        "provider": "gemini",
                        "model": "gemini-3.8-flash",
                        "attempt": 1,
                        "json_valid": False,
                        "elapsed_ms": 100,
                        "outcome": "invalid_json",
                    },
                    {
                        "request": 1,
                        "stage": "offer_analysis",
                        "provider": "groq",
                        "model": "openai/gpt-oss-20b",
                        "attempt": 1,
                        "json_valid": True,
                        "elapsed_ms": 200,
                        "outcome": "success",
                    },
                ],
            },
        }
        first_run_role_one = make_decision_audit_record(
            event, run_id="run1", observed_at=now - timedelta(minutes=30)
        )
        other_role_event = {
            "post": event["post"],
            "record": {
                **event["record"],
                "job_title": "Dessinateur mécanique",
                "application_job_title": "Dessinateur mécanique",
            },
        }
        first_run_role_two = make_decision_audit_record(
            other_role_event, run_id="run1", observed_at=now - timedelta(minutes=20)
        )
        retry = make_decision_audit_record(
            event, run_id="run2", observed_at=now - timedelta(minutes=10)
        )

        _, summary = make_report(
            [first_run_role_one, first_run_role_two, retry], 2, now=now
        )

        self.assertEqual(summary["decision_attempts"], 3)
        self.assertEqual(summary["unique_post_positions"], 2)
        # Two provider attempts in run1 (shared by two extracted roles), plus
        # two fresh provider attempts in the later retry run.
        self.assertEqual(summary["ai_invocation_attempts"], 4)
        self.assertEqual(summary["ai_model_usage"], {
            "gemini/gemini-3.8-flash": 2,
            "groq/openai/gpt-oss-20b": 2,
        })

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
