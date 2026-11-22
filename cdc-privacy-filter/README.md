# CDC Privacy Filter — by Blockverse

Local, network-free redaction/de-redaction of Pakistani financial PII.
Built for the CDC agentic workshop. Two layers:

- **Layer 1 — `openai/privacy-filter`** (real model, wired in): a
  general local PII model that catches free-text personal names,
  addresses, emails, phone numbers, URLs, and dates — things no fixed
  regex can fully enumerate (a broker officer signing a bill by name,
  for instance). Apache-2.0, ~1.5B total/50M active params, 128K
  context, CPU-friendly; long documents are chunked internally to keep
  memory bounded. See `model_backend.py`.
- **Layer 2 — Pakistani financial regex** (`patterns.py`): deterministic
  patterns tuned against the real 50-document CDC sample set, for
  structured identifiers the general model was never trained on as
  Pakistan-specific formats.

## What each layer catches

| Category     | Layer | Example from the sample set                  |
|---------------|-------|-----------------------------------------------|
| `IBAN`        | 2     | `PK38NBPA0082003002200677`                     |
| `NTN`         | 2     | `1234567-8`, `S0860170-4`, `17-00-9805-173-46` |
| `CNIC`        | 2     | 13-digit CNIC (dashed or bare)                 |
| `ACCOUNT_NO`  | 2     | `Account No: 0501-195629-0450-00`, or a bare 14-18 digit account number with no "Account" label nearby |
| `PHONE`       | 2     | `0213-5684962`                                 |
| `FUND`        | 2     | `CDC TRUSTEE ASKARI HIGH YIELD SCHEME`         |
| `BROKER`      | 2     | `Al Habib Capital Markets`                     |
| `PERSON`      | 1     | a signatory's name on a bill (free text)       |
| `ADDRESS`     | 1     | a free-text postal address                     |
| `EMAIL`       | 1     | a personal email mentioned in free text        |
| `PHONE`/`URL`/`DATE`/`SECRET` | 1 | general free-text PII; shares the `PHONE`/`ACCOUNT_NO` category name with Layer 2 so either layer's catch numbers from the same counter |

Note: Layer 1 is tuned to flag *personal* PII, not an organization's
own public contact info — a company's `info@...` address or public
website in a letterhead is correctly left alone (see
`tests/test_model_backend.py::test_corporate_letterhead_contact_is_not_flagged_as_private`).

Each category gets a stable, sequential pseudonym per `ContextMap`
instance: `[IBAN_001]`, `[NTN_002]`, `[FUND_003]`, etc. The same real
value always maps to the same pseudonym within one map, so Claude can
still reason about relationships between redacted documents.

## Usage

```python
from cdc_privacy_filter import ContextMap, PrivacyFilter, default_model_backend

ctx = ContextMap()
pf = PrivacyFilter(context_map=ctx, model_backend=default_model_backend())
# default_model_backend() loads the real Layer-1 model if torch/
# transformers are installed and the weights are reachable, else
# falls back to a no-op automatically — Layer 2 (regex) keeps working
# either way. Omit model_backend entirely for regex-only (PrivacyFilter's
# own default), e.g. in tests that shouldn't need network/torch.

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
- `model_backend.py` — Layer 1 is already real (`OpenAIPrivacyFilterBackend`,
  wrapping `openai/privacy-filter`). To swap in a different/future
  model, implement the same `detect()` call signature (`ModelBackend`
  protocol) — no other code changes needed.
- Fine-tuning the Layer-1 model on CDC's own labelled data (to sharpen
  recall on Pakistan-specific free text it wasn't trained on) is a
  potential paid follow-on — the base pretrained model already runs
  as-is with no fine-tuning required.

## Tests

```
pip install --break-system-packages pytest
pytest cdc-privacy-filter/tests -v
```

The regex-only tests (`test_filter.py`) always run, offline, with no
extra dependencies. The Layer-1 model tests (`test_model_backend.py`)
auto-skip unless `torch`/`transformers` are installed and the model is
reachable — install the optional extra first: `pip install -e ".[model]"`.
