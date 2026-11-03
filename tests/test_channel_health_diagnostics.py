import unittest

from tools.check_telegram_channels import one_line_error


class ChannelHealthDiagnosticTests(unittest.TestCase):
    def test_known_telegram_authorization_error_is_actionable(self):
        self.assertEqual(
            one_line_error(RuntimeError("Telegram user session is not authorized")),
            "RuntimeError: Telegram user session is not authorized",
        )

    def test_known_encrypted_state_error_is_actionable(self):
        self.assertEqual(
            one_line_error(RuntimeError(
                "Could not decrypt state.enc; check JOBFINDER_FERNET_KEY"
            )),
            "RuntimeError: Could not decrypt state.enc; check JOBFINDER_FERNET_KEY",
        )

    def test_unknown_runtime_error_does_not_expose_message(self):
        secret_like_value = "session=do-not-print-this-value"
        self.assertEqual(one_line_error(RuntimeError(secret_like_value)), "RuntimeError")

    def test_non_runtime_error_does_not_expose_message(self):
        secret_like_value = "session=do-not-print-this-value"
        self.assertEqual(one_line_error(ValueError(secret_like_value)), "ValueError")


if __name__ == "__main__":
    unittest.main()
