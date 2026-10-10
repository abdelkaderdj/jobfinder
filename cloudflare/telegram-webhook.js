const COMMANDS = new Set([
  "/start",
  "/help",
  "/status",
  "/today",
  "/pending",
  "/pause",
  "/resume",
]);

const CALLBACK_ACTIONS = new Set(["approve", "manual", "reject", "test"]);

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

async function telegramApi(env, method, payload) {
  if (!env.TELEGRAM_BOT_TOKEN) {
    throw new Error("Telegram bot token is not configured");
  }
  const response = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  const result = await response.json().catch(() => ({}));
  if (!response.ok || !result.ok) {
    throw new Error(`Telegram API ${method} failed with HTTP ${response.status}`);
  }
  return result.result;
}

async function dispatchToGitHub(env, inputs) {
  const owner = env.GITHUB_OWNER || "abdelkaderdj";
  const repo = env.GITHUB_REPO || "jobfinder";
  const workflow = env.GITHUB_WORKFLOW_FILE || "telegram-webhook-event.yml";
  const ref = env.GITHUB_REF || "master";
  if (!env.GITHUB_DISPATCH_TOKEN) {
    throw new Error("GitHub dispatch token is not configured");
  }
  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "accept": "application/vnd.github+json",
      "authorization": `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      "content-type": "application/json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "JobFinder-Telegram-Webhook",
    },
    body: JSON.stringify({ ref, inputs }),
  });
  if (response.status !== 204) {
    // Do not log response body: it may contain repository details.
    throw new Error(`GitHub workflow dispatch failed with HTTP ${response.status}`);
  }
}

async function sendCommandAcknowledgement(env, chatId, command) {
  const text = command === "/pause" || command === "/resume"
    ? "⏳ استلمت الأمر، جارٍ تحديث حالة JobFinder…"
    : "⏳ استلمت الأمر، جارٍ تجهيز الرد…";
  await telegramApi(env, "sendMessage", {
    chat_id: chatId,
    text,
    disable_web_page_preview: true,
  });
}

async function handleMessage(update, env) {
  const message = update.message;
  if (!message || !message.text) return;
  const chat = message.chat || {};
  const sender = message.from || {};
  const allowedChat = String(env.TELEGRAM_BOT_CHAT_ID || "");
  if (
    chat.type !== "private" ||
    !allowedChat ||
    String(chat.id) !== allowedChat ||
    String(sender.id) !== allowedChat
  ) {
    return;
  }

  const rawCommand = message.text.trim().split(/\s+/)[0] || "";
  const command = rawCommand.split("@")[0].toLowerCase();
  if (!COMMANDS.has(command)) return;

  try {
    await sendCommandAcknowledgement(env, chat.id, command);
  } catch (error) {
    // The dispatch should still proceed if the brief acknowledgement message fails.
    console.error("Telegram command acknowledgement failed:", error.message);
  }
  try {
    await dispatchToGitHub(env, {
      update_type: "command",
      command,
      action: "",
      pending_id: "",
      chat_id: String(chat.id),
      user_id: String(sender.id),
      message_id: "",
    });
  } catch (error) {
    console.error("JobFinder command dispatch failed:", error.message);
    await telegramApi(env, "sendMessage", {
      chat_id: chat.id,
      text: "⚠️ تعذر تشغيل JobFinder لمعالجة الأمر. لم يتم تنفيذ أي قرار.",
      disable_web_page_preview: true,
    }).catch(() => {});
  }
}

async function answerCallback(env, callback, text, alert = false) {
  try {
    await telegramApi(env, "answerCallbackQuery", {
      callback_query_id: callback.id,
      text: text.slice(0, 180),
      show_alert: alert,
    });
  } catch (error) {
    // A fast acknowledgement is attempted before starting GitHub Actions.
    console.error("Telegram callback acknowledgement failed:", error.message);
    throw error;
  }
}

async function handleCallback(update, env) {
  const callback = update.callback_query;
  if (!callback) return;
  const message = callback.message || {};
  const chat = message.chat || {};
  const sender = callback.from || {};
  const allowedChat = String(env.TELEGRAM_BOT_CHAT_ID || "");
  const authorized =
    chat.type === "private" &&
    allowedChat &&
    String(chat.id) === allowedChat &&
    String(sender.id) === allowedChat;

  if (!authorized) {
    await answerCallback(env, callback, "غير مصرح لك باستخدام هذا البوت.", true).catch(() => {});
    return;
  }

  const data = String(callback.data || "");
  const parts = data.split(":");
  if (parts.length !== 3 || parts[0] !== "jf" || !CALLBACK_ACTIONS.has(parts[1])) {
    await answerCallback(env, callback, "هذا الزر غير صالح.", true).catch(() => {});
    return;
  }
  const action = parts[1];
  const pendingId = parts[2];
  if (!pendingId || pendingId.length > 32) {
    await answerCallback(env, callback, "معرّف القرار غير صالح.", true).catch(() => {});
    return;
  }

  // Critical UX fix: answer the Telegram callback immediately, before dispatching
  // a GitHub Actions run, so the client's spinner does not wait for the CI runner.
  try {
    await answerCallback(env, callback, "تم استلام الضغط، جارٍ المعالجة…");
  } catch (_) {
    // Continue processing even if Telegram's callback acknowledgement itself failed.
  }

  try {
    await dispatchToGitHub(env, {
      update_type: "callback",
      command: "",
      action,
      pending_id: pendingId,
      chat_id: String(chat.id),
      user_id: String(sender.id),
      message_id: String(message.message_id || ""),
    });
  } catch (error) {
    console.error("JobFinder callback dispatch failed:", error.message);
    await telegramApi(env, "sendMessage", {
      chat_id: chat.id,
      text: "⚠️ لم أتمكن من تشغيل JobFinder لمعالجة هذا الزر. لم يتم تنفيذ القرار. حاول الضغط مجددًا.",
      disable_web_page_preview: true,
    }).catch(() => {});
    return;
  }

  // Remove the buttons to prevent accidental double taps while the cloud run is queued.
  // The GitHub runner edits this same message with the final decision after processing.
  await telegramApi(env, "editMessageText", {
    chat_id: chat.id,
    message_id: message.message_id,
    text: "⏳ تم استلام قرارك. يجري التحقق منه وتنفيذه بواسطة JobFinder…",
    disable_web_page_preview: true,
    reply_markup: { inline_keyboard: [] },
  }).catch((error) => {
    console.error("Could not set callback processing message:", error.message);
  });
}

export default {
  async fetch(request, env) {
    if (request.method === "GET") {
      return new Response("JobFinder Telegram webhook is ready.", {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }
    if (
      !env.TELEGRAM_WEBHOOK_SECRET ||
      request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_WEBHOOK_SECRET
    ) {
      return new Response("Forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch (_) {
      return json({ ok: false, error: "invalid JSON" }, 400);
    }

    try {
      if (update && update.callback_query) {
        await handleCallback(update, env);
      } else if (update && update.message) {
        await handleMessage(update, env);
      }
    } catch (error) {
      console.error("Webhook handler failed:", error.message);
      // Return 200 so Telegram does not keep redelivering a poisoned update.
    }
    return json({ ok: true });
  },
};
