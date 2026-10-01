# AI Portfolio Intelligence Platform

Personal, **read-only** portfolio intelligence for a Zerodha account. Python calculates; Gemini only explains.
No code path can place, modify or cancel an order.

## Run locally

```powershell
# backend (http://localhost:8000, API docs at /docs)
cd backend
uv venv; uv pip install -e ".[dev]"   # add ,ocr to read Feed images offline
uv run uvicorn app.main:create_app --factory --reload

# frontend (http://localhost:5173)
cd frontend
npm install; npm run dev
```

With Docker: `docker compose up` (Postgres + backend + frontend).

### Two Kite accounts at once

Each Kite user's theses, snapshots and alerts are kept apart in the same database (D12). To watch
two portfolios side by side, run a second backend and frontend. Each has its own Kite login:

```powershell
# second backend (port 8001); PI_INSTANCE lets it remember its own user across restarts
cd backend
$env:PI_INSTANCE="second"; $env:PI_APP_URL="http://localhost:5174"
uv run uvicorn app.main:create_app --factory --reload --port 8001

# second frontend (http://localhost:5174), talking to that backend
cd frontend
$env:API_TARGET="http://localhost:8001"; npm run dev -- --port 5174
```

Open 5173 and 5174 in separate tabs and log in to Kite with a different user in each (the header
shows which user a tab serves). If Zerodha's login page remembers the first user, log in to the
second in a private window. Telegram messages from both go to the one linked chat, prefixed with
the Kite user id. The first time after an upgrade, start one backend alone so it can update the
database before the second starts.

By default it runs on the **fixture broker**: a sample tradebook plus deterministic *synthetic* prices,
so every screen works without credentials. Set `PI_BROKER=kite_connect` and the Kite keys in
`backend/.env` (see `.env.example`) to use your real account.

## What's built (plan phases)

| Phase | Status | Where |
| --- | --- | --- |
| 0 Data-source spike | Done for Kite MCP (holdings, quotes, candles); other sources open | `backend/scripts/phase0_mcp_probe.py`, `docs/phase0-checklist.md` |
| 1 Skeleton | Done (no CI) | FastAPI, SQLAlchemy (SQLite/Postgres), Docker Compose, React |
| 2 Broker | Done | `app/broker/`: `BrokerAdapter`, fixture, Kite Connect, Kite MCP (allowlisted read tools only), Console tradebook import |
| 3 Portfolio Engine | Done | `app/engines/portfolio.py`: P&L, weights, sectors, FIFO lots, holding period, ST/LT tax flags, XIRR, dividends |
| 4 Technical Engine | Done | `app/engines/technical.py`: definitions written in the module docstring |
| 5 Thesis | Done | `app/thesis.py`, versioned in DB, editor on the Stock page |
| 6 Risk, snapshots, decision | Done | `app/engines/risk.py`, `app/snapshot.py` (hashes + diff), `app/decision.py` (gates 1–6 + horizon-weighted score), `app/scheduler.py` (daily snapshot 16:00 IST) |
| 7 ADK + Gemini | Done, untested against live Gemini | `app/ai/`: snapshot-hash cache, grounding check, retry, router, usage log, daily caps, read-only ADK tools |
| 8 Dashboard | Done | Dashboard, Holdings, Stock (chart, indicators, gates, thesis, explanation), Chat |
| 13 Alerts | Done (in-app + browser notifications) | `app/alerts.py`: transitions between snapshots; intraday refresh every 15 min in market hours |
| 10 Backtesting | Done; no rule set validated yet | `app/backtest/`: EOD Parquet + DuckDB store, same indicator/scoring code as live, costs + tax, out-of-sample split, criteria locked in `backtest_criteria.json` |
| 9 | In shadow mode | News: official NSE disclosures + verified press; rules-1.4.0 runs alongside the live rules (docs/decisions.md D11) |
| 11 | Done (monitoring) | Fundamentals from NSE MarketLens, business conditions (D8) |
| 12, 14 | Not started | |

Project decisions (signal safeguards, backtest sequence, sealed holdout, the pre-registered rules-1.1.0)
are in [docs/decisions.md](docs/decisions.md).

## Key rules, where they're enforced

- **Read-only broker**: `BrokerAdapter` has no order methods. `KiteConnectAdapter` wraps the client in a
  method allowlist, and `KiteMcpAdapter` refuses any tool not in `READ_TOOLS`.
- **Signals, not candidates**: BUY/SELL states are `*_SIGNAL` until the rule version (`rules-1.0.0`) is listed
  in `backend/validated_rules.json` (written by the future backtesting phase).
- **Missing is never zero**: missing indicators stay `null`, and gate 2 names them.
- **Grounding**: every number in an AI answer must match the snapshot it was given; otherwise it's retried once,
  then returned flagged and not cached.
- **Without `PI_GEMINI_API_KEY`**, explanations come from an offline template, labelled
  `model: offline-template`, so the cache and grounding pipeline still runs. Chat needs a real key and the `ai` extra.

## API

`GET /health` · `GET /api/portfolio` · `GET /api/holdings` · `GET /api/holdings/{symbol}` ·
`POST /api/holdings/{symbol}/explain` · `GET|PUT /api/theses[/{symbol}]` · `POST|GET /api/snapshots` ·
`GET /api/snapshots/diff` · `POST /api/tradebook` (Console CSV) · `GET /api/usage` · `POST /api/chat`

Personal use only. Sharing recommendations with others falls under SEBI research analyst rules.
