"""
CDC Privacy Filter — Render hosting service.

Stop-gap / workshop stand-in for what Blockverse would set up on-premise
for CDC: both privacy-filter layers (Layer 1 model + Layer 2 regex) are
hosted here instead of running in-process on the local machine.

Design choice — detection only, no pseudonym state: this service's one
job is PII *span detection* (/detect): given text, return the (start,
end, category, text) spans Layer 1 + Layer 2 found. It does NOT hold a
ContextMap and never mints or resolves pseudonyms. The caller (the local
cdc-mcp-server, which owns the single process-wide ContextMap for the
life of its session) turns spans into pseudonym substitutions itself via
its own context_map.pseudonymise(), and de-redaction stays a pure local
dict lookup — unchanged from today, no round trip needed.

This keeps the service stateless (no DB, survives restarts/instance
rotation cleanly) and keeps the pseudonym <-> real-value mapping itself
off Render entirely. The raw document text does still cross the wire for
detection to work on it — that's inherent to hosting the detector
remotely at all, not something this split avoids; HTTPS + the shared
API key below are the controls for that.

Auth: a shared API key via the X-API-Key header (PRIVACY_FILTER_API_KEY
env var on the Render service). Workshop stand-in, not a production auth
story — swap for mTLS/OAuth before any real on-prem deployment.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from cdc_privacy_filter import PrivacyFilter, default_model_backend

app = FastAPI(title="CDC Privacy Filter Service", version="0.2.0")

# Loaded once at process startup (default_model_backend() eagerly calls
# _ensure_loaded()) — this is the expensive part and doubles as this
# service's own "pre-warming": the first real request doesn't pay the
# model-load cost. No ContextMap here by design — see module docstring.
_model_backend = default_model_backend()
_detector = PrivacyFilter(model_backend=_model_backend)

_API_KEY = os.environ.get("PRIVACY_FILTER_API_KEY")


def _check_api_key(x_api_key: str | None) -> None:
    if _API_KEY and x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


class DetectRequest(BaseModel):
    text: str


class SpanOut(BaseModel):
    start: int
    end: int
    category: str
    text: str


class DetectResponse(BaseModel):
    spans: list[SpanOut]


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model_backend": type(_model_backend).__name__}


@app.post("/detect", response_model=DetectResponse)
def detect(req: DetectRequest, x_api_key: str | None = Header(default=None)) -> DetectResponse:
    _check_api_key(x_api_key)
    spans = _detector.collect_spans(req.text)
    return DetectResponse(
        spans=[SpanOut(start=s.start, end=s.end, category=s.category, text=s.text) for s in spans]
    )
