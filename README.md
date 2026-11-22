# CDC Workshop — Claude AI Agent Build

Full technical stack for the hands-on Claude AI agent workshop at CDC
(Central Depository Company of Pakistan). Two use cases, built and
rehearsed before the live session.

- **UC1 — Broker Bill Payments**: read broker bill PDFs, extract fields,
  match against the back-office liability export, produce an FMS
  payment upload `.txt`.
- **UC2 — Bank Reconciliation**: read bank statement PDFs, match
  transactions against FMS records, flag reversals via the Annexure-A
  keyword list, produce an FMS reconciliation upload `.txt`.

FMS file format (confirmed): `Date,Description,Dr/Cr,0,Amount`, e.g.
`30/09/2026,Profit/Mark-up ,-,0,29899.05`

## Layout

```
cdc-privacy-filter/   Python package — two-layer PII filter + context map
                      + de-redactor. No CDC data leaves this process;
                      Claude never sees real values. See "Privacy filter
                      — two layers" below.
cdc-mcp-server/       FastMCP server. All file I/O happens here. Claude
                      only sees text already redacted by cdc-privacy-filter.
                        mock_data/uc1/   30 sample broker bills + liability
                                         export (liability export is a
                                         placeholder — see note below)
                        mock_data/uc2/   20 sample bank statements + the
                                         real Annexure-A reversal keywords
                                         + accounts/AMC info (placeholders
                                         — see note below)
skills/               SKILL.md files — written live by workshop
                      participants. This is the intelligence layer; the
                      MCP server only provides tools and privacy.
```

Status vs. the original `/mock-data/` top-level folder in the plan: the
sample PDFs and generated mock JSON live under `cdc-mcp-server/mock_data/`
instead, since the server resolves them relative to its own file — kept
all data in one place rather than duplicating 3MB of PDFs.

## Data — intentionally not in this GitHub repo

`cdc-mcp-server/mock_data/` is **not pushed here**. It holds real CDC
sample documents (30 broker bill PDFs, 20 bank statement PDFs) with real
IBANs, NTNs, account numbers, and fund balances — that stays local only,
on the machine it was built/rehearsed on
(`D:\Blockverse\CDC\CDC-Workshop\cdc-workshop\cdc-mcp-server\mock_data`),
never on GitHub, regardless of repo visibility.

To run this locally, recreate `cdc-mcp-server/mock_data/` with:
```
mock_data/uc1/bills/              # the 30 sample broker bill PDFs
mock_data/uc1/bills_index.json    # parsed {bill_id, broker_name, fund, period, amount}
mock_data/uc1/liability_export.json
mock_data/uc1/output/             # empty, server writes FMS .txt here
mock_data/uc2/statements/         # the 20 sample bank statement PDFs
mock_data/uc2/statements_index.json
mock_data/uc2/accounts_detail.json
mock_data/uc2/amc_info.json
mock_data/uc2/reversal_keywords.json   # real Annexure-A data
mock_data/uc2/output/
```
(All of this already exists on the machine used for the build/rehearsal
— this repo is the code half; that machine has the data half.)

## Privacy filter — two layers

1. **Layer 1 — general PII model.** [`openai/privacy-filter`](https://huggingface.co/openai/privacy-filter)
   (Apache-2.0, ~1.5B total / 50M active params, bidirectional token
   classifier, 128K context, runs fully local on CPU — no network
   egress after the weights are cached once). Catches free-text
   personal names, addresses, emails, phone numbers, URLs, and dates
   that a fixed regex can't fully enumerate (e.g. a broker's officer
   signing a bill by name). Long documents are chunked internally so
   CPU memory stays bounded regardless of document length.
2. **Layer 2 — Pakistani financial regex.** (`cdc_privacy_filter/patterns.py`)
   Deterministic patterns for structured identifiers the general model
   was never trained to recognise as Pakistan-specific: IBAN, NTN
   (all observed formats), CNIC, bank/CDC account numbers (labeled
   *and* bare-digit-run forms), local phone numbers, and known
   fund/broker names from the sample corpus.

Both layers feed the same non-overlapping span merge in `filter.py`;
either can claim a given piece of text, and whichever claims it first
is pseudonymised the same way either way — Claude only ever sees
`[CATEGORY_NNN]` tokens, never the real value. Verified by an exact
redact→deredact round trip and a zero-leak sweep across all 50 real
sample documents (30 bills + 20 statements, see "What's real vs. mock"
below).

Layer 1 is optional at runtime: if `torch`/`transformers` aren't
installed or the model can't be reached, `default_model_backend()`
falls back to a no-op and Layer 2 keeps working on its own — set
`CDC_DISABLE_PII_MODEL=1` to force that fallback deliberately (e.g.
for a fast regex-only smoke test).

## Quickstart

```bash
# core (regex layer only)
pip install -r cdc-mcp-server/requirements.txt
pip install pytest   # for the privacy-filter tests

# verify the privacy filter in isolation (regex-only tests always run;
# the Layer-1 model tests in test_model_backend.py auto-skip if
# torch/transformers aren't installed)
pytest cdc-privacy-filter/tests -v

# run the MCP server (stdio transport) — loads the Layer-1 model once
# at startup (~5-25s depending on whether weights are already cached)
cd cdc-mcp-server
python3 server.py

# ...or skip the model entirely for a fast regex-only run:
CDC_DISABLE_PII_MODEL=1 python3 server.py
```

`pdftotext` (poppler-utils) is used for PDF extraction if present on
`PATH`; otherwise the server falls back to `pypdf` automatically.

## What's real vs. mock right now

| Item | Status |
|---|---|
| 30 broker bill PDFs | Real CDC samples |
| 20 bank statement PDFs | Real CDC samples |
| Annexure-A reversal keywords | **Real**, confirmed by CDC |
| FMS `.txt` format | **Confirmed** |
| Liability export (UC1) | **Placeholder** — derived from the bills themselves, with 3 deliberate mismatches (amount/period) so the recon logic has something real to catch. Swap for CDC's real export once the structure is confirmed. |
| Accounts Detail (UC2) | **Placeholder** — derived from the 20 statements. Swap for CDC's real `Accounts Detail.xlsx`. |
| AMC Info (UC2) | **Placeholder** — inferred from the fund titles seen in the statements. Swap for CDC's real `AMC Info.xlsx`. |
| Letter/EPM decomposition logic | Not yet modelled — still outstanding from CDC. |
| GL Close SOP | Not yet modelled — to be demonstrated live. |

## Workshop → production path

Each MCP tool in `cdc-mcp-server/server.py` is marked
`# SWAP FOR PRODUCTION` where its body currently reads local mock
JSON/PDFs. CDC's IT team replaces those four spots with calls into the
real back-office export, FMS API, and document store — tool names and
signatures stay identical, so every Skill written during the workshop
keeps working unchanged.

The Layer-1 model (`openai/privacy-filter`) runs as-is, pretrained —
no fine-tuning needed to get real value. Fine-tuning it further on
CDC's own labelled data (to sharpen recall on Pakistan-specific free
text it wasn't trained on) is a potential paid follow-on, not a
prerequisite.
