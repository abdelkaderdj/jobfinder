from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def api(token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Telegram error descriptions help distinguish an invalid token from an
        # active webhook or another API issue. Never print the request URL/token.
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = str(body.get("description") or "")
        except Exception:
            pass
        if not detail:
            detail = "Telegram returned an HTTP error without a readable description"
        raise RuntimeError(f"Telegram API HTTP {exc.code}: {detail[:180]}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach Telegram API ({type(exc).__name__}); retry the workflow"
        ) from None
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Telegram API response could not be read ({type(exc).__name__})"
        ) from None
    if not isinstance(result, dict) or not result.get("ok"):
        detail = str(result.get("description") or "Telegram rejected the request") if isinstance(result, dict) else "Unexpected Telegram response"
        raise RuntimeError(f"Telegram API error: {detail[:180]}")
    return result.get("result")


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Missing GitHub Secret: TELEGRAM_BOT_TOKEN")
        return 1

    updates = api(
        token,
        "getUpdates",
        {"timeout": 0, "limit": 100, "allowed_updates": ["message"]},
    )
    if not isinstance(updates, list):
        print("Telegram returned an unexpected update format")
        return 1

    processed_ids = []
    found = False
    for update in updates:
        if not isinstance(update, dict) or "update_id" not in update:
            continue
        processed_ids.append(int(update["update_id"]))
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = str(message.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text else ""
        if chat.get("type") != "private" or command not in {"/start", "/id"}:
            continue

        chat_id = chat.get("id")
        if chat_id is None:
            continue
        api(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"معرّف محادثتك الخاصة هو:\n{chat_id}\n\n"
                    "أضف هذا الرقم إلى GitHub Secrets باسم "
                    "TELEGRAM_BOT_CHAT_ID، ثم أعد تشغيل JobFinder. "
                    "لا ترسل رمز البوت إلى أي شخص."
                ),
                "disable_web_page_preview": True,
            },
        )
        print("Sent private-chat setup instructions.")
        found = True

    # Acknowledge processed updates so setup messages are not repeatedly handled.
    if processed_ids:
        api(
            token,
            "getUpdates",
            {
                "offset": max(processed_ids) + 1,
                "timeout": 0,
                "limit": 1,
                "allowed_updates": ["message"],
            },
        )

    if not found:
        print("No private /start or /id message found.")
        print("Open your bot in Telegram, press Start, then run this workflow again.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Telegram setup failed: {str(exc)[:220]}")
        sys.exit(1)
