# Phase 0: data-source checklist

Fill this in from `backend/phase0_payloads/` after running `scripts/phase0_mcp_probe.py`.

## Kite MCP

| Check | Result | Notes |
| --- | --- | --- |
| Returns structured data we can parse reliably | ✅ | One text block of Kite Connect JSON per call; holdings/margins/quotes/OHLC/positions parse with `kite_mapping.py` unchanged. Holdings include `instrument_token`. Errors come back as `is_error` + plain text ("Failed to execute …", "Please log in first…"). |
| Tool names and output shapes are stable | ☐ | 22 tools on 2026-09-27. Re-run the probe in a week and diff `tools.json`. |
| Callable from our Python backend (not only an AI client) | ✅ | Works via the `mcp` Python SDK (2.2.0, streamable HTTP) after a browser login per MCP session. |
| Session/token lifetime known | ◐ | Login is bound to the MCP session id; the app shows a "Log in to Kite" banner when calls fail. Daily expiry assumed (Kite tokens expire ~06:00 IST); confirm. |
| Historical candles available, and how far back | ◐ | 10-year single request failed: Kite caps daily candles at ~2000 days per request. The adapter now uses windows of 1800 days. Depth still to confirm. |
| Tools exposed, including order tools | ✅ | Order tools exposed by server: place/modify/cancel_order, place/modify/delete_gtt_order (plus get_orders, get_gtts, get_order_history, get_order_trades). None are on `READ_TOOLS`; the adapter refuses them. Unused read tools: get_mf_holdings. |

## Decision per data type

| Data | Source (MCP / Kite Connect / other) | Cost | Freshness | Rate limits | Auth |
| --- | --- | --- | --- | --- | --- |
| Holdings | | | | | |
| Positions / margins | | | | | |
| Quotes / OHLC | | | | | |
| Historical candles | | | | | |
| Tradebook | Console CSV export | Free | Manual | — | Console login |
| EOD, full universe (adjusted) | | | | | |
| Sector classification | | | | | |
| Fundamentals (Phase 11) | NSE MarketLens (undocumented beta) | Free | Quarterly; fetched daily 07:00 IST | ~1 req/s, self-imposed | None |
| News (Phase 9) | Gemini with Google Search grounding; pages fetched and verified | Gemini quota | Daily 07:30 IST | Daily cap per feature | Gemini API key |
| Corporate announcements / results calendar | NSE corporate-announcements, board-meetings, corporate-actions | Free | Daily 07:30 and 18:30 IST | ~1 req/s, self-imposed | None |
