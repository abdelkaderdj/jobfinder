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

    def test_compact_list_extractor_uses_curated_role_examples(self):
        advertisement = (
            "المناصب الشاغرة\n"
            "موظفة استقبال\n"
            "أمين مخزن\n"
            "عامل إنتاج\n"
            "الشروط المطلوبة\n"
            "مستوى دراسي مناسب\n"
        )
        titles = processor.extract_compact_listed_role_titles(advertisement)
        self.assertEqual(titles, ["موظفة استقبال", "أمين مخزن", "عامل إنتاج"])

    def test_receptionist_single_position_recovery_preserves_grounded_fields(self):
        advertisement = (
            "📢 إعلان توظيف | منصب موظفة استقبال\n"
            "مطلوب موظفتان للاستقبال في معرض تجاري.\n"
            "📍 مكان العمل: دالي إبراهيم، الشراقة – الجزائر العاصمة.\n"
            "✅ الخبرة: سنتان على الأقل.\n"
            "💰 الراتب: ابتداءً من 60,000 دج.\n"
            "🕒 التوقيت: السبت إلى الخميس، من 11:00 إلى 19:00، الجمعة عطلة.\n"
            "📧 إرسال السيرة الذاتية إلى mg20005000@gmail.com أو التواصل مباشرة عبر الرسائل الخاصة."
        )
        job = processor.recover_explicit_single_position(advertisement)
        self.assertIsNotNone(job)
        self.assertEqual(job["job_title"], "موظفة استقبال")
        self.assertIn("دالي ابراهيم", processor.normalize_text(job["location"]))
        self.assertIn("الشراقة", processor.normalize_text(job["location"]))
        self.assertEqual(job["company"], "")
        self.assertEqual(processor.extract_emails(advertisement), ["mg20005000@gmail.com"])
        self.assertIsNone(job["score"])
        self.assertEqual(job["decision"], "REVIEW")

    def test_lexicon_reference_is_loaded_from_project_data_file(self):
        path = Path(processor.PROJECT_DIR) / "data" / "recruitment_extraction_lexicon.json"
        self.assertTrue(path.is_file())
        parsed = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("title_families", parsed)
        self.assertGreaterEqual(len(parsed["title_families"]), 10)


if __name__ == "__main__":
    unittest.main()
