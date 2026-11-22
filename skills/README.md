# Skills — written live by workshop participants

This is the intelligence layer. The MCP server only exposes tools and
handles privacy; the Skills decide what to do with the (redacted) data
the tools return.

`broker-bill-reader.md` is the **model skill** — written up front so
participants (and Khurram, facilitating) have a concrete pattern to
follow: how to reference the MCP tools, how to handle pseudonym tokens,
how to call out multiple real-world layout variants, what structured
output to hand off downstream. Verified against both broker-bill
layouts in the real sample set (BILL-01's trade-by-trade format and
BILL-06's VisionMax format) — the net amounts it describes extracting
match the parsed totals exactly (13,343.52 and 854.15 respectively).

The rest are written live during the session, following that pattern:

- `broker-bill-reader.md` — **done** (model skill) — extract broker,
  fund, period, brokerage, WHT, SST, net amount from a redacted broker
  bill, across both real layout formats
- `broker-bill-reconciler.md` — match extracted bill totals against
  `get_liability_export()`, flag mismatches
- `bank-statement-reader.md` — extract transactions from a redacted
  bank statement
- `bank-reconciler.md` — match transactions against FMS records, flag
  reversals using `get_reversal_keywords()`
- `fms-upload-formatter.md` — format matched/reconciled rows into the
  `Date,Description,Dr/Cr,0,Amount` FMS upload format and call
  `write_fms_payment_file()` / `write_fms_reconciliation_file()`

Khurram facilitates; participants supply the business logic live for
the remaining four.
