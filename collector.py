#!/usr/bin/env python3
"""
Pentagon Pizza Watch — live collector + alerting server.

Polls live busyness for pizza places near the Pentagon, computes the Pizza
Index, sends Telegram alerts on spikes, and serves the dashboard at
http://localhost:8787 in live mode.

Two data sources (pick with SOURCE=):
  google   (default) Headless Chrome reads Google Maps' "Popular times" panel,
           e.g. "Currently 72% busy, usually 35% busy". No API key needed.
  besttime BestTime.app foot-traffic API (needs BESTTIME_KEY).

Setup (Google mode)
  pip install requests playwright
  python -m playwright install chromium
  export TELEGRAM_TOKEN=123:abc             # optional, from @BotFather
  export TELEGRAM_CHAT=123456789            # optional, your chat id
  python collector.py                        # put pizza_watch.html next to it
  # add HEADLESS=0 to watch the browser work

Notes
- Scraping Google Maps is against Google's Terms of Service. Keep the poll
  gentle (default every 15 min, random pauses between venues) and expect
  Google to rate-limit or change the page now and then.
- Google only shows the live bar when it has enough data, so a venue may read
  "n/a" at quiet hours; it's left out of the index until it comes back.
- Edit WATCHLIST to change venues. A street address helps Maps pick the right one.
"""
import json, os, random, re, sys, threading, time, datetime as dt
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo
import requests

WATCHLIST = [
    {"id": "dominos",    "name": "Domino's Pizza",       "address": "Pentagon City, Arlington, VA"},
    {"id": "papajohns",  "name": "Papa John's Pizza",    "address": "Pentagon City, Arlington, VA"},
    {"id": "extreme",    "name": "Extreme Pizza",        "address": "Crystal City, Arlington, VA"},
    {"id": "wethepizza", "name": "We, The Pizza",        "address": "Crystal City, Arlington, VA"},
    {"id": "district",   "name": "District Pizza Palace","address": "Arlington, VA"},
    {"id": "freddies",   "name": "Freddie's Beach Bar",  "address": "555 23rd St S, Arlington, VA", "inverse": True},
]

SOURCE     = os.environ.get("SOURCE", "google").lower()
HEADLESS   = os.environ.get("HEADLESS", "1") != "0"
KEY        = os.environ.get("BESTTIME_KEY", "")
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT    = os.environ.get("TELEGRAM_CHAT", "")
POLL_SEC   = int(os.environ.get("POLL_SEC", "900"))    # 15 min; live data is ~hourly
INDEX_THR  = int(os.environ.get("INDEX_THR", "55"))
DELTA_THR  = int(os.environ.get("DELTA_THR", "30"))
PORT       = int(os.environ.get("PORT", "8787"))
HERE       = Path(__file__).resolve().parent
LOG        = HERE / "pizza_history.jsonl"

state = {"venues": [], "index": 0, "updated": None, "above": False, "cool": {}, "history": []}
FEED = HERE / "feed.json"
if FEED.exists():                      # resume alert state between cloud runs
    try:
        state.update(json.loads(FEED.read_text()))
    except Exception:
        pass


# ---------------------------------------------------------------- Google Maps
ET = ZoneInfo("America/New_York")
LIVE_RE  = re.compile(r"Currently\s+(\d+)%\s+busy,\s+usually\s+(\d+)%\s+busy", re.I)
LIVE1_RE = re.compile(r"Currently\s+(\d+)%\s+busy", re.I)
HOUR_RE  = re.compile(r"(\d+)%\s+busy\s+at\s+(\d{1,2})\s*([AP]M)", re.I)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


def _dismiss_consent(page):
    """EU/UK consent wall: choose 'Reject all' if it appears."""
    if "consent." in page.url:
        for label in ("Reject all", "Reject All"):
            btn = page.get_by_role("button", name=label)
            if btn.count():
                btn.first.click()
                page.wait_for_load_state("domcontentloaded")
                return


def _usual_from_histogram(labels):
    """No live bar: read today's usual % for the current ET hour.
    Maps lists 7 day-histograms Sunday→Saturday; a new day starts when the hour goes back down."""
    now = dt.datetime.now(ET)
    days, cur, last_h = [], [], -1
    for lab in labels:
        m = HOUR_RE.search(lab)
        if not m:
            continue
        h = int(m[2]) % 12 + (12 if m[3].upper() == "PM" else 0)
        if h <= last_h and cur:
            days.append(cur); cur = []
        cur.append((h, int(m[1]))); last_h = h
    if cur:
        days.append(cur)
    if len(days) < 7:
        return 0
    today = days[(now.weekday() + 1) % 7]          # Python Mon=0 → Maps Sun=0
    return dict(today).get(now.hour, 0)


def _google_one(page, v):
    q = quote_plus(f"{v['name']} {v['address']}")
    page.goto(f"https://www.google.com/maps/search/?api=1&query={q}&hl=en",
              wait_until="domcontentloaded", timeout=45000)
    _dismiss_consent(page)
    # A search can land on a results list instead of the place; open the first hit.
    try:
        page.wait_for_selector("h1", timeout=8000)
    except Exception:
        first = page.locator("a.hfpxzc").first
        if not first.count():
            raise RuntimeError("place not found on Maps")
        first.click()
        page.wait_for_selector("h1", timeout=12000)
    page.wait_for_timeout(2500)                     # let the Popular times panel render
    page.mouse.wheel(0, 1500); page.wait_for_timeout(1500)
    labels = page.eval_on_selector_all(
        "[aria-label]", "els => els.map(e => e.getAttribute('aria-label'))")
    labels = [l for l in labels if l and "busy" in l]
    blob = "\n".join(labels)
    m = LIVE_RE.search(blob)
    if m:
        return int(m[1]), int(m[2])
    usual = _usual_from_histogram(labels)
    m = LIVE1_RE.search(blob)
    return (int(m[1]) if m else None), usual


def google_fetch_all(watchlist):
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(locale="en-US", timezone_id="America/New_York",
                                  user_agent=UA, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        for v in watchlist:
            try:
                out[v["id"]] = _google_one(page, v)
            except Exception as e:
                print(f"{v['name']}: {e}")
                out[v["id"]] = (None, 0)
            time.sleep(random.uniform(4, 9))        # be gentle
        browser.close()
    return out


# ---------------------------------------------------------------- BestTime
def fetch_live(v):
    """Return (live, usual) busyness 0-100, or (None, usual) if no live data."""
    r = requests.post("https://besttime.app/api/v1/forecasts/live", params={
        "api_key_private": KEY, "venue_name": v["name"], "venue_address": v["address"],
    }, timeout=30)
    a = r.json().get("analysis", {})
    usual = a.get("venue_forecasted_busyness") or 0
    live = a.get("venue_live_busyness") if a.get("venue_live_busyness_available") else None
    return live, usual


# --- scoring: identical to the dashboard's JS ---
def score(live, usual):
    return max(0.0, min(2.0, (live - usual) / max(usual, 15))) * 50

def pizza_index(rows):
    pizza = [r for r in rows if not r.get("inverse") and r["live"] is not None]
    if not pizza:
        return 0
    top = sorted((score(r["live"], r["usual"]) for r in pizza), reverse=True)[:3]
    idx = sum(top) / len(top)
    spiking = sum(1 for r in pizza if r["live"] - r["usual"] >= DELTA_THR)
    idx *= 1 + 0.15 * max(0, spiking - 1)
    bar = next((r for r in rows if r.get("inverse") and r["live"] is not None), None)
    if bar and bar["usual"] >= 25 and bar["live"] < bar["usual"] * 0.6:
        idx += 10
    return round(min(100, idx))


def notify(msg):
    print(f"[ALERT] {msg}")
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": "🍕 " + msg}, timeout=15)
        except Exception as e:
            print("telegram failed:", e)


def poll_once():
    rows = []
    g = google_fetch_all(WATCHLIST) if SOURCE == "google" else None
    for v in WATCHLIST:
        if g is not None:
            live, usual = g[v["id"]]
        else:
            try:
                live, usual = fetch_live(v)
            except Exception as e:
                print(f"{v['name']}: {e}")
                live, usual = None, 0
        rows.append({"id": v["id"], "name": v["name"], "inverse": v.get("inverse", False),
                     "live": live, "usual": usual})
    idx = pizza_index(rows)
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    for r in rows:  # per-venue spike alerts, 2-hour cooldown each
        if r["live"] is None:
            continue
        cd = state["cool"].get(r["id"], 0)
        hit = (r["usual"] >= 25 and r["live"] < r["usual"] * 0.6) if r["inverse"] \
              else r["live"] - r["usual"] >= DELTA_THR
        if hit and time.time() > cd:
            state["cool"][r["id"]] = time.time() + 7200
            notify(f"{r['name']} {'unusually QUIET' if r['inverse'] else 'SPIKING'}: "
                   f"{r['live']}% vs {r['usual']}% usual")

    if idx >= INDEX_THR and not state["above"]:
        state["above"] = True
        notify(f"Pizza Index {idx} crossed {INDEX_THR}. Something may be cooking.")
    elif idx < INDEX_THR - 8 and state["above"]:
        state["above"] = False
        notify(f"Pizza Index back to {idx}. Standing down.")

    state["history"] = (state.get("history", []) + [idx])[-96:]   # ~24h at 15-min runs
    state.update(venues=rows, index=idx, updated=now)
    FEED.write_text(json.dumps(state))
    with LOG.open("a") as f:
        f.write(json.dumps({"t": now, "index": idx, "venues": rows}) + "\n")
    print(f"{now}  index={idx}  " + "  ".join(f"{r['id']}={r['live']}/{r['usual']}" for r in rows))


def loop():
    while True:
        poll_once()
        time.sleep(POLL_SEC)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(HERE), **k)

    def do_GET(self):
        if self.path.startswith("/api/feed"):
            body = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/index.html"):
            html = (HERE / "pizza_watch.html").read_text(encoding="utf-8")
            body = ("<!doctype html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "</head><body>" + html + "</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if "--once" in sys.argv:            # used by the GitHub Actions schedule
        poll_once()
        raise SystemExit(0)
    if SOURCE == "besttime" and not KEY:
        raise SystemExit("Set BESTTIME_KEY (private key from https://besttime.app), or use SOURCE=google.")
    print(f"Source: {SOURCE}")
    threading.Thread(target=loop, daemon=True).start()
    print(f"Dashboard: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
