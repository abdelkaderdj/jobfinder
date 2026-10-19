import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
    def test_gemini_retry_delay_parses_structured_retry_delay(self):
        error = RuntimeError('Gemini HTTP 429: {"error":{"details":[{"retryDelay":"42s"}]}}')
        error.status_code = 429

        seconds = processor._rate_limit_retry_seconds(error)

        self.assertEqual(seconds, 60)  # cooldowns are clamped to at least one minute

    def test_default_gemini_fallbacks_include_multiple_stable_flash_models(self):
        with patch.dict(os.environ, {"GEMINI_MODEL": "", "GEMINI_FALLBACK_MODELS": "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite"}):
            models = processor._configured_gemini_models()

        self.assertEqual(models[0], "gemini-3.8-flash")
        self.assertIn("gemini-3.6-flash", models)
        self.assertIn("gemini-3.5-flash-lite", models)
        self.assertIn("gemini-3.1-flash-lite", models)
        # Google aliases deprecated model IDs to newer models; keep fallback
        # routes distinct instead of repeatedly hitting the same quota pool.
        self.assertNotIn("gemini-3.7-flash", models)
        self.assertNotIn("gemini-3.5-flash", models)
        self.assertEqual(len(models), len(set(models)))

    def test_provider_failure_diagnostic_surfaces_safe_http_detail(self):
        error = RuntimeError(
            'Gemini HTTP 400: {"error":{"code":400,"message":"API key not valid. Please pass a valid API key.","status":"INVALID_ARGUMENT"}}'
        )
        error.status_code = 400

        summary = processor._safe_ai_provider_error(error)

        self.assertIn("HTTP 400", summary)
        self.assertIn("API key not valid", summary)
        self.assertNotIn("AIza", summary)

    def test_provider_failure_diagnostic_exposes_sanitized_transport_reason(self):
        error = RuntimeError(
            "Gemini transport failure (URLError): gaierror: Temporary failure in name resolution"
        )

        summary = processor._safe_ai_provider_error(error)

        self.assertIn("URLError", summary)
        self.assertIn("Temporary failure in name resolution", summary)

    def test_gemini_38_request_omits_deprecated_temperature_parameter(self):
        fake_response = MagicMock()
        fake_response.__enter__.return_value = fake_response
        fake_response.read.return_value = (
            b'{"candidates":[{"content":{"parts":[{"text":"{\\"route\\":\\"gemini\\"}"}]}}]}'
        )
        with (
            patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "gemini-3.8-flash"},
            ),
            patch.object(
                processor.urllib.request,
                "urlopen",
                return_value=fake_response,
            ) as urlopen,
        ):
            result = processor._gemini_json_request(
                [{"role": "user", "content": "Return a JSON object."}]
            )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result, {"route": "gemini"})
        self.assertNotIn("temperature", payload["generationConfig"])
        self.assertEqual(
            payload["generationConfig"]["thinkingConfig"]["thinkingLevel"],
            "low",
        )

    def setUp(self):
        self.original_gemini_disabled = processor._GEMINI_DISABLED_FOR_RUN
        self.original_rate_limited_models = set(processor._RATE_LIMITED_GROQ_MODELS)
        self.original_unavailable_groq_models = set(processor._UNAVAILABLE_GROQ_MODELS)
        self.original_disabled_gemini_models = set(processor._DISABLED_GEMINI_MODELS)
        processor._GEMINI_DISABLED_FOR_RUN = False
        processor._RATE_LIMITED_GROQ_MODELS.clear()
        processor._UNAVAILABLE_GROQ_MODELS.clear()
        processor._DISABLED_GEMINI_MODELS.clear()

    def tearDown(self):
        processor._GEMINI_DISABLED_FOR_RUN = self.original_gemini_disabled
        processor._RATE_LIMITED_GROQ_MODELS.clear()
        processor._RATE_LIMITED_GROQ_MODELS.update(self.original_rate_limited_models)
        processor._UNAVAILABLE_GROQ_MODELS.clear()
        processor._UNAVAILABLE_GROQ_MODELS.update(self.original_unavailable_groq_models)
        processor._DISABLED_GEMINI_MODELS.clear()
        processor._DISABLED_GEMINI_MODELS.update(self.original_disabled_gemini_models)

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

    def test_gemini_503_overload_falls_back_to_another_gemini_model(self):
        overloaded = RuntimeError(
            'Gemini HTTP 503: {"error":{"code":503,"message":"Model is experiencing high demand"}}'
        )
        overloaded.status_code = 503
        with (
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "test-key",
                    "GEMINI_MODEL": "gemini-primary-test",
                    "GEMINI_FALLBACK_MODELS": "gemini-fallback-test",
                },
            ),
            patch.object(
                processor,
                "_gemini_json_request",
                side_effect=[overloaded, {"route": "gemini-fallback"}],
            ) as gemini,
            patch.object(processor.client.chat.completions, "create") as groq,
        ):
            result = processor.groq_json_request([{"role": "user", "content": "test"}])

        self.assertEqual(result, {"route": "gemini-fallback"})
        self.assertEqual(gemini.call_count, 2)
        self.assertIn("gemini-primary-test", processor._DISABLED_GEMINI_MODELS)
        self.assertFalse(processor._GEMINI_DISABLED_FOR_RUN)
        groq.assert_not_called()

    def test_groq_503_overload_skips_model_and_uses_next_model(self):
        overloaded = RuntimeError("provider temporarily unavailable")
        overloaded.status_code = 503
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": ""}),
            patch.object(processor, "GROQ_MODEL_FALLBACKS", ("test/model-a", "test/model-b")),
            patch.object(
                processor.client.chat.completions,
                "create",
                side_effect=[overloaded, response_with_json()],
            ) as groq,
        ):
            result = processor.groq_json_request([{"role": "user", "content": "test"}])

        self.assertEqual(result, {"route": "groq"})
        self.assertEqual(groq.call_count, 2)
        self.assertEqual([call.kwargs["model"] for call in groq.call_args_list], ["test/model-a", "test/model-b"])
        self.assertIn("test/model-a", processor._UNAVAILABLE_GROQ_MODELS)
    def test_gemini_rate_limited_model_falls_back_to_next_gemini_model(self):
        rate_limit = RuntimeError("rate limit reached; retry after 120 seconds")
        rate_limit.status_code = 429
        rate_limit.headers = {"retry-after": "120"}
        with (
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "test-key",
                    "GEMINI_MODEL": "gemini-primary-test",
                    "GEMINI_FALLBACK_MODELS": "gemini-fallback-test",
                },
            ),
            patch.object(
                processor,
                "_gemini_json_request",
                side_effect=[rate_limit, {"route": "gemini-fallback"}],
            ) as gemini,
            patch.object(processor.client.chat.completions, "create") as groq,
        ):
            result = processor.groq_json_request(
                [{"role": "user", "content": "test"}]
            )

        self.assertEqual(result, {"route": "gemini-fallback"})
        self.assertEqual(gemini.call_count, 2)
        self.assertEqual(
            [call.kwargs["model_name"] for call in gemini.call_args_list],
            ["gemini-primary-test", "gemini-fallback-test"],
        )
        self.assertIn("gemini-primary-test", processor._DISABLED_GEMINI_MODELS)
        self.assertFalse(processor._GEMINI_DISABLED_FOR_RUN)
        groq.assert_not_called()

    def test_all_gemini_models_rate_limited_fall_back_to_groq_once_per_run(self):
        rate_limit = RuntimeError("rate limit reached; retry after 120 seconds")
        rate_limit.status_code = 429
        rate_limit.headers = {"retry-after": "120"}
        with (
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "test-key",
                    "GEMINI_MODEL": "gemini-primary-test",
                    "GEMINI_FALLBACK_MODELS": "gemini-fallback-test",
                },
            ),
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
            first = processor.groq_json_request(
                [{"role": "user", "content": "first"}]
            )
            second = processor.groq_json_request(
                [{"role": "user", "content": "second"}]
            )

        self.assertEqual(first, {"route": "groq"})
        self.assertEqual(second, {"route": "groq"})
        self.assertEqual(gemini.call_count, 2)
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
