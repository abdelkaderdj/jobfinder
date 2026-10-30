from __future__ import annotations

import math
import unittest
from pathlib import Path

from jobfinder_candidate_policy import (
    ACCOMMODATION_MARKERS,
    CV_MATCH_SKILL_GROUPS,
    DIRECT_MAINTENANCE_TECHNICIAN_ROLE_PATTERNS,
    ENGINEERING_GRADE_ROLE_TITLE_PATTERNS,
    GENERALIST_ROLE_TITLE_PATTERNS,
    GENERIC_TECHNICAL_ROLE_PATTERNS,
    LOCAL_LOCATION_MARKERS,
    MAX_EXPERIENCE_YEARS_APPLY,
    PRODUCTION_ROLE_TITLE_PATTERNS,
    SOUTH_LOCATION_MARKERS,
    STRONG_RELEVANT_ROLE_PATTERNS,
    UNQUANTIFIED_MANDATORY_EXPERIENCE_DECISION,
    UNRELATED_ROLE_TITLE_PATTERNS,
    candidate_location_policy_prompt_text,
    candidate_role_policy_prompt_text,
    classify_experience_requirement,
    experience_policy_prompt_text,
)


class ExperienceRequirementPolicyTests(unittest.TestCase):
    def test_up_to_and_including_four_years_is_waived(self):
        limit = MAX_EXPERIENCE_YEARS_APPLY
        for years in (0, 0.5, 1, 2, 3, 3.5, 4, limit):
            with self.subTest(years=years):
                self.assertEqual(
                    classify_experience_requirement(years),
                    "waive_experience",
                )

    def test_more_than_four_years_is_rejected(self):
        for years in (4.01, 4.5, 5, 10):
            with self.subTest(years=years):
                self.assertEqual(classify_experience_requirement(years), "reject")

    def test_unknown_or_invalid_years_do_not_become_a_rejection(self):
        for years in (None, "", "unknown", True, False, -1, float("nan"), float("inf")):
            with self.subTest(years=years):
                self.assertIsNone(classify_experience_requirement(years))

    def test_prompt_policy_is_generated_from_the_same_threshold_constant(self):
        text = experience_policy_prompt_text()
        limit = f"{MAX_EXPERIENCE_YEARS_APPLY:g}"

        self.assertIn(f"up to and including {limit} years", text.lower())
        self.assertIn("MUST NOT cause REJECT or REVIEW", text)
        self.assertIn("choose APPLY", text)
        self.assertIn(f"More than {limit} years as an explicit minimum: REJECT", text)
        self.assertIn("optional/preferred experience", text)
        self.assertIn("mandatory but no number", text)

    def test_role_and_location_policy_are_shared_prompt_blocks(self):
        role = candidate_role_policy_prompt_text()
        location = candidate_location_policy_prompt_text()

        for phrase in (
            "mechanical maintenance",
            "refrigeration",
            "information technology/IT support/computer systems/networks/telecommunications",
            "centrifugal pumps",
            "Licence/Bac+3",
            "Ingénieur",
            "Production",
            "generalist/polyvalent",
            "Never borrow tasks",
        ):
            self.assertIn(phrase.lower(), role.lower())

        for phrase in ("Oran", "Mostaganem", "Southern/Saharan", "accommodation"):
            self.assertIn(phrase.lower(), location.lower())

    def test_analysis_and_independent_reviewer_share_the_canonical_policy_blocks(self):
        source_path = Path(__file__).resolve().parents[1] / "auto_jobfinder_cloud.py"
        source = source_path.read_text(encoding="utf-8")

        analysis_start = source.index("def analyze_offer(")
        analysis_end = source.index("\n\ndef ", analysis_start + 8)
        analysis = source[analysis_start:analysis_end]

        reviewer_start = source.index("def verify_apply_decisions(")
        reviewer_end = source.index("\n\ndef ", reviewer_start + 8)
        reviewer = source[reviewer_start:reviewer_end]

        for placeholder in (
            "{{CANDIDATE_ROLE_POLICY_BLOCK}}",
            "{{EXPERIENCE_POLICY_BLOCK}}",
            "{{LOCATION_POLICY_BLOCK}}",
        ):
            self.assertIn(placeholder, analysis)

        self.assertIn("candidate_role_policy_prompt_text()", analysis)
        self.assertIn("experience_policy_prompt_text()", analysis)
        self.assertIn("candidate_location_policy_prompt_text()", analysis)

        self.assertIn("candidate_role_policy_prompt_text()", reviewer)
        self.assertIn("experience_policy_prompt_text()", reviewer)
        self.assertIn("candidate_location_policy_prompt_text()", reviewer)

        self.assertNotIn("3 years: REVIEW", reviewer)
        self.assertNotIn("up to 3 years", analysis.lower())

    def test_deterministic_role_and_location_lists_have_one_canonical_source(self):
        source_path = Path(__file__).resolve().parents[1] / "auto_jobfinder_cloud.py"
        source = source_path.read_text(encoding="utf-8")

        expected_names = (
            "UNRELATED_ROLE_TITLE_PATTERNS",
            "DIRECT_MAINTENANCE_TECHNICIAN_ROLE_PATTERNS",
            "GENERALIST_ROLE_TITLE_PATTERNS",
            "PRODUCTION_ROLE_TITLE_PATTERNS",
            "ENGINEERING_GRADE_ROLE_TITLE_PATTERNS",
            "STRONG_RELEVANT_ROLE_PATTERNS",
            "GENERIC_TECHNICAL_ROLE_PATTERNS",
            "CV_MATCH_SKILL_GROUPS",
            "LOCAL_LOCATION_MARKERS",
            "SOUTH_LOCATION_MARKERS",
            "ACCOMMODATION_MARKERS",
        )
        for name in expected_names:
            with self.subTest(name=name):
                self.assertIn(name, source.split("from jobfinder_candidate_policy import", 1)[1].split(")", 1)[0])
                self.assertNotRegex(source, rf"(?m)^{name}\s*=")

        self.assertTrue(UNRELATED_ROLE_TITLE_PATTERNS)
        self.assertTrue(DIRECT_MAINTENANCE_TECHNICIAN_ROLE_PATTERNS)
        self.assertTrue(GENERALIST_ROLE_TITLE_PATTERNS)
        self.assertTrue(PRODUCTION_ROLE_TITLE_PATTERNS)
        self.assertTrue(ENGINEERING_GRADE_ROLE_TITLE_PATTERNS)
        self.assertTrue(STRONG_RELEVANT_ROLE_PATTERNS)
        self.assertTrue(GENERIC_TECHNICAL_ROLE_PATTERNS)
        self.assertTrue(CV_MATCH_SKILL_GROUPS)
        self.assertIn("oran", {item.lower() for item in LOCAL_LOCATION_MARKERS})
        self.assertIn("mostaganem", {item.lower() for item in LOCAL_LOCATION_MARKERS})
        self.assertIn("ouargla", {item.lower() for item in SOUTH_LOCATION_MARKERS})
        self.assertIn("logement fourni", ACCOMMODATION_MARKERS)

    def test_unquantified_mandatory_experience_is_review_not_rejected(self):
        self.assertEqual(UNQUANTIFIED_MANDATORY_EXPERIENCE_DECISION, "review")

    def test_deterministic_gate_uses_the_canonical_classifier(self):
        source_path = Path(__file__).resolve().parents[1] / "auto_jobfinder_cloud.py"
        source = source_path.read_text(encoding="utf-8")
        start = source.index("def _experience_requirement_policy(")
        end = source.index("\n\ndef ", start + 8)
        policy_function = source[start:end]

        self.assertIn("classify_experience_requirement(years)", policy_function)
        self.assertNotIn('return years, "manual_review"', policy_function)


if __name__ == "__main__":
    unittest.main()
