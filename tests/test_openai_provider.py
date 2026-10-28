import unittest

from jobfinder_openai_provider import (
    build_openai_responses_payload,
    extract_openai_output_text,
)


class OpenAIResponsesPayloadTests(unittest.TestCase):
    def test_payload_uses_configured_model_and_high_reasoning(self):
        payload = build_openai_responses_payload(
            [
                {"role": "system", "content": "Judge carefully."},
                {"role": "user", "content": "Return a JSON object."},
            ],
            model_name="gpt-6-astra",
            max_output_tokens=9000,
            reasoning_level="high",
        )
        self.assertEqual(payload["model"], "gpt-6-astra")
        self.assertEqual(payload["reasoning"]["effort"], "high")
        self.assertEqual(payload["max_output_tokens"], 9000)
        self.assertEqual(payload["text"]["format"]["type"], "json_object")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["input"][0]["role"], "system")
        self.assertEqual(payload["input"][1]["role"], "user")

    def test_payload_clamps_output_tokens(self):
        payload = build_openai_responses_payload(
            [{"role": "user", "content": "test"}],
            max_output_tokens=999999,
        )
        self.assertEqual(payload["max_output_tokens"], 65536)

    def test_payload_falls_back_to_default_model(self):
        payload = build_openai_responses_payload([{"role": "user", "content": "test"}])
        self.assertEqual(payload["model"], "gpt-6.1-sol")

    def test_extracts_top_level_output_text(self):
        self.assertEqual(
            extract_openai_output_text({"output_text": "{\"ok\": true}"}),
            "{\"ok\": true}",
        )

    def test_extracts_text_from_output_items(self):
        body = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "reasoning", "summary": "not output"},
                        {"type": "output_text", "text": "{\"jobs\": []}"},
                    ],
                }
            ]
        }
        self.assertEqual(extract_openai_output_text(body), "{\"jobs\": []}")

    def test_non_message_items_are_ignored(self):
        self.assertEqual(
            extract_openai_output_text(
                {"output": [{"type": "function_call", "name": "not-text"}]}
            ),
            "",
        )

    def test_empty_input_is_rejected(self):
        with self.assertRaises(ValueError):
            build_openai_responses_payload([])


if __name__ == "__main__":
    unittest.main()
