# Pentagon Pizza Watch

Checks Google Maps "Popular times" for pizza places near the Pentagon every
15 minutes with GitHub Actions and shows a live dashboard on GitHub Pages:
https://anandchawla16.github.io/Pizza-watch

## Signed-in Google session (needed for the pizza places)
Signed out, Google serves a "limited view" with no Popular times for most venues.
Use a spare Google account, not your main or work one.

**Option A — on a computer with Python**
```
pip install playwright && python -m playwright install chromium
python collector.py --login
```
Sign in in the window that opens, press Enter, then paste the contents of
`google_state.b64.txt` into the repo secret **GOOGLE_STATE**
(Settings → Secrets and variables → Actions).

**Option B — no Python**
In desktop Chrome, sign in to google.com with the spare account, install the
Cookie-Editor extension, open it on google.com → Export → JSON, and paste that
JSON into the **GOOGLE_STATE** secret.

Sessions expire every few weeks. When the dashboard says "limited view", repeat.

Edit WATCHLIST in collector.py to change venues.
