import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import auto_jobfinder_cloud as processor
from tools.openrouter_smoke_test import main


class OpenRouterSmokeTestTests(unittest.TestCase):
    def test_missing_key_skips_without_network_request(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}),
            patch.object(processor, "_openrouter_json_request") as request,
            redirect_stdout(output),
        ):
            result = main()

        self.assertEqual(result, 0)
        request.assert_not_called()
        self.assertIn("SKIPPED", output.getvalue())
        self.assertIn("OPENROUTER_API_KEY is not configured", output.getvalue())

    def test_configured_key_performs_one_small_free_json_request(self):
        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "OPENROUTER_API_KEY": "unit-test-key",
                    "OPENROUTER_MODEL": "openrouter/free",
                },
            ),
            patch.object(processor, "_configured_openrouter_model", return_value="openrouter/free"),
            patch.object(
                processor,
                "_openrouter_json_request",
                return_value={"ok": True, "probe": "jobfinder-openrouter-free-smoke"},
            ) as request,
            redirect_stdout(output),
        ):
            result = main()

        self.assertEqual(result, 0)
        request.assert_called_once()
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["max_completion_tokens"], 80)
        self.assertEqual(kwargs["messages"][-1]["role"], "user")
        self.assertIn("SMOKE TEST PASSED", output.getvalue())
        self.assertIn("openrouter/free", output.getvalue())

    def test_unexpected_json_payload_fails_smoke_test(self):
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "unit-test-key"}),
            patch.object(processor, "_configured_openrouter_model", return_value="openrouter/free"),
            patch.object(processor, "_openrouter_json_request", return_value={"ok": False}),
        ):
            with self.assertRaisesRegex(RuntimeError, "expected JSON probe result"):
                main()


if __name__ == "__main__":
    unittest.main()
