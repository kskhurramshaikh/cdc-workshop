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

# Local mobile / landline numbers, e.g. 0300-1234567, 03001234567, 021-32413822
PHONE_RE = re.compile(r"\b0\d{2,4}-?\d{6,9}\b")

# CDC trustee / fund titles — pseudonymised as commercially sensitive data,
# not because they are personal PII. Extend this list as new funds appear.
KNOWN_FUND_NAMES = [
    "Dedicated Stock Fund", "DSF",
    "Cash Fund",
    "CDC TRUSTEE ASKARI HIGH YIELD SCHEME",
    "CDC TRUSTEE PAK QATAR CASH PLAN",
    "CDC-TRUSTE NIT ISLAMIC MONEY MARKET FUND",
    "PAK QATAR GOKP ISLAMIC",
    "NIT INCOME FUND",
    "NITKPIPF",
    "NITIF", "NITSIF", "NITAAF", "NITIMMF",
    "AWTIIF",
]

# Broker / AMC names seen across the 30 sample bills.
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
]

# Ordered list of (category, pattern) for the deterministic scan.
# Order matters: IBAN before NTN/CNIC/ACCOUNT_NO (longer, more specific,
# must not be swallowed by a shorter bare-digit rule).
STRUCTURED_PATTERNS = [
    ("IBAN", IBAN_RE),
    ("NTN", NTN_RE),
    ("CNIC", CNIC_RE),
    ("ACCOUNT_NO", ACCOUNT_NO_RE),
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
