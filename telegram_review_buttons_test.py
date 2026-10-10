from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


EXPECTED_BOT_USERNAME = "JobFinderdz_bot"


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
            detail = str(json.loads(exc.read().decode("utf-8")).get("description") or "")
        except Exception:
            pass
        raise RuntimeError(f"Telegram API HTTP {exc.code}: {detail[:160]}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Telegram API connection failed ({type(exc).__name__})") from None
    except json.JSONDecodeError:
        raise RuntimeError("Telegram returned an unreadable response") from None
    if not isinstance(result, dict) or not result.get("ok"):
        detail = str(result.get("description") or "Telegram rejected the request") if isinstance(result, dict) else "Unexpected response"
        raise RuntimeError(f"Telegram API error: {detail[:160]}")
    return result.get("result") or {}


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_BOT_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_CHAT_ID repository secret.")
        return 1

    me = telegram_api(token, "getMe", {})
    username = str(me.get("username") or "")
    if username.casefold() != EXPECTED_BOT_USERNAME.casefold():
        print(f"Wrong bot token: expected @{EXPECTED_BOT_USERNAME}, received @{username or 'unknown'}.")
        return 1

    telegram_api(token, "sendMessage", {
        "chat_id": chat_id,
        "text": (
            "🧪 اختبار آمن لأزرار مراجعة JobFinder\n\n"
            "اضغط زر القبول التجريبي في الرسالة الأولى، وزر الرفض التجريبي في الثانية. "
            "بعد الضغط عليهما، شغّل JobFinder Telegram Control Test على master "
            "لمعالجة الضغطات.\n\n"
            "هذه أزرار اختبار فقط: لن تغيّر وظيفة حقيقية ولن ترسل أي بريد."
        ),
        "disable_web_page_preview": True,
    })

    telegram_api(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "اختبار زر القبول — لا توجد وظيفة حقيقية مرتبطة بهذا الزر.",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🧪 اختبار القبول", "callback_data": "jf:test:accept"}
            ]]
        },
        "disable_web_page_preview": True,
    })
    telegram_api(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "اختبار زر الرفض — لا توجد وظيفة حقيقية مرتبطة بهذا الزر.",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🧪 اختبار الرفض", "callback_data": "jf:test:reject"}
            ]]
        },
        "disable_web_page_preview": True,
    })
    print(f"PASS: sent two harmless review-button test messages to @{username}.")
    print("No job channels were scanned and no application emails were sent.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Telegram review button test failed: {str(exc)[:200]}")
        sys.exit(1)
