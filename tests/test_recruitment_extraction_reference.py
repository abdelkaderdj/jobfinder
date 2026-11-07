import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("GROQ_API_KEY", "validation-dummy-key")
os.environ.setdefault("GMAIL_APP_PASSWORD", "validation-dummy-password")
os.environ.setdefault("GMAIL_ADDRESS", "validation@example.org")
os.environ.setdefault("JOBFINDER_CANDIDATE_PROFILE", "validation profile")
os.environ.setdefault("JOBFINDER_CANDIDATE_NAME", "Validation User")

import auto_jobfinder_cloud as processor


class RecruitmentExtractionReferenceTests(unittest.TestCase):
    def test_curated_lexicon_contains_observed_multilingual_patterns(self):
        reference = processor.recruitment_extraction_reference_text()
        lexicon = json.loads(reference)

        self.assertIn("إعلان توظيف", lexicon["recruitment_signals"]["strong"])
        self.assertIn("Nous recrutons", lexicon["recruitment_signals"]["strong"])
        self.assertIn("مكان العمل", lexicon["location_wording"]["work_location_labels"])
        self.assertIn("Résider à", lexicon["location_wording"]["candidate_residence_labels"])
        self.assertIn("Débutant accepté", lexicon["experience_wording"]["not_required"])
        self.assertIn("Expérience exigée", lexicon["experience_wording"]["mandatory"])
        self.assertIn("4×4", lexicon["work_conditions"]["shift_patterns"])
        self.assertIn("Objet du mail", lexicon["application_wording"]["subject"])

    def test_analyze_offer_injects_lexicon_and_structured_extraction_schema(self):
        with patch.object(
            processor,
            "groq_json_request",
            return_value={"post_type": "other", "post_type_evidence": [], "jobs": []},
        ) as request:
            processor.analyze_offer("Poste à pourvoir : exemple")

        prompt = request.call_args.kwargs["messages"][1]["content"]
        self.assertIn("CURATED MULTILINGUAL PHRASE REFERENCE", prompt)
        self.assertIn("Débutant accepté", prompt)
        self.assertIn("required_candidate_residence", prompt)
        self.assertIn("employer_headquarters", prompt)
        self.assertIn("shift_pattern", prompt)
        self.assertIn("subject_instruction", prompt)
        self.assertIn("This object is descriptive metadata only", prompt)
        self.assertIn("COMPACT OUTPUT IS REQUIRED", prompt)
        self.assertIn("Omit recruitment_details entirely", prompt)
        self.assertNotIn("{{RECRUITMENT_EXTRACTION_LEXICON}}", prompt)

    def test_lexicon_reference_is_loaded_from_project_data_file(self):
        path = Path(processor.PROJECT_DIR) / "data" / "recruitment_extraction_lexicon.json"
        self.assertTrue(path.is_file())
        parsed = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("title_families", parsed)
        self.assertGreaterEqual(len(parsed["title_families"]), 10)


    def test_veolia_explicit_vacancy_list_extracts_all_four_titles_without_lexicon_prefixes(self):
        advertisement = (
            "🚨 Recrutement DZ | فرص عمل بشركة VEOLIA في الجزائر\n"
            "تعلن شركة VEOLIA، الرائدة في مجال الخدمات البيئية وإدارة الموارد، "
            "عن توفر فرص توظيف في عدة تخصصات ومناصب مهنية...\n"
            "📌 المناصب المطلوبة:\n"
            "🔹 Mechanical Technician (تقني ميكانيك).\n"
            "🔹 Operator Junior (عامل تشغيل مبتدئ).\n"
            "🔹 Maintenance Mechanical Technician (تقني صيانة ميكانيكية).\n"
            "🔹 Specialist – Sourcing & Procurement (أخصائي التوريد والمشتريات).\n"
            "📍 أماكن العمل:\n"
            "الجزائر العاصمة (Alger).\n"
            "وهران (Oran).\n"
            "📩 طريقة التقديم: عبر بوابة التوظيف الرسمية:\n"
            "https://jobs.veolia.com/\n"
            "خدمات إضافية للسيرة الذاتية وLinkedIn.\n"
        )

        titles = processor.extract_compact_listed_role_titles(advertisement)

        self.assertEqual(
            [title.split(" (", 1)[0] for title in titles],
            [
                "Mechanical Technician",
                "Operator Junior",
                "Maintenance Mechanical Technician",
                "Specialist – Sourcing & Procurement",
            ],
        )

    def test_veolia_list_recovery_keeps_each_role_separate_and_review_only(self):
        advertisement = (
            "تعلن شركة VEOLIA عن فرص توظيف.\n"
            "📌 المناصب المطلوبة:\n"
            "🔹 Mechanical Technician (تقني ميكانيك).\n"
            "🔹 Operator Junior (عامل تشغيل مبتدئ).\n"
            "🔹 Maintenance Mechanical Technician (تقني صيانة ميكانيكية).\n"
            "🔹 Specialist – Sourcing & Procurement (أخصائي التوريد والمشتريات).\n"
            "📍 أماكن العمل:\n"
            "الجزائر العاصمة (Alger).\n"
            "وهران (Oran).\n"
            "📩 طريقة التقديم: https://jobs.veolia.com/\n"
        )

        titles = processor.extract_compact_listed_role_titles(advertisement)
        jobs, recovered = processor._recover_missing_compact_list_roles(
            titles, [], advertisement
        )

        self.assertEqual(recovered, 4)
        self.assertEqual(len(jobs), 4)
        self.assertEqual({job["company"] for job in jobs}, {"VEOLIA"})
        self.assertTrue(all(job["decision"] == "REVIEW" for job in jobs))
        self.assertTrue(all(job["score"] is None for job in jobs))
        # The two cities are advertised collectively; never assign them to every role.
        self.assertTrue(all(job["location"] == "" for job in jobs))

    def test_company_name_and_plural_multiline_work_locations_are_source_grounded(self):
        advertisement = (
            "تعلن شركة VEOLIA، الرائدة في الخدمات البيئية، عن توظيف.\n"
            "📍 أماكن العمل:\n"
            "الجزائر العاصمة (Alger).\n"
            "وهران (Oran).\n"
            "📩 طريقة التقديم: https://jobs.veolia.com/\n"
        )

        self.assertEqual(processor._infer_company_name_from_advertisement(advertisement), "VEOLIA")
        self.assertEqual(
            processor.infer_explicit_work_location(advertisement),
            "الجزائر العاصمة (Alger)، وهران (Oran)",
        )

    def test_unknown_role_title_requires_an_explicit_vacancy_list_item(self):
        self.assertEqual(
            processor.extract_compact_listed_role_titles(
                "🔹 Mechanical Technician\n🔹 Operator Junior"
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
