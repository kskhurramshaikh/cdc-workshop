---
name: bank-reconciler
description: Flag reversed/returned transactions in a bank statement's transaction list (from bank-statement-reader.md) using the real Annexure-A keyword list, and prepare entries for the FMS reconciliation upload. Second step in UC2 — feeds fms-upload-formatter.md.
---

# Bank Reconciler

## When to use this

After `bank-statement-reader.md` has turned a statement into a
transaction list, this skill flags which transactions are reversals/
returns (so they're excluded or handled separately) and prepares the
remaining transactions for the FMS reconciliation upload.

## What's actually confirmed vs. still outstanding

Be upfront about this with whoever you're working with — don't quietly
paper over the gap:

- **Reversal detection is real.** `get_reversal_keywords()` returns the
  actual Annexure-A list CDC confirmed — bank code → the exact
  keyword(s) that bank uses in its narration when a transaction is
  reversed/returned. This works today.
- **Matching against FMS's own ledger is not yet wired up.** The
  original brief calls for "matching transactions against FMS
  records" — there's no `get_fms_records`-style tool yet because CDC
  hasn't confirmed that export's structure (see `context.txt` — still
  outstanding, along with Letter/EPM decomposition logic and the GL
  Close SOP). Until that's wired in, this skill works directly off the
  bank statement itself: flag reversals, net them out, and hand off
  what's left for the FMS reconciliation file. Full three-way
  matching (statement ↔ FMS ledger ↔ reversal list) is a follow-up once
  CDC delivers that export.

## Reversal detection

1. Call `get_reversal_keywords()`. It returns `{bank_code: [keyword, ...]}`,
   e.g. `{"ZTBL": ["Reversal"], "HBL": ["CHQ Return-CLG CHQ(Only Cr)", "REVERSAL", "reverse"], ...}`.
2. For each transaction from `bank-statement-reader.md`, look up the
   keyword list for that statement's bank (match `bank_name` from
   `list_bank_statements()` against the bank codes — e.g. "Zarai
   Taraqiati Bank Limited" → `ZTBL`, "Habib Metropolitan..." isn't in
   this list by that name, check the actual bank code conventions CDC
   uses, not just the full legal name).
3. Check the transaction's **full narration, including any wrapped
   continuation line** (see `bank-statement-reader.md` — a keyword can
   sit on its own line directly under the transaction it modifies, as
   seen in the real ZTBL sample: `REVERSAL` appears alone on the line
   below a `Remittances (Advices Credit)... WHT` transaction).
4. Case-insensitive, substring match (not exact-word) — the keywords
   themselves are inconsistent case (`REVERSAL` vs `reverse` vs
   `Reversed Wrongly Posted Entry`) because that's literally how the
   banks write them.
5. Mark matching transactions `is_reversal: true`.

## What to do with a flagged reversal

Don't just drop it. A reversal usually has a paired original
transaction it's cancelling out — if you can identify the pair (same
amount, opposite debit/credit, close in date), net them to zero and
note both references. If you can't confidently pair it, keep the
reversal as its own line in the exceptions list rather than guessing —
a wrongly-netted reversal is worse than an unresolved one.

## Preparing entries for FMS upload

For every transaction that is **not** a reversal (or is a reversal
you've successfully paired and netted to zero, so there's nothing left
to upload for that pair):

```json
{
  "date": "12/03/2026",
  "description": "Remittances (Advices Credit) Voucher#20260312-10123275 WHT",
  "dr_cr": "+",
  "amount": "1436.00",
  "is_reversal": false
}
```

`dr_cr`: mirror the statement's own column — a **Credit** on the bank
statement is money coming in to this account (`+`); a **Debit** is
money going out (`-`). This hasn't been separately confirmed by CDC
beyond the one FMS format example in `context.txt`
(`30/09/2026,Profit/Mark-up ,-,0,29899.05`), so treat it as the working
default and flag it as something to confirm with CDC during the
session if the sign ever looks backwards once GL Close behaviour is
demonstrated.

Hand the full list — reversals excluded/netted, everything else dated
and signed — to `fms-upload-formatter.md`.

## Edge cases to watch for (seen in the real sample set)

- A single statement can have multiple reversals from different
  causes (e.g. WHT reversal vs. a returned cheque) — don't assume
  there's at most one per statement.
- The bank-code lookup needs care: `get_reversal_keywords()` keys are
  short codes (`ZTBL`, `HBL`, `MCB`...), but `list_bank_statements()`
  returns full legal names (`"Zarai Taraqiati Bank Limited"`) or, for
  some samples, a filename-derived short form. Build an explicit
  mapping rather than string-matching the full name against the code
  — a few of the sample statements' bank names don't obviously reduce
  to their Annexure-A code (ask CDC to confirm the mapping for any
  bank you're not sure of, rather than guessing).
- Some banks in the real sample set (BOK, NBP, ABHI, MMBL, First
  Microfinance, U-Micro, HBL Microfinance) have **no entry at all** in
  Annexure-A — only the 13 banks CDC confirmed are covered. For a
  statement from a bank not in that list, say so explicitly rather
  than silently treating every transaction as non-reversal; flag it as
  needing a keyword from CDC.
