"""
MCP client for Sarhad — Blockverse's Render-hosted privacy-filter
detection service (render-service/app.py). Used by server.py's
_redact() when RENDER_PRIVACY_FILTER_URL is set, in place of running
Layer 1 + Layer 2 detection in-process.

Sarhad speaks the real MCP protocol (streamable HTTP), not a plain
REST API, so this talks to it the same way any MCP client would.

Design: a small POOL of persistent connections (default size 4, tune via
SARHAD_CLIENT_CONCURRENCY), each held open for the life of this process
and each serviced by its own background thread running its own asyncio
event loop. detect_remote() picks a connection from the pool (round-
robin) and hands work to that connection's background worker via a
thread-safe queue, blocking on a concurrent.futures.Future for the
result. fastmcp.Client's connection context must be entered and exited
on the same asyncio task, so each pooled connection is opened once
inside its own long-running worker coroutine on its own background
thread's loop — never reopened per call.

Why a pool instead of one connection: Sarhad's server side (render-
service) now runs multiple worker processes (one per vCPU, see
gunicorn_conf.py there) specifically so it can detect PII for several
documents at once instead of one at a time. A single persistent
connection here would still cap this *client* at one in-flight
detect_pii call no matter how many workers the server has, because
every call would funnel through the same one queue/connection in
series. Round-robining across a small pool of connections lets this
one client process have several documents' worth of detect_pii calls
in flight concurrently, each landing on whichever server-side worker
accepts that connection — turning "one document at a time" into
"several documents at a time" end to end. Pool size should roughly
match (or modestly exceed) the server's own worker count: more pooled
connections than server workers mostly just queues on the server side
instead of here, and fewer leaves server workers idle.

Why persistent (each pooled connection) instead of connect-per-call: a
fresh MCP connection pays a real TLS + MCP-handshake cost. server.py
often needs many detect_pii calls in a row (one per redactable field
across many records, or one per chunk of a large document) — paying
that handshake every single time turned calls that should individually
take well under a second into minutes of cumulative wall-clock time.
That connect-per-call cost, not the size of any one document or any
one JSON file, was the actual cause of the extended
get_accounts_detail/read_bank_statement delays this pool design
otherwise preserves the fix for.

Auto-recovery: if a pooled connection drops (network blip, Render
restart, DNS hiccup) that connection's own worker loop reconnects on
its own and keeps serving its own queue — a request already enqueued
on it when the drop happened still gets served once that connection
comes back up, instead of silently dying and leaving every future call
routed to it hanging. Other pooled connections are unaffected.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import os
import sys
import threading

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.exceptions import McpError

from cdc_privacy_filter.model_backend import Span
# NOTE: this module must be imported after server.py has already added
# cdc-privacy-filter to sys.path (server.py does that before importing
# anything from cdc_privacy_filter, and imports this module afterwards).

_TIMEOUT_SECONDS = 90  # generous: a cold remote instance may still be loading the model
_SUBMIT_TIMEOUT_SECONDS = _TIMEOUT_SECONDS + 30  # local safety net on top of the MCP call's own timeout
_RECONNECT_DELAY_SECONDS = 2

# How many pooled connections this client keeps open at once. Tune to
# roughly match Sarhad's own WEB_CONCURRENCY (worker count) on the
# server side — see the module docstring above for why.
_DEFAULT_POOL_SIZE = 4
_POOL_SIZE = max(1, int(os.environ.get("SARHAD_CLIENT_CONCURRENCY", _DEFAULT_POOL_SIZE)))


class RemoteDetectionError(RuntimeError):
    """Raised when Sarhad can't be reached or returns an error. Callers
    should decide whether to fall back to local detection or surface
    this to the operator."""


class _PersistentConnection:
    """Owns one long-lived MCP connection to Sarhad, serviced by a
    dedicated background thread + event loop."""

    def __init__(self, base_url: str, api_key: str | None):
        mcp_url = base_url.rstrip("/")
        if not mcp_url.endswith("/mcp"):
            mcp_url = f"{mcp_url}/mcp"
        self._mcp_url = mcp_url
        self._auth = BearerAuth(api_key) if api_key else None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._started = False

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            thread = threading.Thread(
                target=self._run_loop, daemon=True, name="sarhad-mcp-worker"
            )
            thread.start()
            self._ready.wait()
            self._started = True

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._queue = asyncio.Queue()
        self._ready.set()
        self._loop.run_until_complete(self._serve_forever())

    async def _serve_forever(self) -> None:
        while True:
            try:
                async with Client(
                    self._mcp_url, auth=self._auth, timeout=_TIMEOUT_SECONDS
                ) as client:
                    print("[remote_detector] connected to Sarhad.", file=sys.stderr)
                    await self._serve_on(client)
            except Exception as exc:  # noqa: BLE001 — connection-level failure
                # _serve_on only raises out of the `async with` block for a
                # connection/transport-level problem (see below) — never
                # for a normal per-call McpError, which it handles and
                # keeps serving past. Whatever request was in flight when
                # this happened already got fut.set_exception() inside
                # _serve_on, so its caller isn't left hanging; anything
                # still sitting in the queue survives the reconnect below
                # and gets served once the new connection is up.
                print(
                    f"[remote_detector] Sarhad connection lost/unavailable "
                    f"({exc!r}); reconnecting in {_RECONNECT_DELAY_SECONDS}s.",
                    file=sys.stderr,
                )
                await asyncio.sleep(_RECONNECT_DELAY_SECONDS)

    async def _serve_on(self, client: Client) -> None:
        while True:
            text, fut = await self._queue.get()
            if fut.cancelled():
                continue
            try:
                result = await client.call_tool("detect_pii", {"text": text})
            except McpError as exc:
                # Application-level error from Sarhad's own detect_pii
                # tool — the connection/transport itself is still fine,
                # so report this to the caller and keep serving the next
                # queued item on the same connection.
                fut.set_exception(exc)
                continue
            except Exception as exc:
                # Transport/connection-level failure (dropped connection,
                # DNS failure, timeout, etc). Unblock this one caller
                # immediately, then propagate so _serve_forever's `async
                # with` exits and reconnects before the next queued item
                # is attempted.
                fut.set_exception(exc)
                raise
            else:
                spans = [
                    Span(start=s["start"], end=s["end"], category=s["category"], text=s["text"])
                    for s in result.data
                ]
                fut.set_result(spans)

    def submit(self, text: str) -> list[Span]:
        self._ensure_started()
        fut: concurrent.futures.Future = concurrent.futures.Future()

        async def _enqueue() -> None:
            await self._queue.put((text, fut))

        asyncio.run_coroutine_threadsafe(_enqueue(), self._loop)
        return fut.result(timeout=_SUBMIT_TIMEOUT_SECONDS)


class _ConnectionPool:
    """A small, fixed-size pool of _PersistentConnection, round-robined
    across calls so this one client process can have several detect_pii
    calls in flight at once — see the module docstring for why.

    Round-robin (not least-busy) is deliberate: it's simple and
    stateless to dispatch, and good enough as a first cut since
    individual detect_pii calls are all roughly the same shape (one
    document chunk). If real traffic shows some chunks taking much
    longer than others (so round-robin leaves a slow connection's queue
    backed up while others sit idle), a least-busy/queue-depth-aware
    dispatch would be the next upgrade — not needed yet.
    """

    def __init__(self, base_url: str, api_key: str | None, size: int):
        self._conns = [_PersistentConnection(base_url, api_key) for _ in range(size)]
        self._counter = itertools.count()

    def submit(self, text: str) -> list[Span]:
        idx = next(self._counter) % len(self._conns)
        return self._conns[idx].submit(text)


_pool: _ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool(base_url: str, api_key: str | None) -> _ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _ConnectionPool(base_url, api_key, _POOL_SIZE)
    return _pool


def detect_remote(text: str, base_url: str, api_key: str | None) -> list[Span]:
    pool = _get_pool(base_url, api_key)
    try:
        return pool.submit(text)
    except McpError as exc:
        raise RemoteDetectionError(f"Sarhad returned an error: {exc!r}") from exc
    except Exception as exc:  # noqa: BLE001 — network/connection/timeout errors
        raise RemoteDetectionError(f"Sarhad (Render) unreachable: {exc!r}") from exc
