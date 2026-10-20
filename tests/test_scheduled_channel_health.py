from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHANNEL_HEALTH_WORKFLOW = ROOT / ".github" / "workflows" / "jobfinder-channel-health.yml"


class ScheduledChannelHealthTests(unittest.TestCase):
    def test_channel_health_audit_runs_daily_and_remains_manually_runnable(self):
        workflow = CHANNEL_HEALTH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("schedule:", workflow)
        self.assertIn('cron: "23 7 * * *"', workflow)

    def test_channel_health_workflow_is_not_configured_to_call_ai_or_send_jobs(self):
        workflow = CHANNEL_HEALTH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("tools/check_telegram_channels.py", workflow)
        self.assertNotIn("GEMINI_API_KEY", workflow)
        self.assertNotIn("GROQ_API_KEY", workflow)
        self.assertNotIn("GMAIL_APP_PASSWORD", workflow)
        self.assertNotIn("auto_jobfinder_cloud.py", workflow)


if __name__ == "__main__":
    unittest.main()
