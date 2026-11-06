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
from telegram_control import TelegramJobBot


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


    def _sinohydro_multirole_advertisement(self):
        return """🚨 إعلان توظيف | SINOHYDRO Corporation Limited
📌 المناصب المطلوبة:
1. Boom Truck Operator
2. Forklift Operator
3. Backhoe Loader Operator
4. Manlift Operator
5. Water Truck Operator
شروط التوظيف:
- شهادة أو دبلوم معتمد وساري المفعول.
- خبرة مهنية لا تقل عن 3 سنوات في المجال.
📍 مكان العمل: حاسي مسعود – مشروع المصفاة.
نظام العمل: 6×2
"""

    def test_lexicon_examples_are_used_to_recognize_equipment_operator_roles(self):
        advertisement = self._sinohydro_multirole_advertisement()
        titles = processor.extract_compact_listed_role_titles(advertisement)

        self.assertEqual(len(titles), 5)
        normalized = [processor.normalize_text(title) for title in titles]
        self.assertTrue(any("backhoe loader operator" in title for title in normalized))
        self.assertTrue(any("manlift operator" in title for title in normalized))
        self.assertTrue(any("water truck operator" in title for title in normalized))

    def test_scoped_extraction_attaches_shared_conditions_to_each_position(self):
        advertisement = self._sinohydro_multirole_advertisement()
        titles = processor.extract_compact_listed_role_titles(advertisement)
        jobs, recovered_count = processor._recover_missing_compact_list_roles(
            titles,
            [],
            advertisement,
        )

        self.assertEqual(recovered_count, 5)
        self.assertTrue(all(job["decision"] == "REVIEW" for job in jobs))
        self.assertTrue(all(job["score"] is None for job in jobs))

        for job in jobs:
            with self.subTest(job_title=job["job_title"]):
                scope, matched, reason = processor.resolve_job_position_scope(
                    job,
                    advertisement,
                    jobs,
                )
                self.assertTrue(matched, reason)
                details = processor._extract_source_grounded_recruitment_details(scope)

                self.assertEqual(details["experience"]["status"], "required")
                self.assertEqual(details["experience"]["minimum_years"], 3)
                self.assertIn(
                    details["experience"]["evidence"],
                    scope,
                )
                self.assertEqual(details["education"]["level"], "دبلوم معتمد")
                self.assertEqual(details["conditions"]["shift_pattern"], "6×2")
                self.assertEqual(
                    details["workplace"]["worksite"],
                    "حاسي مسعود – مشروع المصفاة",
                )

    def test_telegram_review_displays_only_source_grounded_recruitment_details(self):
        advertisement = self._sinohydro_multirole_advertisement()
        details = {
            "experience": {
                "status": "required",
                "minimum_years": 3,
                "evidence": "خبرة مهنية لا تقل عن 3 سنوات في المجال.",
            },
            "education": {
                "level": "دبلوم معتمد",
                "evidence": "- شهادة أو دبلوم معتمد وساري المفعول.",
            },
            "conditions": {
                "shift_pattern": "6×2",
                "shift_pattern_evidence": "نظام العمل: 6×2",
                "salary": "راتب غير موجود في الإعلان",
                "salary_evidence": "راتب غير موجود في الإعلان",
            },
        }
        bot = TelegramJobBot({}, processor, lambda _reason: None)
        message = bot._review_text({
            "record": {
                "job_title": "Backhoe Loader Operator",
                "application_job_title": "Backhoe Loader Operator",
                "company": "SINOHYDRO Corporation Limited",
                "location": "حاسي مسعود – مشروع المصفاة",
                "score": None,
                "emails": [],
                "reason": "مراجعة يدوية.",
                "decision": "REVIEW",
                "recruitment_details": details,
            },
            "advertisement": advertisement,
            "post": {"source": "test_channel", "message_id": 123},
            "can_auto_send": False,
        })

        self.assertIn("معلومات التوظيف المستخرجة", message)
        self.assertIn("3 سنوات", message)
        self.assertIn("دبلوم معتمد", message)
        self.assertIn("6×2", message)
        self.assertNotIn("راتب غير موجود في الإعلان", message)



if __name__ == "__main__":
    unittest.main()
