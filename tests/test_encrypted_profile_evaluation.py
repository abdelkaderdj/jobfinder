from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from tools.evaluate_encrypted_profile import (
    build_evaluation_args,
    load_encrypted_candidate_profile,
    safe_summary_from_report,
)


class EncryptedProfileEvaluationTests(unittest.TestCase):
    def test_analysis_module_import_does_not_require_gmail_credentials(self):
        root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        for name in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
            env.pop(name, None)
        env["GEMINI_API_KEY"] = "validation-dummy-key"
        env.pop("GROQ_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENROUTER_API_KEY", None)
        env["JOBFINDER_CANDIDATE_PROFILE"] = "Synthetic candidate profile for import test"
        env["JOBFINDER_CANDIDATE_NAME"] = "Synthetic Candidate"

        result = subprocess.run(
            [sys.executable, "-c", "import auto_jobfinder_cloud"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_build_evaluation_args_includes_output_path_and_optional_flags(self):
        output_path = Path("/tmp/private-report.json")
        self.assertEqual(
            build_evaluation_args(output_path, 0, False),
            ["--output", "/tmp/private-report.json"],
        )
        self.assertEqual(
            build_evaluation_args(output_path, 3, True),
            ["--output", "/tmp/private-report.json", "--limit", "3", "--strict"],
        )

    def test_profile_loader_returns_only_analysis_fields(self):
        key = Fernet.generate_key()
        fernet = Fernet(key)
        payload = {
            "candidate_profile": "Synthetic profile for unit tests only",
            "candidate_name": "Synthetic Candidate",
            "gmail_address": "private-address@example.invalid",
        }
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "profile.enc"
            profile_path.write_bytes(fernet.encrypt(json.dumps(payload).encode("utf-8")))
            loaded = load_encrypted_candidate_profile(profile_path, fernet)

        self.assertEqual(
            loaded,
            {
                "JOBFINDER_CANDIDATE_PROFILE": "Synthetic profile for unit tests only",
                "JOBFINDER_CANDIDATE_NAME": "Synthetic Candidate",
            },
        )
        self.assertNotIn("gmail_address", loaded)
        self.assertNotIn("private-address@example.invalid", json.dumps(loaded))

    def test_invalid_encrypted_profile_error_does_not_echo_input(self):
        key = Fernet.generate_key()
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "profile.enc"
            profile_path.write_bytes(b"PLAINTEXT-PRIVATE-SENTINEL")
            with self.assertRaisesRegex(RuntimeError, "could not be decrypted or parsed"):
                load_encrypted_candidate_profile(profile_path, Fernet(key))

    def test_summary_only_whitelist_drops_titles_reasons_and_error_messages(self):
        report = {
            "summary": {
                "cases": 2,
                "cases_passed": 1,
                "post_type_accuracy": 0.5,
                "title_precision": 1.0,
                "title_recall": 0.5,
                "evidence_errors": 1,
                "forbidden_title_hits": 0,
                "non_vacancy_with_jobs": 0,
                "decision_checks": 1,
                "raw_model_decision_accuracy": 0.0,
                "decision_accuracy_after_candidate_gate": 1.0,
                "failed_case_ids": ["private-case-1"],
            },
            "results": [
                {
                    "case_id": "private-case-1",
                    "status": "error",
                    "passed": False,
                    "error": "RuntimeError: PRIVATE_PROFILE_SENTINEL in an API error",
                    "predicted_titles": ["PRIVATE_TITLE_SENTINEL"],
                    "reason": "PRIVATE_REASON_SENTINEL",
                    "provider_trace": [
                        "AI PROVIDER: OpenAI (gpt-test-safe) succeeded",
                        "AI OPENAI REQUEST FAILED: PRIVATE_ERROR_SENTINEL",
                    ],
                }
            ],
            "configured_model_route": {"OPENAI_MODEL": "PRIVATE_CONFIG_SENTINEL"},
        }

        safe = safe_summary_from_report(report)
        rendered = json.dumps(safe, ensure_ascii=False)

        self.assertIn("gpt-test-safe", rendered)
        for forbidden in (
            "PRIVATE_PROFILE_SENTINEL",
            "PRIVATE_TITLE_SENTINEL",
            "PRIVATE_REASON_SENTINEL",
            "PRIVATE_ERROR_SENTINEL",
            "PRIVATE_CONFIG_SENTINEL",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertIn("private-case-1", rendered)
        self.assertIn('"error_type": "RuntimeError"', rendered)


if __name__ == "__main__":
    unittest.main()
