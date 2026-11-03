# Pentagon Pizza Watch

Checks Google Maps "Popular times" for pizza places near the Pentagon every
15 minutes (GitHub Actions), sends spikes to Telegram, and hosts a live
dashboard on GitHub Pages.

Setup steps: see the chat, or:
1. Telegram: @BotFather → /newbot → copy token. Message your bot, then open
   https://api.telegram.org/bot<TOKEN>/getUpdates and copy "chat":{"id":...}
2. GitHub repo → Settings → Secrets and variables → Actions → add
   TELEGRAM_TOKEN and TELEGRAM_CHAT.
3. Settings → Pages → Deploy from branch → main / root.
4. Actions tab → Pizza Watch scan → Run workflow.
