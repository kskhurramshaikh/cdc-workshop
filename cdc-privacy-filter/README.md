# CDC Privacy Filter — by Blockverse

Local, network-free redaction/de-redaction of Pakistani financial PII.
Built for the CDC agentic workshop; wraps a deterministic regex layer
(tuned against real CDC sample documents) around a pluggable model
backend for free-text PII the regex layer can't reliably catch.

## What it catches (regex layer)

| Category     | Example from the sample set                  |
|---------------|-----------------------------------------------|
| `IBAN`        | `PK38NBPA0082003002200677`                     |
| `NTN`         | `1234567-8`, `S0860170-4`, `17-00-9805-173-46` |
| `CNIC`        | 13-digit CNIC (dashed or bare)                 |
| `ACCOUNT_NO`  | `Account No: 0501-195629-0450-00`              |
| `PHONE`       | `0213-5684962`                                 |
| `FUND`        | `CDC TRUSTEE ASKARI HIGH YIELD SCHEME`         |
| `BROKER`      | `Al Habib Capital Markets`                     |

Each category gets a stable, sequential pseudonym per `ContextMap`
instance: `[IBAN_001]`, `[NTN_002]`, `[FUND_003]`, etc. The same real
value always maps to the same pseudonym within one map, so Claude can
still reason about relationships between redacted documents.

## Usage

```python
from cdc_privacy_filter import PrivacyFilter, ContextMap

ctx = ContextMap()
pf = PrivacyFilter(context_map=ctx)

redacted = pf.redact(real_text)      # -> safe to hand to Claude
...
final = pf.deredact(claude_output)   # -> real values restored, for the
                                      #    FMS file written to disk
```

The `ContextMap` must stay server-side (inside the MCP server process).
Claude never sees it and never sees a real value — only pseudonym
tokens round-trip through the model.

## Extending for production

- `patterns.py` — add new `KNOWN_FUND_NAMES` / `KNOWN_BROKER_NAMES` as
  CDC's active broker/fund roster grows; the structured regexes
  (IBAN/NTN/CNIC/ACCOUNT_NO/PHONE) are already general.
- `model_backend.py` — swap `NoOpModelBackend` for a real local
  NER/PII model (the brief references the OpenAI Privacy Filter, a
  small Apache-2.0 model that runs on CDC's own hardware) to also
  catch free-text names/addresses/signatures the regex layer misses
  (e.g. the signatory name on a broker bill). Same `detect()` call
  signature — no other code changes needed.
- Fine-tuning that model on CDC's own labelled data is the planned
  paid follow-on engagement.

## Tests

```
pip install --break-system-packages pytest
pytest cdc-privacy-filter/tests -v
```
