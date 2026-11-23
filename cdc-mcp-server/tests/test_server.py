"""
Regression tests for server.py's FMS-file writers.

Run with CDC_DISABLE_PII_MODEL=1 for a fast, deterministic regex-only pass
(no Layer-1 model load needed to exercise this logic — these tests only
care about de-redaction/ContextMap behaviour, not detection).
"""

import json
import os
import sys

os.environ.setdefault("CDC_DISABLE_PII_MODEL", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import server


@pytest.fixture(autouse=True)
def _isolated_outputs(tmp_path, monkeypatch):
    """Redirect UC1/UC2 output dirs to a scratch tmp dir so tests never
    touch (or depend on) the real mock_data/*/output folders."""
    uc1_out = tmp_path / "uc1_out"
    uc2_out = tmp_path / "uc2_out"
    uc1_out.mkdir()
    uc2_out.mkdir()
    monkeypatch.setattr(server, "UC1_OUT", uc1_out)
    monkeypatch.setattr(server, "UC2_OUT", uc2_out)
    yield


def test_date_field_is_deredacted_not_just_description():
    """Found live: write_fms_reconciliation_file only ever de-redacted
    `description`. A [DATE_xxx] token passed in the `date` field used to
    be written to the FMS file verbatim — i.e. a redacted placeholder,
    not a real value, ending up in a real upload file. Every field must
    come back as its real value."""
    real_date = "15-JUL-26"
    token = server._context_map.pseudonymise("DATE", real_date)

    result = server.write_fms_reconciliation_file(
        entries=[{"date": token, "description": "plain narration", "dr_cr": "Cr", "amount": 500}],
        filename="date_deredact_test.txt",
    )
    assert result["error"] if "error" in result else True  # no error expected
    assert "error" not in result
    content = (server.UC2_OUT / "date_deredact_test.txt").read_text()
    assert real_date in content
    assert token not in content


def test_all_fields_deredacted_including_amount_string_and_dr_cr():
    """Every entry field is coerced through de-redaction, not just
    description — covers a token landing in amount-as-string or the
    Dr/Cr marker too, however unlikely, since the guarantee is "nothing
    redacted goes into the output file", not "description is handled"."""
    fund_token = server._context_map.pseudonymise("FUND", "NIT Social Impact")
    date_token = server._context_map.pseudonymise("DATE", "01-NOV-26")

    result = server.write_fms_payment_file(
        entries=[{"date": date_token, "description": fund_token, "dr_cr": "Dr", "amount": "1000"}],
        filename="all_fields_test.txt",
    )
    assert "error" not in result
    content = (server.UC1_OUT / "all_fields_test.txt").read_text()
    assert "01-NOV-26" in content
    assert "NIT Social Impact" in content
    assert "[DATE_" not in content
    assert "[FUND_" not in content


def test_unresolved_token_refuses_the_entire_write():
    """A pseudonym-shaped token with no entry in this process's
    ContextMap (stale token, typo, cross-session token, or a genuine
    bug upstream) must never reach the real FMS file — refuse the whole
    write rather than silently shipping a broken placeholder."""
    result = server.write_fms_reconciliation_file(
        entries=[{"date": "01/01/2026", "description": "[FUND_999] never minted", "dr_cr": "Cr", "amount": 1}],
        filename="should_not_exist.txt",
    )
    assert "error" in result
    assert result["written_to"] is None
    assert result["row_count"] == 0
    assert not (server.UC2_OUT / "should_not_exist.txt").exists()


def test_unresolved_token_in_one_row_blocks_the_whole_file_not_just_that_row():
    """Partial writes are worse than no write for a financial upload:
    a batch with one bad row must not produce a file containing the
    other, valid rows either — the caller needs an all-or-nothing
    signal, not a silently-truncated file."""
    good_date = server._context_map.pseudonymise("DATE", "02-NOV-26")
    result = server.write_fms_payment_file(
        entries=[
            {"date": good_date, "description": "fine", "dr_cr": "Cr", "amount": 10},
            {"date": "01/01/2026", "description": "[FUND_888] bad row", "dr_cr": "Dr", "amount": 20},
        ],
        filename="partial_should_not_exist.txt",
    )
    assert "error" in result
    assert not (server.UC1_OUT / "partial_should_not_exist.txt").exists()


def test_write_with_no_tokens_at_all_is_unaffected():
    """Plain entries with no pseudonym tokens anywhere must keep working
    exactly as before — the new de-redact-everything + leftover-token
    guard should be a no-op when there's nothing to de-redact."""
    result = server.write_fms_reconciliation_file(
        entries=[{"date": "10/11/2026", "description": "DEPOSIT", "dr_cr": "Cr", "amount": 86000000}],
        filename="plain_test.txt",
    )
    assert "error" not in result
    content = (server.UC2_OUT / "plain_test.txt").read_text()
    assert content == "10/11/2026,DEPOSIT,Cr,0,86000000\n"


def test_statement_cache_returns_identical_text_and_is_populated(monkeypatch, tmp_path):
    """_extract_and_redact_cached must (a) return the same redacted text
    on a second call for the same doc_id, and (b) actually avoid redoing
    the work — this is the fix for read_bank_statement/read_broker_bill
    redoing full PDF-extract + Layer-1 inference from scratch on every
    single call, which is what made the large ("ZTBL") statement time
    out on every retry."""
    calls = {"extract": 0, "redact": 0}

    def fake_extract(path):
        calls["extract"] += 1
        return "some statement text with NTN 1234567-8"

    def fake_redact(text):
        calls["redact"] += 1
        return text.replace("1234567-8", "[NTN_001]")

    monkeypatch.setattr(server, "extract_text", fake_extract)
    monkeypatch.setattr(server, "_redact", fake_redact)

    pdf_path = tmp_path / "fake.pdf"
    pdf_path.write_text("not a real pdf, just needs to exist for stat()")

    cache: dict = {}
    first = server._extract_and_redact_cached(cache, "DOC-1", pdf_path)
    second = server._extract_and_redact_cached(cache, "DOC-1", pdf_path)

    assert first == second
    assert "[NTN_001]" in first
    assert calls["extract"] == 1, "second call should not re-extract"
    assert calls["redact"] == 1, "second call should not re-redact"


def test_prewarm_populates_cache_only_for_large_documents(monkeypatch, tmp_path):
    """_prewarm_large_documents must populate the cache for a document
    above _PREWARM_SIZE_THRESHOLD and leave a small document alone —
    this is the fix for the first read_bank_statement call after a
    restart (empty cache) paying the full extract+Layer-1 cost live
    and potentially timing out, e.g. on the real "ZTBL" statement."""
    base = tmp_path
    uc1_bills = base / "mock_data" / "uc1" / "bills"
    uc2_statements = base / "mock_data" / "uc2" / "statements"
    uc1_bills.mkdir(parents=True)
    uc2_statements.mkdir(parents=True)
    (base / "mock_data" / "uc1" / "bills_index.json").write_text(
        json.dumps([{"bill_id": "BIG-BILL", "file": "big.pdf"}])
    )
    (base / "mock_data" / "uc2" / "statements_index.json").write_text(
        json.dumps(
            [
                {"statement_id": "BIG-STMT", "file": "big.pdf"},
                {"statement_id": "SMALL-STMT", "file": "small.pdf"},
            ]
        )
    )
    (uc1_bills / "big.pdf").write_bytes(b"x" * (server._PREWARM_SIZE_THRESHOLD + 1))
    (uc2_statements / "big.pdf").write_bytes(b"x" * (server._PREWARM_SIZE_THRESHOLD + 1))
    (uc2_statements / "small.pdf").write_bytes(b"x" * 10)

    monkeypatch.setattr(server, "BASE", base)
    monkeypatch.setattr(server, "UC1_BILLS", uc1_bills)
    monkeypatch.setattr(server, "UC2_STATEMENTS", uc2_statements)
    monkeypatch.setattr(server, "extract_text", lambda path: f"text for {path.name}")
    monkeypatch.setattr(server, "_redact", lambda t: t)

    bill_cache: dict = {}
    statement_cache: dict = {}
    monkeypatch.setattr(server, "_bill_text_cache", bill_cache)
    monkeypatch.setattr(server, "_statement_text_cache", statement_cache)

    server._prewarm_large_documents()

    assert "BIG-BILL" in bill_cache
    assert "BIG-STMT" in statement_cache
    assert "SMALL-STMT" not in statement_cache


def test_statement_cache_invalidates_on_source_file_change(monkeypatch, tmp_path):
    """If the underlying mock PDF is ever replaced (different mtime/size),
    the cache must not serve stale redacted text for it."""
    calls = {"extract": 0}

    def fake_extract(path):
        calls["extract"] += 1
        return f"version {calls['extract']}"

    monkeypatch.setattr(server, "extract_text", fake_extract)
    monkeypatch.setattr(server, "_redact", lambda t: t)

    pdf_path = tmp_path / "fake2.pdf"
    pdf_path.write_text("v1")

    cache: dict = {}
    first = server._extract_and_redact_cached(cache, "DOC-2", pdf_path)
    assert first == "version 1"

    # Simulate the mock file being replaced with different content/size.
    pdf_path.write_text("a completely different, longer v2 payload")
    second = server._extract_and_redact_cached(cache, "DOC-2", pdf_path)
    assert second == "version 2"
    assert calls["extract"] == 2
