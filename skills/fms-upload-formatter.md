---
name: fms-upload-formatter
description: Format reconciled UC1 (broker-bill-reconciler.md) or UC2 (bank-reconciler.md) entries into the confirmed FMS .txt format and write the upload file via write_fms_payment_file() / write_fms_reconciliation_file(). Final step for both use cases.
---

# FMS Upload Formatter

## When to use this

Last step for both use cases, once you have a list of `OK` (or
otherwise ready-to-upload) entries from either
`broker-bill-reconciler.md` (UC1) or `bank-reconciler.md` (UC2).

## The format (confirmed by CDC)

```
Date,Description,Dr/Cr,0,Amount
```

Five comma-separated fields, one transaction per line, e.g.:

```
30/09/2026,Profit/Mark-up ,-,0,29899.05
```

- `Date` — `DD/MM/YYYY`.
- `Description` — free text. **Can and will contain pseudonym tokens**
  like `[BROKER_001]` or `[FUND_002]` if that's what came out of the
  earlier skills — that's expected and correct. Do not try to resolve
  them yourself.
- `Dr/Cr` — `-` or `+`.
- the literal `0` — always, every row. Don't substitute anything else
  here even if a source record has a non-zero value in what looks like
  the same position; this field is confirmed fixed at `0`.
- `Amount` — numeric, no currency symbol, no thousands separators
  (plain `29899.05`, not `29,899.05` — strip commas before formatting).

## Calling the tool

Use `write_fms_payment_file()` for UC1 entries, `write_fms_reconciliation_file()`
for UC2 entries. Both take the same `entries` shape:

```json
{
  "date": "30/09/2025",
  "description": "Brokerage payment to [BROKER_001] for [FUND_001]",
  "dr_cr": "-",
  "amount": "13343.52"
}
```

Pass a list of these as `entries`. The server **de-redacts every
pseudonym token in `description` automatically** before writing the
line — `[BROKER_001]` becomes the real broker name in the file on
disk, but you never see that real value; the tool's return only gives
you a redacted preview of the first few lines, for sanity-checking
without exposing anything.

You can optionally pass `filename` (e.g. `"uc1_payments_2025-08.txt"`);
if you don't, the server timestamps one for you.

## Dr/Cr sign convention — confirm this during the session

Only one worked example exists so far (`context.txt`'s
`Profit/Mark-up ,-,0,29899.05`). The working defaults used elsewhere in
this workshop's skills:

- **UC1 (broker bills)**: CDC is paying the broker out → `-`.
- **UC2 (bank reconciliation)**: mirror the statement's own column —
  Credit (money in) → `+`, Debit (money out) → `-` (see
  `bank-reconciler.md`).

Treat both as working assumptions, not confirmed rules — flag them for
CDC to confirm once GL Close behaviour is demonstrated live, per
`context.txt`'s outstanding items.

## Validate before writing

Before calling either `write_fms_*` tool, check every entry has:

- `date` actually in `DD/MM/YYYY` (not `YYYY-MM-DD` from an upstream
  skill's `period` field — reformat it)
- `dr_cr` is exactly `-` or `+`, nothing else
- `amount` is a plain decimal number as a string or number, commas
  stripped

If anything's missing or malformed, don't guess a default — surface it
and ask, rather than writing a bad row into the actual upload file.

## After writing

The tool's return tells you `written_to` (the file path), `row_count`,
and a `preview_redacted` of the first few lines — report those back
plainly. Don't claim to know what the real description text says in
the file; you genuinely don't, and shouldn't try to reconstruct it.

## Edge cases to watch for

- `broker-bill-reconciler.md` only hands you `OK` bills by design —
  don't accidentally pull in `AMOUNT_MISMATCH`/`PERIOD_MISMATCH`/
  `NOT_FOUND` records from its exceptions list; those need a human
  decision first.
- `bank-reconciler.md` nets out paired reversals to zero — don't
  re-include a reversal's original transaction if its pair has already
  been netted, or you'll double-count.
- If you're formatting a large batch, write them in one
  `write_fms_*` call with the full `entries` list rather than one call
  per row — the server writes one file either way, and one call keeps
  the preview check (and any validation failure) in one place.
