"""
Tests for Layer 1 (the real OpenAI Privacy Filter model) and for the
two layers working together through PrivacyFilter.

These are skipped automatically if torch/transformers aren't installed
(the "model" extra — see pyproject.toml) or if the model can't be
loaded (e.g. no network on first run, before weights are cached). The
regex-only test suite in test_filter.py never depends on this and
stays fast/offline either way.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cdc_privacy_filter import ContextMap, NoOpModelBackend, PrivacyFilter

try:
    from cdc_privacy_filter import OpenAIPrivacyFilterBackend

    _backend = OpenAIPrivacyFilterBackend()
    _backend._ensure_loaded()
    MODEL_AVAILABLE = True
except Exception:  # noqa: BLE001 - any load failure means "skip these tests"
    _backend = None
    MODEL_AVAILABLE = False

requires_model = pytest.mark.skipif(
    not MODEL_AVAILABLE,
    reason="openai/privacy-filter model not installed/reachable (pip install -e '.[model]')",
)

# A free-text signature line — the exact gap the regex layer (Layer 2)
# can't cover, since personal names aren't enumerable by pattern.
SIGNATURE_EXCERPT = """
Thanks & Regards,

Bilal Rasheed
Settlement Officer
bilal.rasheed@abaalihabib.com
"""

# A personal email/phone, phrased the way the model was trained to
# recognise private contact info (vs. a company's own public contact
# details in letterhead, which the model correctly leaves alone — see
# test_corporate_letterhead_contact_is_not_flagged_as_private below).
PERSONAL_CONTACT_EXCERPT = "Please reach me at bilal.rasheed@abaalihabib.com for any queries."

LETTERHEAD_EXCERPT = """
Office : 419,419-A & 421, KARACHI STOCK EXCHANGE BLDG., KARACHI
Email  : info@abaalihabib.com
Web    : www.abaalihabib.com
"""


@requires_model
def test_model_detects_free_text_person_name():
    pf = PrivacyFilter(context_map=ContextMap(), model_backend=_backend)
    redacted = pf.redact(SIGNATURE_EXCERPT)
    assert "Bilal Rasheed" not in redacted
    assert "[PERSON_" in redacted


@requires_model
def test_model_detects_personal_email_regex_layer_misses():
    pf = PrivacyFilter(context_map=ContextMap(), model_backend=_backend)
    redacted = pf.redact(PERSONAL_CONTACT_EXCERPT)
    assert "bilal.rasheed@abaalihabib.com" not in redacted
    assert "[EMAIL_" in redacted


@requires_model
def test_corporate_letterhead_contact_is_not_flagged_as_private():
    """Documented model behaviour, not a bug in our wiring: a generic
    company `info@...` / public website in letterhead-style `Label :
    value` formatting is organizational contact info, not personal
    PII, so the model correctly leaves it unredacted. If CDC's own
    policy wants company contact details masked too, that's a Layer-2
    (regex) job, not something to force onto Layer 1."""
    pf = PrivacyFilter(context_map=ContextMap(), model_backend=_backend)
    redacted = pf.redact(LETTERHEAD_EXCERPT)
    assert "info@abaalihabib.com" in redacted
    assert "www.abaalihabib.com" in redacted


@requires_model
def test_model_output_roundtrips_exactly():
    pf = PrivacyFilter(context_map=ContextMap(), model_backend=_backend)
    for excerpt in (SIGNATURE_EXCERPT, LETTERHEAD_EXCERPT):
        assert pf.deredact(pf.redact(excerpt)) == excerpt


@requires_model
def test_regex_layer_still_wins_on_overlap_with_structured_ids():
    """IBAN/NTN/account numbers must still come out as their Layer-2
    category (IBAN/NTN/ACCOUNT_NO), not whatever the model happens to
    tag them as, since Layer 2 claims structured spans first."""
    pf = PrivacyFilter(context_map=ContextMap(), model_backend=_backend)
    text = "IBAN: PK38NBPA0082003002200677 belongs to the account holder."
    redacted = pf.redact(text)
    assert "[IBAN_001]" in redacted
    assert "PK38NBPA0082003002200677" not in redacted


def test_fallback_to_noop_when_model_unavailable():
    """default_model_backend()'s try/except path, exercised directly:
    a PrivacyFilter built with NoOpModelBackend still works (Layer 2
    only) even if Layer 1 can't load."""
    pf = PrivacyFilter(context_map=ContextMap(), model_backend=NoOpModelBackend())
    redacted = pf.redact(SIGNATURE_EXCERPT)
    # No model running -> the free-text name is NOT caught (expected;
    # this is exactly the gap Layer 1 exists to close).
    assert "Bilal Rasheed" in redacted
