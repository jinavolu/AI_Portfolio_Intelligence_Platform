"""Long-lived Kite MCP session for the backend process.

MCP clients are async and their context managers must be entered and exited in
the same task, so the session runs on its own event loop in a daemon thread and
the synchronous adapter calls into it with run_coroutine_threadsafe.

`call_tool` here enforces the same read-only allowlist as KiteMcpAdapter, plus
`login`, the one non-data tool the app uses (via `login_url`).
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any

from app.broker.base import BrokerError
from app.broker.kite_mcp_adapter import READ_TOOLS

_LOGIN_URL = re.compile(r"https://mcp\.kite\.trade/authorize\?[^\s)\]]+")
LOGIN_TIMEOUT = 600  # seconds: a browser login not finished by then no longer blocks data calls
CLOSE_TIMEOUT = 5    # seconds to let an old session close its connection before its loop is stopped


def _serve(loop: asyncio.AbstractEventLoop) -> None:
    loop.run_forever()
    loop.close()  # after stop: the thread ends and the loop's resources are released


async def _cancel_others() -> None:
    """Run on the old loop: cancel its tasks (the session) and wait until they have unwound, which is
    what closes the MCP HTTP connection."""
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class McpSessionBridge:
    def __init__(self, url: str, timeout: float = 60):
        self._url = url
        self._timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._lock = threading.Lock()
        self._login_started: float | None = None

    # ------------------------------------------------------------------ lifecycle

    def _shutdown(self) -> None:
        """Close the current session and its loop (lock held). Cancelling _run lets its `async with`
        blocks close the HTTP connection; only then is the loop stopped and closed, so nothing leaks."""
        loop = self._loop
        self._loop = self._session = None
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(_cancel_others(), loop).result(CLOSE_TIMEOUT)
        except BaseException:  # noqa: BLE001 - failed or slow to close: stopping the loop either way
            pass
        loop.call_soon_threadsafe(loop.stop)

    def _ensure_started(self) -> None:
        # Held for the whole connect: concurrent requests must share ONE MCP session, because the
        # Kite login is bound to the session it was requested on.
        with self._lock:
            if self._session is not None and self._error is None:
                return
            self._shutdown()
            self._ready.clear()
            self._error = None
            self._loop = loop = asyncio.new_event_loop()
            threading.Thread(target=_serve, args=(loop,), daemon=True, name="kite-mcp").start()
            asyncio.run_coroutine_threadsafe(self._run(loop), loop)
            if not self._ready.wait(self._timeout):
                self._error = TimeoutError("connect timed out")
                raise BrokerError("Timed out connecting to Kite MCP")
            if self._error:
                raise BrokerError(f"Could not connect to Kite MCP: {self._error}")

    async def _run(self, loop: asyncio.AbstractEventLoop) -> None:
        from mcp import ClientSession

        try:
            from mcp.client.streamable_http import streamable_http_client as connect
        except ImportError:  # mcp 1.x
            from mcp.client.streamable_http import streamablehttp_client as connect
        try:
            async with connect(self._url) as streams, ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                self._session = session
                self._ready.set()
                await asyncio.Event().wait()  # hold the session open until _shutdown cancels it
        except BaseException as e:  # noqa: BLE001 - surfaced to callers, next call reconnects
            if self._loop is loop:  # an old session closing must not wipe out its replacement
                self._error = e
                self._session = None
                self._ready.set()
            if isinstance(e, asyncio.CancelledError):
                raise

    # ------------------------------------------------------------------ calls

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        if self.awaiting_login:
            # Do not touch the session while the browser login is in progress.
            raise BrokerError("Waiting for the Kite login to finish")
        return self._raw_call(name, arguments)

    def _raw_call(self, name: str, arguments: dict[str, Any]) -> dict:
        # The adapter checks the allowlist too; checking here as well means no path through the bridge
        # (e.g. adapter.login_bridge) can reach an order tool.
        if name not in READ_TOOLS | {"login"}:
            raise PermissionError(f"MCP tool {name!r} is not on the read-only allowlist")
        self._ensure_started()
        with self._lock:  # a concurrent login can reset the session between connect and use
            session, loop = self._session, self._loop
        if session is None or loop is None:
            raise BrokerError("The Kite MCP session was reset; try again")
        fut = asyncio.run_coroutine_threadsafe(session.call_tool(name, arguments), loop)
        try:
            result = fut.result(self._timeout)
        except TimeoutError as e:
            fut.cancel()  # keep the session (and its login); just give up on this call
            raise BrokerError(f"Kite MCP call {name} timed out") from e
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ in ("MCPError", "McpError"):
                # A JSON-RPC / HTTP error for this one request (e.g. rate limit). The session is still
                # alive, so keep it: resetting would throw away the Kite login.
                raise BrokerError(f"Kite MCP call {name} failed: {e}") from e
            self._error = e  # connection is broken: reconnect on the next call (needs a new login)
            raise BrokerError(f"Kite MCP call {name} failed: {e}") from e
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        return {"is_error": bool(is_error),
                "content": [{"text": getattr(c, "text", None)} for c in getattr(result, "content", [])]}

    # ------------------------------------------------------------------ login
    #
    # Mirrors the sequence verified in Phase 0: fresh session → `login` as the FIRST tool call →
    # no tool calls while the user completes the browser login → data calls after they confirm.
    # A data call made while the login is pending makes Kite drop the pending login.

    @property
    def awaiting_login(self) -> bool:
        """A browser login is in progress. One abandoned for LOGIN_TIMEOUT stops counting, so the
        scheduler and the pages can use the (logged-out) session again and say "log in" instead."""
        return self._login_started is not None and time.monotonic() - self._login_started < LOGIN_TIMEOUT

    def _reset(self) -> None:
        with self._lock:
            self._shutdown()
            self._error = None

    def login_url(self) -> str:
        self._reset()
        self._login_started = None
        payload = self._raw_call("login", {})
        text = " ".join(c["text"] or "" for c in payload["content"])
        match = _LOGIN_URL.search(text)
        if not match:
            raise BrokerError("Kite MCP login did not return a login URL")
        self._login_started = time.monotonic()
        return match.group(0)

    def confirm_login(self) -> None:
        self._login_started = None
