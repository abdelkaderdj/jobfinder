import unittest
from unittest.mock import patch

from cloud_runner import ai_provider_routing_summary, require_ai_provider


class AIProviderRoutingSummaryTests(unittest.TestCase):
    def test_gemini_groq_and_openrouter_order_is_reported(self):
        summary = ai_provider_routing_summary(True, True, True, False)

        self.assertIn("Gemini primary", summary)
        self.assertIn("Groq multi-model then OpenRouter free fallback", summary)
        self.assertIn("not verified at startup", summary)

    def test_openrouter_fallback_is_not_hidden_when_gemini_is_primary(self):
        summary = ai_provider_routing_summary(True, False, True, False)

        self.assertIn("Gemini primary", summary)
        self.assertIn("OpenRouter free fallback enabled", summary)
        self.assertNotIn("Groq multi-model", summary)

    def test_groq_and_openrouter_are_reported_in_routing_order(self):
        summary = ai_provider_routing_summary(False, True, True, False)

        self.assertIn("Groq primary", summary)
        self.assertIn("OpenRouter free fallback enabled", summary)
        self.assertIn("Gemini fallback is not configured", summary)

    def test_openrouter_only_mode_is_reported_explicitly(self):
        summary = ai_provider_routing_summary(False, False, True, False)

        self.assertIn("OpenRouter free only", summary)
        self.assertIn("fallbacks are not configured", summary)

    def test_missing_openrouter_key_is_called_out_explicitly(self):
        summary = ai_provider_routing_summary(True, True, False, False)

        self.assertIn("Gemini primary", summary)
        self.assertIn("Groq multi-model", summary)
        self.assertIn("OPENROUTER_API_KEY is empty", summary)

    def test_gemini_only_mode_is_reported_explicitly(self):
        summary = ai_provider_routing_summary(True, False, False, False)

        self.assertIn("Gemini only", summary)
        self.assertIn("no Groq/OpenRouter fallback", summary)


    def test_openai_is_primary_for_high_reasoning_and_reports_astra_review(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_MODEL": "gpt-6.1-sol",
                "OPENAI_REVIEW_MODEL": "gpt-6-astra",
            },
        ):
            summary = ai_provider_routing_summary(True, True, True, True)

        self.assertIn("OpenAI (gpt-6.1-sol) primary for high-reasoning analysis", summary)
        self.assertIn("Gemini model pool then Groq multi-model pool then OpenRouter free", summary)
        self.assertIn("Low-effort helper calls use Gemini first", summary)
        self.assertIn("Dedicated APPLY reviewer: gpt-6-astra", summary)
        self.assertIn("not verified at startup", summary)

    def test_openai_only_mode_is_reported_explicitly(self):
        summary = ai_provider_routing_summary(False, False, False, True)

        self.assertIn("OpenAI only", summary)
        self.assertIn("gpt-6.1-sol", summary)

    def test_openai_only_configuration_passes_startup_provider_check(self):
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "openai-test-key",
                "GEMINI_API_KEY": "",
                "GROQ_API_KEY": "",
                "OPENROUTER_API_KEY": "",
            },
        ):
            self.assertEqual(require_ai_provider(), "OpenAI (high-reasoning)")

    def test_environment_values_can_drive_the_summary(self):
        with patch.dict(
            "os.environ",
            {
                "GEMINI_API_KEY": "",
                "GROQ_API_KEY": "",
                "OPENROUTER_API_KEY": "test-key",
            },
        ):
            self.assertIn("OpenRouter free only", ai_provider_routing_summary())


if __name__ == "__main__":
    unittest.main()
