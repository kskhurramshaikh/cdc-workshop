"""
MCP client for Sarhad — Blockverse's Render-hosted privacy-filter
detection service (render-service/app.py). Used by server.py's
_redact() when RENDER_PRIVACY_FILTER_URL is set, in place of running
Layer 1 + Layer 2 detection in-process.

Sarhad speaks the real MCP protocol (streamable HTTP), not a plain
REST API, so this talks to it the same way any MCP client would:
connect, call the `detect_pii` tool, disconnect. Only detection crosses
the wire (see render-service/app.py's docstring for why) — the
ContextMap and all pseudonym substitution stay local, unchanged from
the fully-local path.

server.py's tools are all plain sync `def`s, which FastMCP runs in a
worker thread (not the server's own asyncio event loop) — so it's safe
for detect_remote() below to open its own event loop per call via
asyncio.run(). This keeps the sync call signature server.py already
relies on (_redact() is called from deep inside synchronous helpers).
"""

from __future__ import annotations

import asyncio

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


async def _detect_remote_async(text: str, base_url: str, api_key: str | None) -> list[Span]:
    mcp_url = base_url.rstrip("/")
    if not mcp_url.endswith("/mcp"):
        mcp_url = f"{mcp_url}/mcp"
    auth = BearerAuth(api_key) if api_key else None

    async with Client(mcp_url, auth=auth, timeout=_TIMEOUT_SECONDS) as client:
        result = await client.call_tool("detect_pii", {"text": text})

    return [
        Span(start=s["start"], end=s["end"], category=s["category"], text=s["text"])
        for s in result.data
    ]


def detect_remote(text: str, base_url: str, api_key: str | None) -> list[Span]:
    try:
        return asyncio.run(_detect_remote_async(text, base_url, api_key))
    except McpError as exc:
        raise RemoteDetectionError(f"Sarhad returned an error: {exc!r}") from exc
    except Exception as exc:  # noqa: BLE001 — network/connection errors from the MCP transport
        raise RemoteDetectionError(f"Sarhad (Render) unreachable: {exc!r}") from exc
