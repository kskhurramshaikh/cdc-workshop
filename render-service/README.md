# CDC Privacy Filter — Render service

Hosts both privacy-filter layers (Layer 1 model + Layer 2 regex) as a small
HTTP detection service, as a stand-in for what Blockverse would set up
on-premise for CDC. See `docs/architecture-sizing.md` for the model-stack
and sizing decisions behind this, and `app.py`'s docstring for the
stateless/no-pseudonym-state design.

## Endpoints

- `GET /health` — readiness + which model backend loaded.
- `POST /detect` — `{"text": "..."}` -> `{"spans": [{"start", "end", "category", "text"}, ...]}`.
  This is detection only; no pseudonym mapping is created or stored here.
  The caller (cdc-mcp-server) applies pseudonym substitution locally via
  its own `ContextMap`, so real-value <-> pseudonym mappings never leave
  the local machine — only the raw text being scanned does.

Auth: `X-API-Key` header, checked against the `PRIVACY_FILTER_API_KEY`
env var. Unset on the server = auth disabled (fine for local testing,
not for a real deployment).

## Deploying

This repo includes a Render Blueprint (`render.yaml` at the repo root).
From the Render dashboard: New -> Blueprint -> point at this repo. Set
`PRIVACY_FILTER_API_KEY` manually in the service's environment variables
(it's marked `sync: false` in the blueprint so it's never committed).

First boot downloads the model weights (~2.8GB) from Hugging Face, which
can take a couple of minutes — this is expected on the first deploy and
after any full redeploy (the filesystem is ephemeral across deploys, not
across restarts).

## Wiring up the local MCP server

Set these on the machine running `cdc-mcp-server`:

```
RENDER_PRIVACY_FILTER_URL=https://<your-service>.onrender.com
PRIVACY_FILTER_API_KEY=<same value as set on the Render service>
```

With `RENDER_PRIVACY_FILTER_URL` set, `server.py`'s `_redact()` calls this
service for detection instead of running the model in-process; unset
(the default), everything stays fully local, unchanged from before this
was added.
