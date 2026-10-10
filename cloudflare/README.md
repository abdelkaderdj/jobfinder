# JobFinder Telegram Webhook (Cloudflare Workers)

This Worker acknowledges inline-button presses immediately, then dispatches a narrowly scoped GitHub Actions run to apply the decision using JobFinder's encrypted state. It does not read job channels or send job applications itself.

## Worker environment variables and secrets

Deploy the contents of \`cloudflare/telegram-webhook.js\` as a Cloudflare Worker. In **Settings → Variables and Secrets**, add:

| Name | Type | Value |
|---|---|---|
| \`TELEGRAM_BOT_TOKEN\` | Secret | The BotFather token for \`@JobFinderdz_bot\` |
| \`TELEGRAM_BOT_CHAT_ID\` | Secret or variable | Your private Telegram chat ID |
| \`TELEGRAM_WEBHOOK_SECRET\` | Secret | A random string using only letters, numbers, underscores, and hyphens |
| \`GITHUB_DISPATCH_TOKEN\` | Secret | A GitHub fine-grained PAT for this repository |
| \`GITHUB_OWNER\` | Variable | \`abdelkaderdj\` |
| \`GITHUB_REPO\` | Variable | \`jobfinder\` |
| \`GITHUB_WORKFLOW_FILE\` | Variable | \`telegram-webhook-event.yml\` |
| \`GITHUB_REF\` | Variable | \`master\` |

The fine-grained GitHub token should be restricted to the \`abdelkaderdj/jobfinder\` repository and grant **Actions: Read and write**. Never commit this token or paste it into chat.

## GitHub secrets

In **Repository → Settings → Secrets and variables → Actions**, add:

- \`TELEGRAM_WEBHOOK_URL\`: the deployed Worker HTTPS URL, for example the \`workers.dev\` URL displayed by Cloudflare.
- \`TELEGRAM_WEBHOOK_SECRET\`: exactly the same random secret configured in the Worker.

The webhook setup workflow calls Telegram's \`setWebhook\` API and restricts updates to private messages and callback queries. Once the webhook is enabled, Telegram's \`getUpdates\` polling is no longer available for this bot; the regular JobFinder workflow switches to webhook mode when \`TELEGRAM_WEBHOOK_URL\` is configured.

## Security and expected behavior

- The Worker validates the Telegram secret header and your configured private chat/user ID.
- Callback presses are acknowledged before GitHub Actions is dispatched; this should stop Telegram's spinner promptly.
- The Worker briefly replaces the button message with a processing notice and removes the buttons. GitHub Actions then replaces that notice with the final outcome.
- The workflow event handler returns before scanning job channels. Real application approval still passes JobFinder's recipient, CV, location, and duplicate guards.
- Keep \`cron-job.org\` disabled during the first live test.
