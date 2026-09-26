# SwingLab Signals

Type any NSE stock, see what 5 backtested trading strategies currently say
about it. 500 stocks answer instantly (pre-computed); anything else gets
fetched and analyzed live, right on the page — no need to ask anyone to
add it.

## Run it on your own computer first

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 — that's the whole app, frontend and backend
together. Try a big name (instant) and a small/recent one (a few seconds).

## Put it on the internet (so it works from your phone, anywhere)

This needs two accounts that only you can create — I can't sign up for
services on your behalf. Both are free.

**Step 1 — GitHub** (if you don't already have an account: github.com → Sign up)
1. Create a new repository (github.com → the `+` in the top right → New repository). Any name, e.g. `swinglab-signals`.
2. Push this folder to it:
   ```bash
   cd swinglab-signals-app
   git init
   git add .
   git commit -m "SwingLab Signals"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/swinglab-signals.git
   git push -u origin main
   ```

**Step 2 — Render** (render.com → Sign up, the free tier is enough)
1. Dashboard → **New** → **Blueprint**.
2. Connect your GitHub account, pick the repo you just pushed.
3. Render reads `render.yaml` in this folder automatically and sets
   everything up — just click **Apply**.
4. Wait ~2-3 minutes for the first build. You'll get a URL like
   `https://swinglab-signals.onrender.com` — that's it, live.

Every time you `git push` again, Render redeploys automatically.

## The one real limitation to know about

Render's **free tier** doesn't keep a persistent disk — the small database
that caches stocks you've looked up gets wiped whenever the free instance
restarts (which happens after ~15 minutes of no traffic, and on every
redeploy). In practice this means:
- The 500 pre-built stocks are always there (they get reloaded from
  `data/lookup_bundle.json` on every startup).
- A stock you fetched live stays instant *for a while*, then eventually
  needs re-fetching (still just a few seconds) after the instance sleeps.

If you want live-fetched stocks to stay cached forever, Render's paid tier
(~$7/month) adds a persistent disk — a one-line change to `render.yaml`
(`disk: { name: data, mountPath: /opt/render/project/src/data, sizeGB: 1 }`).
Not necessary to get started.

## What's actually in here

```
app/
  main.py         FastAPI app — the 3 endpoints the frontend calls
  engine.py       fetches + scores ONE stock live (Yahoo + screener.in)
  db.py           tiny SQLite cache
  swinglab/       the backtesting engine itself (price data, fundamentals,
                  the trend-following strategy, portfolio simulation)
data/
  lookup_bundle.json     the 500 pre-computed stocks
  universe_context.json  ranking data a new stock gets compared against
  verdicts.json           which parameters each strategy uses
static/
  index.html      the whole frontend — one file, no build step
```

## Known scope limits, stated plainly

- Live analysis only computes **today's signal** for Strategies 2-5, not a
  full historical trade log for that specific stock (that needs re-ranking
  the whole 500-stock universe on every historical date, which is too slow
  to do on a live request). Strategy 1 *does* get a real, full backtest
  live, since that one only needs the stock's own price history.
- Fundamentals (Strategies 3 & 5) come from screener.in's public pages —
  free, but not an official API. If their page layout changes, that part
  can break; price data (Yahoo) is more stable.
- This is a research tool. Nothing here is investment advice.
