"""
Sarhad — Blockverse's privacy-filter MCP service (Render hosting).

This is "Sarhad": the commercial, standalone PII-detection service that
Blockverse sells as a service (charged upfront for the default model;
Pakistan-regex / company-specific fine-tuning sold as add-ons on top of
this default Layer 1 + Layer 2). It speaks the real MCP protocol over
HTTP, so any MCP client — Claude, another agent, or CDC's own
cdc-mcp-server — can connect to it directly with a standard MCP client
config, the same way they'd connect to any other MCP server.

Design choice — detection only, no pseudonym state: this service's one
job is PII *span detection*, exposed as the `detect_pii` MCP tool: given
text, return the (start, end, category, text) spans Layer 1 (model) +
Layer 2 (Pakistan regex) found. It does NOT hold a ContextMap and never
mints or resolves pseudonyms. The caller (the local cdc-mcp-server,
which owns the single process-wide ContextMap for the life of its
session) turns spans into pseudonym substitutions itself via its own
context_map.pseudonymise(), through PrivacyFilter.apply_spans(). This
keeps the pseudonym <-> real-value mapping itself off Render entirely,
and keeps this service itself stateless (no DB, survives restarts /
instance rotation cleanly). The raw document text does still cross the
wire for detection to work on it — that's inherent to hosting the
detector remotely at all, not something this split avoids; HTTPS + the
Bearer-token auth below are the controls for that.

Auth: a shared Bearer token (PRIVACY_FILTER_API_KEY env var on the
Render service), checked via FastMCP's StaticTokenVerifier. This is a
workshop/early-customer auth story — a single shared secret per
deployed instance, the MCP-native equivalent of the X-API-Key header
this service used before it spoke MCP. Swap for a per-client token
registry or full OAuth before scaling past a handful of paying
customers.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse

from cdc_privacy_filter import PrivacyFilter, default_model_backend

_API_KEY = os.environ.get("PRIVACY_FILTER_API_KEY")

_auth = None
if _API_KEY:
    # Single shared secret, the MCP-native stand-in for the old
    # X-API-Key header. Any client presenting this exact bearer token
    # is treated as "cdc-mcp-server" (or whichever paying client holds
    # this instance's key) — see module docstring for why this is fine
    # for now and what it should become later.
    _auth = StaticTokenVerifier(tokens={_API_KEY: {"client_id": "sarhad-client"}})
else:
    print(
        "[sarhad] WARNING: PRIVACY_FILTER_API_KEY not set — running with no auth. "
        "Set it before deploying anywhere reachable from outside this workshop.",
        flush=True,
    )

mcp = FastMCP(name="Sarhad Privacy Filter", auth=_auth)

# Loaded once at process startup (default_model_backend() eagerly calls
# _ensure_loaded()) — this is the expensive part and doubles as this
# service's own "pre-warming": the first real tool call doesn't pay the
# model-load cost. No ContextMap here by design — see module docstring.
_model_backend = default_model_backend()
_detector = PrivacyFilter(model_backend=_model_backend)


@mcp.tool
def detect_pii(text: str) -> list[dict]:
    """Detect PII spans in `text` using Layer 1 (model) + Layer 2 (Pakistan
    regex). Returns a list of spans, each `{start, end, category, text}`,
    sorted by start offset. Detection only — no pseudonyms are minted and
    nothing is retained after the call returns."""
    spans = _detector.collect_spans(text)
    return [
        {"start": s.start, "end": s.end, "category": s.category, "text": s.text}
        for s in spans
    ]


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    """Plain HTTP health check (outside the MCP protocol) for Render's
    own health-check probe — Render expects an ordinary GET that returns
    2xx, not an MCP tool call."""
    return JSONResponse({"status": "ok", "model_backend": type(_model_backend).__name__})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    mcp.run(transport="http", host="0.0.0.0", port=port, path="/mcp")
