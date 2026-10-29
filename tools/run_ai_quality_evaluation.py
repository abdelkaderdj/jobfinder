#!/usr/bin/env python3
"""Load the encrypted private candidate profile, then run the read-only AI benchmark.

Only the candidate profile is decrypted. This wrapper does not restore CV files,
application history, Telegram state, bot cursors, or any other runtime state.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    # Import the crypto/file helpers without calling cloud_runner.main().
    from cloud_runner import PRIVATE_DIR, get_fernet, read_encrypted_json

    encrypted_profile_path = PRIVATE_DIR / "profile.enc"
    profile = read_encrypted_json(encrypted_profile_path, get_fernet())

    required_fields = ("candidate_profile", "candidate_name", "gmail_address")
    if any(not isinstance(profile.get(field), str) or not profile[field].strip()
           for field in required_fields):
        raise RuntimeError("The encrypted private candidate profile is incomplete")

    # Keep the profile in this process's environment only; never print its values.
    os.environ["JOBFINDER_CANDIDATE_PROFILE"] = profile["candidate_profile"]
    os.environ["JOBFINDER_CANDIDATE_NAME"] = profile["candidate_name"]
    os.environ["GMAIL_ADDRESS"] = profile["gmail_address"]

    from tools.evaluate_ai_quality import main as evaluate_main

    return evaluate_main(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        # Avoid printing the candidate profile, decrypted artifacts, or secrets.
        print(f"AI quality evaluation setup failed: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        raise SystemExit(2)
