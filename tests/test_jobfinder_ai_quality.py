import unittest

from jobfinder_ai_quality import (
    apply_verification_result,
    enforce_verification_gate,
    normalize_reasoning_level,
)


class ReasoningLevelTests(unittest.TestCase):
    def test_accepts_supported_levels(self):
        self.assertEqual(normalize_reasoning_level("HIGH"), "high")
        self.assertEqual(normalize_reasoning_level("medium"), "medium")
        self.assertEqual(normalize_reasoning_level("low"), "low")

    def test_invalid_level_uses_safe_default(self):
        self.assertEqual(normalize_reasoning_level("max", default="high"), "high")
        self.assertEqual(normalize_reasoning_level("max"), "low")


class IndependentApplyReviewTests(unittest.TestCase):
    def setUp(self):
        self.jobs = [
            {
                "job_title": "Technicien de maintenance",
                "decision": "APPLY",
                "score": 85,
            },
            {
                "job_title": "Agent de production",
                "decision": "REVIEW",
                "score": 45,
            },
        ]
        self.grounded_quotes = {"Technicien de maintenance demandé."}

    def grounded(self, quote):
        return quote in self.grounded_quotes

    def test_only_grounded_matching_review_can_preserve_apply(self):
        result = apply_verification_result(
            self.jobs,
            {
                "reviews": [
                    {
                        "job_index": 0,
                        "job_title": "Technicien de maintenance",
                        "verdict": "safe_to_keep_apply",
                        "evidence_quotes": ["Technicien de maintenance demandé."],
                        "reason": "No mandatory conflict was found.",
                    }
                ]
            },
            self.grounded,
        )
        self.assertEqual(result[0]["decision"], "APPLY")
        self.assertEqual(result[0]["verification_gate"], "passed")
        self.assertEqual(result[1]["decision"], "REVIEW")

    def test_missing_review_fails_closed(self):
        result = apply_verification_result(self.jobs, None, self.grounded)
        self.assertEqual(result[0]["decision"], "APPLY")
        self.assertEqual(result[0]["verification_gate"], "forced_review")
        self.assertTrue(result[0]["verification_reason"])

    def test_unmatched_title_fails_closed(self):
        result = apply_verification_result(
            self.jobs,
            {
                "reviews": [
                    {
                        "job_index": 0,
                        "job_title": "Technicien supérieur",
                        "verdict": "safe_to_keep_apply",
                        "evidence_quotes": ["Technicien de maintenance demandé."],
                    }
                ]
            },
            self.grounded,
        )
        self.assertEqual(result[0]["verification_gate"], "forced_review")

    def test_un_grounded_evidence_fails_closed(self):
        result = apply_verification_result(
            self.jobs,
            {
                "reviews": [
                    {
                        "job_index": 0,
                        "job_title": "Technicien de maintenance",
                        "verdict": "safe_to_keep_apply",
                        "evidence_quotes": ["Cette phrase n'existe pas dans l'annonce."],
                    }
                ]
            },
            self.grounded,
        )
        self.assertEqual(result[0]["verification_gate"], "forced_review")


    def test_later_fit_override_cannot_reenable_apply_without_passed_gate(self):
        # Simulate a later deterministic rule that tries to promote REVIEW to APPLY.
        decision, reason = enforce_verification_gate(
            "APPLY",
            "forced_review",
            "Independent evidence check was inconclusive.",
        )
        self.assertEqual(decision, "REVIEW")
        self.assertIn("inconclusive", reason)

    def test_passed_gate_does_not_change_existing_decision(self):
        self.assertEqual(enforce_verification_gate("APPLY", "passed"), ("APPLY", ""))
        self.assertEqual(enforce_verification_gate("REVIEW", "passed"), ("REVIEW", ""))

    def test_invalid_decision_fails_closed(self):
        self.assertEqual(enforce_verification_gate("MAYBE", "passed"), ("REJECT", ""))

    def test_reviewer_cannot_promote_review_to_apply(self):
        result = apply_verification_result(
            self.jobs,
            {
                "reviews": [
                    {
                        "job_index": 0,
                        "job_title": "Technicien de maintenance",
                        "verdict": "safe_to_keep_apply",
                        "evidence_quotes": ["Technicien de maintenance demandé."],
                    },
                    {
                        "job_index": 1,
                        "job_title": "Agent de production",
                        "verdict": "safe_to_keep_apply",
                        "evidence_quotes": ["Technicien de maintenance demandé."],
                    },
                ]
            },
            self.grounded,
        )
        self.assertEqual(result[1]["decision"], "REVIEW")
        self.assertNotIn("verification_gate", result[1])


if __name__ == "__main__":
    unittest.main()
