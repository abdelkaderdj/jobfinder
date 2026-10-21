import unittest

from tools import export_historical_job_audit as audit


class HistoricalJobAuditTests(unittest.TestCase):
    def test_algeria_window_covers_exact_local_calendar_days(self):
        self.assertEqual(
            audit.WINDOW_START_UTC.isoformat(),
            "2026-10-19T23:00:00+00:00",
        )
        self.assertEqual(
            audit.WINDOW_END_UTC.isoformat(),
            "2026-10-21T23:00:00+00:00",
        )

    def test_parses_actual_decisions_and_scores_from_monitor_log(self):
        run = {
            "id": 77,
            "run_number": 3300,
            "created_at_utc": "2026-10-21T12:00:00Z",
            "conclusion": "success",
            "url": "https://github.com/example/run/77",
        }
        log = "\n".join([
            "[rcrdz1] new post id=22716; analyzing",
            "Analyzing new job post...",
            "JOB RESULT: identifying details redacted from public workflow logs",
            "DECISION: REVIEW",
            "ORIGINAL AI DECISION: REJECT",
            "SCORE: 30",
            "LOCATION ELIGIBILITY: allowed_local",
            "SENT: False",
            "JOB DUPLICATE: False",
        ])

        history, failures = audit.correlate_run_logs([run], [(77, [log], "")])

        self.assertEqual(failures, [])
        attempt = history["rcrdz1/22716"][0]
        self.assertEqual(len(attempt["job_decisions"]), 1)
        decision = attempt["job_decisions"][0]
        self.assertEqual(decision["decision"], "REVIEW")
        self.assertEqual(decision["original_ai_decision"], "REJECT")
        self.assertEqual(decision["score"], 30)
        self.assertEqual(decision["location_eligibility"], "allowed_local")
        self.assertFalse(decision["sent"])
        self.assertFalse(decision["duplicate"])

    def test_uses_latest_attempt_with_a_decision_not_a_later_failed_attempt(self):
        first = {
            "run_id": 1,
            "run_number": 10,
            "run_created_at_utc": "2026-10-20T10:00:00Z",
            "job_decisions": [{"sequence": 1, "decision": "REJECT"}],
            "markers": [],
        }
        later_failed = {
            "run_id": 2,
            "run_number": 11,
            "run_created_at_utc": "2026-10-20T10:10:00Z",
            "job_decisions": [],
            "markers": ["خطأ معالجة منشور"],
        }
        picked = audit.final_attempt_for_post([first, later_failed])
        self.assertIs(picked, first)

    def test_no_matching_log_is_not_mislabeled_as_a_bot_rejection(self):
        posts = [{
            "source": "rcrdz1",
            "message_id": 22716,
            "published_at_utc": "2026-10-20T10:00:00Z",
            "published_at_algeria": "2026-10-20T11:00:00+01:00",
            "url": "https://t.me/rcrdz1/22716",
            "text": "Opérateur de maintenance.",
        }]
        summary, enriched = audit.summarize(
            posts,
            history={},
            channel_counts={channel: 0 for channel in audit.CHANNELS},
            channel_errors={},
            channels_at_limit=[],
            log_failures=[],
            monitor_runs_examined=100,
        )
        self.assertEqual(summary["posts_without_matching_bot_trace"], 1)
        self.assertEqual(summary["logged_job_decision_counts_latest_attempt_per_post"]["REJECT"], 0)
        self.assertEqual(enriched[0]["bot_history_status"], "no_matching_bot_log_found")
        self.assertEqual(enriched[0]["latest_logged_bot_decisions"], [])


if __name__ == "__main__":
    unittest.main()
