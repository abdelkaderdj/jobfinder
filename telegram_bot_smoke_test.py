from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


EXPECTED_BOT_USERNAME = "JobFinderdz_bot"
TEST_MESSAGE = (
    "✅ تم اختبار اتصال بوت JobFinder بنجاح.\n"
    "هذه رسالة اختبار فقط: لم يتم تحليل عروض ولم تُرسل أي طلبات توظيف."
)


def telegram_api(token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = str(body.get("description") or "")
        except Exception:
            pass
        # Do not print the request URL because it contains the bot token.
        if detail:
            raise RuntimeError(f"Telegram API returned HTTP {exc.code}: {detail[:160]}") from None
        raise RuntimeError(f"Telegram API returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not reach Telegram API ({type(exc).__name__})") from None
    except json.JSONDecodeError:
        raise RuntimeError("Telegram returned an unreadable response") from None

    if not isinstance(result, dict) or not result.get("ok"):
        detail = str(result.get("description") or "Telegram rejected the request") if isinstance(result, dict) else "Unexpected Telegram response"
        raise RuntimeError(f"Telegram API error: {detail[:160]}")
    return result.get("result") or {}


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_BOT_CHAT_ID", "").strip()
    if not token:
        print("Missing repository secret: TELEGRAM_BOT_TOKEN")
        return 1
    if not chat_id:
        print("Missing repository secret: TELEGRAM_BOT_CHAT_ID")
        return 1

    bot = telegram_api(token, "getMe", {})
    username = str((bot or {}).get("username") or "")
    if username.casefold() != EXPECTED_BOT_USERNAME.casefold():
        print(
            f"Wrong bot token: expected @{EXPECTED_BOT_USERNAME}, "
            f"received @{username or 'unknown'}."
        )
        return 1

    telegram_api(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": TEST_MESSAGE,
            "disable_web_page_preview": True,
        },
    )
    print(f"PASS: authenticated as @{username}; test message sent to configured private chat.")
    print("No job channels were read and no application emails were sent.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Telegram smoke test failed: {str(exc)[:200]}")
        sys.exit(1)
