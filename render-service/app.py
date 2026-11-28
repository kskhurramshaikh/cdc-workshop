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
import shutil
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse

from cdc_privacy_filter import NoOpModelBackend, PrivacyFilter, default_model_backend

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
#
# min_score lowered from the library default (0.5) to 0.35 after the live
# UC2 workshop run: short, extremely common two-token Pakistani names
# (e.g. "Ahmed Muhammad") were scoring below 0.5 and passing through
# unredacted, while longer/more distinctive names (e.g. "Ahmed Khan
# Muhammad Awais") scored well above it and were caught correctly. This
# is a blunt, global knob — it trades some additional false positives
# for fewer missed short names, which is the safer failure direction for
# a privacy filter (over-redaction is an inconvenience; under-redaction
# is a leak). It does NOT fix the root cause (the model's confidence is
# genuinely context-dependent for short/common-token names) — a
# context-anchored Layer 2 fallback (matching short names next to known
# document labels like signature lines) would be the more precise fix
# and is worth adding once we have concrete examples of where these
# names appear in the documents.
#
# CDC_DISABLE_PII_MODEL=1 skips loading Layer 1 here too (same flag,
# same meaning as cdc-mcp-server's own local-mode switch) — useful for
# smoke-testing this service's HTTP/MCP plumbing and the multi-worker
# setup itself without paying the ~2.8GB model download/load cost, and
# for local development. Layer 2 (regex) keeps working on its own
# either way; never used as a production fallback for this service,
# only ever set deliberately.
if os.environ.get("CDC_DISABLE_PII_MODEL") == "1":
    _model_backend = NoOpModelBackend()
    print(
        "[sarhad] CDC_DISABLE_PII_MODEL=1 — Layer 1 (model) skipped, regex-only.",
        flush=True,
    )
else:
    _model_backend = default_model_backend(min_score=0.35)
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
    2xx, not an MCP tool call.

    Deliberately returns a fixed, generic backend label rather than
    `type(_model_backend).__name__` (the previous behaviour): the real
    class name (`OpenAIPrivacyFilterBackend`) was leaking which
    underlying open-weight model Sarhad wraps to anyone who simply called
    this unauthenticated, public route — which is exactly what happened
    during the live UC2 workshop. This endpoint's only job is "is the
    process up", not "what's inside it".
    """
    return JSONResponse({"status": "ok", "model_backend": "Sarhad-Layer1"})


# ASGI app for the multi-worker production path (gunicorn + uvicorn
# workers, see gunicorn_conf.py and the __main__ block below). Built once
# per process at import time, after the tool and the /health route are
# registered above, so both are present on the app this returns. In
# gunicorn's preload mode this whole module — including the model load
# above — is imported exactly once in the master process before any
# worker is forked; each forked worker then serves this same `app`
# object (inherited via fork, not re-imported), sharing the loaded model
# weights via copy-on-write rather than loading its own copy.
app = mcp.http_app(path="/mcp")


if __name__ == "__main__":
    # Render's Start Command stays "python app.py" (see render.yaml) for
    # both paths below — this process re-execs into gunicorn so the
    # multi-worker/preload design takes effect with no dashboard change.
    #
    # Set SARHAD_SINGLE_PROCESS=1 to skip gunicorn and run the original
    # single-process path directly instead — useful for quick local
    # testing without gunicorn/uvicorn-worker installed, or to isolate
    # whether a problem is in the app itself vs. the multi-worker setup.
    if os.environ.get("SARHAD_SINGLE_PROCESS") == "1":
        port = int(os.environ.get("PORT", "8000"))
        mcp.run(transport="http", host="0.0.0.0", port=port, path="/mcp")
    else:
        gunicorn_conf = str(Path(__file__).resolve().parent / "gunicorn_conf.py")
        # Resolve gunicorn explicitly rather than relying on a bare "gunicorn"
        # lookup on $PATH: that fails whenever this script is invoked via a
        # venv's python binary directly (python app.py) without the venv
        # being "activated" first, since activation is what adds its bin/
        # directory to $PATH. shutil.which() still finds it if $PATH is
        # already correct (e.g. a normally-activated venv or a system
        # install); the "-m gunicorn" fallback works unconditionally,
        # because it imports gunicorn from whatever site-packages
        # sys.executable itself resolves to, with no $PATH involved at all.
        gunicorn_path = shutil.which("gunicorn")
        if gunicorn_path:
            os.execv(gunicorn_path, [gunicorn_path, "-c", gunicorn_conf, "app:app"])
        else:
            os.execv(
                sys.executable,
                [sys.executable, "-m", "gunicorn", "-c", gunicorn_conf, "app:app"],
            )
