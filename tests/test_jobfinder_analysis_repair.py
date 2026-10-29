from __future__ import annotations

import os
import unittest

# Importing the analysis module checks that at least one provider is configured.
# These harmless placeholders are never used for a network request in this test.
os.environ.setdefault("GROQ_API_KEY", "unit-test-unused")
os.environ.setdefault("GMAIL_APP_PASSWORD", "unit-test-unused")
os.environ.setdefault("GMAIL_ADDRESS", "unit-test@example.invalid")

import auto_jobfinder_cloud as processor


class GroundedOfferRecoveryTests(unittest.TestCase):
    def test_specific_current_offer_after_rumour_disclaimer_is_recovered_to_review(self):
        source = (
            "تتداول معلومات غير مؤكدة، لكن شركة Example تعلن عن توظيف "
            "تقني صيانة ميكانيكية في وهران. أرسل سيرتك الذاتية إلى jobs@example.org."
        )
        result = {
            "post_type": "unconfirmed_rumour",
            "post_type_evidence": ["تتداول معلومات غير مؤكدة"],
            "jobs": [],
        }

        recovered = processor._postprocess_analysis_result(source, result)

        self.assertEqual(recovered["post_type"], "actual_vacancy")
        self.assertEqual(len(recovered["jobs"]), 1)
        job = recovered["jobs"][0]
        self.assertEqual(job["job_title"], "تقني صيانة ميكانيكية")
        self.assertEqual(job["title_evidence"], "تقني صيانة ميكانيكية")
        self.assertEqual(job["company"], "Example")
        self.assertEqual(job["location"], "وهران")
        self.assertEqual(job["emails"], ["jobs@example.org"])
        self.assertEqual(job["decision"], "REVIEW")
        self.assertIn("شركة Example تعلن عن توظيف", recovered["post_type_evidence"][0])

    def test_future_teaser_evidence_is_replaced_only_with_verbatim_source_clause(self):
        source = (
            "الله يبارك، راكم اغلبيتكم جامعيين و من كل ولايات، "
            "شوفو خاوتي خواتاتي، رايح نحط مجموعه عروض عمل، "
            "تخص ولايات الشرق و الغرب و الوسط و الصحراء، نحتاج منكم التفاعل."
        )
        result = {
            "post_type": "future_teaser",
            "post_type_evidence": ["The author will post several offers soon."],
            "jobs": [],
        }

        repaired = processor._postprocess_analysis_result(source, result)

        self.assertEqual(repaired["post_type"], "future_teaser")
        self.assertEqual(repaired["post_type_evidence"], ["رايح نحط مجموعه عروض عمل"])
        self.assertIn(repaired["post_type_evidence"][0], source)

    def test_generic_rumour_is_not_rescued(self):
        source = (
            "تتداول معلومات حول إمكانية فتح مناصب توظيف جديدة في الشركات الوطنية. "
            "تابعوا الإعلانات الرسمية jobs@example.org."
        )
        result = {
            "post_type": "unconfirmed_rumour",
            "post_type_evidence": ["تتداول معلومات"],
            "jobs": [],
        }

        recovered = processor._postprocess_analysis_result(source, result)

        self.assertEqual(recovered["post_type"], "unconfirmed_rumour")
        self.assertEqual(recovered["jobs"], [])

    def test_explicit_single_recruitment_heading_restores_original_title(self):
        source = (
            "Recrutement : Ingénieurs techniques - Bureau technique. "
            "Diplôme d'ingénieur exigé. Envoyez votre candidature à jobs@example.org."
        )
        result = {
            "post_type": "actual_vacancy",
            "post_type_evidence": ["Recrutement"],
            "jobs": [{
                "job_title": "Bureau technique",
                "title_evidence": "Bureau technique",
                "application_title_fr": "Ingénieur technique",
                "application_title_en": "Technical Engineer",
                "decision": "REJECT",
                "score": 20,
                "emails": ["jobs@example.org"],
            }],
        }

        repaired = processor._postprocess_analysis_result(source, result)

        self.assertEqual(repaired["jobs"][0]["job_title"], "Ingénieurs techniques - Bureau technique")
        self.assertEqual(repaired["jobs"][0]["title_evidence"], "Ingénieurs techniques - Bureau technique")

    def test_ungrounded_title_evidence_uses_exact_original_title_if_present(self):
        source = "Maintenance & Technique\\n🔹 رئيس فريق صيانة\\n🔹 كهربائي\\nإرسال السيرة الذاتية إلى jobs@example.org"
        result = {
            "post_type": "actual_vacancy",
            "post_type_evidence": ["إرسال السيرة الذاتية إلى jobs@example.org"],
            "jobs": [{
                "job_title": "كهربائي",
                "title_evidence": "Maintenance & Technique",
                "decision": "REJECT",
            }],
        }

        repaired = processor._postprocess_analysis_result(source, result)

        self.assertEqual(repaired["jobs"][0]["title_evidence"], "كهربائي")


if __name__ == "__main__":
    unittest.main()
