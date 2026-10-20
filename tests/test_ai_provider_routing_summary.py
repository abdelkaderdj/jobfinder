import unittest
from unittest.mock import patch

from cloud_runner import ai_provider_routing_summary


class AIProviderRoutingSummaryTests(unittest.TestCase):
    def test_gemini_groq_and_openrouter_order_is_reported(self):
        summary = ai_provider_routing_summary(True, True, True)

        self.assertIn("Gemini primary", summary)
        self.assertIn("Groq multi-model then OpenRouter free fallback", summary)
        self.assertIn("not verified at startup", summary)

    def test_openrouter_fallback_is_not_hidden_when_gemini_is_primary(self):
        summary = ai_provider_routing_summary(True, False, True)

        self.assertIn("Gemini primary", summary)
        self.assertIn("OpenRouter free fallback enabled", summary)
        self.assertNotIn("Groq multi-model", summary)

    def test_groq_and_openrouter_are_reported_in_routing_order(self):
        summary = ai_provider_routing_summary(False, True, True)

        self.assertIn("Groq primary", summary)
        self.assertIn("OpenRouter free fallback enabled", summary)
        self.assertIn("Gemini fallback is not configured", summary)

    def test_openrouter_only_mode_is_reported_explicitly(self):
        summary = ai_provider_routing_summary(False, False, True)

        self.assertIn("OpenRouter free only", summary)
        self.assertIn("fallbacks are not configured", summary)

    def test_gemini_only_mode_is_reported_explicitly(self):
        summary = ai_provider_routing_summary(True, False, False)

        self.assertIn("Gemini only", summary)
        self.assertIn("no Groq/OpenRouter fallback", summary)

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
