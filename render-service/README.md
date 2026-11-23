# Sarhad — privacy-filter MCP service (Render hosting)

Hosts both privacy-filter layers (Layer 1 model + Layer 2 Pakistan regex) as
an MCP server, as the first hosted version of "Sarhad" — Blockverse's
commercial PII-detection offering. See `docs/architecture-sizing.md` for the
model-stack and sizing decisions behind this, and `app.py`'s docstring for
the stateless/no-pseudonym-state design and the auth story.

Sarhad speaks the real MCP protocol (streamable HTTP over `/mcp`), not a
plain REST API — any MCP client (Claude, cdc-mcp-server, or a future paying
customer's own agent) connects to it directly with a standard MCP client
config, the same way it would connect to any other MCP server.

## Interface

- **MCP tool `detect_pii(text: str) -> list[dict]`** — the main entry point.
  Returns spans as `{"start", "end", "category", "text"}`, sorted by start
  offset. Detection only; no pseudonym mapping is created or stored here.
  The caller (cdc-mcp-server) applies pseudonym substitution locally via its
  own `ContextMap`, so real-value <-> pseudonym mappings never leave the
  local machine — only the raw text being scanned does.
- **`GET /health`** — plain HTTP route (outside the MCP protocol, via
  `@mcp.custom_route`) for Render's own health-check probe. Returns
  `{"status": "ok", "model_backend": "..."}`.

Auth: a shared Bearer token, checked via FastMCP's `StaticTokenVerifier`
against the `PRIVACY_FILTER_API_KEY` env var. Unset on the server = auth
disabled (fine for local testing, not for a real deployment). Any MCP
client passes this as a standard `Authorization: Bearer <token>` header —
`fastmcp.client.auth.BearerAuth` does this automatically.

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

With `RENDER_PRIVACY_FILTER_URL` set, `server.py`'s `_redact()` calls Sarhad
(via `remote_detector.py`, an MCP client) for detection instead of running
the model in-process, and the local model is never loaded; unset (the
default), everything stays fully local, unchanged from before this was
added. `remote_detector.py` appends `/mcp` to the URL automatically if not
already present.

## Connecting any other MCP client to Sarhad

Because Sarhad is a standard MCP server, it can be added to any MCP
client's config directly, e.g.:

```json
{
  "mcpServers": {
    "sarhad": {
      "url": "https://<your-service>.onrender.com/mcp",
      "headers": { "Authorization": "Bearer <PRIVACY_FILTER_API_KEY value>" }
    }
  }
}
```

This is the shape CDC (or any future Sarhad customer) would use to call
Sarhad directly from their own agent, independent of cdc-mcp-server.
