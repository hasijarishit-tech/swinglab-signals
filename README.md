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
- **Investing Journey**, **Methodology** — the story so far, and a fully
  transparent writeup of data sources, point-in-time discipline, and every
  known limitation.

Command palette: `⌘K` or `/` anywhere on the site.

## Signing in

The site sits behind a simple login (`app/auth.py`) — not a real multi-user
system (there's no per-user data; watchlist/journal live in each visitor's
own browser), just enough to keep the public URL from being wide open.

- **Demo account** — `demo` / `demo1234`. Meant to be public; shown right on
  the login page, with a "Continue as demo" button.
- **Your own login** — set `SWINGLAB_USERNAME` and `SWINGLAB_PASSWORD` as
  environment variables (Render → your service → **Environment**, not in
  `render.yaml`, since that file is in this public repo). Without them, the
  app generates a random password at startup and prints it once to the
  server's own logs (Render → **Logs**) — safe, but you'll need to check the
  logs again after every restart. Setting the env vars gives you a stable
  login.
- Also set `SWINGLAB_SECRET_KEY` (any long random string) so login sessions
  survive a restart/redeploy instead of everyone being signed out. Without
  it, a random key is generated per-process — secure, just less convenient
  on Render's free tier, which restarts often.

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
