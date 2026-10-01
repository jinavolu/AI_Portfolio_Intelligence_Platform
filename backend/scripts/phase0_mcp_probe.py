"""Phase 0 spike: list Kite MCP tools, flag order tools, and save real read-only payloads.

    uv pip install -e .[mcp]
    uv run python scripts/phase0_mcp_probe.py [--url https://mcp.kite.trade/mcp]

Output goes to phase0_payloads/ (git-ignored: it contains your real portfolio).
Only tools in KiteMcpAdapter.READ_TOOLS are ever called.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.broker.base import FORBIDDEN_NAME_FRAGMENTS  # noqa: E402
from app.broker.kite_mcp_adapter import READ_TOOLS  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "phase0_payloads"

PROBES: list[tuple[str, dict]] = [
    ("get_profile", {}),
    ("get_holdings", {}),
    ("get_positions", {}),
    ("get_margins", {}),
    ("get_quotes", {"instruments": ["NSE:INFY"]}),
    ("get_ohlc", {"instruments": ["NSE:INFY"]}),
    ("search_instruments", {"query": "INFY", "filter_on": "tradingsymbol"}),
]


def _dump(result) -> dict:
    is_error = getattr(result, "is_error", None)
    return {"is_error": getattr(result, "isError", None) if is_error is None else is_error,
            "content": [getattr(c, "text", None) or str(c) for c in getattr(result, "content", [])]}


async def main(url: str) -> None:
    from mcp import ClientSession

    try:  # mcp >= 2.0 yields (read, write)
        from mcp.client.streamable_http import streamable_http_client as connect
    except ImportError:  # mcp 1.x yields (read, write, get_session_id)
        from mcp.client.streamable_http import streamablehttp_client as connect

    OUT.mkdir(exist_ok=True)
    async with connect(url) as streams, ClientSession(streams[0], streams[1]) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        inventory = [{"name": t.name, "description": t.description,
                      "input_schema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None),
                      "order_tool": any(f in t.name.lower() for f in FORBIDDEN_NAME_FRAGMENTS),
                      "on_read_allowlist": t.name in READ_TOOLS} for t in tools]
        (OUT / "tools.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
        print(f"{len(tools)} tools; order tools exposed by server (never registered with the agent):")
        for t in inventory:
            if t["order_tool"]:
                print("   -", t["name"])
        missing = sorted(READ_TOOLS - {t.name for t in tools})
        if missing:
            print("Allowlisted tools the server does NOT expose (fix READ_TOOLS):", missing)

        if "login" in {t.name for t in tools}:
            print("\nLogin:", _dump(await session.call_tool("login", {}))["content"])
            input("Complete the browser login, then press Enter... ")

        for name, args in PROBES:
            if name not in {t.name for t in tools}:
                continue
            payload = _dump(await session.call_tool(name, args))
            (OUT / f"{name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"saved {name}.json (error={payload['is_error']})")

        found = json.loads((OUT / "search_instruments.json").read_text())["content"][0] \
            if (OUT / "search_instruments.json").exists() else None
        try:
            token = next(i["instrument_token"] for i in json.loads(found)
                         if i.get("tradingsymbol") == "INFY" and i.get("exchange") == "NSE")
            end = date.today()
            args = {"instrument_token": token, "from_date": f"{end - timedelta(days=3650)} 00:00:00",
                    "to_date": f"{end} 23:59:59", "interval": "day"}
            payload = _dump(await session.call_tool("get_historical_data", args))
            (OUT / "get_historical_data.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print("saved get_historical_data.json - check how far back the candles go")
        except Exception as e:  # noqa: BLE001 - spike script, report and continue
            print("historical probe skipped:", e)

    print(f"\nNow fill in docs/phase0-checklist.md using {OUT}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="https://mcp.kite.trade/mcp")
    asyncio.run(main(p.parse_args().url))
