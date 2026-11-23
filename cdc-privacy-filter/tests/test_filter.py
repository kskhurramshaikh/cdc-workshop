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
    # Case-insensitive: this excerpt's "CDC-TRUSTE NIT ISLAMIC MONEY
    # MARKET FUND" (ALL CAPS) also matches the FUND canonicalisation case
    # — see test_name_canonicalisation_is_stable_across_case — so the IBAN
    # itself (this test's actual subject) round-trips exactly, but the
    # fund name comes back in its canonical Title Case instead.
    assert restored.lower() == BANK_STATEMENT_EXCERPT.lower()
    assert "PK38NBPA0082003002200677" in restored


def test_ntn_formats():
    pf = _fresh_filter()
    redacted = pf.redact(BROKER_BILL_EXCERPT)
    assert "1234567-8" not in redacted
    assert "[NTN_001]" in redacted
    # NTN itself round-trips byte-exact (no canonicalisation applies to
    # NTN) — this excerpt also contains FUND/BROKER names, which DO get
    # canonicalised on de-redact (see test_name_canonicalisation_is_stable
    # and test_deredact_is_exact_roundtrip_for_all_samples below), so a
    # full-string comparison against the original isn't the right check
    # here; just confirm the real NTN comes back correctly.
    assert "1234567-8" in pf.deredact(redacted)


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


WORD_DATE_EXCERPT = "Karachi    September 1, 2025\nStatement period: 01 OCT 2026 to 5 October 2026"


def test_word_format_dates_redacted():
    """Word-format dates (bill issue dates, statement periods) — a
    different shape from the dashed DD-MM-YYYY dates above, but still
    enumerable (a fixed set of month names/abbreviations), so this is
    Layer 2's job too rather than something to fine-tune Layer 1 for."""
    pf = _fresh_filter()
    redacted = pf.redact(WORD_DATE_EXCERPT)
    assert "September 1, 2025" not in redacted
    assert "01 OCT 2026" not in redacted
    assert "5 October 2026" not in redacted
    assert redacted.count("[DATE_") == 3
    assert pf.deredact(redacted) == WORD_DATE_EXCERPT


PHONE_LABELED_EXCERPT = (
    "Phone #    :  32429664-7\nSales Tax Reg.:   S-1234567-8"
)
PHONE_LABELED_EXCERPT_2 = "TEL: (9221) 34302182-84 URL: www.example.com"
PHONE_LABELED_EXCERPT_3 = "TEL: +92 21 38694242 EMAIL: accounts@example.pk"


def test_labeled_phone_numbers_redacted_without_leading_zero():
    """Found via real-corpus smoke testing: PHONE_RE only matches numbers
    with a leading 0 (e.g. '021-32413822'), but real broker letterheads
    also use 'Phone #: 32429664-7' (no leading 0) and 'TEL: (9221)
    34302182-84' (parenthesised city code) — both missed entirely before
    this fix. The label is what makes a bare-digit fallback safe here: an
    unlabeled version of this fix was rejected because the same digit
    shapes also show up as cheque/reference numbers in UC2 bank
    statements, which must NOT be masked."""
    pf = _fresh_filter()
    redacted = pf.redact(PHONE_LABELED_EXCERPT)
    assert "32429664-7" not in redacted
    assert "[PHONE_" in redacted
    assert "Phone #" in redacted  # the label itself stays visible
    assert pf.deredact(redacted) == PHONE_LABELED_EXCERPT


def test_labeled_phone_number_not_mislabeled_as_account_no():
    """A labeled phone number like 'TEL: (9221) 34302182-84' has the same
    'digit groups joined by dashes' shape as ACCOUNT_NO_RE's label-free
    bare-digit fallback, so PHONE_LABELED_RE must claim it first — same
    mislabeling failure mode DATE_RE was added to prevent earlier."""
    pf = _fresh_filter()
    redacted = pf.redact(PHONE_LABELED_EXCERPT_2)
    assert "34302182-84" not in redacted
    assert "[PHONE_" in redacted
    assert "[ACCOUNT_NO_" not in redacted
    assert pf.deredact(redacted) == PHONE_LABELED_EXCERPT_2


def test_international_prefix_phone_number_redacted():
    pf = _fresh_filter()
    redacted = pf.redact(PHONE_LABELED_EXCERPT_3)
    assert "+92 21 38694242" not in redacted
    assert "[PHONE_" in redacted
    assert pf.deredact(redacted) == PHONE_LABELED_EXCERPT_3


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


def test_name_canonicalisation_is_stable_across_case():
    # The actual regression NAME_CANON was added to fix: the same real
    # broker/fund appearing in two different letter-cases in the same
    # document set (ALL CAPS in a signature/title line, Title Case in
    # metadata) must resolve to the SAME pseudonym token, not two
    # different ones — otherwise downstream matching (e.g. UC1's
    # reconciliation, which joins on the broker/fund token) silently
    # breaks. BROKER_BILL_EXCERPT deliberately contains both
    # "DEDICATED STOCK FUND" (caps) and "Dedicated Stock Fund" (title
    # case) for exactly this reason.
    pf = _fresh_filter()
    redacted = pf.redact(BROKER_BILL_EXCERPT)
    assert redacted.count("[FUND_001]") == 2  # DSF's two case variants
    assert redacted.count("[FUND_002]") == 2  # "(DSF)" short form, twice


def test_deredact_is_exact_roundtrip_for_all_samples():
    # Byte-exact round-trip holds for every structured category (NTN,
    # IBAN, account numbers, dates, phone numbers...) with no
    # canonicalisation layer. FUND/BROKER names are the one deliberate
    # exception: NAME_CANON (see test above) collapses every case variant
    # of the same real-world name onto ONE pseudonym token so cross-
    # document matching works, which means de-redaction necessarily
    # restores the list's canonical spelling, not whichever casing a
    # given source document happened to use. ABHI_EXCERPT contains no
    # FUND/BROKER name at all, so byte-exact round-trip holds for it.
    # BROKER_BILL_EXCERPT (mixed-case "Dedicated Stock Fund"),
    # HABIB_METRO_EXCERPT (ALL-CAPS "HABIB METROPOLITAN..." vs. the
    # canonical list's Title Case), and BANK_STATEMENT_EXCERPT (ALL-CAPS
    # "...NIT ISLAMIC MONEY MARKET FUND" vs. the list's Title Case) all
    # hit the canonicalisation case above, so they're compared
    # case-insensitively.
    for excerpt, exact in (
        (BROKER_BILL_EXCERPT, False),
        (HABIB_METRO_EXCERPT, False),
        (BANK_STATEMENT_EXCERPT, False),
        (ABHI_EXCERPT, True),
    ):
        pf = _fresh_filter()
        restored = pf.deredact(pf.redact(excerpt))
        if exact:
            assert restored == excerpt
        else:
            assert restored.lower() == excerpt.lower()


def test_no_pii_left_behind_sweep():
    pf = _fresh_filter()
    redacted = pf.redact(BANK_STATEMENT_EXCERPT + ABHI_EXCERPT)
    for leaked in ("PK38NBPA", "PK04ABHI", "3002200677", "0501-195629-0450-00"):
        assert leaked not in redacted


# Found via a live, full-corpus UC2 sweep (all 20 real bank statements)
# that ran *after* Bug 3's initial fix — a second pass over genuine fund
# names, a date format, and account-number labels the first fix still
# missed. Each excerpt below is the exact raw shape seen in a real
# statement.
HYPHENATED_SHORT_DATE_EXCERPT = (
    "Closing Balance as on :   23-SEP-26               1,701,127.88"
)
NEW_UC2_FUND_NAMES_EXCERPT = (
    "CDC TRUSTEE NIT SOCIAL IMPACT\n"
    "CDC TRUSTEE AWT ISLAMIC INCOME FUND\n"
    "PENSION FUND DEBT SUB FUND\n"
    "PENSION FUND EQUITY INDEXSUB FUND\n"
    "PENSION FUND EQUITY SUBFUND"
)
LABELED_ACCOUNT_NO_VARIANTS_EXCERPT = (
    "Chq: 55669202 To A/c PKR1536000010024\n"
    "Account Statement             2013635849\n"
    "From Acc.No. - 2001050705"
)


def test_hyphenated_short_year_dates_redacted():
    """ZTBL's own 'DD-MON-YY' statement date format (e.g. '23-SEP-26')
    is neither purely numeric (DATE_RE) nor space-separated
    (MONTH_DATE_RE's existing alternatives), so it leaked in full until
    a third MONTH_DATE_RE alternative was added for it."""
    pf = _fresh_filter()
    redacted = pf.redact(HYPHENATED_SHORT_DATE_EXCERPT)
    assert "23-SEP-26" not in redacted
    assert "[DATE_" in redacted
    assert pf.deredact(redacted) == HYPHENATED_SHORT_DATE_EXCERPT


def test_second_pass_uc2_fund_names_redacted():
    """UC2 fund names found leaking in a live full-corpus sweep after
    Bug 3's first fix: NIT Social Impact and AWT Islamic Income Fund
    (neither in amc_info.json, so not caught by the first pass at all),
    plus three BOK pension sub-fund titles, two of which are glued
    differently by that bank's own statement template."""
    pf = _fresh_filter()
    redacted = pf.redact(NEW_UC2_FUND_NAMES_EXCERPT)
    assert redacted.count("[FUND_") == 5
    for leaked in (
        "NIT SOCIAL IMPACT",
        "AWT ISLAMIC INCOME FUND",
        "PENSION FUND DEBT SUB FUND",
        "PENSION FUND EQUITY INDEXSUB FUND",
        "PENSION FUND EQUITY SUBFUND",
    ):
        assert leaked not in redacted


def test_labeled_account_number_variants_redacted():
    """Three label shapes ACCOUNT_NO_RE's original literal-'Account'-only
    match missed: U Microfinance's 'Account Statement <digits>' header,
    BOK's 'To A/c PKR<digits>' transfer narration (PKR itself is just a
    currency code, not sensitive, so it's fine left visible), and the
    abbreviated 'Acc.No. - <digits>' label."""
    pf = _fresh_filter()
    redacted = pf.redact(LABELED_ACCOUNT_NO_VARIANTS_EXCERPT)
    assert "1536000010024" not in redacted
    assert "2013635849" not in redacted
    assert "2001050705" not in redacted
    assert redacted.count("[ACCOUNT_NO_") == 3
    assert pf.deredact(redacted) == LABELED_ACCOUNT_NO_VARIANTS_EXCERPT
