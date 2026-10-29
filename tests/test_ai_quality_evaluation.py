from __future__ import annotations

import unittest
from pathlib import Path

from tools.evaluate_ai_quality import (
    evaluate_case,
    load_cases,
    source_quote_is_grounded,
    summarize,
)


class AIQualityEvaluationTests(unittest.TestCase):
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
