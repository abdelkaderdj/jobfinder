import json
import os
import smtplib
import tempfile
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

    def test_configured_gemini_fallbacks_include_distinct_stable_flash_models(self):
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

    def test_deprecated_gemini_aliases_are_canonicalized_and_deduplicated(self):
        with patch.dict(
            os.environ,
            {
                "GEMINI_MODEL": "gemini-3.7-flash",
                "GEMINI_FALLBACK_MODELS": (
                    "gemini-3.8-flash,gemini-3.5-flash,"
                    "gemini-3.6-flash,gemini-3.5-flash-lite"
                ),
            },
        ):
            models = processor._configured_gemini_models()

        self.assertEqual(
            models,
            ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"),
        )

    def test_provider_pacing_waits_when_requests_arrive_too_soon(self):
        processor._AI_PROVIDER_LAST_REQUEST_AT.clear()
        try:
            with (
                patch.dict(
                    os.environ,
                    {"JOBFINDER_AI_MIN_REQUEST_INTERVAL_SECONDS": "2"},
                ),
                patch.object(processor.time, "monotonic", side_effect=[100.0, 100.5]),
                patch.object(processor.time, "sleep") as sleep,
            ):
                processor._pace_ai_provider("gemini")
                processor._pace_ai_provider("gemini")

            sleep.assert_called_once_with(1.5)
            self.assertEqual(processor._AI_PROVIDER_LAST_REQUEST_AT["gemini"], 102.0)
        finally:
            processor._AI_PROVIDER_LAST_REQUEST_AT.clear()

    def test_smtp_quit_failure_after_acceptance_does_not_report_send_failure(self):
        server = MagicMock()
        server.send_message.return_value = {}
        # Gmail has already accepted the message. A QUIT error must not turn the
        # accepted send into an apparent failure that gets retried as a duplicate.
        server.quit.side_effect = smtplib.SMTPResponseException(500, b"QUIT failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            cv_path = os.path.join(temp_dir, "cv.pdf")
            with open(cv_path, "wb") as cv_file:
                cv_file.write(b"%PDF-test")

            with (
                patch.object(
                    processor,
                    "build_email",
                    return_value=("Test subject", "Test body"),
                ),
                patch.object(processor.smtplib, "SMTP", return_value=server),
            ):
                result = processor.send_application(
                    "jobs@example.org",
                    {"job_title": "Technicien de maintenance"},
                    cv_path,
                    "fr",
                    "Test advertisement",
                    "Technicien de maintenance",
                )

        self.assertIsNone(result)
        server.send_message.assert_called_once()
        server.quit.assert_called_once()

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
        self.assertTrue(processor._is_temporary_provider_overload(error))

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
        self.original_groq_disabled = processor._GROQ_DISABLED_FOR_RUN
        self.original_rate_limited_models = set(processor._RATE_LIMITED_GROQ_MODELS)
        self.original_unavailable_groq_models = set(processor._UNAVAILABLE_GROQ_MODELS)
        self.original_disabled_gemini_models = set(processor._DISABLED_GEMINI_MODELS)
        processor._GEMINI_DISABLED_FOR_RUN = False
        processor._GROQ_DISABLED_FOR_RUN = False
        processor._RATE_LIMITED_GROQ_MODELS.clear()
        processor._UNAVAILABLE_GROQ_MODELS.clear()
        processor._DISABLED_GEMINI_MODELS.clear()

    def tearDown(self):
        processor._GEMINI_DISABLED_FOR_RUN = self.original_gemini_disabled
        processor._GROQ_DISABLED_FOR_RUN = self.original_groq_disabled
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

    def test_qwen_fallback_uses_low_hidden_reasoning_to_limit_token_overhead(self):
        with (
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "",
                    "JOBFINDER_AI_MIN_REQUEST_INTERVAL_SECONDS": "0",
                },
            ),
            patch.object(
                processor,
                "GROQ_MODEL_FALLBACKS",
                ("qwen/qwen3.8-27b",),
            ),
            patch.object(
                processor.client.chat.completions,
                "create",
                return_value=response_with_json(),
            ) as groq,
        ):
            result = processor.groq_json_request(
                [{"role": "user", "content": "Return structured JSON."}]
            )

        self.assertEqual(result, {"route": "groq"})
        self.assertEqual(groq.call_args.kwargs["reasoning_effort"], "low")
        self.assertEqual(groq.call_args.kwargs["reasoning_format"], "hidden")

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

    def test_mixed_rate_limit_and_overload_persists_quota_cooldown(self):
        rate_limit = RuntimeError("rate limit reached")
        rate_limit.status_code = 429
        rate_limit.headers = {"retry-after": "120"}
        overloaded = RuntimeError("provider temporarily unavailable")
        overloaded.status_code = 503
        callbacks = []

        with (
            patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "", "JOBFINDER_AI_MIN_REQUEST_INTERVAL_SECONDS": "0"},
            ),
            patch.object(
                processor,
                "GROQ_MODEL_FALLBACKS",
                ("test/model-a", "test/model-b"),
            ),
            patch.object(
                processor.client.chat.completions,
                "create",
                side_effect=[rate_limit, overloaded],
            ),
            patch.object(
                processor,
                "JOB_AI_RATE_LIMIT_CALLBACK",
                lambda seconds, reason: callbacks.append((seconds, reason)),
            ),
        ):
            with self.assertRaises(processor.AIProviderRateLimited) as raised:
                processor.groq_json_request([{"role": "user", "content": "test"}])

        self.assertEqual(raised.exception.retry_after_seconds, 135)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0][0], 135)
        self.assertIn("some routes may also be temporarily overloaded", callbacks[0][1])

    def test_all_overloaded_groq_routes_use_short_circuit_breaker_cooldown(self):
        overloaded = RuntimeError("provider temporarily unavailable")
        overloaded.status_code = 503
        callbacks = []

        with (
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "",
                    "JOBFINDER_AI_MIN_REQUEST_INTERVAL_SECONDS": "0",
                    "JOBFINDER_AI_OUTAGE_RETRY_SECONDS": "240",
                },
            ),
            patch.object(
                processor,
                "GROQ_MODEL_FALLBACKS",
                ("test/model-a", "test/model-b"),
            ),
            patch.object(
                processor.client.chat.completions,
                "create",
                side_effect=[overloaded, overloaded],
            ),
            patch.object(
                processor,
                "JOB_AI_RATE_LIMIT_CALLBACK",
                lambda seconds, reason: callbacks.append((seconds, reason)),
            ),
        ):
            with self.assertRaises(processor.AIProviderUnavailable) as raised:
                processor.groq_json_request([{"role": "user", "content": "test"}])

        self.assertNotIsInstance(raised.exception, processor.AIProviderRateLimited)
        self.assertEqual(raised.exception.retry_after_seconds, 240)
        self.assertEqual(callbacks, [(240, "All configured Groq routes are temporarily overloaded")])

    def test_invalid_gemini_key_disables_gemini_models_and_uses_groq(self):
        invalid_key = RuntimeError(
            'Gemini HTTP 400: {"error":{"code":400,"message":"API key not valid. Please pass a valid API key.","status":"INVALID_ARGUMENT"}}'
        )
        invalid_key.status_code = 400

        with (
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "test-key",
                    "GEMINI_MODEL": "gemini-primary-test",
                    "GEMINI_FALLBACK_MODELS": "gemini-fallback-test",
                    "JOBFINDER_AI_MIN_REQUEST_INTERVAL_SECONDS": "0",
                },
            ),
            patch.object(processor, "_gemini_json_request", side_effect=invalid_key) as gemini,
            patch.object(
                processor.client.chat.completions,
                "create",
                return_value=response_with_json(),
            ) as groq,
        ):
            result = processor.groq_json_request(
                [{"role": "user", "content": "test"}]
            )

        self.assertEqual(result, {"route": "groq"})
        gemini.assert_called_once()
        groq.assert_called_once()
        self.assertTrue(processor._GEMINI_DISABLED_FOR_RUN)

    def test_bad_provider_credentials_notify_owner_and_apply_config_cooldown(self):
        invalid_gemini_key = RuntimeError(
            'Gemini HTTP 400: {"error":{"code":400,"message":"API key not valid","status":"INVALID_ARGUMENT"}}'
        )
        invalid_gemini_key.status_code = 400
        invalid_groq_key = RuntimeError("401 Unauthorized: invalid API key")
        invalid_groq_key.status_code = 401
        callbacks = []

        with (
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "test-key",
                    "GEMINI_MODEL": "gemini-primary-test",
                    "GEMINI_FALLBACK_MODELS": "gemini-fallback-test",
                    "JOBFINDER_AI_CONFIG_RETRY_SECONDS": "600",
                    "JOBFINDER_AI_MIN_REQUEST_INTERVAL_SECONDS": "0",
                },
            ),
            patch.object(
                processor,
                "GROQ_MODEL_FALLBACKS",
                ("test/model-a", "test/model-b"),
            ),
            patch.object(
                processor,
                "_gemini_json_request",
                side_effect=invalid_gemini_key,
            ),
            patch.object(
                processor.client.chat.completions,
                "create",
                side_effect=invalid_groq_key,
            ),
            patch.object(
                processor,
                "JOB_AI_RATE_LIMIT_CALLBACK",
                lambda seconds, reason: callbacks.append((seconds, reason)),
            ),
        ):
            with self.assertRaises(processor.AIProviderUnavailable) as raised:
                processor.groq_json_request(
                    [{"role": "user", "content": "test"}]
                )

        self.assertNotIsInstance(raised.exception, processor.AIProviderRateLimited)
        self.assertEqual(raised.exception.retry_after_seconds, 600)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0][0], 600)
        self.assertIn("credentials/permissions rejected", callbacks[0][1])
        self.assertNotIn("test-key", callbacks[0][1])

    def test_gemini_only_configuration_succeeds_without_groq_client(self):
        with (
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "test-key",
                    "JOBFINDER_AI_MIN_REQUEST_INTERVAL_SECONDS": "0",
                },
            ),
            patch.object(processor, "client", None),
            patch.object(processor, "GROQ_MODEL_FALLBACKS", ()),
            patch.object(
                processor,
                "_gemini_json_request",
                return_value={"route": "gemini"},
            ) as gemini,
        ):
            result = processor.groq_json_request(
                [{"role": "user", "content": "test"}]
            )

        self.assertEqual(result, {"route": "gemini"})
        gemini.assert_called_once()

    def test_gemini_only_network_failure_uses_outage_cooldown(self):
        network_error = RuntimeError(
            "Gemini transport failure (URLError): Temporary failure in name resolution"
        )
        callbacks = []

        with (
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "test-key",
                    "GEMINI_MODEL": "gemini-primary-test",
                    "GEMINI_FALLBACK_MODELS": "gemini-fallback-test",
                    "JOBFINDER_AI_MIN_REQUEST_INTERVAL_SECONDS": "0",
                    "JOBFINDER_AI_OUTAGE_RETRY_SECONDS": "180",
                },
            ),
            patch.object(processor, "client", None),
            patch.object(processor, "GROQ_MODEL_FALLBACKS", ()),
            patch.object(processor, "_gemini_json_request", side_effect=network_error),
            patch.object(
                processor,
                "JOB_AI_RATE_LIMIT_CALLBACK",
                lambda seconds, reason: callbacks.append((seconds, reason)),
            ),
        ):
            with self.assertRaises(processor.AIProviderUnavailable) as raised:
                processor.groq_json_request(
                    [{"role": "user", "content": "test"}]
                )

        self.assertNotIsInstance(raised.exception, processor.AIProviderRateLimited)
        self.assertEqual(raised.exception.retry_after_seconds, 180)
        self.assertEqual(callbacks, [
            (180, "All configured Gemini/Groq routes are temporarily unavailable")
        ])

    def test_gemini_only_quota_exhaustion_persists_cooldown(self):
        rate_limit = RuntimeError("rate limit reached; retry after 120 seconds")
        rate_limit.status_code = 429
        rate_limit.headers = {"retry-after": "120"}
        callbacks = []

        with (
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "test-key",
                    "GEMINI_MODEL": "gemini-primary-test",
                    "GEMINI_FALLBACK_MODELS": "gemini-fallback-test",
                    "JOBFINDER_AI_MIN_REQUEST_INTERVAL_SECONDS": "0",
                },
            ),
            patch.object(processor, "client", None),
            patch.object(processor, "GROQ_MODEL_FALLBACKS", ()),
            patch.object(processor, "_gemini_json_request", side_effect=rate_limit),
            patch.object(
                processor,
                "JOB_AI_RATE_LIMIT_CALLBACK",
                lambda seconds, reason: callbacks.append((seconds, reason)),
            ),
        ):
            with self.assertRaises(processor.AIProviderRateLimited) as raised:
                processor.groq_json_request(
                    [{"role": "user", "content": "test"}]
                )

        self.assertEqual(raised.exception.retry_after_seconds, 135)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0][0], 135)
        self.assertIn("Gemini/Groq routes", callbacks[0][1])

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
