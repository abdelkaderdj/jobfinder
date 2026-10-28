from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "jobfinder-cloud.yml",
    ROOT / ".github" / "workflows" / "jobfinder-ai-provider-smoke-test.yml",
    ROOT / ".github" / "workflows" / "jobfinder-audit-48h.yml",
    ROOT / ".github" / "workflows" / "jobfinder-telegram-control-test.yml",
    ROOT / ".github" / "workflows" / "telegram-webhook-event.yml",
)


class OpenRouterWorkflowConfigurationTests(unittest.TestCase):
    def test_cloud_workflow_passes_optional_dedicated_gemini_review_model(self):
        workflow = WORKFLOWS[0].read_text(encoding="utf-8")
        self.assertIn("GEMINI_REVIEW_MODEL: ${{ vars.GEMINI_REVIEW_MODEL }}", workflow)

    def test_cloud_and_provider_smoke_workflows_pass_optional_openai_configuration(self):
        for path in WORKFLOWS[:2]:
            with self.subTest(workflow=path.name):
                workflow = path.read_text(encoding="utf-8")
                self.assertIn("OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}", workflow)
                self.assertIn("OPENAI_MODEL: ${{ vars.OPENAI_MODEL || 'gpt-6.1-sol' }}", workflow)
                self.assertIn("OPENAI_REVIEW_MODEL: ${{ vars.OPENAI_REVIEW_MODEL }}", workflow)

    def test_workflows_pass_openrouter_as_an_optional_secret(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                workflow = path.read_text(encoding="utf-8")
                self.assertIn("OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}", workflow)
                self.assertIn("OPENROUTER_MODEL: ${{ vars.OPENROUTER_MODEL || 'openrouter/free' }}", workflow)


if __name__ == "__main__":
    unittest.main()
