"""
Thin HTTP client for the Render-hosted privacy-filter detection service
(render-service/app.py). Used by server.py's _redact() when
RENDER_PRIVACY_FILTER_URL is set, in place of running Layer 1 + Layer 2
detection in-process.

Only detection crosses the wire (see render-service/app.py's docstring
for why) — the ContextMap and all pseudonym substitution stay local,
unchanged from the fully-local path.
"""

from __future__ import annotations

import requests

from cdc_privacy_filter.model_backend import Span
# NOTE: this module must be imported after server.py has already added
# cdc-privacy-filter to sys.path (server.py does that before importing
# anything from cdc_privacy_filter, and imports this module afterwards).

_TIMEOUT_SECONDS = 90  # generous: a cold remote instance may still be loading the model


class RemoteDetectionError(RuntimeError):
    """Raised when the Render detection service can't be reached or
    returns an error. Callers should decide whether to fall back to
    local detection or surface this to the operator."""


def detect_remote(text: str, base_url: str, api_key: str | None) -> list[Span]:
    headers = {"X-API-Key": api_key} if api_key else {}
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/detect",
            json={"text": text},
            headers=headers,
            timeout=_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RemoteDetectionError(f"Render detection service unreachable: {exc!r}") from exc

    data = resp.json()
    return [
        Span(start=s["start"], end=s["end"], category=s["category"], text=s["text"])
        for s in data["spans"]
    ]
