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

# Real letterhead from the sample bills — company contact info, not
# personal PII. CDC's policy is to mask this too (unlike Layer 1, the
# general model, which deliberately leaves organizational contact
# details alone), so it's Layer 2's job.
LETTERHEAD_EXCERPT = """
Office : 419,419-A & 421, KARACHI STOCK EXCHANGE BLDG., KARACHI
Email  : info@abaalihabib.com
Web    : www.abaalihabib.com
Fax    : 32413822
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


DATE_EXCERPT = """
With Reference that the following details are Brokerage, WHT & SST on
Ready the Trade for the Dedicated Stock Fund(DSF) from 01-08-2025 to
31-08-2025.
"""

VERSION_STAMP_EXCERPT = "Form ref: DP-LST-0001/1.0/15-02-2017\nStatement From: 01-Jun-2026"


def test_dashed_dates_are_categorised_as_date_not_account_no():
    """Found via real-corpus smoke testing: a DD-MM-YYYY date has the
    same 'digit groups joined by dashes' shape as ACCOUNT_NO_RE's
    bare-digit fallback, so without a dedicated DATE_RE claiming it
    first, dates get masked but mislabeled as account numbers — and a
    date immediately followed by punctuation (e.g. '31-08-2025.') used
    to fall through to the free-text model instead, which can split a
    date across token boundaries and leak a trailing fragment like the
    bare year."""
    pf = _fresh_filter()
    redacted = pf.redact(DATE_EXCERPT)
    assert "01-08-2025" not in redacted
    assert "31-08-2025" not in redacted
    assert redacted.count("[DATE_") == 2
    assert "[ACCOUNT_NO_" not in redacted
    assert pf.deredact(redacted) == DATE_EXCERPT


def test_date_after_unrelated_version_stamp_still_caught():
    """A date can appear right after an unrelated slash-separated
    version/form-reference stamp (e.g. 'DP-LST-0001/1.0/15-02-2017',
    seen in a real bank statement) — DATE_RE must not be guarded so
    tightly against 'looks like part of a longer chain' that it misses
    this real, standalone date."""
    pf = _fresh_filter()
    redacted = pf.redact(VERSION_STAMP_EXCERPT)
    assert "15-02-2017" not in redacted
    assert "[DATE_" in redacted
    assert pf.deredact(redacted) == VERSION_STAMP_EXCERPT


def test_company_email_and_website_redacted():
    """Layer 2 masks organizational contact info too — unlike Layer 1
    (the model), which deliberately leaves it alone since it's scoped
    to personal PII, not corporate boilerplate."""
    pf = _fresh_filter()
    redacted = pf.redact(LETTERHEAD_EXCERPT)
    assert "info@abaalihabib.com" not in redacted
    assert "www.abaalihabib.com" not in redacted
    assert "[EMAIL_" in redacted
    assert "[URL_" in redacted
    assert pf.deredact(redacted) == LETTERHEAD_EXCERPT


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
