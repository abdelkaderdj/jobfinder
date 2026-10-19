import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("GROQ_API_KEY", "validation-dummy-key")
os.environ.setdefault("GMAIL_APP_PASSWORD", "validation-dummy-password")
os.environ.setdefault("GMAIL_ADDRESS", "validation@example.org")
os.environ.setdefault("JOBFINDER_CANDIDATE_PROFILE", "validation profile")
os.environ.setdefault("JOBFINDER_CANDIDATE_NAME", "Validation User")

import auto_jobfinder_cloud as processor


def response_with_json(payload='{"route":"groq"}'):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=payload),
            )
        ]
    )


class AIProviderRoutingTests(unittest.TestCase):
    def setUp(self):
        self.original_gemini_disabled = processor._GEMINI_DISABLED_FOR_RUN
        self.original_rate_limited_models = set(processor._RATE_LIMITED_GROQ_MODELS)
        processor._GEMINI_DISABLED_FOR_RUN = False
        processor._RATE_LIMITED_GROQ_MODELS.clear()

    def tearDown(self):
        processor._GEMINI_DISABLED_FOR_RUN = self.original_gemini_disabled
        processor._RATE_LIMITED_GROQ_MODELS.clear()
        processor._RATE_LIMITED_GROQ_MODELS.update(self.original_rate_limited_models)

    def test_gemini_is_primary_when_key_is_configured(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch.object(
                processor,
                "_gemini_json_request",
                return_value={"route": "gemini"},
            ) as gemini,
            patch.object(processor.client.chat.completions, "create") as groq,
        ):
            result = processor.groq_json_request([{"role": "user", "content": "test"}])

        self.assertEqual(result, {"route": "gemini"})
        gemini.assert_called_once()
        groq.assert_not_called()

    def test_gemini_rate_limit_falls_back_to_groq_and_is_skipped_for_rest_of_run(self):
        rate_limit = RuntimeError("rate limit reached; retry after 120 seconds")
        rate_limit.status_code = 429
        rate_limit.headers = {"retry-after": "120"}
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch.object(
                processor,
                "_gemini_json_request",
                side_effect=rate_limit,
            ) as gemini,
            patch.object(
                processor.client.chat.completions,
                "create",
                return_value=response_with_json(),
            ) as groq,
        ):
            first = processor.groq_json_request([{"role": "user", "content": "first"}])
            second = processor.groq_json_request([{"role": "user", "content": "second"}])

        self.assertEqual(first, {"route": "groq"})
        self.assertEqual(second, {"route": "groq"})
        gemini.assert_called_once()
        self.assertEqual(groq.call_count, 2)
        self.assertTrue(processor._GEMINI_DISABLED_FOR_RUN)

    def test_empty_gemini_key_uses_groq_without_attempting_gemini(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}),
            patch.object(processor, "_gemini_json_request") as gemini,
            patch.object(
                processor.client.chat.completions,
                "create",
                return_value=response_with_json(),
            ) as groq,
        ):
            result = processor.groq_json_request([{"role": "user", "content": "test"}])

        self.assertEqual(result, {"route": "groq"})
        gemini.assert_not_called()
        groq.assert_called_once()


if __name__ == "__main__":
    unittest.main()
