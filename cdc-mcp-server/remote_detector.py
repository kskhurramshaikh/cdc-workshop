"""
MCP client for Sarhad — Blockverse's Render-hosted privacy-filter
detection service (render-service/app.py). Used by server.py's
_redact() when RENDER_PRIVACY_FILTER_URL is set, in place of running
Layer 1 + Layer 2 detection in-process.

Sarhad speaks the real MCP protocol (streamable HTTP), not a plain
REST API, so this talks to it the same way any MCP client would. The
one thing this module exists to get right: it keeps ONE MCP connection
open for the life of the server process and reuses it for every
detect_remote() call, instead of reconnecting per call.

Why this matters: a naive "open a Client, call the tool, close it"
per call looks fine against localhost (no real network latency), but
list_broker_bills() alone makes ~60 _redact() calls (2 fields x 30
bills) — at real internet latency to Render, 60 fresh TLS + MCP
handshakes blew past the 80s device-bridge RPC window twice in a row
in live testing. One persistent connection turns that into 60 cheap
calls over an already-open session instead.

Implementation: a dedicated background thread runs its own asyncio
event loop forever. A single long-lived worker coroutine, scheduled
onto that loop exactly once, opens the MCP Client (`async with
client:`) and then services a queue of (text, future) requests in a
loop, one call_tool per item, for as long as the connection holds.
This matters because fastmcp's Client ties its connection context to
the asyncio *task* that entered it — so the connect-and-serve loop
has to be one single task, not "connect in task A, call tools from
task B" (which is what naively scheduling a fresh coroutine per call
onto a shared loop would do). server.py's tools are plain sync `def`s
that FastMCP runs in a worker thread, so detect_remote() below blocks
that thread on a concurrent.futures.Future while the background loop
thread does the actual MCP call — safe because it's a different
thread from the server's own asyncio loop (if any), so there's no
risk of blocking on itself.

If the connection drops (Render redeploy, network blip, idle
timeout), the worker loop notices, closes out, and reconnects
automatically on the next call — callers don't need to know this
happened, beyond the occasional single call seeing a RemoteDetectionError
if it lands exactly during a reconnect.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future as ConcurrentFuture

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.exceptions import McpError

from cdc_privacy_filter.model_backend import Span
# NOTE: this module must be imported after server.py has already added
# cdc-privacy-filter to sys.path (server.py does that before importing
# anything from cdc_privacy_filter, and imports this module afterwards).

_TIMEOUT_SECONDS = 90  # generous: a cold remote instance may still be loading the model


class RemoteDetectionError(RuntimeError):
    """Raised when Sarhad can't be reached or returns an error. Callers
    should decide whether to fall back to local detection or surface
    this to the operator."""


def _mcp_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    return url if url.endswith("/mcp") else f"{url}/mcp"


class _PersistentConnection:
    """Owns the background event-loop thread and the one long-lived
    worker task that holds Sarhad's MCP connection open. Lazily started
    on first use; (base_url, api_key) are fixed for this process's
    lifetime (server.py reads them once from the environment at
    startup, so there's no need to support switching targets mid-run)."""

    def __init__(self, base_url: str, api_key: str | None) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="sarhad-mcp-conn"
        )
        self._thread.start()
        self._queue: asyncio.Queue | None = None
        self._queue_ready = threading.Event()
        asyncio.run_coroutine_threadsafe(self._worker(), self._loop)

    async def _worker(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        self._queue = queue
        self._queue_ready.set()

        while True:
            text, fut = await queue.get()
            try:
                auth = BearerAuth(self._api_key) if self._api_key else None
                async with Client(
                    _mcp_url(self._base_url), auth=auth, timeout=_TIMEOUT_SECONDS
                ) as client:
                    # Serve this item and every subsequent one on the same
                    # connection, for as long as it stays up — reconnecting
                    # per item is exactly the cost this class exists to avoid.
                    while True:
                        try:
                            result = await client.call_tool("detect_pii", {"text": text})
                            if not fut.done():
                                fut.set_result(result.data)
                        except Exception as exc:  # noqa: BLE001
                            if not fut.done():
                                fut.set_exception(exc)
                            if not client.is_connected():
                                break  # drop out to reconnect
                        text, fut = await queue.get()
            except Exception as exc:  # noqa: BLE001 — connection itself failed
                if not fut.done():
                    fut.set_exception(exc)
                # Loop back around and try to reconnect on the *next* item
                # rather than retrying immediately — avoids a hot-looping
                # reconnect storm if Sarhad is actually down.

    def submit(self, text: str) -> list[dict]:
        self._queue_ready.wait(timeout=10)
        if self._queue is None:
            raise RemoteDetectionError("Sarhad connection worker never started")

        fut: ConcurrentFuture = ConcurrentFuture()

        async def _enqueue() -> None:
            await self._queue.put((text, fut))  # type: ignore[union-attr]

        asyncio.run_coroutine_threadsafe(_enqueue(), self._loop)
        return fut.result(timeout=_TIMEOUT_SECONDS)


_connection: _PersistentConnection | None = None
_connection_lock = threading.Lock()


def _get_connection(base_url: str, api_key: str | None) -> _PersistentConnection:
    global _connection
    with _connection_lock:
        if _connection is None:
            _connection = _PersistentConnection(base_url, api_key)
        return _connection


def detect_remote(text: str, base_url: str, api_key: str | None) -> list[Span]:
    conn = _get_connection(base_url, api_key)
    try:
        raw_spans = conn.submit(text)
    except McpError as exc:
        raise RemoteDetectionError(f"Sarhad returned an error: {exc!r}") from exc
    except Exception as exc:  # noqa: BLE001 — timeout/network/connection errors
        raise RemoteDetectionError(f"Sarhad (Render) unreachable: {exc!r}") from exc

    return [
        Span(start=s["start"], end=s["end"], category=s["category"], text=s["text"])
        for s in raw_spans
    ]
