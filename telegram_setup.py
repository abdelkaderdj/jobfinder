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
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Telegram Bot API request failed: {type(exc).__name__}") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("Telegram rejected the setup request; check the bot token")
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
        print(f"Telegram setup failed: {type(exc).__name__}")
        sys.exit(1)
