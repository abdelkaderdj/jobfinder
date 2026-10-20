from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
AUDIT_WORKFLOW = ROOT / ".github" / "workflows" / "jobfinder-audit-48h.yml"
AUDIT_SCRIPT = ROOT / "tools" / "audit_recent_posts.py"


class PublicAuditOutputPrivacyTests(unittest.TestCase):
    def test_public_audit_workflow_does_not_upload_detailed_report_artifacts(self):
        workflow = AUDIT_WORKFLOW.read_text(encoding="utf-8")

        self.assertNotIn("actions/upload-artifact", workflow)
        self.assertNotIn("jobfinder_audit_report.md", workflow)
        self.assertNotIn("jobfinder_audit_report.json", workflow)

    def test_audit_script_does_not_persist_detailed_report_files(self):
        script = AUDIT_SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("jobfinder_audit_report.md", script)
        self.assertNotIn("jobfinder_audit_report.json", script)
        self.assertNotIn("write_text(markdown", script)


if __name__ == "__main__":
    unittest.main()
