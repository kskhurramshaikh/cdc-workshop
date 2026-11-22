import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cdc_privacy_filter import ContextMap, PrivacyFilter

# Real excerpts (anonymised line context) from the CDC sample set, used to
# make sure the regex layer actually fires against production-shaped text.

BROKER_BILL_EXCERPT = """
DEDICATED STOCK FUND(DSF)
Karachi                                                September 1, 2025
NTN # 1234567-8

With Reference that the following details are Brokerage, WHT & SST on
Ready the Trade for the Dedicated Stock Fund(DSF).

Against the above cheque in favour of ABA ALI HABIB SECURITIES (PVT) LTD.
"""

HABIB_METRO_EXCERPT = """
HABIB METROPOLITAN FINANCIAL SERVICES LTD.
NTN : 3066667-8 - SNTN : 17-00-9805-173-46
"""

BANK_STATEMENT_EXCERPT = """
National Bank Of Pakistan
Account Title(s): CDC-TRUSTE NIT ISLAMIC MONEY MARKET FUND
Branch Code/Name: 82      82 IBB Paposh Nagar Branch   Account No: 3002200677
IBAN: PK38NBPA0082003002200677
"""

ABHI_EXCERPT = """
IBAN :          PK04ABHI0501195629045000 ROZANA MUNAFA ACCOUNT
Account No :    0501-195629-0450-00 PKRS ROZANA MUNAFA ACCOUNT
"""


def _fresh_filter():
    return PrivacyFilter(context_map=ContextMap())


def test_iban_redacted_and_restored():
    pf = _fresh_filter()
    redacted = pf.redact(BANK_STATEMENT_EXCERPT)
    assert "PK38NBPA0082003002200677" not in redacted
    assert "[IBAN_001]" in redacted
    restored = pf.deredact(redacted)
    assert restored == BANK_STATEMENT_EXCERPT


def test_ntn_formats():
    pf = _fresh_filter()
    redacted = pf.redact(BROKER_BILL_EXCERPT)
    assert "1234567-8" not in redacted
    assert "[NTN_001]" in redacted
    assert pf.deredact(redacted) == BROKER_BILL_EXCERPT


def test_habib_metro_composite_ntn():
    pf = _fresh_filter()
    redacted = pf.redact(HABIB_METRO_EXCERPT)
    # both the plain NTN and the composite SNTN must be caught
    assert "3066667-8" not in redacted
    assert "17-00-9805-173-46" not in redacted
    assert redacted.count("[NTN_") == 2


def test_fund_and_broker_names():
    pf = _fresh_filter()
    redacted = pf.redact(BROKER_BILL_EXCERPT)
    assert "ABA ALI HABIB SECURITIES" not in redacted.upper() or "[BROKER_" in redacted
    assert "Dedicated Stock Fund" not in redacted
    assert "[FUND_" in redacted
    assert "[BROKER_" in redacted


def test_account_no_dashed_format():
    pf = _fresh_filter()
    redacted = pf.redact(ABHI_EXCERPT)
    assert "0501-195629-0450-00" not in redacted
    assert "PK04ABHI0501195629045000" not in redacted
    assert pf.deredact(redacted) == ABHI_EXCERPT


def test_stable_pseudonyms_within_one_context_map():
    pf = _fresh_filter()
    text = BROKER_BILL_EXCERPT + "\n" + "NTN # 1234567-8 appears again."
    redacted = pf.redact(text)
    # same real NTN -> same pseudonym both times
    assert redacted.count("[NTN_001]") == 2


def test_deredact_is_exact_roundtrip_for_all_samples():
    for excerpt in (
        BROKER_BILL_EXCERPT,
        HABIB_METRO_EXCERPT,
        BANK_STATEMENT_EXCERPT,
        ABHI_EXCERPT,
    ):
        pf = _fresh_filter()
        assert pf.deredact(pf.redact(excerpt)) == excerpt


def test_no_pii_left_behind_sweep():
    pf = _fresh_filter()
    redacted = pf.redact(BANK_STATEMENT_EXCERPT + ABHI_EXCERPT)
    for leaked in ("PK38NBPA", "PK04ABHI", "3002200677", "0501-195629-0450-00"):
        assert leaked not in redacted
