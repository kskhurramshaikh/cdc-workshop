"""
cdc-mcp-server — FastMCP server for the CDC agentic workshop.

Runs entirely on the local machine. All file I/O (PDFs, mock data, FMS
output) happens here; Claude only ever sees text that has already passed
through cdc-privacy-filter. Real values are restored by this server,
never by Claude, right before the final FMS upload file is written.

Workshop -> production path: swap the bodies of the four functions
marked "# SWAP FOR PRODUCTION" for calls into CDC's real back-office
exports, FMS API, and document store. Every tool name and signature
stays the same, so the Skills participants write live keep working
unchanged.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cdc-privacy-filter"))
from cdc_privacy_filter import (  # noqa: E402
    ContextMap,
    NoOpModelBackend,
    PrivacyFilter,
    default_model_backend,
)

from pdf_extract import extract_text  # noqa: E402

BASE = Path(__file__).resolve().parent
UC1_BILLS = BASE / "mock_data" / "uc1" / "bills"
UC1_OUT = BASE / "mock_data" / "uc1" / "output"
UC2_STATEMENTS = BASE / "mock_data" / "uc2" / "statements"
UC2_OUT = BASE / "mock_data" / "uc2" / "output"

UC1_OUT.mkdir(parents=True, exist_ok=True)
UC2_OUT.mkdir(parents=True, exist_ok=True)

# One ContextMap for the life of this server process: the same real value
# (an NTN, an IBAN, a broker name) always gets the same pseudonym across
# every document and tool call in a session, and this is the only place
# the real<->pseudonym mapping lives.
_context_map = ContextMap()

# Two-layer privacy filter:
#   Layer 1 (model_backend) — OpenAI Privacy Filter, a general local PII
#   model: catches free-text names/addresses/emails/phones/urls/dates that
#   a fixed regex can't fully enumerate. Loaded once at process startup
#   (first import of this module), not per-request.
#   Layer 2 (patterns.py, inside PrivacyFilter) — deterministic regex for
#   Pakistan-specific structured identifiers (IBAN, NTN, CNIC, account
#   numbers, known fund/broker names) the general model was never trained
#   to recognise as such.
# Set CDC_DISABLE_PII_MODEL=1 to skip loading Layer 1 (e.g. for a quick
# regex-only smoke test, or a machine without torch/transformers) — Layer
# 2 keeps working on its own either way.
if os.environ.get("CDC_DISABLE_PII_MODEL") == "1":
    _model_backend = NoOpModelBackend()
    print("[cdc-mcp-server] CDC_DISABLE_PII_MODEL=1 — Layer 1 (model) skipped, regex-only.", file=sys.stderr)
else:
    _model_backend = default_model_backend()

_privacy_filter = PrivacyFilter(context_map=_context_map, model_backend=_model_backend)

mcp = FastMCP("cdc-workshop")


def _redact(text: str) -> str:
    return _privacy_filter.redact(text)


def _deredact(text: str) -> str:
    return _privacy_filter.deredact(text)


def _redact_value(value):
    """Redact PII inside a value that may be a str, or a dict/list of
    strings (used for JSON mock-data records)."""
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    return value


# Fields whose category is known from the mock-data schema itself (the
# value is a bare identifier, with no surrounding "Account No:" / "NTN:"
# label for the regex layer to key off). Pseudonymising these directly
# through the ContextMap avoids the mis-classification a label-less
# regex scan would otherwise risk (e.g. a dashed account number's first
# segment looking like a phone number in isolation).
_FIELD_CATEGORY = {
    "account_no": "ACCOUNT_NO",
    "iban": "IBAN",
}


def _redact_record(record: dict) -> dict:
    """Redact a mock-data record, using the known field->category map
    where available and falling back to regex-based redaction (which
    catches broker/fund names, free-text titles, etc.) everywhere else.
    """
    out = {}
    for key, value in record.items():
        if value is None:
            out[key] = value
        elif key in _FIELD_CATEGORY and isinstance(value, str):
            out[key] = _context_map.pseudonymise(_FIELD_CATEGORY[key], value)
        else:
            out[key] = _redact_value(value)
    return out


# ---------------------------------------------------------------------
# UC1 — Broker Bill Payments
# ---------------------------------------------------------------------

@mcp.tool()
def list_broker_bills() -> list[dict]:
    """List available broker bill PDFs with their key metadata, redacted.
    Returns bill_id, fund, period for each — broker name comes back as a
    pseudonym token (resolve via read_broker_bill)."""
    # SWAP FOR PRODUCTION: replace with a directory listing / API call
    # against CDC's real incoming-bills folder.
    index = json.loads((BASE / "mock_data" / "uc1" / "bills_index.json").read_text())
    return [
        {
            "bill_id": r["bill_id"],
            "broker_name": _redact(r["broker_name"]),
            "fund": _redact(r["fund"]),
            "period": r["period"],
        }
        for r in index
    ]


@mcp.tool()
def read_broker_bill(bill_id: str) -> str:
    """Return the redacted text of one broker bill PDF (brokerage, WHT,
    SST, net amount — all numeric fields are left intact; only PII and
    broker/fund names are pseudonymised)."""
    # SWAP FOR PRODUCTION: fetch the real PDF bytes from CDC's document
    # store / network folder instead of the local mock_data directory.
    index = json.loads((BASE / "mock_data" / "uc1" / "bills_index.json").read_text())
    rec = next((r for r in index if r["bill_id"] == bill_id), None)
    if rec is None:
        return f"ERROR: no such bill_id '{bill_id}'"

    pdf_path = UC1_BILLS / rec["file"]
    raw_text = extract_text(pdf_path)
    return _redact(raw_text)


@mcp.tool()
def get_liability_export() -> list[dict]:
    """Return the back-office liability export: expected broker/fund/
    period/amount, for matching against bill totals. Broker and fund
    names come back pseudonymised; amounts are plain numbers.

    NOTE: CDC's real liability export structure hasn't been provided yet
    (see context.txt — still outstanding). This mock mirrors the 30
    sample bills' actual totals, with 3 deliberate mismatches (amount/
    period) so the reconciliation logic has something real to catch.
    """
    # SWAP FOR PRODUCTION: pull from CDC's back-office liability export
    # (format TBD from CDC).
    rows = json.loads((BASE / "mock_data" / "uc1" / "liability_export.json").read_text())
    return [_redact_value(r) for r in rows]


@mcp.tool()
def write_fms_payment_file(
    entries: list[dict], filename: str | None = None
) -> dict:
    """Write an FMS payment upload .txt file. Each entry must have:
    date (DD/MM/YYYY), description (str — may contain pseudonym tokens,
    which will be de-redacted automatically), dr_cr ("-" or "+"), amount
    (number or numeric string). Produces comma-separated lines:
    Date,Description,Dr/Cr,0,Amount — matching the confirmed FMS format.
    """
    # SWAP FOR PRODUCTION: push to FMS's real upload endpoint/SFTP drop
    # instead of writing a local .txt file.
    lines = []
    for e in entries:
        date = e["date"]
        description = _deredact(str(e["description"]))
        dr_cr = e["dr_cr"]
        amount = e["amount"]
        lines.append(f"{date},{description},{dr_cr},0,{amount}")

    if filename is None:
        filename = f"fms_payment_{datetime.now():%Y%m%d_%H%M%S}.txt"
    out_path = UC1_OUT / filename
    out_path.write_text("\n".join(lines) + "\n")

    return {
        "written_to": str(out_path),
        "row_count": len(lines),
        "preview_redacted": [_redact(l) for l in lines[:5]],
    }


# ---------------------------------------------------------------------
# UC2 — Bank Reconciliation
# ---------------------------------------------------------------------

@mcp.tool()
def list_bank_statements() -> list[dict]:
    """List available bank statement PDFs with redacted account metadata.
    Returns statement_id, bank_name, and pseudonymised account_no/IBAN/
    account_title."""
    index = json.loads((BASE / "mock_data" / "uc2" / "statements_index.json").read_text())
    return [_redact_record(r) for r in index]


@mcp.tool()
def read_bank_statement(statement_id: str) -> str:
    """Return the redacted text of one bank statement PDF (transaction
    dates, descriptions, debit/credit/balance figures left intact; IBANs,
    account numbers, and fund/customer titles pseudonymised)."""
    index = json.loads((BASE / "mock_data" / "uc2" / "statements_index.json").read_text())
    rec = next((r for r in index if r["statement_id"] == statement_id), None)
    if rec is None:
        return f"ERROR: no such statement_id '{statement_id}'"

    pdf_path = UC2_STATEMENTS / rec["file"]
    raw_text = extract_text(pdf_path)
    return _redact(raw_text)


@mcp.tool()
def get_accounts_detail() -> list[dict]:
    """Return the account prioritisation list (operational first, then
    recon-date ascending, then transaction volume — per CDC's confirmed
    rule). Account identifiers come back pseudonymised.

    NOTE: this is a placeholder pending CDC's real "Accounts Detail.xlsx"
    (still outstanding per context.txt). It's derived automatically from
    the 20 sample statements so the recon flow has real shapes to work
    with; swap this tool body for the real file once CDC delivers it.
    """
    rows = json.loads((BASE / "mock_data" / "uc2" / "accounts_detail.json").read_text())
    return [_redact_record(r) for r in rows]


@mcp.tool()
def get_amc_info() -> list[dict]:
    """Return AMC-name <-> fund-code mapping.

    NOTE: placeholder pending CDC's real "AMC Info.xlsx" (still
    outstanding per context.txt). Derived from the fund titles seen in
    the sample bank statements.
    """
    rows = json.loads((BASE / "mock_data" / "uc2" / "amc_info.json").read_text())
    return [_redact_value(r) for r in rows]


@mcp.tool()
def get_reversal_keywords() -> dict:
    """Return the real Annexure-A bank -> reversal/return keyword list,
    confirmed by CDC, for identifying reversed/returned transactions in
    a bank statement. Bank codes and keywords are not PII — returned
    as-is."""
    return json.loads((BASE / "mock_data" / "uc2" / "reversal_keywords.json").read_text())


@mcp.tool()
def write_fms_reconciliation_file(
    entries: list[dict], filename: str | None = None
) -> dict:
    """Write an FMS bank-reconciliation upload .txt file. Same format as
    write_fms_payment_file: Date,Description,Dr/Cr,0,Amount. Description
    may contain pseudonym tokens, which are de-redacted automatically
    before the line is written."""
    lines = []
    for e in entries:
        date = e["date"]
        description = _deredact(str(e["description"]))
        dr_cr = e["dr_cr"]
        amount = e["amount"]
        lines.append(f"{date},{description},{dr_cr},0,{amount}")

    if filename is None:
        filename = f"fms_reconciliation_{datetime.now():%Y%m%d_%H%M%S}.txt"
    out_path = UC2_OUT / filename
    out_path.write_text("\n".join(lines) + "\n")

    return {
        "written_to": str(out_path),
        "row_count": len(lines),
        "preview_redacted": [_redact(l) for l in lines[:5]],
    }


if __name__ == "__main__":
    mcp.run()
