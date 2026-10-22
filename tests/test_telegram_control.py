import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("GROQ_API_KEY", "validation-dummy-key")
os.environ.setdefault("GMAIL_APP_PASSWORD", "validation-dummy-password")
os.environ.setdefault("GMAIL_ADDRESS", "validation@example.org")
os.environ.setdefault("JOBFINDER_CANDIDATE_PROFILE", "validation profile")
os.environ.setdefault("JOBFINDER_CANDIDATE_NAME", "Validation User")

os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_BOT_CHAT_ID"] = "12345"

from telegram_control import TelegramJobBot, event_key, main_menu_keyboard, now_local
from cloud_runner import (
    provider_cooldown_remaining_seconds,
    record_ai_provider_status,
    require_ai_provider,
)
import auto_jobfinder_cloud as processor
from auto_jobfinder_cloud import log_job_result_safely


class FakeProcessor:
    def __init__(self, status="sent"):
        self.status = status
        self.calls = []

    def send_approved_review(self, item):
        self.calls.append(item)
        return {"status": self.status}


def sample_event(decision="REVIEW"):
    record = {
        "source": "rcrdz1",
        "message_id": 77,
        "channel": "rcrdz1",
        "job_title": "Technicien de Maintenance",
        "application_job_title": "Technicien de Maintenance",
        "company": "Test Company",
        "location": "Oran",
        "decision": decision,
        "score": 75,
        "reason": "Test reason",
        "emails": ["jobs@example.org"],
        "cv": "/tmp/cv_ats_fr.pdf",
        "location_status": "allowed_local",
        "contact_scope_matched": True,
        "sent": False,
        "job_duplicate": False,
    }
    return {
        "post": {
            "source": "rcrdz1",
            "message_id": 77,
            "text": "Technicien de Maintenance — Oran — jobs@example.org",
        },
        "record": record,
        "job": {"job_title": "Technicien de Maintenance", "company": "Test Company"},
        "advertisement": "Technicien de Maintenance — Oran — jobs@example.org",
    }


class TelegramControlTests(unittest.TestCase):
    def build_bot(self, processor=None, allow_review_actions=True):
        bot = TelegramJobBot(
            {}, processor or FakeProcessor(), lambda _reason: None,
            allow_review_actions=allow_review_actions,
        )
        sent_messages = []
        bot._send_message = lambda chat, text, keyboard=None: (
            sent_messages.append((str(chat), text, keyboard)) or {"message_id": 42}
        )
        bot._answer_callback = lambda *args, **kwargs: None
        bot._edit_message = lambda *args, **kwargs: None
        return bot, sent_messages

    def test_start_displays_persistent_arabic_main_menu(self):
        bot, sent = self.build_bot()
        bot._handle_message({
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "text": "/start",
        })
        self.assertEqual(len(sent), 1)
        self.assertIn("مرحبًا بك في JobFinder", sent[0][1])
        keyboard = sent[0][2]
        self.assertTrue(keyboard["is_persistent"])
        self.assertTrue(keyboard["resize_keyboard"])
        labels = [button["text"] for row in keyboard["keyboard"] for button in row]
        self.assertIn("📥 المراجعات المعلقة", labels)
        self.assertIn("📨 طلبات اليوم", labels)
        self.assertIn("📊 ملخص اليوم", labels)
        self.assertIn("📡 حالة النظام", labels)
        self.assertNotIn("▶️ استئناف", labels)

    def test_main_menu_keyboard_uses_single_state_aware_toggle_action(self):
        active = [button["text"] for row in main_menu_keyboard(False)["keyboard"] for button in row]
        paused = [button["text"] for row in main_menu_keyboard(True)["keyboard"] for button in row]
        self.assertIn("⏯ تشغيل/إيقاف", active)
        self.assertIn("⏯ تشغيل/إيقاف", paused)
        for labels in (active, paused):
            self.assertNotIn("⏸ إيقاف مؤقت", labels)
            self.assertNotIn("▶️ استئناف", labels)

    def test_toggle_menu_button_switches_saved_runtime_state(self):
        bot, sent = self.build_bot()
        message = {
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "text": "⏯ تشغيل/إيقاف",
        }
        bot._handle_message(message)
        self.assertTrue(bot.state["paused"])
        self.assertIn("أوقفت معالجة الإعلانات", sent[-1][1])
        bot._handle_message(message)
        self.assertFalse(bot.state["paused"])
        self.assertIn("استؤنفت معالجة الإعلانات", sent[-1][1])

    def test_sent_today_lists_only_records_with_todays_algeria_timestamp(self):
        bot, sent = self.build_bot()
        today = now_local().date().isoformat()
        bot.state["sent_applications_jsonl"] = "\n".join([
            json.dumps({
                "key": "today",
                "sent_at": f"{today}T10:45:00+01:00",
                "application_job_title": "Technicien de Maintenance",
                "company": "Today Company",
                "location": "Oran",
                "email": "today@example.org",
                "cv": "/runner/cvs/cv_ats_fr.pdf",
            }),
            json.dumps({
                "key": "old",
                "sent_at": "2020-01-01T10:45:00+01:00",
                "application_job_title": "Old Job",
                "company": "Old Company",
            }),
            json.dumps({
                "key": "legacy",
                "application_job_title": "Legacy Job",
            }),
        ])
        bot._handle_message({
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "text": "/sent",
        })
        self.assertEqual(len(sent), 1)
        self.assertIn("Technicien de Maintenance", sent[0][1])
        self.assertIn("Today Company", sent[0][1])
        self.assertNotIn("Old Job", sent[0][1])
        self.assertNotIn("Legacy Job", sent[0][1])
        self.assertIn("سجلات إرسال قديمة بلا توقيت محفوظ؛ ليست طلبات اليوم", sent[0][1])

    def test_sent_shows_legacy_undated_records_when_no_dated_records_exist(self):
        bot, sent = self.build_bot()
        bot.state["sent_applications_jsonl"] = json.dumps({
            "key": "legacy",
            "application_job_title": "Legacy Job",
            "company": "Legacy Company",
        })
        bot._handle_message({
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "text": "/sent",
        })
        self.assertEqual(len(sent), 1)
        self.assertIn("لا توجد طلبات مسجلة اليوم بوقت إرسال محفوظ", sent[0][1])
        self.assertIn("سجلات إرسال قديمة بلا توقيت: 1", sent[0][1])
        self.assertIn("محفوظة فقط لمنع تكرار التقديم", sent[0][1])
        self.assertNotIn("Legacy Job", sent[0][1])
        self.assertNotIn("Legacy Company", sent[0][1])
        self.assertNotIn("أول سجلات قديمة", sent[0][1])
        self.assertNotIn("تُعرض أول 5 سجلات", sent[0][1])

    def test_daily_summary_distinguishes_reviews_and_manual_rejections(self):
        bot, sent = self.build_bot()
        today = now_local().date().isoformat()
        bot.state["daily_stats"] = {
            "date": today,
            "analyzed": 155,
            "sent": 0,
            "review": 4,
            "rejected": 151,
            "manual_rejected": 4,
            "duplicates": 0,
            "send_failed": 0,
            "manual_rejections_reconciled": True,
        }
        bot._handle_message({
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "text": "/today",
        })
        report = sent[-1][1]
        self.assertIn("الوظائف التي جرى تقييمها اليوم: 155", report)
        self.assertIn("مراجعات جديدة اليوم: 4", report)
        self.assertIn("المرفوضة تلقائيًا اليوم: 151", report)
        self.assertIn("مراجعات رُفضت يدويًا اليوم: 4", report)
        self.assertNotIn("ملاحظة:", report)
        self.assertNotIn("الرفض اليدوي يُحسب منفصلًا", report)
        self.assertNotIn("السجلات القديمة بلا توقيت لا تُنسب", report)

    def test_start_dashboard_displays_key_metrics_and_persistent_menu(self):
        bot, sent = self.build_bot()
        bot._handle_message({
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "text": "/start",
        })
        self.assertEqual(len(sent), 1)
        self.assertIn("لوحة التحكم", sent[0][1])
        self.assertIn("المراجعات المعلقة", sent[0][1])
        self.assertIn("طلبات مسجلة اليوم", sent[0][1])
        self.assertIn("سجلات إرسال قديمة بلا توقيت", sent[0][1])
        self.assertTrue(sent[0][2]["is_persistent"])

    def test_main_menu_button_routes_to_status_command(self):
        bot, sent = self.build_bot()
        bot._handle_message({
            "chat": {"id": 12345, "type": "private"},
            "from": {"id": 12345},
            "text": "📡 حالة النظام",
        })
        self.assertEqual(len(sent), 1)
        self.assertIn("حالة JobFinder", sent[0][1])
        self.assertTrue(sent[0][2]["is_persistent"])

    def test_candidate_fit_gate_rejects_low_score_and_unrelated_roles(self):
        cases = [
            ({"job_title": "Agent de sécurité", "score": 0}, "REVIEW"),
            ({"job_title": "Spécialiste en toxicologie", "score": 60}, "REVIEW"),
            ({"job_title": "Commercial(e)", "score": 91}, "APPLY"),
            ({"job_title": "Agents Polyvalents", "score": 88}, "APPLY"),
        ]
        for job, decision in cases:
            with self.subTest(job=job["job_title"]):
                final, reason = processor.enforce_candidate_fit_rule(job, decision)
                self.assertEqual(final, "REJECT")
                self.assertTrue(reason)

    def test_borderline_cnc_rejection_without_explicit_requirement_becomes_review(self):
        job = {
            "job_title": "Opérateur de Machine CNC",
            "score": 30,
            "reason": (
                "The role requires professional experience or training in CNC "
                "machining, which the candidate lacks."
            ),
        }
        advertisement = (
            "Recrutement: Opérateur de Machine CNC. "
            "Envoyez votre CV à jobs@example.org."
        )

        final, reason = processor.enforce_candidate_fit_rule(
            job, "REJECT", advertisement
        )

        self.assertEqual(final, "REVIEW")
        self.assertIn("مراجعة بشرية", reason)
        self.assertIn("مراجعة بشرية", reason)

    def test_explicit_mandatory_experience_can_keep_relevant_role_rejected(self):
        job = {
            "job_title": "Opérateur de Machine CNC",
            "score": 30,
            "reason": (
                "The role requires professional experience in CNC machining "
                "and the candidate lacks it."
            ),
        }
        advertisement = (
            "Recrutement: Opérateur de Machine CNC. "
            "Expérience professionnelle obligatoire, minimum 2 ans en usinage CNC."
        )

        final, _reason = processor.enforce_candidate_fit_rule(
            job, "REJECT", advertisement
        )

        self.assertEqual(final, "REJECT")

    def test_relevant_role_with_low_score_is_manual_review_not_auto_rejection(self):
        job = {
            "job_title": "Technicien de maintenance",
            "score": 25,
            "reason": "The candidate's experience is not confirmed.",
        }

        final, reason = processor.enforce_candidate_fit_rule(
            job, "APPLY", "Technicien de maintenance — Oran."
        )

        self.assertEqual(final, "REVIEW")
        self.assertTrue(reason)

    def test_senior_technical_role_remains_rejected(self):
        final, _reason = processor.enforce_candidate_fit_rule(
            {
                "job_title": "Chef d'atelier tuyauterie",
                "score": 20,
                "reason": "The candidate lacks leadership experience.",
            },
            "REJECT",
            "Recrutement: Chef d'atelier tuyauterie.",
        )

        self.assertEqual(final, "REJECT")

    def test_cv_keyword_match_scores_mechanical_maintenance_at_least_thirty(self):
        score, groups = processor.calculate_candidate_cv_match(
            {"job_title": "Technicien de maintenance"},
            "Maintenance des équipements, pompes centrifuges, systèmes fluides et plans techniques.",
        )
        self.assertGreaterEqual(score, 30)
        self.assertIn("industrial_maintenance", groups)
        self.assertIn("pumps_fluids_hydraulics", groups)

        final, reason = processor.enforce_candidate_fit_rule(
            {
                "job_title": "Technicien de maintenance",
                "score": 5,
                "reason": "Professional experience is not confirmed.",
            },
            "REJECT",
            "Poste de technicien de maintenance des équipements.",
            cv_match_score=score,
        )
        self.assertEqual(final, "REVIEW")
        self.assertIn("مراجعة بشرية", reason)
        self.assertNotEqual(final, "APPLY")

    def test_engineer_title_is_rejected_even_when_mechanical_keywords_overlap(self):
        titles = [
            "Ingénieur Électromécanique",
            "Ingénieur Génie Mécanique",
            "Ingénieur Génie Civil",
            "Ingénieur Électricité",
            "Ingénieur Irrigation et Hydraulique",
            "Ingénieur HSE",
            "Aircraft Maintenance Engineer",
        ]
        for title in titles:
            with self.subTest(title=title):
                score, groups = processor.calculate_candidate_cv_match(
                    {"job_title": title},
                    "Maintenance, pumps, fluids, hydraulics, industrial equipment, energy.",
                )
                self.assertEqual(score, 0)
                self.assertEqual(groups, [])
                final, reason = processor.enforce_candidate_fit_rule(
                    {"job_title": title, "score": 80},
                    "REVIEW",
                    "Diplôme Licence/Bac+3 en génie mécanique.",
                )
                self.assertEqual(final, "REJECT")
                self.assertIn("Licence/Bac+3", reason)

    def test_electrical_and_electromechanical_trades_are_excluded(self):
        titles = [
            "Électricien Industriel",
            "Electrical Technician",
            "Électromécanicien",
            "Ingénieur Électromécanique",
        ]
        for title in titles:
            with self.subTest(title=title):
                final, _reason = processor.enforce_candidate_fit_rule(
                    {"job_title": title, "score": 90},
                    "APPLY",
                    "Maintenance, industrial equipment and pumps.",
                )
                self.assertEqual(final, "REJECT")

    def test_stock_and_laboratory_positions_are_excluded_despite_shared_post_keywords(self):
        for title in ("Gestionnaire des Stocks", "Technologist – Laboratory, Assoc"):
            with self.subTest(title=title):
                score, groups = processor.calculate_candidate_cv_match(
                    {"job_title": title},
                    "Industrial maintenance, pumps, fluids, production and mechanical equipment.",
                )
                self.assertEqual(score, 0)
                self.assertEqual(groups, [])
                final, _reason = processor.enforce_candidate_fit_rule(
                    {"job_title": title, "score": 95},
                    "REVIEW",
                    "Industrial maintenance, pumps, fluids, production and mechanical equipment.",
                )
                self.assertEqual(final, "REJECT")

    def test_six_months_at_least_arabic_is_hard_rejection_even_if_ai_says_review(self):
        title = "Service Operator I – Surface Solutions / Baroid"
        advertisement = (
            "Service Operator I – Surface Solutions / Baroid\\n"
            "المستوى المطلوب: شهادة ثانوية أو ما يعادلها.\\n"
            "الخبرة: 6 أشهر على الأقل في المجال.\\n"
            "تشغيل ومتابعة معدات معالجة سوائل الحفر."
        )
        self.assertTrue(
            processor._advertisement_explicitly_requires_missing_qualification(
                {"job_title": title}, advertisement
            )
        )
        final, reason = processor.enforce_candidate_fit_rule(
            {"job_title": title, "score": 30},
            "REVIEW",
            advertisement,
        )
        self.assertEqual(final, "REJECT")
        self.assertIn("شرط خبرة", reason)

    def test_french_two_to_five_year_requirement_is_hard_rejection(self):
        advertisement = (
            "Technicien Maintenance\\n"
            "Expérience pratique de 2 à 5 ans dans un environnement industriel."
        )
        self.assertTrue(
            processor._advertisement_explicitly_requires_missing_qualification(
                {"job_title": "Technicien Maintenance"}, advertisement
            )
        )

    def test_combined_ad_resolves_position_blocks_without_borrowing_other_job_experience(self):
        advertisement = """SARL BATELEC – recrutement de plusieurs postes
1️⃣ Opérateur de Ligne
Missions: conduite de ligne, contrôle qualité, nettoyage et maintenance de premier niveau.
Niveau: CAP/BEP ou bac professionnel technique.
Expérience: débutant accepté.
2️⃣ Gestionnaire des Stocks
Missions: inventaire et gestion du stock.
Expérience pratique de 2 à 5 ans en logistique.
3️⃣ Technicien Maintenance
"""
        operator = {
            "job_title": "Opérateur de Ligne",
            "application_title_fr": "Opérateur de Ligne",
        }
        stocks = {
            "job_title": "Gestionnaire des Stocks",
            "application_title_fr": "Gestionnaire des Stocks",
        }
        maintenance = {
            "job_title": "Technicien Maintenance",
            "application_title_fr": "Technicien Maintenance",
        }
        jobs = [operator, stocks, maintenance]
        op_text, op_ok, _ = processor.resolve_job_position_scope(operator, advertisement, jobs)
        stock_text, stock_ok, _ = processor.resolve_job_position_scope(stocks, advertisement, jobs)
        maint_text, maint_ok, _ = processor.resolve_job_position_scope(maintenance, advertisement, jobs)

        self.assertTrue(op_ok)
        self.assertTrue(stock_ok)
        self.assertTrue(maint_ok)
        self.assertIn("debutant accepte", processor.normalize_text(op_text))
        self.assertNotIn("2 a 5 ans", processor.normalize_text(op_text))
        self.assertIn("2 a 5 ans", processor.normalize_text(stock_text))
        self.assertNotIn("2 a 5 ans", processor.normalize_text(maint_text))
        self.assertFalse(
            processor._advertisement_explicitly_requires_missing_qualification(
                maintenance, maint_text
            )
        )

    def test_no_experience_required_must_not_be_treated_as_a_hard_mismatch(self):
        self.assertFalse(
            processor._advertisement_explicitly_requires_missing_qualification(
                {
                    "job_title": "Junior Maintenance Technician",
                    "reason": "The candidate has no professional experience.",
                },
                "Débutant accepté. Aucune expérience professionnelle exigée.",
            )
        )

    def test_unrelated_mandatory_licence_does_not_make_at_ex_a_hard_mismatch(self):
        self.assertFalse(
            processor._advertisement_explicitly_requires_missing_qualification(
                {
                    "job_title": "Technicien de maintenance",
                    "reason": "ATEX is not listed in the CV.",
                },
                "Expérience appréciée. Certification ATEX souhaitée; permis de conduire obligatoire.",
            )
        )

    def test_licence_requirement_is_not_mistaken_for_missing_engineer_degree(self):
        self.assertFalse(
            processor._advertisement_explicitly_requires_missing_qualification(
                {
                    "job_title": "Technicien mécanique",
                    "reason": "The model says a qualification is missing.",
                },
                "Licence/Bac+3 en génie mécanique obligatoire.",
            )
        )

    def test_explicit_master_or_engineer_state_diploma_is_a_hard_mismatch(self):
        self.assertTrue(
            processor._advertisement_explicitly_requires_missing_qualification(
                {
                    "job_title": "Ingénieur en mécanique",
                    "reason": "The candidate has only Licence/Bac+3.",
                },
                "Diplôme d'ingénieur d'État ou Master/Bac+5 obligatoire.",
            )
        )

    def test_unspecified_experience_is_not_a_hard_mismatch(self):
        self.assertFalse(
            processor._advertisement_explicitly_requires_missing_qualification(
                {
                    "job_title": "Technicien de maintenance",
                    "reason": "Professional experience could not be verified.",
                },
                "Recrutement d'un technicien de maintenance industrielle.",
            )
        )

    def test_residency_wording_is_not_employer_provided_housing(self):
        advertisement = (
            "Poste basé à Alger. Candidats résidant dans la wilaya d'Alger exigés."
        )
        self.assertIsNone(processor.accommodation_signal_in_text(advertisement))
        self.assertEqual(
            processor.scoped_accommodation_value(
                {"accommodation_provided": "true"},
                advertisement,
                has_multiple_sections=False,
            ),
            "unknown",
        )
        decision, status, _reason = processor.enforce_location_rule(
            {
                "location": "Alger",
                "accommodation_provided": "unknown",
            },
            "APPLY",
            advertisement,
        )
        self.assertEqual(decision, "REJECT")
        self.assertEqual(status, "rejected_location")

    def test_explicitly_provided_housing_allows_other_algerian_location(self):
        advertisement = "Poste à Alger; logement fourni et pris en charge par l'employeur."
        self.assertTrue(processor.accommodation_signal_in_text(advertisement))
        housing = processor.scoped_accommodation_value(
            {"accommodation_provided": "unknown"},
            advertisement,
            has_multiple_sections=False,
        )
        self.assertEqual(housing, "true")
        decision, status, _reason = processor.enforce_location_rule(
            {"location": "Alger", "accommodation_provided": housing},
            "REVIEW",
            advertisement,
        )
        self.assertEqual(decision, "REVIEW")
        self.assertEqual(status, "allowed_housing")

    def test_unscoped_multi_position_location_routes_apply_to_review(self):
        decision, status, reason = processor.enforce_location_rule(
            {
                "location": "Alger",
                "accommodation_provided": "unknown",
            },
            "APPLY",
            "",
            section_verified=False,
        )

        self.assertEqual(decision, "REVIEW")
        self.assertEqual(status, "location_section_unverified")
        self.assertIn("automatic sending is disabled", reason)

    def test_no_housing_provided_in_french_is_not_a_positive_signal(self):
        self.assertFalse(
            processor.accommodation_signal_in_text(
                "Poste à Alger, aucun logement fourni par l'employeur."
            )
        )
        self.assertFalse(
            processor.accommodation_signal_in_text(
                "Poste à Alger, hébergement non assuré."
            )
        )

    def test_ai_claimed_housing_without_explicit_ad_evidence_is_ignored(self):
        advertisement = "Poste basé à Alger. Résidence exigée pour les candidats."
        housing = processor.scoped_accommodation_value(
            {"accommodation_provided": "true"},
            advertisement,
            has_multiple_sections=False,
        )
        self.assertEqual(housing, "unknown")
        decision, status, _reason = processor.enforce_location_rule(
            {"location": "Alger", "accommodation_provided": housing},
            "APPLY",
            advertisement,
        )
        self.assertEqual(decision, "REJECT")
        self.assertEqual(status, "rejected_location")

    def test_candidate_fit_gate_keeps_strong_mechanical_role(self):
        final, reason = processor.enforce_candidate_fit_rule(
            {"job_title": "Junior Mechanical Maintenance Technician", "score": 75},
            "APPLY",
        )
        self.assertEqual(final, "APPLY")
        self.assertEqual(reason, "")

    def test_generic_technician_title_cannot_auto_send(self):
        final, reason = processor.enforce_candidate_fit_rule(
            {"job_title": "Technicien", "score": 75},
            "APPLY",
        )
        self.assertEqual(final, "REVIEW")
        self.assertTrue(reason)

    def test_review_is_queued_and_shows_controls(self):
        bot, sent = self.build_bot()
        bot.on_job_event(sample_event())
        self.assertEqual(len(bot.state["pending_reviews"]), 1)
        self.assertEqual(len(sent), 1)
        keyboard = sent[0][2]["inline_keyboard"][0]
        self.assertIn("jf:approve:", keyboard[0]["callback_data"])
        self.assertIn("jf:reject:", keyboard[1]["callback_data"])

    def test_apply_duplicate_increments_daily_duplicate_counter(self):
        bot, _ = self.build_bot()
        event = sample_event("APPLY")
        event["record"]["job_duplicate"] = True
        bot.on_job_event(event)
        stats = bot._today_stats()
        self.assertEqual(stats["analyzed"], 1)
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["send_failed"], 0)
        self.assertEqual(stats["sent"], 0)

    def test_same_day_legacy_stats_migrate_five_unclassified_apply_duplicates(self):
        bot, _ = self.build_bot()
        today = now_local().date().isoformat()
        bot.state["daily_stats"] = {
            "date": today,
            "analyzed": 109,
            "sent": 0,
            "review": 9,
            "rejected": 95,
            "send_failed": 0,
        }
        stats = bot._today_stats()
        self.assertEqual(stats["duplicates"], 5)
        self.assertEqual(
            stats["sent"] + stats["review"] + stats["rejected"]
            + stats["send_failed"] + stats["duplicates"],
            stats["analyzed"],
        )

    def test_manual_review_rejection_does_not_inflate_automatic_rejects(self):
        bot, _ = self.build_bot()
        bot.on_job_event(sample_event("REVIEW"))
        pending_id = next(iter(bot.state["pending_reviews"]))
        before = dict(bot._today_stats())
        self.assertEqual(before["analyzed"], 1)
        self.assertEqual(before["review"], 1)
        self.assertEqual(before["rejected"], 0)

        bot._handle_callback({
            "id": "manual-reject-counter",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 42},
            "data": f"jf:reject:{pending_id}",
        })
        stats = bot._today_stats()
        self.assertEqual(stats["analyzed"], 1)
        self.assertEqual(stats["review"], 1)
        self.assertEqual(stats["rejected"], 0)
        self.assertEqual(stats["manual_rejected"], 1)

    def test_legacy_manual_rejections_are_reconciled_once(self):
        bot, _ = self.build_bot()
        today = now_local().date().isoformat()
        timestamp = now_local().isoformat(timespec="seconds")
        bot.state["daily_stats"] = {
            "date": today,
            "analyzed": 155,
            "sent": 0,
            "review": 4,
            "rejected": 155,
            "duplicates": 0,
            "send_failed": 0,
        }
        bot.state["review_history"] = {
            f"manual-{index}": {"status": "rejected", "at": timestamp}
            for index in range(4)
        }

        first = bot._today_stats()
        self.assertEqual(first["analyzed"], 155)
        self.assertEqual(first["review"], 4)
        self.assertEqual(first["rejected"], 151)
        self.assertEqual(first["manual_rejected"], 4)
        self.assertTrue(first["manual_rejections_reconciled"])

        second = bot._today_stats()
        self.assertEqual(second["rejected"], 151)
        self.assertEqual(second["manual_rejected"], 4)

    def test_known_non_job_review_is_dismissed_without_rejection_or_email(self):
        bot, _ = self.build_bot()
        event = sample_event("REVIEW")
        event["post"]["source"] = "ajob58dz"
        event["post"]["message_id"] = 7654
        event["record"]["source"] = "ajob58dz"
        event["record"]["message_id"] = 7654
        bot.on_job_event(event)
        pending_id = next(iter(bot.state["pending_reviews"]))
        edits = []
        bot._edit_message = lambda query, text: edits.append((query, text))

        dismissed = bot.dismiss_non_job_reviews_for_post(
            "ajob58dz",
            7654,
            "تصحيح: هذا المنشور ليس إعلان توظيف.",
        )

        self.assertEqual(dismissed, 1)
        self.assertNotIn(pending_id, bot.state["pending_reviews"])
        self.assertEqual(
            bot.state["review_history"][pending_id]["status"],
            "ignored_non_job",
        )
        self.assertEqual(bot.state.get("rejected_job_history", []), [])
        self.assertEqual(bot._today_stats()["rejected"], 0)
        self.assertEqual(bot._today_stats()["analyzed"], 0)
        self.assertEqual(bot._today_stats()["review"], 0)
        self.assertEqual(len(edits), 1)
        self.assertIn("ليس إعلان توظيف", edits[0][1])

    def test_reject_callback_resolves_review(self):
        bot, _ = self.build_bot()
        bot.on_job_event(sample_event())
        pending_id = next(iter(bot.state["pending_reviews"]))
        bot._handle_callback({
            "id": "callback-1",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 42},
            "data": f"jf:reject:{pending_id}",
        })
        self.assertNotIn(pending_id, bot.state["pending_reviews"])
        self.assertEqual(bot.state["review_history"][pending_id]["status"], "rejected")
        self.assertEqual(len(bot.state["rejected_job_history"]), 1)

    def test_previously_rejected_job_is_blocked_across_reposts(self):
        bot, sent = self.build_bot()
        first = sample_event("REVIEW")
        bot.on_job_event(first)
        pending_id = next(iter(bot.state["pending_reviews"]))
        bot._handle_callback({
            "id": "reject-first",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 42},
            "data": f"jf:reject:{pending_id}",
        })

        repost = sample_event("APPLY")
        repost["post"]["message_id"] = 99
        repost["record"]["message_id"] = 99
        reason = bot.pre_send_guard_for_job(
            repost["job"],
            repost["record"]["application_job_title"],
            repost["record"]["emails"],
            repost["post"],
        )
        self.assertIn("سبق رفضها", reason)
        before = len(sent)
        bot.on_job_event(repost)
        self.assertEqual(len(bot.state["pending_reviews"]), 0)
        self.assertEqual(len(sent), before)

    def test_unauthorized_user_cannot_approve(self):
        processor = FakeProcessor()
        bot, _ = self.build_bot(processor)
        bot.on_job_event(sample_event())
        pending_id = next(iter(bot.state["pending_reviews"]))
        bot._handle_callback({
            "id": "callback-2",
            "from": {"id": 99999},
            "message": {"chat": {"id": 12345}, "message_id": 42},
            "data": f"jf:approve:{pending_id}",
        })
        self.assertEqual(processor.calls, [])
        self.assertIn(pending_id, bot.state["pending_reviews"])

    def test_approved_application_is_processed_once_and_resolved(self):
        processor = FakeProcessor("sent")
        bot, _ = self.build_bot(processor)
        bot.on_job_event(sample_event())
        pending_id = next(iter(bot.state["pending_reviews"]))
        bot._handle_callback({
            "id": "callback-3",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 42},
            "data": f"jf:approve:{pending_id}",
        })
        self.assertEqual(len(processor.calls), 1)
        self.assertNotIn(pending_id, bot.state["pending_reviews"])
        self.assertEqual(bot.state["review_history"][pending_id]["status"], "sent")

    def test_public_workflow_logs_redact_job_identity_and_reasons(self):
        output = StringIO()
        record = {
            "job_title": "Highly Private Test Role",
            "application_job_title": "Confidential Application Title",
            "company": "Private Employer Name",
            "location": "Private City Name",
            "location_status": "allowed_local",
            "accommodation_provided": "provided",
            "decision": "REVIEW",
            "original_ai_decision": "APPLY",
            "score": 75,
            "language": "fr",
            "application_language": "fr",
            "application_language_reason": "Private employer instructions",
            "application_cv_language": "fr",
            "cv": "/private/cvs/private-cv.pdf",
            "emails": ["private-contact@example.org"],
            "job_duplicate": False,
            "sent": False,
            "reason": "Private candidate-specific rejection reason",
            "location_reason": "Private location details",
        }
        with redirect_stdout(output):
            log_job_result_safely(record, "Private contact review details")

        rendered = output.getvalue()
        for secret_detail in (
            "Highly Private Test Role",
            "Confidential Application Title",
            "Private Employer Name",
            "Private City Name",
            "private-contact@example.org",
            "Private candidate-specific rejection reason",
            "Private location details",
            "Private contact review details",
        ):
            self.assertNotIn(secret_detail, rendered)
        self.assertIn("DECISION: REVIEW", rendered)
        self.assertIn("SCORE: 75", rendered)
        self.assertIn("SCOPED EMAIL COUNT: 1", rendered)
        self.assertIn("SENT: False", rendered)

    def test_previously_dismissed_non_job_history_reconciles_today_counters_once(self):
        bot, _ = self.build_bot()
        today = now_local().date().isoformat()
        bot.state["review_history"]["old-false-review"] = {
            "status": "ignored_non_job",
            "at": now_local().isoformat(),
            "source": "ajob58dz",
            "message_id": "7654",
        }
        bot.state["daily_stats"] = {
            "date": today,
            "analyzed": 1,
            "sent": 0,
            "review": 1,
            "rejected": 0,
            "manual_rejected": 0,
            "duplicates": 0,
            "send_failed": 0,
            "manual_rejections_reconciled": True,
        }

        first = bot.dismiss_non_job_reviews_for_post(
            "ajob58dz",
            7654,
            "تصحيح: هذا المنشور ليس إعلان توظيف.",
        )
        self.assertEqual(first, 0)
        self.assertEqual(bot.state["daily_stats"]["analyzed"], 0)
        self.assertEqual(bot.state["daily_stats"]["review"], 0)
        self.assertTrue(
            bot.state["review_history"]["old-false-review"]["counters_reconciled"]
        )

        second = bot.dismiss_non_job_reviews_for_post(
            "ajob58dz",
            7654,
            "تصحيح: هذا المنشور ليس إعلان توظيف.",
        )
        self.assertEqual(second, 0)
        self.assertEqual(bot.state["daily_stats"]["analyzed"], 0)
        self.assertEqual(bot.state["daily_stats"]["review"], 0)

    def test_vacancy_signal_detects_french_and_arabic_job_posts(self):
        self.assertTrue(processor.looks_like_vacancy_ad(
            "OFFRE D’EMPLOI — Technicien de Maintenance à Oran"
        ))
        self.assertTrue(processor.looks_like_vacancy_ad(
            "إعلان توظيف: مطلوب تقني صيانة ميكانيكية"
        ))
        self.assertFalse(processor.looks_like_vacancy_ad(
            "Discussion générale sur les transports et la météo"
        ))
        self.assertFalse(processor.looks_like_vacancy_ad("عروض عمل"))
        self.assertFalse(processor.looks_like_vacancy_ad(
            "رايح نحط مجموعه عروض عمل تخص ولايات الشرق والغرب"
        ))


    def test_upcoming_job_offers_teaser_is_ignored_before_ai_or_review(self):
        # Regression for @rcrdz1/22650: the author promises to post offers later.
        post_text = (
            "الله يبارك، راكم اغلبيتكم جامعيين و من كل ولايات، "
            "شوفو خاوتي خواتاتي، رايح نحط مجموعه عروض عمل، تخص ولايات "
            "الشرق و الغرب و الوسط و الصحراء، نحتاج منكم التفاعل، "
            "كيما راكم تتفاعلو فالتعليقات، جاهزيين شباااب ♥؟"
        )
        self.assertTrue(processor.looks_like_non_job_job_teaser(post_text))
        self.assertFalse(processor.looks_like_vacancy_ad(post_text))

        post = {
            "source": "rcrdz1",
            "channel": "rcrdz1",
            "message_id": 22650,
            "text": post_text,
        }
        events = []
        with (
            patch.object(processor, "analyze_offer") as analyze,
            patch.object(processor, "save_result") as save_result,
            patch.object(processor, "JOB_EVENT_CALLBACK", new=events.append),
            patch.object(processor, "send_application") as send_application,
        ):
            ok = processor.process_post(post)

        self.assertTrue(ok)
        analyze.assert_not_called()
        save_result.assert_not_called()
        send_application.assert_not_called()
        self.assertEqual(events, [])

    def test_obvious_social_migration_post_is_not_sent_to_ai_or_review(self):
        post_text = (
            "عروض عمل\\n"
            "اللهم ☝️ في ليلة الجمعة لنا أصدقاء خرجو في قوارب 🚣الهجرة "
            "يوم الاثنين مازال مابان عليهم والو دعواتكم لهم ربي يسمع "
            "عائلاتهم خبر الخير 🤲"
        )
        self.assertFalse(processor.looks_like_vacancy_ad(post_text))
        self.assertTrue(processor.looks_like_non_job_social_post(post_text))
        self.assertFalse(processor.looks_like_non_job_social_post(
            "إعلان توظيف: مطلوب تقني صيانة ميكانيكية في وهران"
        ))

        post = {
            "source": "ajob58dz",
            "channel": "ajob58dz",
            "message_id": 7654,
            "text": post_text,
        }
        events = []
        with (
            patch.object(processor, "analyze_offer") as analyze,
            patch.object(processor, "save_result") as save_result,
            patch.object(processor, "JOB_EVENT_CALLBACK", new=events.append),
        ):
            ok = processor.process_post(post)

        self.assertTrue(ok)
        analyze.assert_not_called()
        save_result.assert_not_called()
        self.assertEqual(events, [])

    def test_cv_service_promotion_is_ignored_before_ai_or_review(self):
        post_text = (
            "✨ حاب تطوّر من روحك ومن مستواك المهني؟ 💼💵❤️\n"
            "حاب يكون عندك CV احترافي يفتحلك أبواب فرص جديدة؟ 📄🤍\n\n"
            "💡 مع Recrutement DZ، نرافقوك باش تطوّر ملفك المهني "
            "وتقدّم روحك بطريقة احترافية تليق بطموحاتك!\n\n"
            "📲 عندك هدف وحاب تبدأ أول خطوة؟ تواصل معنا عبر WhatsApp "
            "واكتشف خدماتنا! 💬\n\n"
            "Recrutement DZ 💙\n"
            "طوّر من نفسك، واستثمر في مستقبلك! ✨"
        )
        self.assertFalse(processor.looks_like_vacancy_ad(post_text))
        self.assertTrue(processor.looks_like_non_job_service_promotion(post_text))

        post = {
            "source": "rcrdz1",
            "channel": "rcrdz1",
            "message_id": 22606,
            "text": post_text,
        }
        events = []
        with (
            patch.object(processor, "analyze_offer") as analyze,
            patch.object(processor, "save_result") as save_result,
            patch.object(processor, "JOB_EVENT_CALLBACK", new=events.append),
        ):
            ok = processor.process_post(post)

        self.assertTrue(ok)
        analyze.assert_not_called()
        save_result.assert_not_called()
        self.assertEqual(events, [])

    def test_cv_discount_promotion_with_opportunity_slogan_is_not_a_job(self):
        # Regression for @rcrdz1/22642: generic "فرصة عمل أفضل" is marketing,
        # not proof that the publisher is hiring.
        post_text = (
            "📢✨ خبر سار لأعضاء جروب Recrutement DZ! 🎉💼\n\n"
            "❤️ لأن نجاحكم يهمّنا، حضّرنا لكم PROMOTION خاصة وتخفيضات على خدماتنا! 🎁🔥\n\n"
            "🌟 خدماتنا تشمل:\n"
            "📄 إنشاء وتصميم CV professionnel بالفرنسية أو الإنجليزية.\n"
            "☑️ إعداد CV متوافق مع أنظمة الفرز الآلي ATS.\n"
            "✍️ كتابة Lettre de motivation / Cover Letter.\n"
            "🔗 إنشاء وتحسين حساب LinkedIn.\n"
            "💻 المساعدة في إنشاء وتفعيل حسابات Emploitic وLinkedIn.\n"
            "🌍 المساعدة في التقديم على عروض العمل والمنح الدولية.\n"
            "📢 نشر السير الذاتية والمساعدة في الوصول إلى فرص عمل مناسبة.\n\n"
            "🥰 الحمد لله، ثقتكم ورضاكم هما أكبر حافز لينا باش نطوّروا خدماتنا "
            "ونقدّموا لكم الأفضل. شكرًا لكل من اختار Recrutement DZ! ❤️🤝\n\n"
            "🎯 ما تفوّتوش فرصة الاستفادة من التخفيضات!\n\n"
            "📲 للاستفسار وطلب الخدمات، تواصلوا معنا عبر واتساب:\n"
            "📞 0776 837 862\n"
            "💬 ابعثولنا رسالة ونعطوكم تفاصيل البروموسيون والخدمات المناسبة لاحتياجاتكم.\n\n"
            "❤️ Recrutement DZ 🇩🇿 — معكم نحو فرصة عمل أفضل! 🚀"
        )
        self.assertFalse(processor.looks_like_vacancy_ad(post_text))
        self.assertTrue(processor.looks_like_non_job_service_promotion(post_text))

        post = {
            "source": "rcrdz1",
            "channel": "rcrdz1",
            "message_id": 22642,
            "text": post_text,
        }
        events = []
        with (
            patch.object(processor, "analyze_offer") as analyze,
            patch.object(processor, "save_result") as save_result,
            patch.object(processor, "JOB_EVENT_CALLBACK", new=events.append),
            patch.object(processor, "send_application") as send_application,
        ):
            ok = processor.process_post(post)

        self.assertTrue(ok)
        analyze.assert_not_called()
        save_result.assert_not_called()
        send_application.assert_not_called()
        self.assertEqual(events, [])

    def test_service_promotion_filter_does_not_hide_an_actual_job_offer(self):
        actual_job = (
            "Offre d'emploi: Recrutement d'un Technicien de Maintenance "
            "à Oran. Envoyez votre CV pour postuler."
        )
        self.assertFalse(processor.looks_like_non_job_service_promotion(actual_job))

        arabic_actual_job = (
            "فرصة عمل: مطلوب مهندس صيانة في وهران. "
            "أرسل سيرتك الذاتية إلى jobs@example.org."
        )
        self.assertFalse(
            processor.looks_like_non_job_service_promotion(arabic_actual_job)
        )

    def test_extraction_fallback_card_is_not_presented_as_confirmed_job(self):
        bot, _sent = self.build_bot()
        event = sample_event("REVIEW")
        event["record"].update({
            "job_title": "إعلان توظيف يحتاج إلى مراجعة يدوية",
            "application_job_title": "إعلان توظيف يحتاج إلى مراجعة يدوية",
            "company": "",
            "location": "",
            "score": None,
            "analysis_fallback": True,
        })
        event["can_auto_send"] = False

        rendered = bot._review_text(event)

        self.assertIn("🟠 تعذّر تحليل إعلان توظيف", rendered)
        self.assertIn("لم يُستخرج المسمى الوظيفي", rendered)
        self.assertIn("لم تُستخرج بيانات الشركة", rendered)
        self.assertIn("لم يُستخرج الموقع", rendered)
        self.assertIn("هذه بطاقة استرداد للمراجعة فقط", rendered)
        self.assertNotIn("القبول سيرسل طلبًا بالبريد", rendered)

    def test_rejecting_one_fallback_card_does_not_hide_later_unparsed_ads(self):
        bot, _sent = self.build_bot()

        def fallback_event(message_id):
            event = sample_event("REVIEW")
            event["post"]["message_id"] = message_id
            event["record"]["message_id"] = message_id
            event["record"].update({
                "job_title": "إعلان توظيف يحتاج إلى مراجعة يدوية",
                "application_job_title": "إعلان توظيف يحتاج إلى مراجعة يدوية",
                "company": "",
                "location": "",
                "emails": ["jobs@example.org"],
                "cv": "",
                "location_status": "unknown_location",
                "contact_scope_matched": False,
                "analysis_fallback": True,
            })
            event["job"] = {
                "job_title": "إعلان توظيف يحتاج إلى مراجعة يدوية",
                "company": "",
                "location": "",
            }
            return event

        bot.on_job_event(fallback_event(100))
        first_pending_id = next(iter(bot.state["pending_reviews"]))
        bot._handle_callback({
            "id": "reject-fallback-1",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 42},
            "data": f"jf:reject:{first_pending_id}",
        })

        second = fallback_event(101)
        bot.on_job_event(second)

        self.assertEqual(second["record"]["decision"], "REVIEW")
        self.assertEqual(len(bot.state["pending_reviews"]), 1)
        remaining = next(iter(bot.state["pending_reviews"].values()))
        self.assertTrue(remaining["record"]["analysis_fallback"])

    def test_extraction_fallback_never_gets_auto_send_button_even_with_complete_fields(self):
        bot, _sent = self.build_bot()
        event = sample_event("REVIEW")
        # Simulate stale/populated fields so the explicit fallback guard is
        # exercised instead of relying on missing CV/location/contact details.
        event["record"]["analysis_fallback"] = True

        bot.on_job_event(event)
        pending_id = next(iter(bot.state["pending_reviews"]))
        item = bot.state["pending_reviews"][pending_id]
        keyboard = bot._review_keyboard(item)
        callback_data = [
            button["callback_data"]
            for row in keyboard["inline_keyboard"]
            for button in row
        ]

        self.assertFalse(item["can_auto_send"])
        self.assertIn(f"jf:manual:{pending_id}", callback_data)
        self.assertNotIn(f"jf:approve:{pending_id}", callback_data)

    def test_empty_ai_extraction_is_retried_and_queued_for_manual_review(self):
        # Only explicit recruitment-like text may use the empty-extraction fallback.
        post = {
            "source": "rcrdz1",
            "channel": "rcrdz1",
            "message_id": 7654,
            "date": "2026-10-15T22:21:14+00:00",
            "text": "إعلان توظيف: Technicien de Maintenance mécanique — Alger. CV: jobs@example.org",
        }
        events = []
        with (
            patch.object(
                processor,
                "analyze_offer",
                side_effect=[{"jobs": []}, {"jobs": []}],
            ) as analyze,
            patch.object(processor, "save_result") as save_result,
            patch.object(processor, "JOB_EVENT_CALLBACK", new=events.append),
            patch.object(processor, "send_application") as send_application,
        ):
            ok = processor.process_post(post)

        self.assertTrue(ok)
        self.assertEqual(analyze.call_count, 2)
        self.assertFalse(analyze.call_args_list[0].kwargs.get("vacancy_rescue", False))
        self.assertTrue(analyze.call_args_list[1].kwargs["vacancy_rescue"])
        save_result.assert_called_once()
        send_application.assert_not_called()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["post"]["message_id"], 7654)
        fallback = events[0]["record"]
        self.assertEqual(fallback["decision"], "REVIEW")
        self.assertEqual(fallback["original_ai_decision"], "REVIEW")
        self.assertIsNone(fallback["score"])
        self.assertFalse(fallback["sent"])
        self.assertFalse(fallback["contact_scope_matched"])
        self.assertEqual(fallback["emails"], ["jobs@example.org"])
        self.assertIn("لن يُرسل أي بريد تلقائي", fallback["reason"])

    def test_provider_rate_limit_during_vacancy_rescue_keeps_post_for_retry(self):
        post = {
            "source": "rcrdz1",
            "channel": "rcrdz1",
            "message_id": 7655,
            "date": "2026-10-19T12:00:00+00:00",
            "text": "إعلان توظيف: Technicien de Maintenance mécanique — Alger. CV: jobs@example.org",
        }
        events = []
        with (
            patch.object(
                processor,
                "analyze_offer",
                side_effect=[
                    {"jobs": []},
                    processor.AIProviderRateLimited(240),
                ],
            ) as analyze,
            patch.object(processor, "save_result") as save_result,
            patch.object(processor, "JOB_EVENT_CALLBACK", new=events.append),
            patch.object(processor, "send_application") as send_application,
        ):
            ok = processor.process_post(post)

        self.assertFalse(ok)
        self.assertEqual(analyze.call_count, 2)
        save_result.assert_not_called()
        send_application.assert_not_called()
        self.assertEqual(events, [])

    def test_event_key_is_stable(self):
        record = sample_event()["record"]
        self.assertEqual(event_key(record), event_key(dict(record)))

    def test_webhook_test_button_edits_message_without_real_job_action(self):
        processor = FakeProcessor()
        bot, _ = self.build_bot(processor)
        edits = []
        bot._edit_message = lambda query, text: edits.append((query, text))
        bot.handle_webhook_event({
            "update_type": "callback",
            "action": "test",
            "pending_id": "accept",
            "chat_id": "12345",
            "user_id": "12345",
            "message_id": "42",
        })
        self.assertEqual(processor.calls, [])
        self.assertTrue(any("نجح اختبار زر القبول" in text for _, text in edits))

    def test_webhook_status_command_replies_to_owner(self):
        bot, sent = self.build_bot()
        bot.handle_webhook_event({
            "update_type": "command",
            "command": "/status",
            "chat_id": "12345",
            "user_id": "12345",
        })
        self.assertTrue(any("حالة JobFinder" in text for _, text, _ in sent))

    def test_webhook_event_rejects_unauthorized_user(self):
        bot, sent = self.build_bot()
        bot.handle_webhook_event({
            "update_type": "command",
            "command": "/status",
            "chat_id": "12345",
            "user_id": "99999",
        })
        self.assertEqual(sent, [])

    def test_empty_callback_id_is_not_answered_twice(self):
        bot, _ = self.build_bot()
        with patch.object(bot, "_api") as api:
            TelegramJobBot._answer_callback(bot, "", "already acknowledged")
        api.assert_not_called()

    def test_test_accept_button_never_calls_email_sender(self):
        processor = FakeProcessor()
        bot, _ = self.build_bot(processor, allow_review_actions=False)
        bot._handle_callback({
            "id": "test-callback-accept",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 42},
            "data": "jf:test:accept",
        })
        self.assertEqual(processor.calls, [])
        self.assertEqual(bot.state["pending_reviews"], {})

    def test_test_reject_button_never_changes_real_review_state(self):
        processor = FakeProcessor()
        bot, _ = self.build_bot(processor, allow_review_actions=False)
        bot._handle_callback({
            "id": "test-callback-reject",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 43},
            "data": "jf:test:reject",
        })
        self.assertEqual(processor.calls, [])
        self.assertEqual(bot.state["pending_reviews"], {})
        self.assertEqual(bot.state["review_history"], {})

    def test_real_approval_is_blocked_in_test_only_mode(self):
        processor = FakeProcessor()
        bot, _ = self.build_bot(processor, allow_review_actions=False)
        bot.state["pending_reviews"]["real-review"] = {
            "id": "real-review",
            "can_auto_send": True,
            "record": {"application_job_title": "Test"},
        }
        bot._handle_callback({
            "id": "test-callback-real",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}, "message_id": 44},
            "data": "jf:approve:real-review",
        })
        self.assertEqual(processor.calls, [])
        self.assertIn("real-review", bot.state["pending_reviews"])

    def sample_review_item(self, can_auto_send=True):
        event = sample_event()
        return {
            "id": "review123",
            "can_auto_send": can_auto_send,
            "record": event["record"],
            "job": event["job"],
            "post": event["post"],
            "advertisement": event["advertisement"],
        }

    def test_processor_blocks_unsafe_review_before_any_send(self):
        with self.assertRaisesRegex(RuntimeError, "not safe"):
            processor.send_approved_review(self.sample_review_item(False))

    def test_processor_skips_already_sent_duplicate(self):
        item = self.sample_review_item(True)
        with (
            patch.object(processor.os.path, "isfile", return_value=True),
            patch.object(processor, "already_sent_same_job", return_value=(True, {})),
            patch.object(processor, "already_sent", return_value=False),
            patch.object(processor, "send_application") as send,
            patch.object(processor, "save_sent"),
            patch.object(processor, "save_result"),
        ):
            result = processor.send_approved_review(item)
        self.assertEqual(result["status"], "duplicate")
        send.assert_not_called()

    def test_processor_sends_only_after_validated_approval(self):
        item = self.sample_review_item(True)
        with (
            patch.object(processor.os.path, "isfile", return_value=True),
            patch.object(processor, "already_sent_same_job", return_value=(False, None)),
            patch.object(processor, "already_sent", return_value=False),
            patch.object(processor, "send_application") as send,
            patch.object(processor, "save_sent") as save_sent,
            patch.object(processor, "save_result"),
        ):
            result = processor.send_approved_review(item)
        self.assertEqual(result["status"], "sent")
        send.assert_called_once()
        save_sent.assert_called_once()




class JobIdentityAndGroqFallbackTests(unittest.TestCase):
    def test_singular_and_plural_technician_titles_share_identity_tokens(self):
        self.assertEqual(
            processor.canonical_title_tokens("Technicien"),
            processor.canonical_title_tokens("Techniciens"),
        )

    def test_singular_and_plural_mechanic_titles_detect_same_sent_job(self):
        email = "hmd.recruit.30@gmail.com"
        old_job = {
            "company": "",
            "location": "Hassi Messaoud, Ouargla, Algeria",
        }
        old_record = {
            "application_job_title": "Mécanicien",
            "email": email,
            "company": "",
            "location": "Hassi Messaoud, Ouargla, Algeria",
            "identity_key": processor.job_identity_key(
                old_job, "Mécanicien", email
            ),
        }
        current_job = {"company": "", "location": "Hassi Messaoud"}

        self.assertTrue(
            processor.same_job_identity(
                current_job, "Mécaniciens", email, old_record
            )
        )

    def test_same_job_is_deduplicated_when_sent_within_48_hours(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        email = "recruitment@example.com"
        job = {"company": "Example Energy", "location": "Oran"}
        sent_at = (
            datetime.now(ZoneInfo("Africa/Algiers")) - timedelta(hours=24)
        ).isoformat(timespec="seconds")
        old_record = {
            "application_job_title": "Technicien de Maintenance",
            "email": email,
            "company": "Example Energy",
            "location": "Oran",
            "sent_at": sent_at,
        }

        with patch.object(processor, "load_sent_records", return_value=[old_record]):
            duplicate, matched = processor.already_sent_same_job(
                job, "Technicien de Maintenance", [email]
            )

        self.assertTrue(duplicate)
        self.assertEqual(matched, old_record)

    def test_same_job_reposted_after_one_week_is_not_blocked_forever(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        email = "recruitment@example.com"
        job = {"company": "Example Energy", "location": "Oran"}
        sent_at = (
            datetime.now(ZoneInfo("Africa/Algiers")) - timedelta(days=7)
        ).isoformat(timespec="seconds")
        old_record = {
            "application_job_title": "Technicien de Maintenance",
            "email": email,
            "company": "Example Energy",
            "location": "Oran",
            "sent_at": sent_at,
        }

        with patch.object(processor, "load_sent_records", return_value=[old_record]):
            duplicate, matched = processor.already_sent_same_job(
                job, "Technicien de Maintenance", [email]
            )

        self.assertFalse(duplicate)
        self.assertIsNone(matched)

    def test_legacy_sent_record_without_timestamp_does_not_block_new_repost(self):
        email = "recruitment@example.com"
        job = {"company": "Example Energy", "location": "Oran"}
        old_record = {
            "application_job_title": "Technicien de Maintenance",
            "email": email,
            "company": "Example Energy",
            "location": "Oran",
            # Intentionally no sent_at: this is legacy history.
        }

        with patch.object(processor, "load_sent_records", return_value=[old_record]):
            duplicate, matched = processor.already_sent_same_job(
                job, "Technicien de Maintenance", [email]
            )

        self.assertFalse(duplicate)
        self.assertIsNone(matched)

    def test_exact_same_telegram_post_key_remains_a_permanent_duplicate_guard(self):
        post = {"source": "rcrdz1", "message_id": 22606}
        job = {"job_title": "Technicien de Maintenance"}
        email = "recruitment@example.com"
        key = processor.application_key(post, job, email)

        with patch.object(processor, "load_sent_records", return_value=[{"key": key}]):
            self.assertTrue(processor.already_sent(key))

    def test_gemini_only_is_accepted_as_valid_runner_configuration(self):
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "gemini-test-key", "GROQ_API_KEY": "", "OPENROUTER_API_KEY": ""},
        ):
            self.assertEqual(require_ai_provider(), "Gemini")

    def test_openrouter_only_is_accepted_as_valid_runner_configuration(self):
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "",
                "GROQ_API_KEY": "",
                "OPENROUTER_API_KEY": "openrouter-test-key",
            },
        ):
            self.assertEqual(require_ai_provider(), "OpenRouter (free fallback)")

    def test_runner_fails_fast_when_no_ai_provider_key_is_configured(self):
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "", "GROQ_API_KEY": "", "OPENROUTER_API_KEY": ""},
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "GEMINI_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY",
            ):
                require_ai_provider()

    def test_short_gemini_overload_and_recovery_do_not_spam_telegram(self):
        bot = TelegramJobBot({}, FakeProcessor(), lambda _reason: None)
        record_ai_provider_status(
            bot.state,
            {
                "status": "degraded",
                "provider": "gemini",
                "model": "gemini-3.6-flash",
                "reason": "gemini-3.8-flash: HTTP 503: high demand",
                "gemini_configured": True,
                "checked_at_utc": "2026-10-19T10:00:00Z",
            },
            bot,
        )
        self.assertEqual(len(bot.state["telegram_outbox"]), 0)
        self.assertEqual(bot.state["ai_provider_degraded_since_utc"], "2026-10-19T10:00:00+00:00")

        record_ai_provider_status(
            bot.state,
            {
                "status": "degraded",
                "provider": "gemini",
                "model": "gemini-3.6-flash",
                "reason": "gemini-3.8-flash: HTTP 503: high demand",
                "gemini_configured": True,
                "checked_at_utc": "2026-10-19T10:04:00Z",
            },
            bot,
        )
        self.assertEqual(len(bot.state["telegram_outbox"]), 0)

        record_ai_provider_status(
            bot.state,
            {
                "status": "healthy",
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "reason": "",
                "gemini_configured": True,
                "checked_at_utc": "2026-10-19T10:06:00Z",
            },
            bot,
        )
        self.assertEqual(len(bot.state["telegram_outbox"]), 0)
        self.assertNotIn("ai_provider_degraded_since_utc", bot.state)
        self.assertNotIn("ai_provider_degraded_alert_sent", bot.state)

    def test_sustained_degradation_alerts_once_and_recovery_is_reported_once(self):
        bot = TelegramJobBot({}, FakeProcessor(), lambda _reason: None)
        first = {
            "status": "degraded",
            "provider": "gemini",
            "model": "gemini-3.6-flash",
            "reason": "gemini-3.8-flash: HTTP 503: high demand",
            "gemini_configured": True,
            "checked_at_utc": "2026-10-19T10:00:00Z",
        }
        record_ai_provider_status(bot.state, first, bot)
        self.assertEqual(len(bot.state["telegram_outbox"]), 0)

        persistent = dict(first, checked_at_utc="2026-10-19T10:16:00Z")
        record_ai_provider_status(bot.state, persistent, bot)
        self.assertEqual(len(bot.state["telegram_outbox"]), 1)
        self.assertIn("استمر تعذّر المسار الأساسي لمدة 15 دقيقة", bot.state["telegram_outbox"][0]["text"])

        repeated = dict(
            persistent,
            provider="groq",
            model="openai/gpt-oss-120b",
            checked_at_utc="2026-10-19T10:21:00Z",
        )
        record_ai_provider_status(bot.state, repeated, bot)
        self.assertEqual(len(bot.state["telegram_outbox"]), 1)

        record_ai_provider_status(
            bot.state,
            {
                "status": "healthy",
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "reason": "",
                "gemini_configured": True,
                "checked_at_utc": "2026-10-19T10:25:00Z",
            },
            bot,
        )
        self.assertEqual(len(bot.state["telegram_outbox"]), 2)
        self.assertIn("استقر مسار الذكاء الاصطناعي", bot.state["telegram_outbox"][1]["text"])
        self.assertNotIn("ai_provider_degraded_since_utc", bot.state)
        self.assertNotIn("ai_provider_degraded_alert_sent", bot.state)

    def test_gemini_activation_after_groq_only_sends_owner_notification(self):
        bot = TelegramJobBot({}, FakeProcessor(), lambda _reason: None)
        bot.state["ai_provider_status"] = {
            "status": "groq_only",
            "provider": "groq",
            "model": "openai/gpt-oss-120b",
            "reason": "GEMINI_API_KEY is not configured.",
            "gemini_configured": False,
            "checked_at_utc": "2026-10-19T09:00:00Z",
        }

        record_ai_provider_status(
            bot.state,
            {
                "status": "healthy",
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "reason": "",
                "gemini_configured": True,
                "checked_at_utc": "2026-10-19T10:00:00Z",
            },
            bot,
        )

        self.assertEqual(len(bot.state["telegram_outbox"]), 1)
        self.assertIn("تم تفعيل Gemini بنجاح", bot.state["telegram_outbox"][0]["text"])

    def test_dashboard_displays_active_fallback_provider_and_reason(self):
        bot = TelegramJobBot({}, FakeProcessor(), lambda _reason: None)
        bot.state["ai_provider_status"] = {
            "status": "degraded",
            "provider": "groq",
            "model": "openai/gpt-oss-120b",
            "reason": "Gemini HTTP 503: high demand",
            "gemini_configured": True,
            "checked_at_utc": "2026-10-19T10:00:00Z",
        }

        dashboard = bot._dashboard_text()

        self.assertIn("البديل النشط: GROQ", dashboard)
        self.assertIn("Gemini HTTP 503", dashboard)

    def test_provider_cooldown_is_read_from_persisted_encrypted_state(self):
        now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        state = {"ai_provider_retry_after_utc": "2026-10-19T12:07:30+00:00"}
        self.assertEqual(provider_cooldown_remaining_seconds(state, now), 450)
        self.assertEqual(provider_cooldown_remaining_seconds(state, now + timedelta(minutes=8)), 0)

    def test_rate_limit_duration_parser_handles_provider_reset_headers(self):
        self.assertEqual(processor._parse_rate_limit_duration("2m59.5s"), 179.5)
        self.assertEqual(processor._parse_rate_limit_duration("1h 2m"), 3720)

    def test_exhausted_groq_models_persist_cooldown_from_retry_after(self):
        errors = []
        for seconds in (120, 240):
            error = RuntimeError("429 rate limit exceeded")
            error.status_code = 429
            error.headers = {"Retry-After": str(seconds)}
            errors.append(error)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=errors))))
        callbacks = []
        messages = [{"role": "user", "content": "Return a JSON object."}]
        with (
            patch.object(processor, "client", fake_client),
            patch.object(processor, "GROQ_MODEL_FALLBACKS", ("test/model-a", "test/model-b")),
            patch.object(processor, "_RATE_LIMITED_GROQ_MODELS", set()),
            patch.object(processor, "JOB_AI_RATE_LIMIT_CALLBACK", lambda seconds, provider: callbacks.append((seconds, provider))),
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}),
        ):
            with self.assertRaises(processor.AIProviderRateLimited) as raised:
                processor.groq_json_request(messages)
        self.assertEqual(raised.exception.retry_after_seconds, 255)
        self.assertEqual(callbacks[0][0], 255)
        self.assertIn("Groq", callbacks[0][1])

    def test_gemini_is_used_when_groq_model_pool_is_rate_limited(self):
        errors = []
        for _ in range(2):
            error = RuntimeError("429 rate limit exceeded")
            error.status_code = 429
            errors.append(error)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=errors))))
        api_response = {"candidates": [{"content": {"parts": [{"text": '{"jobs": []}'}]}}]}
        fake_http_response = Mock()
        fake_http_response.__enter__ = Mock(return_value=fake_http_response)
        fake_http_response.__exit__ = Mock(return_value=False)
        fake_http_response.read.return_value = json.dumps(api_response).encode("utf-8")
        messages = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "Analyze."}]
        with (
            patch.object(processor, "client", fake_client),
            patch.object(processor, "GROQ_MODEL_FALLBACKS", ("test/model-a", "test/model-b")),
            patch.object(processor, "_RATE_LIMITED_GROQ_MODELS", set()),
            patch.object(processor, "JOB_AI_RATE_LIMIT_CALLBACK", None),
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-gemini-key"}),
            patch.object(processor.urllib.request, "urlopen", return_value=fake_http_response) as urlopen,
        ):
            result = processor.groq_json_request(messages)
        self.assertEqual(result, {"jobs": []})
        urlopen.assert_called_once()
        self.assertIn("models/gemini-3.8-flash:generateContent", urlopen.call_args.args[0].full_url)

    def test_rate_limited_primary_model_falls_back_without_retrying_it(self):
        rate_limit_error = RuntimeError("429 rate limit exceeded")
        rate_limit_error.status_code = 429
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"ok": true}')
                )
            ]
        )
        create = Mock(side_effect=[rate_limit_error, response])
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )
        )
        messages = [{"role": "user", "content": "Return a JSON object."}]

        with (
            patch.object(processor, "client", fake_client),
            patch.object(processor, "_RATE_LIMITED_GROQ_MODELS", set()),
        ):
            result = processor.groq_json_request(messages)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(create.call_count, 2)
        self.assertEqual(
            create.call_args_list[0].kwargs["model"],
            "openai/gpt-oss-120b",
        )
        self.assertEqual(
            create.call_args_list[1].kwargs["model"],
            "openai/gpt-oss-20b",
        )


    def test_email_attachment_uses_named_english_ats_filename(self):
        self.assertEqual(
            processor.cv_attachment_filename("/runner/cvs/cv_ats_en.pdf"),
            "DJEZIRI_ABDELKADER_CV_ATS_EN.pdf",
        )

    def test_email_attachment_uses_named_french_ats_filename(self):
        self.assertEqual(
            processor.cv_attachment_filename("/runner/cvs/cv_ats_fr.pdf"),
            "DJEZIRI_ABDELKADER_CV_ATS_FR.pdf",
        )

    def test_email_attachment_fallback_keeps_original_filename(self):
        self.assertEqual(
            processor.cv_attachment_filename("/runner/cvs/other_cv.pdf"),
            "other_cv.pdf",
        )

    def test_save_sent_adds_algeria_local_timestamp(self):
        from datetime import datetime
        from tempfile import TemporaryDirectory
        from zoneinfo import ZoneInfo

        with TemporaryDirectory() as temp_dir:
            sent_path = os.path.join(temp_dir, "sent_applications.jsonl")
            with patch.object(processor, "SENT_FILE", sent_path):
                processor.save_sent("timestamp-test", {
                    "application_job_title": "Test Job",
                })
            with open(sent_path, "r", encoding="utf-8") as handle:
                record = json.loads(handle.readline())
        sent_at = datetime.fromisoformat(record["sent_at"])
        self.assertIsNotNone(sent_at.tzinfo)
        self.assertEqual(
            sent_at.astimezone(ZoneInfo("Africa/Algiers")).date(),
            now_local().date(),
        )


if __name__ == "__main__":
    unittest.main()
