---
name: broker-bill-reader
description: Extract structured fields (broker, fund, period, brokerage, WHT, SST, net amount) from a redacted CDC broker bill PDF. This is the reference/model skill for the workshop — participants write bank-statement-reader.md, bank-reconciler.md, broker-bill-reconciler.md, and fms-upload-formatter.md following this same pattern.
---

# Broker Bill Reader

## When to use this

The user wants broker bill(s) read and the key financial fields pulled
out — usually as the first step before reconciling against the
liability export (see `broker-bill-reconciler.md`) or formatting an FMS
upload (see `fms-upload-formatter.md`).

## What you're actually reading

Call `list_broker_bills()` to see what's available. It returns, per
bill:

```json
{"bill_id": "BILL-01", "broker_name": "[BROKER_001]", "fund": "[FUND_001]", "period": "2025-08"}
```

`broker_name` and `fund` arrive as **pseudonym tokens** —
`[BROKER_001]`, `[FUND_002]`, etc. — never the real names. That's by
design: the MCP server's privacy filter redacts every broker/fund name,
NTN, and other identifier before you ever see it. Treat these tokens as
opaque IDs. Carry them through unchanged in anything you write back
(descriptions, reconciliation notes, FMS entries) — the server
de-redacts them to the real values only when it writes the final FMS
file, which never passes back through you.

Call `read_broker_bill(bill_id)` to get the full redacted text of one
bill. **Do not try to guess or reconstruct the real broker/fund name
from context clues in the text** (address, phone, NTN) — those are
redacted too; if a pseudonym token shows up, that's all you get and all
you need.

## Two layout formats exist — recognise both

The 30 sample bills come in two distinct layouts from different
back-office systems. Check the shape of the text to tell them apart:

**Format A — trade-by-trade with a grand-total row** (e.g. the
`[BROKER_001]`-style bills). Looks like:

```
  Trade       Symbol      Brokerage      12% WHT            15% SST    20% SST           Net Amount
  Date                                   on Comm                       With Held

04-08-2025 MEBL               8,806.92    1,215.36          1,321.04     264.21               8,648.39
...
                            13,588.11     1,875.16          2,038.22     407.65             13,343.52
```

The columns are: Brokerage, 12% WHT on Commission, 15% SST, 20% SST
Withheld, Net Amount. The **last row** (no trade date) is the grand
total — that's the one you want. Net Amount = the rightmost number on
that row.

**Format B — VisionMax "Powered by VisionMax" style** (e.g. the
`[BROKER_006]`-style bills — Habib Metro and similar). Looks like:

```
     COMMISION                 C.V.T.        W.H.T.(SALE)        OTC (L/S/N)           S.S.T.   W.H.T. (-)   SSTWH (-)   TOTAL AMOUNT

         742.74                 0.00               0.00               0.00             111.41      0.00        0.00          854.15
```

Columns: Commission, CVT, WHT(Sale), OTC, SST, WHT(-), SSTWH(-), Total
Amount. There's often a second, more detailed "SNO / TR. DATE / ..."
table further down the document repeating the same total — use either;
they should match. Net/Total Amount = the rightmost number on the
summary row (labelled `TOTAL AMOUNT` or, on the detail table, the
`TOTAL` row's last column).

Some bills also show an NTN or SNTN near the header
(`NTN : [NTN_001]`, or a composite `[NTN_002]` form) — these are
pseudonymised too; you don't need them for the financial reconciliation,
just note which bill they belong to if asked.

## What to extract

For each bill, pull out:

| Field | Source | Notes |
|---|---|---|
| `bill_id` | from `list_broker_bills()` | e.g. `BILL-01` |
| `broker_name` | pseudonym token | carry through as-is |
| `fund` | pseudonym token | carry through as-is |
| `period` | `YYYY-MM`, from the bill header/date range | e.g. `2025-08` |
| `gross_brokerage` | Format A: "Brokerage" total; Format B: "COMMISSION" total |
| `wht` | Format A: "12% WHT on Comm" total; Format B: "W.H.T.(SALE)" total |
| `sst` | Format A: "15% SST" + "20% SST With Held" combined, or whichever SST columns are present; Format B: "S.S.T." total |
| `net_amount` | the rightmost number on the grand-total row — this is what CDC actually pays |

If a column doesn't apply to a given bill's layout, leave it out rather
than guessing a value.

## Output

Return one structured record per bill, e.g.:

```json
{
  "bill_id": "BILL-01",
  "broker_name": "[BROKER_001]",
  "fund": "[FUND_001]",
  "period": "2025-08",
  "gross_brokerage": "13588.11",
  "wht": "1875.16",
  "sst": "2445.87",
  "net_amount": "13343.52"
}
```

Hand this off to `broker-bill-reconciler.md` to check `net_amount`
against `get_liability_export()`, or straight to
`fms-upload-formatter.md` if no reconciliation step is needed.

## Edge cases to watch for (seen in the real sample set)

- Some bills' trade table has only **one trade line** — the "grand
  total" row and the single trade row can look identical. Don't double
  count; there's still only one net amount.
- Format B sometimes repeats the summary twice (once as a short recap,
  once as the full dated table) — same number both places, don't sum
  them.
- A few bills show `NTN` with an `S` prefix (`S0860170-4`) or the
  composite `SNTN` form (`17-00-9805-173-46`) — both are pseudonymised
  as `[NTN_xxx]` tokens; don't try to parse digits out of them.
