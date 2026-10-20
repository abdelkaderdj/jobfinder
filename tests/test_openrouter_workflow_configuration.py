from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "jobfinder-cloud.yml",
    ROOT / ".github" / "workflows" / "jobfinder-ai-provider-smoke-test.yml",
    ROOT / ".github" / "workflows" / "jobfinder-audit-48h.yml",
)


class OpenRouterWorkflowConfigurationTests(unittest.TestCase):
    def test_workflows_pass_openrouter_as_an_optional_secret(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                workflow = path.read_text(encoding="utf-8")
                self.assertIn("OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}", workflow)
                self.assertIn("OPENROUTER_MODEL: ${{ vars.OPENROUTER_MODEL || 'openrouter/free' }}", workflow)


if __name__ == "__main__":
    unittest.main()
