from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "jobfinder-channel-health.yml"
CHECKER = ROOT / "tools" / "check_telegram_channels.py"


class ChannelHealthWorkflowTests(unittest.TestCase):
    def test_daily_read_only_channel_check_remains_scheduled(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("schedule:", workflow)
        self.assertIn('cron: "23 7 * * *"', workflow)
        self.assertIn("run: python tools/check_telegram_channels.py", workflow)

    def test_channel_check_shares_production_concurrency_lock(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("group: jobfinder-cloud-monitor", workflow)
        self.assertIn("queue: max", workflow)
        self.assertNotIn("group: jobfinder-channel-health-audit", workflow)

    def test_read_only_health_check_does_not_use_ai_or_write_cursors(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        checker = CHECKER.read_text(encoding="utf-8")

        for secret_name in (
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "OPENROUTER_API_KEY",
            "GMAIL_APP_PASSWORD",
        ):
            self.assertNotIn(secret_name, workflow)

        self.assertIn("read_encrypted_json", checker)
        self.assertNotIn("write_encrypted_json", checker)
        self.assertNotIn("auto_jobfinder_cloud", checker)


if __name__ == "__main__":
    unittest.main()
