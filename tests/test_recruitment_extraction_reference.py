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


    def test_uploaded_corpus_titles_recover_across_distinct_job_families(self):
        advertisement = """🚨 إعلان توظيف | حملة متعددة المناصب
📌 المناصب المطلوبة:
1. Comptable
2. Front Office Manager
3. Médecin Biologiste
4. Agent d’Administration
5. Chef de Projet SI
6. Chef de Rang
7. Développeur des Ventes
8. Soudeur
9. Opérateur KHS
10. Agent de Réclamation
"""
        titles = processor.extract_compact_listed_role_titles(advertisement)
        normalized = [processor.normalize_text(title) for title in titles]

        self.assertEqual(len(titles), 10)
        for expected in (
            "comptable",
            "front office manager",
            "medecin biologiste",
            "agent d administration",
            "chef de projet si",
            "chef de rang",
            "developpeur des ventes",
            "soudeur",
            "operateur khs",
            "agent de reclamation",
        ):
            with self.subTest(title=expected):
                self.assertTrue(
                    any(expected in title for title in normalized),
                    f"Missing source-derived title: {expected}",
                )

    def test_uploaded_corpus_linova_style_roles_are_recognized(self):
        advertisement = """🚨 إعلان توظيف | LINOVA
📌 المناصب المطلوبة:
1. لحام (03 مناصب)
2. عون مبيعات خارجي (02 منصبين)
3. مسؤول منتج (01 منصب)
4. عون مصلحة ما بعد البيع (01 منصب)
"""
        titles = processor.extract_compact_listed_role_titles(advertisement)
        normalized = [processor.normalize_text(title) for title in titles]

        self.assertEqual(len(titles), 4)
        for expected in (
            "لحام",
            "عون مبيعات خارجي",
            "مسؤول منتج",
            "عون مصلحة ما بعد البيع",
        ):
            normalized_expected = processor.normalize_text(expected)
            with self.subTest(title=expected):
                self.assertTrue(
                    any(normalized_expected in title for title in normalized),
                    f"Missing normalized title: {normalized_expected}; extracted={normalized}",
                )

    def test_uploaded_corpus_includes_non_vacancy_context_separately(self):
        lexicon = json.loads(processor.recruitment_extraction_reference_text())
        signals = lexicon["recruitment_signals"]

        self.assertIn("Nous recherchons", signals["strong"])
        self.assertIn("عن فتح باب التوظيف", signals["strong"])
        self.assertIn("technical question", signals["non_vacancy_context"])
        self.assertIn("خدمة إعداد السيرة الذاتية", signals["non_vacancy_context"])
        self.assertIn("social discussion", signals["non_vacancy_context"])
        self.assertGreaterEqual(len(lexicon["title_families"]), 20)


    def test_uploaded_corpus_candidate_conditions_and_salary_are_source_grounded(self):
        advertisement = """🚨 إعلان توظيف | شركة صناعية
📌 المنصب المطلوب:
Chauffeur
يشترط الإقامة في خميس الخشنة وضواحيها.
العمر بين 25 و40 سنة.
يشترط بطاقة الخدمة الوطنية.
امتلاك سيارة خاصة مطلوب.
راتب شهري 60,000 دج.
"""
        details = processor._extract_source_grounded_recruitment_details(advertisement)

        self.assertEqual(details["candidate_residence"]["status"], "required")
        self.assertIn(
            "يشترط الإقامة في خميس الخشنة وضواحيها.",
            details["candidate_residence"]["evidence"],
        )
        conditions = details["conditions"]
        self.assertIn("25 و40", conditions["age"])
        self.assertIn("60,000", conditions["salary"])
        self.assertIn("بطاقة الخدمة الوطنية", conditions["military_status"])
        self.assertIn("سيارة خاصة", conditions["vehicle"])
        for field in ("age", "salary", "military_status", "vehicle"):
            self.assertIn(f"{field}_evidence", conditions)
            self.assertIn(conditions[f"{field}_evidence"], advertisement)

    def test_telegram_review_displays_verified_residence_age_salary_and_personal_vehicle(self):
        advertisement = """إعلان توظيف
يشترط الإقامة في خميس الخشنة وضواحيها.
العمر بين 25 و40 سنة.
راتب شهري 60,000 دج.
امتلاك سيارة خاصة مطلوب.
"""
        details = processor._extract_source_grounded_recruitment_details(advertisement)
        bot = TelegramJobBot({}, processor, lambda _reason: None)
        message = bot._review_text({
            "record": {
                "job_title": "Chauffeur",
                "application_job_title": "Chauffeur",
                "company": "شركة صناعية",
                "location": "خميس الخشنة",
                "score": None,
                "emails": [],
                "reason": "مراجعة يدوية.",
                "decision": "REVIEW",
                "recruitment_details": details,
            },
            "advertisement": advertisement,
            "post": {"source": "test_channel", "message_id": 456},
            "can_auto_send": False,
        })

        self.assertIn("إقامة المترشح (شرط مطلوب)", message)
        self.assertIn("السن: العمر بين 25 و40 سنة", message)
        self.assertIn("الأجر: راتب شهري 60,000 دج", message)
        self.assertIn("وسيلة النقل الشخصية: امتلاك سيارة خاصة مطلوب", message)


    def test_uploaded_corpus_plural_feminine_and_specialized_titles_are_recovered(self):
        advertisement = """🚨 إعلان توظيف | وظائف إضافية من العينة
📌 المناصب المطلوبة:
1. Chargé(e) de Casting Influence
2. Caissière
3. Superviseurs des Ventes
4. Pré-vendeurs Terrain
5. Peintres en Bâtiment
6. Responsable d'Atelier
7. Chargé de Remplir des Cahiers de Charge
8. Chargé des Assurances et Sinistres Automobiles
9. Couturière
"""
        titles = processor.extract_compact_listed_role_titles(advertisement)
        normalized = [processor.normalize_text(title) for title in titles]

        self.assertEqual(len(titles), 9)
        for expected in (
            "Chargé(e) de Casting Influence",
            "Caissière",
            "Superviseurs des Ventes",
            "Pré-vendeurs Terrain",
            "Peintres en Bâtiment",
            "Responsable d'Atelier",
            "Chargé de Remplir des Cahiers de Charge",
            "Chargé des Assurances et Sinistres Automobiles",
            "Couturière",
        ):
            normalized_expected = processor.normalize_text(expected)
            with self.subTest(title=expected):
                self.assertTrue(
                    any(normalized_expected in title for title in normalized),
                    f"Missing normalized title: {normalized_expected}; extracted={normalized}",
                )


    def test_gusto_style_multi_role_post_keeps_each_roles_conditions_separate(self):
        advertisement = """📢 Gusto Distribution
📌 Postes à pourvoir:
1. Superviseurs des Ventes - Hommes (02)
Lieu de travail et résidence: Alger / Boumerdès.
Expérience confirmée en encadrement des équipes commerciales.
2. Pré-vendeurs Terrain - Hommes (03)
Lieu de travail et résidence: Alger.
Âge entre 20 et 30 ans. Expérience minimum de 2 ans sur le terrain.
Permis moto obligatoire.
3. Gestionnaire de Stock - Homme (01)
Lieu de travail: Boudouaou.
Lieu de résidence: Réghaïa, Boudouaou ou environs.
Âge entre 25 et 30 ans. Expérience minimum d'une année en gestion des stocks.
Conduite du chariot élévateur appréciée.
4. Assistante de Direction - Femme (01)
Lieu de travail: Boudouaou.
Lieu de résidence: Réghaïa, Boudouaou ou environs.
Femme entre 25 et 35 ans. Sens commercial et traitement des commandes par téléphone.
5. Chargé(e) RH Junior - H/F (01)
Lieu de travail: Boudouaou.
Lieu de résidence: Réghaïa, Boudouaou ou environs.
Expérience minimum de 2 ans en gestion du personnel et paie.
6. Caissières - Femmes (02)
Lieu de travail: Boudouaou.
Lieu de résidence: Réghaïa, Boudouaou ou environs.
Précision, honnêteté et maîtrise des opérations de caisse.
"""
        titles = processor.extract_compact_listed_role_titles(advertisement)
        self.assertEqual(len(titles), 6)

        jobs, recovered_count = processor._recover_missing_compact_list_roles(
            titles, [], advertisement
        )
        self.assertEqual(recovered_count, 6)
        self.assertTrue(all(job["decision"] == "REVIEW" for job in jobs))
        self.assertTrue(all(job["score"] is None for job in jobs))

        scopes = {}
        for job in jobs:
            scope, matched, reason = processor.resolve_job_position_scope(
                job, advertisement, jobs
            )
            self.assertTrue(matched, f"{job['job_title']}: {reason}")
            scopes[processor.normalize_text(job["job_title"])] = scope

        pre_vendor = next(
            scope for title, scope in scopes.items()
            if "pre vendeurs terrain" in title
        )
        sales_supervisor = next(
            scope for title, scope in scopes.items()
            if "superviseurs des ventes" in title
        )
        stock_manager = next(
            scope for title, scope in scopes.items()
            if "gestionnaire de stock" in title
        )

        pre_details = processor._extract_source_grounded_recruitment_details(pre_vendor)
        supervisor_details = processor._extract_source_grounded_recruitment_details(sales_supervisor)
        stock_details = processor._extract_source_grounded_recruitment_details(stock_manager)

        self.assertIn("20 et 30", pre_details["conditions"]["age"])
        self.assertIn("permis moto", processor.normalize_text(pre_details["conditions"]["driving_license"]))
        self.assertNotIn("conditions", supervisor_details) or self.assertNotIn(
            "age", supervisor_details.get("conditions", {})
        )
        self.assertNotIn("20 et 30", supervisor_details.get("conditions", {}).get("age", ""))
        self.assertIn("25 et 30", stock_details["conditions"]["age"])
        self.assertIn("Lieu de résidence", stock_details["candidate_residence"]["evidence"])
        self.assertNotIn("20 et 30", stock_details["conditions"]["age"])



if __name__ == "__main__":
    unittest.main()
