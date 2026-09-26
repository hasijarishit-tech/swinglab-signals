# SwingLab — Personal Investing Research Lab

A systematic framework for studying Indian equities: fundamentals, valuation,
momentum, quality, market context and risk, for any NSE company — not just
the 500 that are pre-computed. Personal research project, not investment
advice.

## Pages

- **Home** — live Nifty 50 / Nifty Midcap 50 / India VIX snapshot, market
  breadth, and a "Today's Research Desk" (momentum leaders/laggards) —
  research categories, not buy/sell calls.
- **Company Research Terminal** (`#/company/TICKER`) — the centerpiece.
  An independent-dimension snapshot matrix (quality, valuation, growth,
  momentum, profitability, balance sheet, cash flow, market trend, risk —
  no single "buy score"), a live log-scale price chart with the strategy
  engine's real buy/sell markers, full fundamental history with charts and
  data-supported observations, a valuation panel (current P/E/P/B/EV-EBITDA
  vs. the company's own 5-year median and the Nifty 500 median), the 5
  existing technical strategies (renamed: Long-Term Trend, Momentum, Value,
  Price Strength, Quality), and a personal investment-thesis canvas saved
  in your browser. A **Long Term / Swing** toggle reorders the page to
  emphasize fundamentals or technicals.
- **Strategy Lab** — the full comparison table (CAGR, vol, Sharpe, Sortino,
  max drawdown, Calmar, win rate, turnover) plus interactive growth-of-₹1,
  drawdown, rolling-Sharpe and calendar-year-return charts, all from the
  original Nifty 500 backtest.
- **Watchlist**, **Daily Research Journal** — saved in your browser
  (`localStorage`), no account needed.
- **Methodology** — a fully transparent writeup of data sources,
  point-in-time discipline, and every known limitation.

Command palette: `⌘K` or `/` anywhere on the site.

## Signing in

The site sits behind a login (`app/auth.py`) — not a real multi-tenant
product (there's no per-user server-side data; watchlist/journal live in
each visitor's own browser), just enough to keep the public URL from being
wide open, plus real self-service signup for anyone who wants their own.

- **Sign up** — anyone can create a username/password on the login page
  ("Create an account"). Passwords are hashed with a per-user random salt
  (PBKDF2, 200,000 rounds) — never stored or logged in plaintext. **Real
  limitation, stated plainly**: these accounts live in the same SQLite file
  as the stock cache, which sits on Render's free-tier *ephemeral* disk —
  wiped on redeploy and after a period of inactivity (see below). A
  signed-up account can disappear when that happens. Fine for a session of
  research; not yet durable across restarts. Fixed the same way the stock
  cache's durability would be: add a persistent disk on Render's paid tier.
- **Demo account** — `demo` / `demo1234`. Meant to be public; shown right on
  the login page, with a "Continue as demo" button.
- **The owner login** — set `SWINGLAB_USERNAME` and `SWINGLAB_PASSWORD` as
  environment variables (Render → your service → **Environment**, not in
  `render.yaml`, since that file is in this public repo). This is the one
  login guaranteed to survive a restart, since it lives in Render's config,
  not the wipeable disk. Without them, the app generates a random password
  at startup and prints it once to the server's own logs (Render →
  **Logs**).
- Also set `SWINGLAB_SECRET_KEY` (any long random string) so login sessions
  survive a restart/redeploy instead of everyone being signed out. Without
  it, a random key is generated per-process — secure, just less convenient
  on Render's free tier, which restarts often.

## Chat assistant ("Ask SwingLab")

A floating chat button (bottom-right, once signed in) answers questions
about whatever real data is already on the page — it's given that data as
context and instructed to only cite numbers from it, never invent one, and
never give a direct buy/sell call. Backed by Google's Gemini API
(`app/chat.py`).

To turn it on, set `GEMINI_API_KEY` as an environment variable (Render →
your service → **Environment** — never in `render.yaml` or any committed
file, since this repo is public). Get a key at
https://aistudio.google.com/apikey. Without it, the chat button still
appears but replies with a plain "not set up yet" message instead of
erroring. `GEMINI_MODEL` (default `gemini-2.0-flash`) is also
env-overridable if that model name is ever retired.

## Is the data live?

- **Any company you search**: fetched and scored from Yahoo (price) and
  screener.in (fundamentals) at the moment you ask.
- **Everything, including the 500 pre-built companies**: a cached result
  is only reused while less than 4 days old (`app/db.py`) — past that, the
  next request re-analyzes it live. Nothing shown is ever more than a few
  days old.
- **The price chart and the market snapshot**: always live, every time,
  no cache.

## Run it on your own computer

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

## Deploying it

Currently deployed on **Render's free tier**: https://swinglab-signals.onrender.com

Render's free plan sleeps after ~15 minutes idle (a visit after that takes
~30-60s to wake up) and has no persistent disk, so the on-demand cache
resets on every restart — the 500 pre-built companies always come back
(they reload from `data/lookup_bundle.json`), a freshly-searched one
doesn't.

**For everyday, always-on use**, two realistic upgrades from here:
- **Render's paid tier** (~$7/month) — no sleep, add a persistent disk with
  a one-line change to `render.yaml` (`disk: {name: data, mountPath:
  /opt/render/project/src/data, sizeGB: 1}`). Same repo, same deploy flow.
- **Fly.io or Railway** — similar free-tier sleep behavior to Render;
  their paid tiers are comparable in price. Not a meaningfully different
  trade-off from staying on Render's paid tier.

Redeploy by pushing to `main` — Render rebuilds automatically.

## What's actually in here

```
app/
  main.py          FastAPI app — all endpoints
  engine.py         fetches + scores ONE stock live, builds its price
                     chart and full fundamentals/valuation history
  market.py          live Nifty/VIX snapshot + universe breadth
  db.py               SQLite cache, 4-day freshness check
  swinglab/            the backtesting engine (price data, fundamentals
                        scraping, the trend strategy, portfolio simulation)
data/
  lookup_bundle.json      the 500 pre-computed companies
  universe_context.json    ranking data a new company is compared against
  verdicts.json              which parameters each strategy uses
  strategy_lab.json           the Strategy Lab's comparison table + charts
static/
  index.html          the entire frontend — one file, hash-routed, no
                       build step
  charts/              legacy static PNGs (turnover chart; the rest have
                       interactive replacements in Strategy Lab)
```

## Known limitations, stated plainly

- Live analysis for a company outside the pre-built 500 only computes
  *today's* signal for Strategies 2–5, not a full historical trade log for
  that specific company (needs re-ranking the whole universe on every past
  date — too slow to do live).
- Fundamentals history runs to about 12 years (FY2015+), shorter than the
  16-year price history.
- Historical P/E divides today's split-adjusted price convention against
  each year's *as-reported* EPS — for a company with a stock split partway
  through its history, older P/E figures can be skewed. See Methodology.
- Banks/NBFCs are excluded from fundamentals entirely (different statement
  format — Revenue/Financing-Profit instead of Sales/EBITDA).
- "Sector median" valuation isn't implemented — the Nifty 500 universe
  median is shown instead, labeled as such.
- No portfolio-level correlation/exposure analysis yet (a "Portfolio Lab").
  Watchlist + per-company research cover the gap for now.
- Everything on this site is a research output, not investment advice.
