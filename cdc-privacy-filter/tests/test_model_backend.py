"""
Tests for Layer 1 (the real OpenAI Privacy Filter model) and for the
two layers working together through PrivacyFilter.

These are skipped automatically if torch/transformers aren't installed
(the "model" extra — see pyproject.toml) or if the model can't be
loaded (e.g. no network on first run, before weights are cached). The
regex-only test suite in test_filter.py never depends on this and
stays fast/offline either way.

The second half of this file (below the "chunking/batching/min_score"
banner) tests OpenAIPrivacyFilterBackend's internal chunking, batching,
and score-filtering logic against a *fake* HF pipeline, so those run
fast and deterministically without ever needing the real model loaded.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cdc_privacy_filter import ContextMap, NoOpModelBackend, PrivacyFilter
from cdc_privacy_filter.model_backend import OpenAIPrivacyFilterBackend

try:
    from cdc_privacy_filter import OpenAIPrivacyFilterBackend as _RealBackendClass

    _backend = _RealBackendClass()
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
# details in letterhead — see test_model_itself_does_not_flag_corporate_
# letterhead_contact below, which checks the MODEL alone, not the full
# filter: Layer 2 now masks letterhead contact info too, by policy, so
# going through PrivacyFilter would mask it regardless of what Layer 1
# does).
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
def test_model_itself_does_not_flag_corporate_letterhead_contact():
    """Documented model behaviour, checked against the model in
    isolation (not through PrivacyFilter, since Layer 2 now masks
    letterhead contact info too by CDC policy — see
    test_filter.py::test_company_email_and_website_redacted — which
    would mask it regardless of what Layer 1 does): a generic company
    `info@...` / public website in letterhead-style `Label : value`
    formatting is organizational contact info, not personal PII, so
    the model itself correctly leaves it undetected. This is exactly
    why masking company contact info is Layer 2's job, not Layer 1's."""
    categories = {span.category for span in _backend.detect(LETTERHEAD_EXCERPT)}
    assert "EMAIL" not in categories
    assert "URL" not in categories


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


# ---------------------------------------------------------------------
# Chunking / batching / min_score — fake-pipeline tests (no real model)
# ---------------------------------------------------------------------
#
# Context: a live full-corpus sweep found one bank statement ("ZTBL", no
# numeric suffix) more than twice the size of any other in the UC2 set.
# Its extracted text produces far more MAX_CHUNK_CHARS-sized chunks than
# usual, and the original code called `self._pipeline(chunk)` once per
# chunk in a tight sequential loop — the dominant cost of a
# read_bank_statement call, and large enough for this one document that
# it consistently exceeded the device bridge's ~80s RPC window on every
# retry. Two fixes, tested below with a fake pipeline so they run fast
# and deterministically regardless of whether the real model is
# installed:
#
# 1. Batch chunks into one `self._pipeline(list_of_chunks)` call instead
#    of N separate calls (cuts per-call Python/tokenizer overhead).
# 2. Apply `min_score` (accepted by __init__, but never actually used to
#    filter anything) to drop low-confidence false positives.


class _FakePipeline:
    """Stand-in for transformers.pipeline(task="token-classification").
    Records every call's input shape and returns scripted entities per
    chunk, keyed by the chunk's exact text."""

    def __init__(self, entities_by_chunk):
        self.entities_by_chunk = entities_by_chunk
        self.calls: list = []  # each entry: the exact argument passed

    def __call__(self, inp):
        self.calls.append(inp)
        if isinstance(inp, list):
            return [self.entities_by_chunk.get(c, []) for c in inp]
        return self.entities_by_chunk.get(inp, [])


def _backend_with_fake_pipeline(entities_by_chunk, min_score=0.5):
    backend = OpenAIPrivacyFilterBackend(min_score=min_score)
    backend._pipeline = _FakePipeline(entities_by_chunk)  # skip real _ensure_loaded()
    return backend


def _ent(entity_group, start, end, score):
    return {"entity_group": entity_group, "start": start, "end": end, "score": score}


def test_min_score_filters_low_confidence_false_positive():
    """Found live: a 0.59-score 'private_person' hit on the plain word
    "Profit" passed straight through before this fix, because min_score
    was accepted by __init__ but never checked anywhere in detect()."""
    text = "Profit paid to Ahsan Qureshi today."
    entities_by_chunk = {
        text: [
            _ent("private_person", 0, 6, 0.59),  # "Profit" — false positive, below 0.6
            _ent("private_person", 15, 28, 0.99),  # "Ahsan Qureshi" — genuine, high confidence
        ]
    }
    backend = _backend_with_fake_pipeline(entities_by_chunk, min_score=0.6)
    spans = backend.detect(text)

    assert len(spans) == 1
    assert spans[0].text == "Ahsan Qureshi"
    assert all(s.text != "Profit" for s in spans)


def test_min_score_keeps_everything_at_or_above_threshold():
    text = "Ahsan Qureshi"
    entities_by_chunk = {text: [_ent("private_person", 0, 13, 0.6)]}
    backend = _backend_with_fake_pipeline(entities_by_chunk, min_score=0.6)
    spans = backend.detect(text)
    assert len(spans) == 1
    assert spans[0].text == "Ahsan Qureshi"


def test_chunks_are_batched_into_groups_of_pipeline_batch_size():
    """With PIPELINE_BATCH_SIZE=8 and more than 8 chunks, the fake
    pipeline must see calls of at most 8 chunks each, as lists — not
    one call per chunk."""
    chunk_size = OpenAIPrivacyFilterBackend.MAX_CHUNK_CHARS
    overlap = OpenAIPrivacyFilterBackend.CHUNK_OVERLAP_CHARS
    # 20 chunks' worth of plain, entity-free text (content doesn't
    # matter here — only the batching shape under test).
    text = "x" * (chunk_size * 20)

    backend = _backend_with_fake_pipeline({})

    def catch_all(inp):
        backend._pipeline.calls.append(inp)
        if isinstance(inp, list):
            return [[] for _ in inp]
        return []

    backend._pipeline.__call__ = catch_all

    backend.detect(text)

    assert len(backend._pipeline.calls) >= 2, "should have made more than one batch call"
    for call in backend._pipeline.calls:
        assert isinstance(call, list), "each call should batch a list of chunks, not a single chunk"
        assert len(call) <= backend.PIPELINE_BATCH_SIZE

    total_chunks_seen = sum(len(call) for call in backend._pipeline.calls)
    # Sanity: roughly 20 MAX_CHUNK_CHARS-sized chunks expected (chunking
    # logic itself is untouched by the batching change; this just
    # confirms batching didn't drop or duplicate chunks).
    assert total_chunks_seen >= 15
    assert overlap >= 0  # documents overlap's role; not itself under test here


def test_batching_preserves_span_offsets_and_dedup_across_batches():
    """End-to-end: entities detected in chunks landing in *different*
    pipeline batches must still come back with correct absolute offsets
    into the original text, and overlap-dedup across the whole document
    must still work exactly as before batching was introduced."""
    chunk_size = OpenAIPrivacyFilterBackend.MAX_CHUNK_CHARS
    filler = "z" * (chunk_size // 2)
    # Two "pages", each with one clean entity-bearing line, far enough
    # apart that they land in different MAX_CHUNK_CHARS chunks, but each
    # comfortably under MAX_CHUNK_CHARS itself so the forced chunk split
    # below lands each page in exactly one, un-truncated chunk.
    page1 = "Contact: alice@example.com " + filler
    page2 = "Contact: bob@example.com " + filler
    text = page1 + page2

    entities_by_chunk = {
        page1: [_ent("private_email", 9, 27, 0.99)],  # "alice@example.com"
        page2: [_ent("private_email", 9, 25, 0.99)],  # "bob@example.com", chunk-relative
    }
    backend = _backend_with_fake_pipeline(entities_by_chunk, min_score=0.5)

    # Force chunk boundaries to land exactly on the page split, with no
    # overlap bleed, so our scripted per-chunk offsets above are exact.
    backend.CHUNK_OVERLAP_CHARS = 0
    backend._chunk_offsets = lambda t: [0, len(page1)]

    spans = backend.detect(text)
    texts = sorted(s.text for s in spans)
    assert texts == ["alice@example.com", "bob@example.com"]

    # Confirm absolute offsets are correct (not left as chunk-relative).
    for span in spans:
        assert text[span.start:span.end] == span.text
