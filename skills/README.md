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

All five skills are now drafted, following that pattern end to end for
both use cases:

- `broker-bill-reader.md` — extract broker, fund, period, brokerage,
  WHT, SST, net amount from a redacted broker bill, across both real
  layout formats (model skill)
- `broker-bill-reconciler.md` — match extracted bill totals against
  `get_liability_export()`, flag amount/period mismatches
- `bank-statement-reader.md` — extract a clean transaction list from a
  redacted bank statement, across the 4 real layout formats
- `bank-reconciler.md` — flag reversed/returned transactions using
  the real `get_reversal_keywords()` (Annexure-A) list; notes that
  full FMS-ledger matching isn't wired up yet (CDC hasn't delivered
  that export's structure)
- `fms-upload-formatter.md` — format matched/reconciled rows into the
  `Date,Description,Dr/Cr,0,Amount` FMS upload format and call
  `write_fms_payment_file()` / `write_fms_reconciliation_file()`;
  flags the Dr/Cr sign convention as a working assumption to confirm
  with CDC live

These are drafted ahead of the workshop as working references. Khurram
facilitates the live session where participants read, discuss, and
extend/adjust this logic against CDC's actual rules and edge cases as
they come up — the drafts give the session a running start rather than
a blank page.
