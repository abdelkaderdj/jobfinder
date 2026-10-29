from __future__ import annotations

import math
import unittest
from pathlib import Path

from jobfinder_candidate_policy import (
    MAX_EXPERIENCE_YEARS_MANUAL_REVIEW,
    MAX_EXPERIENCE_YEARS_WAIVED,
    classify_experience_requirement,
    experience_policy_prompt_text,
)


class ExperienceRequirementPolicyTests(unittest.TestCase):
    def test_experience_thresholds_have_one_canonical_result(self):
        waived = MAX_EXPERIENCE_YEARS_WAIVED
        review_max = MAX_EXPERIENCE_YEARS_MANUAL_REVIEW

        for years in (0, 0.5, 1, 2, waived):
            with self.subTest(years=years):
                self.assertEqual(
                    classify_experience_requirement(years),
                    "waive_experience",
                )

        for years in (waived + 0.01, 3.5, review_max):
            with self.subTest(years=years):
                self.assertEqual(
                    classify_experience_requirement(years),
                    "manual_review",
                )

        for years in (review_max + 0.01, 5, 10):
            with self.subTest(years=years):
                self.assertEqual(
                    classify_experience_requirement(years),
                    "reject",
                )

    def test_unknown_or_invalid_years_do_not_become_a_rejection(self):
        for years in (None, "", "unknown", True, False, -1, float("nan"), float("inf")):
            with self.subTest(years=years):
                self.assertIsNone(classify_experience_requirement(years))

    def test_prompt_policy_is_generated_from_the_same_threshold_constants(self):
        text = experience_policy_prompt_text()
        waived = f"{MAX_EXPERIENCE_YEARS_WAIVED:g}"
        review_max = f"{MAX_EXPERIENCE_YEARS_MANUAL_REVIEW:g}"

        self.assertIn(f"Up to and including {waived} years", text)
        self.assertIn("MUST NOT cause REJECT or REVIEW", text)
        self.assertIn("choose APPLY", text)
        self.assertIn(f"up to and including {review_max} years", text)
        self.assertIn(f"More than {review_max} years: REJECT", text)
        self.assertIn("does not override a mismatched discipline", text)

    def test_analysis_and_independent_reviewer_share_the_policy_text(self):
        source_path = Path(__file__).resolve().parents[1] / "auto_jobfinder_cloud.py"
        source = source_path.read_text(encoding="utf-8")

        analysis_start = source.index("def analyze_offer(")
        analysis_end = source.index("\n\ndef ", analysis_start + 8)
        analysis = source[analysis_start:analysis_end]

        reviewer_start = source.index("def verify_apply_decisions(")
        reviewer_end = source.index("\n\ndef ", reviewer_start + 8)
        reviewer = source[reviewer_start:reviewer_end]

        self.assertIn('prompt = prompt.replace("{{EXPERIENCE_POLICY_BLOCK}}", experience_policy_prompt_text())', analysis)
        self.assertIn("{{EXPERIENCE_POLICY_BLOCK}}", analysis)
        self.assertIn("experience_policy_prompt_text()", reviewer)
        self.assertNotIn(
            "minimum experience requirements not met by the candidate are REJECT",
            analysis,
        )

    def test_two_year_direct_maintenance_role_applies_when_experience_is_only_threshold(self):
        import os
        from unittest.mock import patch

        # Avoid a live provider; the deterministic gate takes a supplied score.
        with patch.dict(os.environ, {"GROQ_API_KEY": "unit-test-unused"}, clear=False):
            import auto_jobfinder_cloud as processor

        job = {
            "job_title": "Technicien de maintenance",
            "application_title_fr": "Technicien de maintenance",
            "application_title_en": "Maintenance Technician",
            "score": 70,
            "reason": "Candidate has no professional experience.",
        }
        advertisement = (
            "Recrutement : Technicien de maintenance. "
            "Expérience professionnelle minimum : 2 ans. "
            "Lieu de travail : Oran. "
            "Tâches : maintenance préventive et corrective des équipements mécaniques et des pompes. "
            "Envoyez votre CV à jobs@example.org."
        )
        decision, reason = processor.enforce_candidate_fit_rule(
            job,
            "REVIEW",
            advertisement,
            cv_match_score=45,
        )
        self.assertEqual(decision, "APPLY")
        self.assertIn("تجاوز شرط الخبرة", reason)

    def test_deterministic_gate_uses_the_same_classifier(self):
        source_path = Path(__file__).resolve().parents[1] / "auto_jobfinder_cloud.py"
        source = source_path.read_text(encoding="utf-8")
        start = source.index("def _experience_requirement_policy(")
        end = source.index("\n\ndef ", start + 8)
        policy_function = source[start:end]

        self.assertIn("classify_experience_requirement(years)", policy_function)
        self.assertNotIn("if years <= 3:", policy_function)
        self.assertNotIn("if years <= 4:", policy_function)


    def test_model_prompt_explicitly_requires_review_for_vague_generalist_roles(self):
        source_path = Path(__file__).resolve().parents[1] / "auto_jobfinder_cloud.py"
        source = source_path.read_text(encoding="utf-8")
        analysis_start = source.index("def analyze_offer(")
        analysis_end = source.index("\\n\\ndef ", analysis_start + 8)
        analysis = source[analysis_start:analysis_end]

        self.assertIn("GENERALIST / POLYVALENT ROLE DECISION", analysis)
        self.assertIn('"Agents polyvalents"', analysis)
        self.assertIn("raw decision MUST be REVIEW", analysis)
        self.assertIn("Do NOT choose APPLY merely because the employer is a factory", analysis)


if __name__ == "__main__":
    unittest.main()
