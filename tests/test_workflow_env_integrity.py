from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"


def find_duplicate_env_keys(workflow_text):
    """Return duplicate variable names within individual GitHub Actions env blocks."""
    duplicates = []
    env_indent = None
    seen = {}

    def finish_block():
        for key, lines in seen.items():
            if len(lines) > 1:
                duplicates.append((key, tuple(lines)))

    for line_number, line in enumerate(workflow_text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*)\s*:", line)
        if not match:
            continue

        indent = len(match.group(1))
        key = match.group(2)

        if key == "env":
            if env_indent is not None:
                finish_block()
            env_indent = indent
            seen = {}
            continue

        if env_indent is None:
            continue

        if indent <= env_indent:
            finish_block()
            env_indent = None
            seen = {}
            continue

        seen.setdefault(key, []).append(line_number)

    if env_indent is not None:
        finish_block()

    return duplicates


class WorkflowEnvIntegrityTests(unittest.TestCase):
    def test_no_workflow_env_block_defines_a_variable_more_than_once(self):
        problems = []
        for workflow_path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
            duplicates = find_duplicate_env_keys(
                workflow_path.read_text(encoding="utf-8")
            )
            for key, lines in duplicates:
                problems.append(
                    f"{workflow_path.relative_to(ROOT)}: {key} appears on lines {lines}"
                )

        self.assertEqual(problems, [], "\n".join(problems))

    def test_duplicate_detector_catches_a_duplicate_within_one_env_block(self):
        sample = """jobs:
  example:
    steps:
      - name: Example
        env:
          API_KEY: first
          OTHER_KEY: value
          API_KEY: second
        run: echo test
"""
        self.assertEqual(
            find_duplicate_env_keys(sample),
            [("API_KEY", (6, 8))],
        )

    def test_same_variable_in_separate_env_blocks_is_allowed(self):
        sample = """jobs:
  first:
    steps:
      - env:
          API_KEY: first
        run: echo first
  second:
    steps:
      - env:
          API_KEY: second
        run: echo second
"""
        self.assertEqual(find_duplicate_env_keys(sample), [])


if __name__ == "__main__":
    unittest.main()
