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

import concurrent.futures
import json
import os
import re
import sys
import threading
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
# Sarhad on Render (workshop stand-in for an on-prem/hosted deployment):
# if set, both privacy-filter layers run on Render instead of in this
# process — _redact() calls Sarhad's `detect_pii` MCP tool (via
# remote_detector.py) for span detection and applies the pseudonym
# substitution locally via _privacy_filter.apply_spans(), so
# the ContextMap (the only place real PII<->pseudonym mappings live)
# never leaves this machine. Unset (the default) keeps everything local,
# exactly as before this was added.
_RENDER_URL = os.environ.get("RENDER_PRIVACY_FILTER_URL")
_RENDER_API_KEY = os.environ.get("PRIVACY_FILTER_API_KEY")
if _RENDER_URL:
    from remote_detector import RemoteDetectionError, detect_remote  # noqa: E402

# Set CDC_DISABLE_PII_MODEL=1 to skip loading Layer 1 locally (e.g. for a
# quick regex-only smoke test, or a machine without torch/transformers)
# — Layer 2 keeps working on its own either way. When RENDER_PRIVACY_FILTER_URL
# is set, Layer 1 runs on Render instead, so there is no reason for this
# process to also load the ~2.8GB model locally — that would double the
# memory/download cost for no benefit, since _redact() below never calls
# the local model backend in that case.
if _RENDER_URL:
    _model_backend = NoOpModelBackend()
    print(f"[cdc-mcp-server] Privacy filter hosted on Render: {_RENDER_URL} — local model not loaded.", file=sys.stderr)
elif os.environ.get("CDC_DISABLE_PII_MODEL") == "1":
    _model_backend = NoOpModelBackend()
    print("[cdc-mcp-server] CDC_DISABLE_PII_MODEL=1 — Layer 1 (model) skipped, regex-only.", file=sys.stderr)
else:
    _model_backend = default_model_backend()

_privacy_filter = PrivacyFilter(context_map=_context_map, model_backend=_model_backend)

mcp = FastMCP("cdc-workshop")

# In-process cache: statement_id/bill_id -> already-redacted text. Keyed on
# the source PDF's (mtime, size) so a changed mock file invalidates itself
# automatically. This is memory-only, scoped to this one server process —
# never persisted to disk, so it never goes stale or inconsistent across a
# restart (restart resets _context_map too, so pseudonym numbering and this
# cache always come from the exact same process).
#
# Why this exists: before this, read_bank_statement/read_broker_bill redid
# the full PDF-extraction + Layer-1-model pass from scratch on *every*
# call, even a second call for the same already-seen document. For a large
# statement (e.g. a 664KB PDF whose extracted text runs to dozens of
# Layer-1 chunks) that first pass can take longer than the device bridge's
# ~80s RPC window, so the caller's call times out — but the work keeps
# running server-side (Python doesn't know or care that the caller gave
# up listening). Without a cache, a retry paid that same full cost again
# and could time out again forever. With it, the first call's result
# lands in the cache the moment it finishes (even if that caller never
# saw it), and a retry a few seconds later returns instantly.
class _CoalescingCache:
    """A (mtime, size)-versioned cache that also coalesces concurrent
    callers for the same key, so a retry joins the one computation
    already running instead of starting a second, fully independent
    copy of it.

    Why this exists: the plain dict cache below only remembers a
    *finished* result. It does nothing for a caller that arrives while
    an earlier call for the same key is still computing — e.g. a
    document whose redaction genuinely takes longer than the device
    bridge's ~80s RPC window, so the caller's RPC times out and it
    retries. FastMCP does not cancel the still-running first attempt,
    and the plain cache gives a retry no way to notice that work is
    already in flight, so it just starts a second, fully redundant
    pass over the same document. Because the persistent Sarhad
    connection (remote_detector.py) serves one detect_pii call at a
    time off a single queue, those duplicate passes don't run in
    parallel — they queue up and run one after another, so the Nth
    retry has to wait for every earlier (wasted) attempt to finish
    before it even starts its own. This is what turned a single large
    statement (ZTBL, 664KB) into a string of ~90-100s duplicate
    passes instead of one.

    With this, a second caller for a key that's still being computed
    blocks on the *same* in-progress Future and gets the first
    caller's result directly — no duplicate work, no queue pile-up.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._done: dict[str, tuple[float, int, object]] = {}
        self._inflight: dict[str, concurrent.futures.Future] = {}

    def get_or_compute(self, key: str, version: tuple[float, int], compute_fn):
        with self._lock:
            cached = self._done.get(key)
            if cached is not None and (cached[0], cached[1]) == version:
                return cached[2]
            fut = self._inflight.get(key)
            if fut is not None:
                owner = False
            else:
                fut = concurrent.futures.Future()
                self._inflight[key] = fut
                owner = True

        if not owner:
            # Someone else is already computing this exact key — wait
            # on their result instead of recomputing it ourselves.
            return fut.result()

        try:
            result = compute_fn()
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
            fut.set_exception(exc)
            raise
        else:
            with self._lock:
                self._done[key] = (version[0], version[1], result)
                self._inflight.pop(key, None)
            fut.set_result(result)
            return result


_statement_text_cache = _CoalescingCache()
_bill_text_cache = _CoalescingCache()

# Same idea as the PDF cache above, for the small static JSON mock-data
# files (bills_index, liability_export, statements_index, accounts_detail,
# amc_info). Before this, list_broker_bills/list_bank_statements/
# get_liability_export/get_accounts_detail/get_amc_info re-redacted every
# field of their (unchanging) source file on *every single call* — with
# Sarhad configured (RENDER_PRIVACY_FILTER_URL set) that means a real
# network round-trip per redactable field, every time. A handful of
# fields is fine once, but FastMCP does not cancel an already-running
# sync tool call when its caller stops waiting (e.g. the device bridge's
# ~80s RPC window expires) — so a caller that times out and retries just
# stacks another full, fully redundant pass over the same unchanging data
# on top of whatever earlier attempt(s) are still running, and those
# pile up. This is what turned get_accounts_detail (20 small JSON rows)
# into a 20+ minute operation under repeated retries: not bulk PDF work,
# just the same tiny redaction job being redone again and again with no
# cache to short-circuit it. Using _CoalescingCache (see above) rather
# than a plain dict means this is also safe for a *slow* single
# document (e.g. read_bank_statement on a large PDF): a retry joins the
# one computation already running instead of adding another to the
# queue.
_json_record_cache = _CoalescingCache()


def _redact_json_records_cached(cache_key: str, path: Path, redact_fn):
    """Load path's JSON once, run redact_fn(rows) once, and cache the
    result under cache_key — keyed on path's (mtime, size) so editing
    the mock file invalidates its own entry automatically. Concurrent/
    retried calls for the same cache_key coalesce onto one computation
    (see _CoalescingCache)."""
    stat = path.stat()
    return _json_record_cache.get_or_compute(
        cache_key,
        (stat.st_mtime, stat.st_size),
        lambda: redact_fn(json.loads(path.read_text())),
    )


def _redact(text: str) -> str:
    if _RENDER_URL:
        try:
            spans = detect_remote(text, _RENDER_URL, _RENDER_API_KEY)
            return _privacy_filter.apply_spans(text, spans)
        except RemoteDetectionError as exc:
            # Fail closed, not open: if Render is unreachable we do NOT
            # silently fall back to local detection, because that would
            # mean a server advertised as "redaction via Render" quietly
            # redacted locally instead with no signal to the operator.
            # Surface the error so the caller sees the tool call fail
            # rather than getting text that was redacted a different way
            # than configured.
            print(f"[cdc-mcp-server] Render detection failed: {exc}", file=sys.stderr)
            raise
    return _privacy_filter.redact(text)


def _deredact(text: str) -> str:
    return _privacy_filter.deredact(text)


def _extract_and_redact_cached(
    cache: "_CoalescingCache", doc_id: str, pdf_path: Path
) -> str:
    """extract_text() + _redact(), cached per doc_id for this process's
    lifetime. Keyed on the source file's (mtime, size) so editing/
    replacing a mock PDF invalidates its own cache entry automatically.
    Concurrent/retried calls for the same doc_id coalesce onto one
    computation (see _CoalescingCache) instead of each paying the full
    extract+redact cost independently."""
    stat = pdf_path.stat()
    return cache.get_or_compute(
        doc_id,
        (stat.st_mtime, stat.st_size),
        lambda: _redact(extract_text(pdf_path)),
    )


# Pseudonym tokens look like [CATEGORY_NNN] (see ContextMap.pseudonymise).
# Used as a final, hard safety net on every FMS output line: de-redaction
# only restores tokens this process's ContextMap actually minted, so any
# token shape still present afterwards is unresolved — never write that to
# a real payment/reconciliation file.
_PSEUDO_TOKEN_RE = re.compile(r"\[[A-Z][A-Z_]*_\d{3}\]")


def _deredact_field(value) -> str:
    """De-redact any pseudonym token inside a single FMS entry field.
    Every field is coerced through this — not just `description` — so a
    token in the date, Dr/Cr marker, or a string amount is restored too,
    not left as a literal [CATEGORY_NNN] in the output file."""
    return _deredact(str(value))


def _build_fms_line(entry: dict, row_index: int) -> str:
    date = _deredact_field(entry["date"])
    description = _deredact_field(entry["description"])
    dr_cr = _deredact_field(entry["dr_cr"])
    amount = entry["amount"]
    if isinstance(amount, str):
        amount = _deredact_field(amount)
    line = f"{date},{description},{dr_cr},0,{amount}"

    leftover = _PSEUDO_TOKEN_RE.findall(line)
    if leftover:
        # This process's ContextMap has no real value for this token (a
        # stale token from a different run, a typo, or a genuine bug) —
        # refusing beats silently shipping a broken placeholder into a
        # real FMS upload file.
        raise ValueError(
            f"row {row_index}: unresolved pseudonym token(s) {leftover} — "
            "no real value for these in this server's context map; "
            "refusing to write any part of this file."
        )
    return line


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

# Fields that are structured financial/period data, not PII, from the
# mock-data schema itself — must pass through untouched, never run
# through the regex+model redaction pipeline. Found via live testing on
# get_liability_export: without this, "period" values like "2025-08" get
# swallowed whole by ACCOUNT_NO_RE's bare-digit-dash fallback (identical
# shape to a dashed account number), and bare "expected_amount" strings
# like "13,343.52" get partially mangled by the free-text model backend
# (Layer 1), which has no sentence context to work with on an isolated
# number and can misfire — e.g. "13,343.52" coming back as
# "13,[ADDRESS_001].52". The whole point of this filter is to protect
# identity/contact info, not obscure the figures the reconciliation
# logic itself needs to read — same principle read_broker_bill already
# follows for brokerage/WHT/SST amounts in free text.
_PASSTHROUGH_FIELDS = {"period", "expected_amount", "amount", "bill_id", "statement_id"}


def _redact_record(record: dict) -> dict:
    """Redact a mock-data record, using the known field->category map
    where available, passing structured numeric/period fields through
    untouched, and falling back to regex-based redaction (which catches
    broker/fund names, free-text titles, etc.) everywhere else.
    """
    out = {}
    for key, value in record.items():
        if value is None or key in _PASSTHROUGH_FIELDS:
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
    def _redact_rows(index):
        return [
            {
                "bill_id": r["bill_id"],
                "broker_name": _redact(r["broker_name"]),
                "fund": _redact(r["fund"]),
                "period": r["period"],
            }
            for r in index
        ]

    path = BASE / "mock_data" / "uc1" / "bills_index.json"
    return _redact_json_records_cached("list_broker_bills", path, _redact_rows)


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
    return _extract_and_redact_cached(_bill_text_cache, bill_id, pdf_path)


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
    path = BASE / "mock_data" / "uc1" / "liability_export.json"
    return _redact_json_records_cached(
        "get_liability_export", path, lambda rows: [_redact_record(r) for r in rows]
    )


@mcp.tool()
def write_fms_payment_file(
    entries: list[dict], filename: str | None = None
) -> dict:
    """Write an FMS payment upload .txt file. Each entry must have:
    date (DD/MM/YYYY), description (str — may contain pseudonym tokens,
    which will be de-redacted automatically), dr_cr ("-" or "+"), amount
    (number or numeric string). Produces comma-separated lines:
    Date,Description,Dr/Cr,0,Amount — matching the confirmed FMS format.
    Every field (not just description) is de-redacted, and the whole
    write is refused — nothing is written — if any field still contains
    an unresolved pseudonym token afterwards.
    """
    # SWAP FOR PRODUCTION: push to FMS's real upload endpoint/SFTP drop
    # instead of writing a local .txt file.
    try:
        lines = [_build_fms_line(e, i) for i, e in enumerate(entries)]
    except ValueError as exc:
        return {"error": str(exc), "written_to": None, "row_count": 0}

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
    path = BASE / "mock_data" / "uc2" / "statements_index.json"
    return _redact_json_records_cached(
        "list_bank_statements", path, lambda rows: [_redact_record(r) for r in rows]
    )


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
    return _extract_and_redact_cached(_statement_text_cache, statement_id, pdf_path)


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
    path = BASE / "mock_data" / "uc2" / "accounts_detail.json"
    return _redact_json_records_cached(
        "get_accounts_detail", path, lambda rows: [_redact_record(r) for r in rows]
    )


@mcp.tool()
def get_amc_info() -> list[dict]:
    """Return AMC-name <-> fund-code mapping.

    NOTE: placeholder pending CDC's real "AMC Info.xlsx" (still
    outstanding per context.txt). Derived from the fund titles seen in
    the sample bank statements.
    """
    path = BASE / "mock_data" / "uc2" / "amc_info.json"
    return _redact_json_records_cached(
        "get_amc_info", path, lambda rows: [_redact_value(r) for r in rows]
    )


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
    write_fms_payment_file: Date,Description,Dr/Cr,0,Amount. Every field
    (date, description, Dr/Cr, amount) may contain pseudonym tokens and
    is de-redacted automatically before the line is written; the whole
    write is refused — nothing is written — if any field still contains
    an unresolved pseudonym token afterwards."""
    try:
        lines = [_build_fms_line(e, i) for i, e in enumerate(entries)]
    except ValueError as exc:
        return {"error": str(exc), "written_to": None, "row_count": 0}

    if filename is None:
        filename = f"fms_reconciliation_{datetime.now():%Y%m%d_%H%M%S}.txt"
    out_path = UC2_OUT / filename
    out_path.write_text("\n".join(lines) + "\n")

    return {
        "written_to": str(out_path),
        "row_count": len(lines),
        "preview_redacted": [_redact(l) for l in lines[:5]],
    }


# Documents above this size get their extract+redact work done proactively
# at server startup instead of lazily on first request. Same ballpark as
# the "ZTBL" statement (664KB) that was more than 2x any other UC2
# statement and consistently exceeded the device bridge's ~80s RPC window
# on its first (uncached) read. Without pre-warming, _extract_and_redact_
# cached() still protects every *retry*, but the very first caller after
# a server restart still pays the full cost live and can time out. Set
# CDC_DISABLE_PREWARM=1 to skip this (e.g. for a quick local smoke test
# where a slower-but-eventually-correct first call doesn't matter).
_PREWARM_SIZE_THRESHOLD = 400_000  # bytes


def _prewarm_large_documents() -> None:
    """Populate _bill_text_cache/_statement_text_cache for any document
    whose PDF exceeds _PREWARM_SIZE_THRESHOLD, so the first real
    read_bank_statement/read_broker_bill call for it already hits the
    cache instead of doing full PDF-extraction + Layer-1 inference
    inside a live, time-limited request. Intended to run in a background
    thread at startup: never blocks the server from accepting requests,
    and any failure here is logged and swallowed rather than raised,
    since a pre-warm problem must never prevent the server itself from
    starting."""
    try:
        bills_index = json.loads((BASE / "mock_data" / "uc1" / "bills_index.json").read_text())
        for rec in bills_index:
            pdf_path = UC1_BILLS / rec["file"]
            if pdf_path.exists() and pdf_path.stat().st_size > _PREWARM_SIZE_THRESHOLD:
                print(
                    f"[cdc-mcp-server] pre-warming bill cache: {rec['bill_id']} "
                    f"({pdf_path.stat().st_size} bytes)",
                    file=sys.stderr,
                )
                _extract_and_redact_cached(_bill_text_cache, rec["bill_id"], pdf_path)

        statements_index = json.loads((BASE / "mock_data" / "uc2" / "statements_index.json").read_text())
        for rec in statements_index:
            pdf_path = UC2_STATEMENTS / rec["file"]
            if pdf_path.exists() and pdf_path.stat().st_size > _PREWARM_SIZE_THRESHOLD:
                print(
                    f"[cdc-mcp-server] pre-warming statement cache: {rec['statement_id']} "
                    f"({pdf_path.stat().st_size} bytes)",
                    file=sys.stderr,
                )
                _extract_and_redact_cached(_statement_text_cache, rec["statement_id"], pdf_path)

        print("[cdc-mcp-server] pre-warm complete.", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - pre-warm is best-effort, never fatal
        print(f"[cdc-mcp-server] pre-warm failed (non-fatal): {exc!r}", file=sys.stderr)


if __name__ == "__main__":
    if os.environ.get("CDC_DISABLE_PREWARM") != "1":
        threading.Thread(target=_prewarm_large_documents, daemon=True, name="cdc-prewarm").start()
    mcp.run()
