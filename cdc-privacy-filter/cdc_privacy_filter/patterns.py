"""
CDC Privacy Filter — Pakistani Financial Regex Layer
by Blockverse

Regex patterns derived from real CDC sample documents (30 broker bills,
20 bank statements). Each entry is (CATEGORY, compiled_regex, priority).
Lower priority number = matched/replaced first (more specific patterns
must win over more general ones, e.g. IBAN before a bare digit run).

Categories map to pseudonym prefixes used by ContextMap, e.g.
CNIC -> [CNIC_001], IBAN -> [IBAN_001].
"""

import re

# ---------------------------------------------------------------------------
# Structured identifier patterns (regex layer — the deterministic, auditable
# part of the filter; no model inference needed for these).
# ---------------------------------------------------------------------------

# Pakistani IBAN: PK + 2 check digits + 4-letter bank code + 16 digits = 24 chars
# e.g. PK38NBPA0082003002200677, PK04ABHI0501195629045000
IBAN_RE = re.compile(r"\bPK\d{2}[A-Z]{4}\d{16}\b")

# NTN (National Tax Number). Observed formats in real bills:
#   1234567-8        (7 digits - 1 check digit, most common)
#   0709929-7
#   S0860170-4       (S prefix used by some brokers)
#   17-00-9805-173-46 (SNTN composite format used by Habib Metro-style bills)
#   3536119          (bare 7-digit, no check digit shown)
#   1700853101682    (13-digit variant, individual/CNIC-linked NTN)
NTN_RE = re.compile(
    r"\bS?\d{2}-\d{2}-\d{4}-\d{3}-\d{2}\b"      # 17-00-9805-173-46
    r"|\bS?\d{7}-\d\b"                           # 1234567-8 / S0860170-4
    r"|\bS?\d{13}\b"                             # 1700853101682
    r"|\bS?\d{7}\b"                              # 3536119 (bare, lowest priority)
)

# CNIC: 13 digits, with or without the standard 5-7-1 dash grouping.
# Dashed form not seen in the current sample set but kept for production
# (participant-facing uploads may include it): 42101-1234567-1
CNIC_RE = re.compile(r"\b\d{5}-\d{7}-\d\b|\b\d{13}\b")

# Bank account numbers. Observed forms:
#   Account :        3007880374
#   Account # :      10014040000143
#   Account No :     0501-195629-0450-00
#   Account No:      1081032827497015
#   90006930000036   (bare, following "Account #:")
#
# Real bank statements (e.g. the ZTBL/BOP corpus) also repeat the account
# number many times on later lines with NO "Account" label nearby at all
# (e.g. inside a transaction table, or embedded as the trailing 16 digits
# of an IBAN that itself only appears once). Group 2 is a label-free
# fallback: a standalone 14-18 digit run (the observed CDC/bank account
# number length range) that is NOT part of a longer digit run — the
# lookaround asserts nothing digit/punctuation-joined sits immediately
# before or after, so it can't clip a longer number or a smaller one
# already caught by CNIC_RE (13 digits) or PHONE_RE.
ACCOUNT_NO_RE = re.compile(
    r"\bAccount\s*(?:No\.?|#)?\s*[:.]?\s*((?:\d{2,8}-){1,3}\d{2,8}|\d{6,20})"
    r"|(?<![\w.,-])((?:\d{2,8}-){1,3}\d{2,8}|\d{14,18})(?![\w.,-])",
    re.IGNORECASE,
)

# Dashed/slashed numeric dates, e.g. 01-08-2025, 31-08-2025, 04-08-2025,
# 10/08/2025. Must be claimed as DATE *before* ACCOUNT_NO_RE's bare-digit
# fallback gets a chance at them: a DD-MM-YYYY date has the exact same
# "digit groups joined by dashes" shape as an account number, so without
# this pattern running first, dates get mislabeled as ACCOUNT_NO (not a
# leak — still masked — but wrong, confusing category). It also closes a
# real partial-leak gap: a date immediately followed by punctuation (e.g.
# "31-08-2025.") falls through ACCOUNT_NO_RE's own lookahead guard and
# used to fall to the free-text model instead, which can split the date
# across BPE token boundaries and leave a trailing fragment (e.g. the
# bare year "2025") unmasked. Claiming the whole date deterministically
# here removes that dependency on the model's span boundaries entirely.
# No extra lookaround needed to guard against a longer composite ID (e.g.
# the SNTN format 17-00-9805-173-46) containing a same-shaped date-like
# run: NTN_RE/CNIC_RE/IBAN_RE run before this in STRUCTURED_PATTERNS and
# claim their own spans first, and filter.py's overlap-skip logic (not
# this regex) is what keeps DATE_RE from double-claiming inside them. An
# earlier version added lookarounds here to be extra careful, but they
# backfired on a real document where a date followed an unrelated
# "/1.0/" version stamp (DP-LST-0001/1.0/15-02-2017) — the lookbehind
# mistook that for "part of a longer chain" and blocked a genuine date.
DATE_RE = re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b")

# Word-format dates, e.g. "September 1, 2025" (bill issue dates, UC1) and
# "01 OCT 2026" / "5 October 2026" (statement period dates, UC2 — seen in
# both all-caps abbreviated and mixed-case full-name forms). Enumerable
# (a fixed list of month names/abbreviations), so this is Layer 2's job
# too, same as the dashed-date fix above — not something worth
# fine-tuning Layer 1 (the model) for. Shares the "DATE" category with
# DATE_RE so both numeric and word-format dates draw from the same
# pseudonym counter.
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
MONTH_DATE_RE = re.compile(
    rf"\b{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}\b"       # September 1, 2025
    rf"|\b\d{{1,2}}\s+{_MONTH},?\s+\d{{4}}\b",     # 01 OCT 2026 / 5 October 2026
    re.IGNORECASE,
)

# Local mobile / landline numbers, e.g. 0300-1234567, 03001234567, 021-32413822
PHONE_RE = re.compile(r"\b0\d{2,4}-?\d{6,9}\b")

# Office phone/fax numbers introduced by a "Phone"/"Fax"/"Tel" label.
# Found via real-corpus smoke testing: PHONE_RE above only matches numbers
# with a leading 0 (e.g. "021-32413822"), but the broker letterheads in the
# real bills use several other shapes for the *same* kind of number, all
# missed by PHONE_RE:
#   Phone #    :  32429664-7        (no leading 0, dash-separated extension)
#   Phone #    :  32270808-13
#   TEL: +92 21 38694242            (international prefix)
#   TEL: (9221) 246 3002-4          (parenthesised city code)
#   TEL: (9221) 111 82 87 87
#   TEL: (9221) 34302182-84
# These shapes alone (bare digit runs) would be far too risky to match
# without a label — a corpus-wide check found bare 7-8 digit runs are also
# used for cheque numbers, reference numbers and customer IDs in the UC2
# bank statements, and masking those would be a real functional regression
# (UC2's reconciliation flow needs cheque numbers intact). The label is
# what makes this safe and specific, same principle as ACCOUNT_NO_RE's own
# labeled alternative. Capturing group 1 holds just the number, so only
# that gets pseudonymised and the "Phone #:" label itself stays visible —
# see filter.py's generic "match.lastindex" span selection.
#
# Known residual gap (documented, not fixed here): some letterheads also
# carry a second, *unlabeled* number — almost always the Fax — that the
# PDF's column-based text extraction separates from its "Fax" label
# entirely, so there is no reliable label to anchor on. A bare-digit
# fallback broad enough to catch it would reintroduce the cheque/reference
# -number false-positive risk above, so this is left as a known limitation
# rather than guessed at.
PHONE_LABELED_RE = re.compile(
    r"\b(?:Phone|Fax|Tel(?:ephone)?)\s*(?:#|No\.?)?\s*[:.]?\s*"
    r"((?:\+|\()?\d[\d()\s-]{3,25}\d)",
    re.IGNORECASE,
)

# Email addresses and websites — including a company's OWN letterhead
# contact info (e.g. "info@abaalihabib.com", "www.abaalihabib.com").
# Layer 1 (the general PII model) deliberately leaves organizational
# contact details alone — it's scoped to personal PII, not corporate
# boilerplate — but CDC's policy for this workshop is to mask company
# contact info too, so that's a Layer 2 job: unlike the model, this
# regex makes no personal-vs-corporate distinction, by design.
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
URL_RE = re.compile(
    r"\b(?:https?://[^\s,]+|www\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:/[^\s,]*)?)",
    re.IGNORECASE,
)

# CDC trustee / fund titles — pseudonymised as commercially sensitive data,
# not because they are personal PII. Extend this list as new funds appear.
# Full names below are taken verbatim from CDC's own amc_info.json
# (fund_codes), the authoritative source for fund naming in this corpus —
# not guessed from the truncated/glued forms the bank statements print.
#
# Entries are kept prefix-independent of the "CDC TRUSTEE" / "CDC-TRUSTEE" /
# "CDCTrustee" / "CDCTRUSTEE" boilerplate on purpose — that prefix is not
# identifying (every one of these accounts has CDC as trustee) and its
# spelling/punctuation varies per bank's statement template (confirmed via
# the live UC2 statement sweep: "CDC TRUSTEE ASKARI HIGH YIELD SCHEME" as a
# literal string silently failed to match ZTBL's own "CDC-TRUSTEE ASKARI
# HIGH YIELD SCHEME" — hyphen vs. space — leaking the fund name in full).
# Several bank statement templates also hard-truncate the customer-title
# field at a fixed column width (confirmed in the PDFs themselves, not an
# extraction artifact — e.g. "...NIT KP ISLAMIC PE", "...NIT ASSET ALLOCA" —
# amc_info.json has no untruncated spelled-out form for these two, only the
# short fund_codes "NITKPIPF"/"NITAAF", so the literal truncated fragment
# is the only reachable match for them). longest-first matching in
# build_name_pattern() prefers the full form whenever it's present.
KNOWN_FUND_NAMES = [
    "Dedicated Stock Fund", "DSF",
    "Cash Fund",
    "Askari High Yield Scheme", "Askari High Yield",  # 2nd: U-Micro's "...Sch" truncation
    "Askari Sovereign Yield Enhancer", "Askari Sovereign Yield",  # 2nd: "...Yield" truncation
    "Askari Sovereign Cash Fund",
    "Pak Qatar Cash Plan",
    "Pak Qatar GOKP Islamic",
    "NIT Income Fund",
    "NIT Islamic Money Market Fund",  # full form seen in NBP's statement;
                                       # amc_info.json only has the short
                                       # code "NITIMMF" for this one
    "MCB Alhamra KPK Govt Employee",
    "NIT ASSET ALLOCA",   # truncated; no full spelled form in amc_info.json
    "NIT KP ISLAMIC PE",  # truncated; no full spelled form in amc_info.json
    # BOK's statement template runs "TRUSTEE" straight into the fund name
    # with no space ("CDC TRUSTEEMCB ALHAMRA...", confirmed in the PDF
    # itself, not an extraction artifact) — build_name_pattern()'s \b
    # boundary can't start a match mid-word, so the literal has to include
    # the glued "TRUSTEEMCB" run to be reachable at all.
    "TRUSTEEMCB ALHAMRA KPK GOVT EMPLOYEE",
    "NITKPIPF",
    "NITIF", "NITSIF", "NITAAF", "NITIMMF",
    "AWTIIF",
]

# Broker / AMC names seen across the 30 sample bills and 20 bank
# statements. The AMC names (amc_info.json's amc_name field) are listed
# here rather than in KNOWN_FUND_NAMES because this list's own header
# comment already scopes it to "Broker / AMC names".
KNOWN_BROKER_NAMES = [
    "ABA Ali Habib Securities",
    "Al Habib Capital Markets",
    "Alpha Capital",
    "Growth Securities",
    "Habib Metropolitan Financial Services",
    "Optimus Capital Management",
    "Taurus Securities",
    "Topline Securities",
    "Akik Capital",
    "Ismail Iqbal Securities",
    "Next Capital",
    "BMA Capital Management",
    "Shajar Capital Pakistan",
    "National Investment Trust Limited",
    "Askari Investment Management Limited",
    "Pak Qatar Asset Management Company",
    "MCB Investment Management Limited",
    "Bank of Punjab Asset Management",
]

# Ordered list of (category, pattern) for the deterministic scan.
# Order matters: IBAN before NTN/CNIC/ACCOUNT_NO (longer, more specific,
# must not be swallowed by a shorter bare-digit rule). EMAIL/URL go
# before PHONE/ACCOUNT_NO's bare-digit fallbacks aren't at risk of
# collision (letters/@ vs. pure digit runs), but checking them early
# costs nothing and keeps the "most specific first" convention.
#
# EMAIL/URL/PHONE share their category name with Layer 1 (the model) on
# purpose — see model_backend.py's _LABEL_TO_CATEGORY — so a value
# either layer catches is numbered from the same ContextMap counter.
# PHONE_LABELED_RE runs *before* ACCOUNT_NO_RE: a labeled phone number
# like "Phone #    :  32270808-13" or "TEL: (9221) 34302182-84" has the
# exact same "digit groups joined by dashes" shape as ACCOUNT_NO_RE's
# label-free bare-digit fallback, so without this ordering those numbers
# would get claimed (and mislabeled) as ACCOUNT_NO first — the same
# mislabeling failure mode DATE_RE was added to prevent above.
STRUCTURED_PATTERNS = [
    ("IBAN", IBAN_RE),
    ("NTN", NTN_RE),
    ("CNIC", CNIC_RE),
    ("DATE", DATE_RE),
    ("DATE", MONTH_DATE_RE),
    ("PHONE", PHONE_LABELED_RE),
    ("ACCOUNT_NO", ACCOUNT_NO_RE),
    ("EMAIL", EMAIL_RE),
    ("URL", URL_RE),
    ("PHONE", PHONE_RE),
]


def build_name_pattern(names: list[str]) -> re.Pattern:
    """Compile a case-insensitive alternation pattern for a list of known
    names, longest-first so multi-word names aren't partially matched."""
    escaped = sorted((re.escape(n) for n in names), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


FUND_NAME_RE = build_name_pattern(KNOWN_FUND_NAMES)
BROKER_NAME_RE = build_name_pattern(KNOWN_BROKER_NAMES)

NAME_PATTERNS = [
    ("FUND", FUND_NAME_RE),
    ("BROKER", BROKER_NAME_RE),
]

# Canonicalisation lookup, lowercased-match -> canonical spelling, for each
# NAME_PATTERNS category. BROKER_NAME_RE/FUND_NAME_RE match case-
# insensitively (by design — the same broker appears in ALL CAPS in a
# bill's signature line and in Title Case in the index metadata), but
# ContextMap.pseudonymise() keys on the exact matched string. Without
# canonicalising to one spelling before that lookup, the same real-world
# broker/fund gets two different pseudonym tokens depending on which
# casing variant happened to match first (confirmed via live testing:
# "ABA Ali Habib Securities" from bills_index.json's metadata field vs.
# "ABA ALI HABIB SECURITIES" from the PDF's own "in favour of..." line
# produced [BROKER_001] and [BROKER_014] for the identical broker) — which
# silently breaks any downstream logic trying to match a bill's broker
# token against the liability export's broker token for the same entity.
NAME_CANON = {
    "FUND": {n.lower(): n for n in KNOWN_FUND_NAMES},
    "BROKER": {n.lower(): n for n in KNOWN_BROKER_NAMES},
}
