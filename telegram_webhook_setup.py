from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request


def api(token: str, method: str, payload: dict) -> dict:
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
            detail = str(json.loads(exc.read().decode("utf-8")).get("description") or "")
        except Exception:
            pass
        raise RuntimeError(f"Telegram API HTTP {exc.code}: {detail[:160]}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Telegram API request failed ({type(exc).__name__})") from None
    except json.JSONDecodeError:
        raise RuntimeError("Telegram returned an unreadable response") from None
    if not isinstance(result, dict) or not result.get("ok"):
        detail = str(result.get("description") or "Telegram rejected the request") if isinstance(result, dict) else "Unexpected response"
        raise RuntimeError(f"Telegram API error: {detail[:160]}")
    return result.get("result") or {}


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not token or not webhook_url or not webhook_secret:
        print("Missing one of: TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_URL, TELEGRAM_WEBHOOK_SECRET")
        return 1
    if not webhook_url.startswith("https://"):
        print("TELEGRAM_WEBHOOK_URL must start with https://")
        return 1
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", webhook_secret):
        print("TELEGRAM_WEBHOOK_SECRET must use only letters, digits, underscores, and hyphens")
        return 1

    bot_info = api(token, "getMe", {})
    username = str(bot_info.get("username") or "")
    if username.casefold() != "jobfinderdz_bot":
        print(f"Wrong Telegram token; expected @JobFinderdz_bot, received @{username or 'unknown'}.")
        return 1

    api(token, "setWebhook", {
        "url": webhook_url,
        "secret_token": webhook_secret,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    })
    info = api(token, "getWebhookInfo", {})
    print(f"Webhook configured for @{username}: {bool(info.get('url'))}")
    print(f"Pending Telegram updates: {int(info.get('pending_update_count') or 0)}")
    if info.get("last_error_message"):
        print(f"Last Telegram webhook delivery error: {str(info['last_error_message'])[:160]}")
    else:
        print("Telegram reports no recent webhook delivery error.")
    print("getUpdates polling is now disabled for this bot; webhook mode will be used.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Webhook setup failed: {str(exc)[:200]}")
        sys.exit(1)
