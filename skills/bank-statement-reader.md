---
name: bank-statement-reader
description: Extract a clean transaction list (date, description, debit, credit, balance) from a redacted CDC bank statement PDF, across the multiple real bank layout formats in the sample set. First step in UC2 — feeds bank-reconciler.md.
---

# Bank Statement Reader

## When to use this

The user wants a bank statement (or several) read and turned into a
clean, structured transaction list — the first step before flagging
reversals or formatting an FMS reconciliation upload.

## What you're actually reading

Call `list_bank_statements()` to see what's available:

```json
{"statement_id": "ABHI Microfinance", "file": "ABHI Microfinance.pdf", "bank_name": "ABHI Microfinance Bank", "account_title": "CDC TRUSTEE (fund account)", "account_no": "[ACCOUNT_NO_001]", "iban": "[IBAN_001]"}
```

`account_no` and `iban` are **pseudonym tokens** — never the real
numbers. `bank_name` is returned plain (bank names aren't PII, and you
need it to look up the right row in `get_reversal_keywords()`). Keep
`statement_id` as the key you pass to `read_bank_statement()`.

Call `read_bank_statement(statement_id)` to get the full redacted text.
Transaction dates, narrations, and the actual Debit/Credit/Balance
figures are left intact — only account numbers, IBANs, and fund/
customer titles are pseudonymised.

> **Note on accounts/AMC metadata**: `get_accounts_detail()` and
> `get_amc_info()` are currently placeholders (CDC hasn't delivered the
> real `Accounts Detail.xlsx` / `AMC Info.xlsx` yet — see
> `context.txt`). Use them for rough account-priority context, but
> don't treat the account-type/priority values as final.

## Four layout formats exist — recognise all of them

The 20 sample statements come from different banks with genuinely
different column layouts. Identify the shape before parsing:

**Format 1 — BOK style** (running narrative, often multi-line per
transaction):
```
Book Date           Reference              Descript               Value Date   Debit        Credit           Closing Balance

08 JAN 26           AAACT26008LGCMZ0D7      Profit - Capitalise    01 JAN 26                 1,956.73         502,956.73
                                           Profit on Savings A/c (Islamic)
                                           Capitalise Schedule
```
Columns: Book Date, Reference, Description, Value Date, Debit, Credit,
Closing Balance. The description often wraps onto extra lines below
the transaction row — those extra lines are still part of the same
transaction's narration, not separate transactions.

**Format 2 — ZTBL style** (one line per leg, blank-line separated):
```
Tr. Date   Particulars                                                                 Debit             Credit                     Balance
12/03/26   Remittances (Advices Credit) Voucher#20260312-10123275 WHT                                  1,436.00                     3,602.77
           REVERSAL
```
Columns: Tr. Date, Particulars, Debit, Credit, Balance. A reversal
keyword (see `bank-reconciler.md`) can appear on its own continuation
line directly under the transaction it applies to — treat it as part
of that transaction's narration, not a separate line item.

**Format 3 — NBP style** (numbered rows with an instrument number
column):
```
S.No. Date         Particulars           Instno        Memo          Debit             Credit            Balance
    1   02/09/2026 TRANSFER               9900501045                    800,000,000.00              0.00    563,845,453.44
```
Columns: S.No, Date, Particulars, Instrument No., Memo, Debit, Credit,
Balance.

**Format 4 — ABHI/Microfinance style** (separate post date and value
date, with a document ID column):
```
Post Date Value Date Vch Narration                                  Doc.ID                     Debit             Credit           Balance
  30/JUN/26    01/JUL/26 PROFIT PAID TO ACCOUNT #                     0                                       820,353.66      81,312,049.18
```
Columns: Post Date, Value Date, Narration, Doc ID, Debit, Credit,
Balance. Use Value Date (not Post Date) as the transaction's effective
date unless told otherwise — it's what the FMS format's `Date` field
should reflect.

## What to extract per transaction

| Field | Notes |
|---|---|
| `statement_id` | from `list_bank_statements()` |
| `date` | the transaction's effective date (Value Date where the format has one) — normalise to `DD/MM/YYYY` for downstream FMS formatting |
| `description` | the narration, including any wrapped continuation lines, trimmed |
| `debit` | numeric, or `null`/omit if this transaction is a credit |
| `credit` | numeric, or `null`/omit if this transaction is a debit |
| `balance` | running balance after this transaction, if you need it for reconciliation sanity checks |
| `reference` | voucher/cheque/instrument number if present — useful for matching against FMS, not required |

## Output

One record per transaction, grouped under its `statement_id`:

```json
{
  "statement_id": "ZTBL -1",
  "date": "12/03/2026",
  "description": "Remittances (Advices Credit) Voucher#20260312-10123275 WHT REVERSAL",
  "credit": "1436.00",
  "balance": "3602.77"
}
```

Hand the full list off to `bank-reconciler.md`.

## Edge cases to watch for (seen in the real sample set)

- Don't treat a wrapped narration line (no date, no amount columns) as
  a new transaction — it belongs to the transaction above it.
- Amounts can run into the billions of PKR on some fund accounts
  (RTGS inflows/outflows) — don't truncate or round; these are real
  institutional cash-management accounts, not retail-sized balances.
- A blank line between transaction blocks is layout padding, not a
  signal of anything — don't count it as a transaction or a separator
  with semantic meaning.
- Some statements span many pages with the bank's letterhead repeated
  on each page (`Powered by VisionMax`-style footers, bank branch
  addresses) — these aren't transactions; skip repeated headers/footers
  when scanning.
