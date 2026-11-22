---
name: broker-bill-reconciler
description: Match extracted broker bill totals (from broker-bill-reader.md) against the back-office liability export, flag amount and period mismatches. Second step in UC1 — feeds fms-upload-formatter.md.
---

# Broker Bill Reconciler

## When to use this

After `broker-bill-reader.md` has turned a bill PDF into a structured
record (`bill_id`, `broker_name`, `fund`, `period`, `net_amount`, ...),
this skill checks that number against what the back office expects to
pay, before anything goes into the FMS upload file.

## What you're checking against

Call `get_liability_export()`. Each row:

```json
{"broker_name": "[BROKER_001]", "fund": "[FUND_001]", "period": "2025-08", "expected_amount": "13,343.52"}
```

`broker_name` and `fund` are the **same pseudonym tokens** you already
have from `list_broker_bills()` / `broker-bill-reader.md` — the MCP
server's `ContextMap` is shared across every tool call in a session, so
`[BROKER_001]` means the same real broker everywhere. Match rows by
`(broker_name, fund)` — don't try to match on `period` first, since a
period mismatch is exactly one of the things you're checking for.

> **Note on this data**: CDC's real liability export structure hasn't
> been confirmed yet (per `context.txt`). `get_liability_export()`
> currently returns a mock built from the bills' own actual totals,
> with **3 deliberate mismatches** seeded in so this reconciliation
> logic has real discrepancies to catch — don't be surprised if you
> find exactly 3 flagged bills out of 30; that's by design, not a bug
> in your logic.

## Matching logic

For each bill record from `broker-bill-reader.md`:

1. Find the liability row with the same `(broker_name, fund)` pair.
   - **No match found** → status `NOT_FOUND`. Flag for manual review;
     don't guess which liability row it "probably" is.
2. Compare `net_amount` (from the bill) to `expected_amount` (from the
   liability row), as numbers — strip commas, compare to the cent.
   - **Equal** → amount OK.
   - **Not equal** → status `AMOUNT_MISMATCH`, record both values and
     the difference (`net_amount - expected_amount`; positive means the
     broker billed more than expected).
3. Compare the bill's `period` to the liability row's `period`.
   - **Equal** → period OK.
   - **Not equal** → status `PERIOD_MISMATCH` (can co-occur with an
     amount match — the money's right, it's just filed under the wrong
     month; still needs a human to confirm which period it belongs to
     before upload).
4. If both match → status `OK`, ready for `fms-upload-formatter.md`.

A bill can be `OK`, `AMOUNT_MISMATCH`, `PERIOD_MISMATCH`, or both
mismatches at once — don't collapse these into a single pass/fail; CDC
needs to know *which* thing is wrong.

## Output

One record per bill, e.g.:

```json
{
  "bill_id": "BILL-03",
  "broker_name": "[BROKER_003]",
  "fund": "[FUND_001]",
  "period": "2025-08",
  "net_amount": "19348.09",
  "expected_amount": "19848.09",
  "status": "AMOUNT_MISMATCH",
  "difference": "-500.00"
}
```

Only hand `OK` records to `fms-upload-formatter.md` automatically.
`AMOUNT_MISMATCH`, `PERIOD_MISMATCH`, and `NOT_FOUND` records go into a
separate exceptions list for CDC staff to resolve — never silently
"correct" a bill's amount to match the liability export, or vice versa;
you don't know which side is actually right.

## Edge cases to watch for (seen in the real sample set)

- A mismatch can be *either* direction — the broker bill can be higher
  or lower than the liability export. Report the signed difference, not
  just "mismatch."
- A period mismatch with the amount otherwise correct is still a real
  exception, not a false positive — don't auto-resolve it by assuming
  the bill's period is right just because the amount lines up.
- If two bills exist for the same broker in the same month but
  different funds (common — e.g. a broker with both a Dedicated Stock
  Fund and a Cash Fund bill), make sure you're matching on fund too, not
  broker alone, or you'll cross-match the wrong liability row.
