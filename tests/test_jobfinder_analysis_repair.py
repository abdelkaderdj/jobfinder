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

    def test_short_exact_arabic_occupational_title_is_grounded_and_kept(self):
        source = (
            "Maintenance & Technique\n"
            "🔹 رئيس فريق صيانة\n"
            "🔹 كهربائي\n"
            "إرسال السيرة الذاتية إلى jobs@example.org"
        )
        job = {
            "job_title": "كهربائي",
            "title_evidence": "كهربائي",
            "decision": "REJECT",
        }

        self.assertTrue(processor._job_title_evidence_is_grounded(job, source))
        validated = processor._validated_job_records(
            {"post_type": "actual_vacancy", "jobs": [job]},
            source,
        )
        self.assertEqual(validated, [job])

    def test_short_evidence_not_matching_the_extracted_title_is_rejected(self):
        source = "🔹 كهربائي\nإرسال السيرة الذاتية إلى jobs@example.org"
        job = {
            "job_title": "كهربائي",
            "title_evidence": "كهربا",
            "decision": "REJECT",
        }

        self.assertFalse(processor._job_title_evidence_is_grounded(job, source))


    def test_actual_vacancy_evidence_filters_unquoted_model_paraphrases(self):
        source = (
            "Maintenance & Technique\\n"
            "🔹 رئيس فريق صيانة\\n"
            "🔹 كهربائي\\n"
            "إرسال السيرة الذاتية إلى jobs@example.org"
        )
        result = {
            "post_type": "actual_vacancy",
            "post_type_evidence": [
                "The author is advertising maintenance vacancies.",
            ],
            "jobs": [],
        }

        repaired = processor._postprocess_analysis_result(source, result)

        self.assertTrue(repaired["post_type_evidence"])
        for quote in repaired["post_type_evidence"]:
            self.assertTrue(processor._source_quote_is_grounded_for_analysis(quote, source))

    def test_missing_type_and_empty_jobs_recover_explicit_cariste_as_review_only(self):
        source = (
            "SM MOBILIER، أولاد موسى – بومرداس، recrute un CARISTE. "
            "Expérience dans un poste similaire souhaitée. "
            "Maîtrise de la conduite du chariot élévateur. "
            "Disponibilité immédiate. Envoyer CV à +213563000790."
        )
        result = {"jobs": []}

        recovered = processor._postprocess_analysis_result(source, result)

        self.assertEqual(recovered["post_type"], "actual_vacancy")
        self.assertEqual(len(recovered["jobs"]), 1)
        job = recovered["jobs"][0]
        self.assertEqual(job["job_title"], "CARISTE")
        self.assertEqual(job["title_evidence"], "CARISTE")
        self.assertEqual(job["decision"], "REVIEW")
        self.assertIsNone(job["score"])
        self.assertEqual(job["company"], "")

    def test_missing_type_and_empty_jobs_recover_ets_south_project_positions_as_review_only(self):
        source = (
            "تعلن شركة SARL Equipment Technology and Services (ETS)، "
            "الكائن مقرها بالقيدي، برج الكيفان – الجزائر العاصمة، عن توظيف "
            "تقنيين وكهربائيين وعمال ومناولي أشغال للمشاركة في مشروع هام بجنوب الجزائر. "
            "الراتب ابتداء من 95,000 دج. نظام العمل 45 يوم عمل / 15 يوم راحة. "
            "الإقامة والإطعام متكفل بهما بالكامل. إرسال CV إلى etsm@yahoo.com."
        )
        result = {"jobs": []}

        recovered = processor._postprocess_analysis_result(source, result)

        self.assertEqual(recovered["post_type"], "actual_vacancy")
        jobs = recovered["jobs"]
        self.assertEqual(len(jobs), 3)
        titles = {job["job_title"] for job in jobs}
        self.assertEqual(titles, {"تقنيين", "كهربائيين", "عمال ومناولي أشغال"})
        self.assertEqual({job["decision"] for job in jobs}, {"REVIEW"})
        self.assertTrue(all(job["score"] is None for job in jobs))
        self.assertTrue(all(job["location"] == "" for job in jobs))
        self.assertTrue(all(job["emails"] == ["etsm@yahoo.com"] for job in jobs))

    def test_generic_showroom_descriptor_is_not_a_company_name(self):
        source = (
            "📢 إعلان توظيف | منصب موظفة استقبال\\n"
            "مطلوب موظفتان للاستقبال في معرض تجاري.\\n"
            "📍 مكان العمل: دالي إبراهيم، الشراقة – الجزائر العاصمة.\\n"
            "📧 إرسال السيرة الذاتية إلى mg20005000@gmail.com"
        )
        result = {
            "post_type": "actual_vacancy",
            "post_type_evidence": ["منصب موظفة استقبال"],
            "jobs": [{
                "job_title": "موظفة استقبال",
                "title_evidence": "موظفة استقبال",
                "company": "معرض تجاري",
                "decision": "REVIEW",
            }],
        }

        repaired = processor._postprocess_analysis_result(source, result)

        self.assertEqual(repaired["jobs"][0]["company"], "")


if __name__ == "__main__":
    unittest.main()
