import unittest
from unittest.mock import patch

from cloud_runner import ai_provider_routing_summary, require_ai_provider


class AIProviderRoutingSummaryTests(unittest.TestCase):
    def test_gemini_groq_and_openrouter_order_is_reported(self):
        summary = ai_provider_routing_summary(True, True, True)
        self.assertIn("free-only", summary)
        self.assertIn("Gemini Flash free tier primary", summary)
        self.assertIn("Groq Free model pool then OpenRouter Free", summary)
        self.assertIn("not verified at startup", summary)

    def test_openrouter_fallback_is_not_hidden_when_gemini_is_primary(self):
        summary = ai_provider_routing_summary(True, False, True)
        self.assertIn("Gemini Flash free tier primary", summary)
        self.assertIn("OpenRouter Free fallback enabled", summary)
        self.assertNotIn("Groq Free model pool", summary)

    def test_groq_and_openrouter_are_reported_in_routing_order(self):
        summary = ai_provider_routing_summary(False, True, True)
        self.assertIn("Groq Free primary", summary)
        self.assertIn("OpenRouter Free fallback enabled", summary)
        self.assertIn("Gemini is not configured", summary)

    def test_openrouter_only_mode_is_reported_explicitly(self):
        summary = ai_provider_routing_summary(False, False, True)
        self.assertIn("OpenRouter Free only", summary)
        self.assertIn("fallbacks are not configured", summary)

    def test_missing_openrouter_key_is_called_out_explicitly(self):
        summary = ai_provider_routing_summary(True, True, False)
        self.assertIn("Gemini Flash free tier primary", summary)
        self.assertIn("Groq Free model pool", summary)
        self.assertIn("OPENROUTER_API_KEY is empty", summary)

    def test_gemini_only_mode_is_reported_explicitly(self):
        summary = ai_provider_routing_summary(True, False, False)
        self.assertIn("Gemini Flash free tier only", summary)
        self.assertIn("no Groq/OpenRouter Free fallback", summary)

    def test_gemini_is_the_first_free_startup_route(self):
        with patch.dict("os.environ", {
            "GEMINI_API_KEY": "test-key",
            "GROQ_API_KEY": "groq-test-key",
            "OPENROUTER_API_KEY": "router-test-key",
            "OPENAI_API_KEY": "must-be-ignored",
        }):
            self.assertEqual(require_ai_provider(), "Gemini (free tier)")

    def test_groq_free_is_accepted_without_gemini(self):
        with patch.dict("os.environ", {
            "GEMINI_API_KEY": "",
            "GROQ_API_KEY": "groq-test-key",
            "OPENROUTER_API_KEY": "",
            "OPENAI_API_KEY": "must-be-ignored",
        }):
            self.assertEqual(require_ai_provider(), "Groq (free tier)")

    def test_openrouter_free_is_accepted_as_last_route(self):
        with patch.dict("os.environ", {
            "GEMINI_API_KEY": "",
            "GROQ_API_KEY": "",
            "OPENROUTER_API_KEY": "router-test-key",
            "OPENAI_API_KEY": "must-be-ignored",
        }):
            self.assertEqual(require_ai_provider(), "OpenRouter (free fallback)")

    def test_paid_openai_key_alone_is_not_a_supported_configuration(self):
        with patch.dict("os.environ", {
            "GEMINI_API_KEY": "",
            "GROQ_API_KEY": "",
            "OPENROUTER_API_KEY": "",
            "OPENAI_API_KEY": "must-be-ignored",
        }):
            with self.assertRaisesRegex(RuntimeError, "free-tier AI provider"):
                require_ai_provider()

    def test_environment_values_can_drive_the_summary(self):
        with patch.dict("os.environ", {
            "GEMINI_API_KEY": "",
            "GROQ_API_KEY": "",
            "OPENROUTER_API_KEY": "test-key",
            "OPENAI_API_KEY": "must-be-ignored",
        }):
            self.assertIn("OpenRouter Free only", ai_provider_routing_summary())


if __name__ == "__main__":
    unittest.main()
