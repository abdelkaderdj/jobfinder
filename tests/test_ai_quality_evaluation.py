from __future__ import annotations

import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.evaluate_ai_quality import (
    emit_live_progress,
    evaluate_case,
    load_cases,
    source_quote_is_grounded,
    summarize,
)


class AIQualityEvaluationTests(unittest.TestCase):
    def test_live_progress_is_opt_in_and_contains_only_safe_metadata(self):
        stream = io.StringIO()
        with patch.dict(os.environ, {"JOBFINDER_EVAL_LIVE_PROGRESS": "1"}):
            with patch("tools.evaluate_ai_quality.sys.__stdout__", stream):
                emit_live_progress(
                    "case_finished",
                    index=2,
                    total=9,
                    case_id="case/02",
                    status="evaluated",
                    passed=False,
                )
        rendered = stream.getvalue()
        self.assertIn('"event":"case_finished"', rendered)
        self.assertIn('"case_id":"case02"', rendered)
        self.assertIn('"index":2', rendered)
        self.assertIn('"total":9', rendered)
        self.assertIn('"passed":false', rendered)
        self.assertNotIn("source_text", rendered)
        self.assertNotIn("candidate", rendered)

    def test_starter_dataset_has_labelled_cases_and_historical_regressions(self):
        path = Path(__file__).resolve().parents[1] / "data" / "ai_quality_eval_cases.jsonl"
        cases = load_cases(path)
        self.assertGreaterEqual(len(cases), 8)
        self.assertTrue(any(
            item.get("provenance") == "known_historical_regression_rcrdz1_23066"
            for item in cases
        ))

    def test_grounding_check_requires_a_source_quote(self):
        source = "Recrutement : Technicien de maintenance à Oran."
        self.assertTrue(source_quote_is_grounded("Technicien de maintenance", source))
        self.assertFalse(source_quote_is_grounded("Maintenance Technician", source))
        self.assertFalse(source_quote_is_grounded("", source))

    def test_metrics_separate_missing_titles_from_unexpected_titles(self):
        case = {
            "id": "title-metrics",
            "source_text": "Maintenance technician and production operator openings. Apply by email.",
            "expected_post_type": "actual_vacancy",
            "expected_titles": [
                {"aliases": ["Maintenance technician"]},
                {"aliases": ["Production operator"]},
            ],
            "expected_decisions": [],
        }
        result = {
            "post_type": "actual_vacancy",
            "post_type_evidence": ["Apply by email"],
            "jobs": [
                {
                    "job_title": "Maintenance technician",
                    "title_evidence": "Maintenance technician",
                    "decision": "REVIEW",
                },
                {
                    "job_title": "Storekeeper",
                    "title_evidence": "Storekeeper",
                    "decision": "REJECT",
                },
            ],
        }
        evaluated = evaluate_case(case, result)
        self.assertEqual(len(evaluated["matched_titles"]), 1)
        self.assertEqual(evaluated["missing_titles"], ["Production operator"])
        self.assertEqual(evaluated["unexpected_titles"], ["Storekeeper"])
        self.assertEqual(evaluated["title_precision"], 0.5)
        self.assertEqual(evaluated["title_recall"], 0.5)
        self.assertFalse(evaluated["passed"])

        summary = summarize([evaluated])
        self.assertEqual(summary["title_precision"], 0.5)
        self.assertEqual(summary["title_recall"], 0.5)

    def test_evaluator_flags_ungrounded_evidence_and_nonvacancy_with_jobs(self):
        case = {
            "id": "bad-evidence",
            "source_text": "No current vacancy; wait for official announcement.",
            "expected_post_type": "unconfirmed_rumour",
            "expected_titles": [],
            "expected_decisions": [],
        }
        result = {
            "post_type": "unconfirmed_rumour",
            "post_type_evidence": ["this is invented evidence"],
            "jobs": [{"job_title": "Technicien Maintenance", "title_evidence": "Maintenance"}],
        }
        evaluated = evaluate_case(case, result)
        self.assertFalse(evaluated["passed"])
        self.assertTrue(evaluated["non_vacancy_with_jobs"])
        self.assertGreaterEqual(len(evaluated["evidence_errors"]), 2)

    def test_expected_decision_can_use_final_gate_result(self):
        case = {
            "id": "decision-metrics",
            "source_text": "Technicien de production. Apply by email.",
            "expected_post_type": "actual_vacancy",
            "expected_titles": [{"aliases": ["Technicien de production"]}],
            "expected_decisions": [
                {
                    "title_aliases": ["Technicien de production"],
                    "expected": "REVIEW",
                }
            ],
        }
        result = {
            "post_type": "actual_vacancy",
            "post_type_evidence": ["Apply by email"],
            "jobs": [
                {
                    "job_title": "Technicien de production",
                    "title_evidence": "Technicien de production",
                    "decision": "APPLY",
                }
            ],
        }
        evaluated = evaluate_case(
            case,
            result,
            decision_transform=lambda _job, _source: ("REVIEW", "Production role requires manual review."),
        )
        self.assertTrue(evaluated["decision_checks"][0]["passed"])
        self.assertEqual(evaluated["decision_checks"][0]["raw"], "APPLY")
        self.assertEqual(evaluated["decision_checks"][0]["after_candidate_gate"], "REVIEW")
        self.assertTrue(evaluated["passed"])


if __name__ == "__main__":
    unittest.main()
