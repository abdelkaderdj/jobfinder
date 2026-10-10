from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon.sessions import StringSession

BASE_DIR = Path(__file__).resolve().parent
PRIVATE_DIR = BASE_DIR / "cloud_private"
STATE_ENC = PRIVATE_DIR / "state.enc"
ENCRYPTED_CVS_DIR = PRIVATE_DIR / "cvs"
SENT_FILE = BASE_DIR / "sent_applications.jsonl"
CV_DIR = BASE_DIR / "cvs"
CHANNELS = ["rcrdz1", "Jobs_dz7", "china1644", "ajob58dz", "ridkh", "CVDZJOBS"]
RUNNER_VERSION = "2026-10-10-contact-scope-v2"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required GitHub Secret is missing: {name}")
    return value


def get_fernet() -> Fernet:
    key = required_env("JOBFINDER_FERNET_KEY").encode("ascii")
    try:
        return Fernet(key)
    except Exception as exc:
        raise RuntimeError("JOBFINDER_FERNET_KEY is invalid") from exc


def read_encrypted_json(path: Path, fernet: Fernet) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"Encrypted data file is missing: {path.relative_to(BASE_DIR)}")
    try:
        raw = fernet.decrypt(path.read_bytes())
        result = json.loads(raw.decode("utf-8"))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not decrypt {path.name}; check JOBFINDER_FERNET_KEY") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Encrypted JSON file has an invalid structure: {path.name}")
    return result


def write_encrypted_json(path: Path, payload: dict[str, Any], fernet: Fernet) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fernet.encrypt(raw))


def restore_private_files(fernet: Fernet, state: dict[str, Any]) -> None:
    # Decrypt candidate details from a private encrypted blob; never log the values.
    profile = read_encrypted_json(PRIVATE_DIR / "profile.enc", fernet)
    for field in ("candidate_profile", "candidate_name", "gmail_address"):
        if not isinstance(profile.get(field), str) or not profile[field].strip():
            raise RuntimeError("The encrypted private profile is incomplete")
    os.environ["JOBFINDER_CANDIDATE_PROFILE"] = profile["candidate_profile"]
    os.environ["JOBFINDER_CANDIDATE_NAME"] = profile["candidate_name"]
    os.environ["GMAIL_ADDRESS"] = profile["gmail_address"]

    CV_DIR.mkdir(parents=True, exist_ok=True)
    expected = {"cv_ats_en.pdf", "cv_ats_fr.pdf", "cv_normal_en.pdf", "cv_normal_fr.pdf"}
    found = set()
    for encrypted_path in ENCRYPTED_CVS_DIR.glob("*.pdf.fernet"):
        try:
            plaintext = fernet.decrypt(encrypted_path.read_bytes())
        except InvalidToken as exc:
            raise RuntimeError(f"Could not decrypt CV file: {encrypted_path.name}") from exc
        filename = encrypted_path.name[:-len(".fernet")]
        (CV_DIR / filename).write_bytes(plaintext)
        found.add(filename)
    missing = expected - found
    if missing:
        raise RuntimeError("Encrypted CV files missing: " + ", ".join(sorted(missing)))

    SENT_FILE.write_text(str(state.get("sent_applications_jsonl", "")), encoding="utf-8")


async def poll_and_process(state: dict[str, Any]) -> None:
    # Import only after secrets and encrypted private profile have been loaded.
    import auto_jobfinder_cloud as processor

    api_id = int(required_env("TELEGRAM_API_ID"))
    api_hash = required_env("TELEGRAM_API_HASH")
    session = required_env("TELEGRAM_SESSION")
    last_ids = state.setdefault("last_message_ids", {})
    if not isinstance(last_ids, dict):
        raise RuntimeError("Encrypted Telegram cursor state is invalid")

    client = TelegramClient(StringSession(session), api_id, api_hash,
                            connection_retries=3, retry_delay=5)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized; renew TELEGRAM_SESSION")

        for channel in CHANNELS:
            try:
                entity = await client.get_entity(channel)
                current_cursor = last_ids.get(channel)
                if current_cursor is None:
                    latest = await client.get_messages(entity, limit=1)
                    latest_id = int(latest[0].id) if latest else 0
                    last_ids[channel] = latest_id
                    print(f"[{channel}] initial baseline set; existing posts skipped")
                    continue

                cursor = int(current_cursor)
                # reverse=True processes oldest-to-newest. min_id selects posts newer than cursor.
                async for message in client.iter_messages(entity, min_id=cursor, reverse=True):
                    message_id = int(message.id)
                    if message_id <= cursor:
                        continue
                    text = (getattr(message, "message", None) or "").strip()
                    if not text:
                        cursor = message_id
                        last_ids[channel] = cursor
                        continue

                    post = {
                        "source": channel,
                        "channel": channel,
                        "message_id": message_id,
                        "date": message.date.isoformat() if message.date else "",
                        "text": text,
                    }
                    print(f"[{channel}] new post id={message_id}; analyzing")
                    try:
                        ok = processor.process_post(post)
                    except Exception as exc:
                        print(f"POST PROCESS ERROR: {type(exc).__name__}; post will retry")
                        ok = False
                    if ok is False:
                        # Do not advance beyond this post. The next scheduled run retries it.
                        print(f"[{channel}] cursor held at {cursor}; failed post will retry")
                        break

                    cursor = message_id
                    last_ids[channel] = cursor

            except Exception as exc:
                # Do not expose message bodies, contact details, or credential-bearing errors in public logs.
                print(f"[{channel}] Telegram polling error: {type(exc).__name__}; cursor preserved")
                continue
    finally:
        await client.disconnect()


def main() -> int:
    print(f"JobFinder cloud runner version: {RUNNER_VERSION}")
    # Fail fast if required application credentials are absent.
    required_env("GROQ_API_KEY")
    required_env("GMAIL_APP_PASSWORD")
    fernet = get_fernet()
    state = read_encrypted_json(STATE_ENC, fernet)
    original_state = copy.deepcopy(state)

    try:
        restore_private_files(fernet, state)
        asyncio.run(poll_and_process(state))
    except Exception as exc:
        print(f"JOBFINDER RUN ERROR: {type(exc).__name__}: {str(exc)[:180]}")
        result_code = 1
    else:
        result_code = 0
    finally:
        # Persist Telegram cursors and sent-application deduplication history only as ciphertext.
        if SENT_FILE.exists():
            state["sent_applications_jsonl"] = SENT_FILE.read_text(encoding="utf-8")
        if state != original_state:
            write_encrypted_json(STATE_ENC, state, fernet)
            print("Encrypted state updated")
        else:
            print("No state changes")

    return result_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # No traceback to prevent accidental disclosure in public workflow logs.
        print(f"FATAL: {type(exc).__name__}: {str(exc)[:180]}")
        sys.exit(1)
