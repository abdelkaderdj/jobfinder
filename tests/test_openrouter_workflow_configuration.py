from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "jobfinder-cloud.yml",
    ROOT / ".github" / "workflows" / "jobfinder-ai-provider-smoke-test.yml",
    ROOT / ".github" / "workflows" / "jobfinder-audit-48h.yml",
    ROOT / ".github" / "workflows" / "jobfinder-telegram-control-test.yml",
    ROOT / ".github" / "workflows" / "telegram-webhook-event.yml",
    ROOT / ".github" / "workflows" / "jobfinder-encrypted-profile-evaluation.yml",
)


class FreeAIWorkflowConfigurationTests(unittest.TestCase):
    def test_cloud_workflow_uses_no_paid_ai_keys_or_model_overrides(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                workflow = path.read_text(encoding="utf-8")
                self.assertNotIn("OPENAI_API_KEY", workflow)
                self.assertNotIn("OPENAI_MODEL", workflow)
                self.assertNotIn("OPENAI_REVIEW_MODEL", workflow)
                self.assertNotIn("GEMINI_MODEL:", workflow)
                self.assertNotIn("GEMINI_REVIEW_MODEL:", workflow)
                self.assertNotIn("GEMINI_FALLBACK_MODELS:", workflow)

    def test_all_ai_workflows_use_gemini_groq_and_openrouter_free(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                workflow = path.read_text(encoding="utf-8")
                self.assertIn("GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}", workflow)
                self.assertIn("GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}", workflow)
                self.assertIn("OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}", workflow)

    def test_openrouter_model_is_not_overridden_to_a_paid_route(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                workflow = path.read_text(encoding="utf-8")
                self.assertNotIn("OPENROUTER_MODEL: ${{ vars.OPENROUTER_MODEL", workflow)


if __name__ == "__main__":
    unittest.main()
