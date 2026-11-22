"""
Pluggable model backend — Layer 1 of the two-layer privacy filter.

Layer 1 (this module): a general-purpose local PII model. Catches
free-text personal names, addresses, emails, URLs, and dates that
appear inconsistently across documents (e.g. "BILAL RASHEED" signing
a broker bill, or a webmaster's email in a letterhead) — things a
fixed regex can never fully enumerate.

Layer 2 (patterns.py, applied in filter.py): deterministic regex for
structured Pakistani financial identifiers — IBAN, NTN, CNIC, account
numbers, known fund/broker names — which the general model was never
trained to recognise as Pakistan-specific formats.

Both layers feed the same PrivacyFilter._collect_spans() merge step,
so either can catch a given span; whichever claims it first wins
and the context_map pseudonymises it the same way either way.

Layer 1 backend: OpenAI Privacy Filter (`openai/privacy-filter` on
Hugging Face) — Apache-2.0, ~1.5B total / 50M active params (MoE),
bidirectional token-classification, 128K context, runs fully local
(CPU is fine for this corpus's document sizes; no network egress is
needed once the weights are cached locally the first time). It
detects: private_person, private_address, private_email,
private_phone, private_url, private_date, secret, account_number.

This module stays usable without that model installed: construction
of OpenAIPrivacyFilterBackend raises ImportError / OSError if
`torch`/`transformers` aren't present or the weights can't be fetched,
and `default_model_backend()` catches that and falls back to
NoOpModelBackend so the regex layer (Layer 2) keeps working on its
own. `PrivacyFilter()`'s own default stays NoOpModelBackend too —
nothing here downloads a model just because the package was
imported; the MCP server opts in explicitly via
`default_model_backend()`.
"""

from __future__ import annotations

import sys
from typing import NamedTuple, Protocol


class Span(NamedTuple):
    start: int
    end: int
    category: str
    text: str


class ModelBackend(Protocol):
    def detect(self, text: str) -> list[Span]:
        """Return PII spans the regex layer wouldn't catch (free-text
        names, addresses, signatures, etc.)."""
        ...


class NoOpModelBackend:
    """Fallback backend: detects nothing. Used when the real model
    isn't installed/reachable, or anywhere a caller wants the regex
    layer to run on its own (fast, offline, deterministic)."""

    def detect(self, text: str) -> list[Span]:
        return []


# Map the model's label taxonomy onto our pseudonym categories. Phone
# and account_number share a category name with the regex layer's own
# categories (PHONE, ACCOUNT_NO) on purpose: a value either layer
# catches gets numbered from the same counter in the ContextMap, so
# redacted output doesn't end up with two different pseudonym
# families for the same kind of thing.
_LABEL_TO_CATEGORY = {
    "private_person": "PERSON",
    "private_address": "ADDRESS",
    "private_email": "EMAIL",
    "private_phone": "PHONE",
    "private_url": "URL",
    "private_date": "DATE",
    "secret": "SECRET",
    "account_number": "ACCOUNT_NO",
}


class OpenAIPrivacyFilterBackend:
    """Layer 1: wraps the `openai/privacy-filter` token-classification
    model. Loads lazily on first `detect()` call (not at construction)
    so importing this module, or constructing the backend, never
    triggers a model load or a network call by itself — only the
    first real use does.
    """

    MODEL_ID = "openai/privacy-filter"

    # The model supports up to 128K tokens, but CPU self-attention memory
    # grows with sequence length and this workshop runs without a GPU.
    # One real sample document (a multi-page bank statement, ~46K chars)
    # was enough to OOM a CPU process running the full model. Chunk long
    # documents instead of ever handing the model one giant sequence.
    MAX_CHUNK_CHARS = 3000
    CHUNK_OVERLAP_CHARS = 150

    def __init__(self, min_score: float = 0.5):
        self.min_score = min_score
        self._pipeline = None  # lazy

    def _ensure_loaded(self):
        if self._pipeline is not None:
            return
        try:
            from transformers import pipeline
        except ImportError as exc:  # pragma: no cover - exercised via fallback test
            raise ImportError(
                "OpenAIPrivacyFilterBackend requires `torch` and `transformers` "
                "(pip install torch transformers). Install them, or use "
                "NoOpModelBackend / default_model_backend()'s automatic fallback."
            ) from exc

        print(
            f"[cdc_privacy_filter] loading {self.MODEL_ID} (first call only, "
            "cached locally afterwards; no PII leaves this machine)...",
            file=sys.stderr,
        )
        self._pipeline = pipeline(
            task="token-classification",
            model=self.MODEL_ID,
            aggregation_strategy="simple",
        )

    def _chunk_offsets(self, text: str) -> list[int]:
        """Start offsets for MAX_CHUNK_CHARS-sized, overlapping chunks,
        each starting right after a newline where possible (so an
        entity is less likely to be cut mid-word at the boundary)."""
        if len(text) <= self.MAX_CHUNK_CHARS:
            return [0]
        offsets = [0]
        pos = self.MAX_CHUNK_CHARS - self.CHUNK_OVERLAP_CHARS
        while pos < len(text):
            nl = text.rfind("\n", max(0, pos - 200), pos + 1)
            offsets.append(nl + 1 if nl != -1 else pos)
            pos += self.MAX_CHUNK_CHARS - self.CHUNK_OVERLAP_CHARS
        return offsets

    def detect(self, text: str) -> list[Span]:
        if not text.strip():
            return []
        self._ensure_loaded()

        all_spans: list[Span] = []
        seen: set[tuple[int, int, str]] = set()
        offsets = self._chunk_offsets(text)
        for i, start_off in enumerate(offsets):
            end_off = offsets[i + 1] + self.CHUNK_OVERLAP_CHARS if i + 1 < len(offsets) else len(text)
            end_off = min(end_off, start_off + self.MAX_CHUNK_CHARS + self.CHUNK_OVERLAP_CHARS, len(text))
            chunk = text[start_off:end_off]
            if not chunk.strip():
                continue
            for ent in self._pipeline(chunk):
                category = _LABEL_TO_CATEGORY.get(ent["entity_group"])
                if category is None:
                    continue
                start, end = start_off + int(ent["start"]), start_off + int(ent["end"])
                # BPE token offsets can include a leading/trailing space
                # (e.g. " Alice" tokenizes with the space attached). Trim
                # it off the span itself: ContextMap.pseudonymise() strips
                # the value it stores, so an untrimmed span would redact
                # the space out of the text but never restore it on
                # deredact. Trimming here keeps span.text == text[start:end]
                # exactly equal to what gets stored, both ways.
                while start < end and text[start].isspace():
                    start += 1
                while end > start and text[end - 1].isspace():
                    end -= 1
                if start >= end:
                    continue
                key = (start, end, category)
                if key in seen:
                    continue  # same span re-detected via chunk overlap
                seen.add(key)
                all_spans.append(Span(start, end, category, text[start:end]))

        all_spans.sort(key=lambda s: s.start)

        # Chunk overlap can also produce two spans (from adjacent chunks)
        # that genuinely overlap without being byte-identical — e.g. a
        # name that lands differently relative to each chunk's own token
        # boundaries. Greedily keep the first (earliest-starting) span
        # and drop anything that overlaps it, so what's returned is
        # always non-overlapping — filter.py's cursor-based substitution
        # assumes that invariant.
        non_overlapping: list[Span] = []
        claimed_end = -1
        for span in all_spans:
            if span.start < claimed_end:
                continue
            non_overlapping.append(span)
            claimed_end = span.end
        all_spans = non_overlapping

        # The model's "simple" aggregation can still split one logical
        # entity into adjacent pieces (e.g. "Alice" / " Smith", or an
        # email's local part / domain split at a token boundary).
        # Merge same-category entities that are touching or separated
        # only by whitespace/punctuation into a single span so each
        # real-world entity gets exactly one pseudonym.
        merged: list[Span] = []
        for span in all_spans:
            if (
                merged
                and merged[-1].category == span.category
                and span.start - merged[-1].end <= 1
            ):
                prev = merged[-1]
                merged[-1] = Span(prev.start, span.end, prev.category, text[prev.start:span.end])
            else:
                merged.append(span)
        return merged


def default_model_backend(min_score: float = 0.5) -> ModelBackend:
    """Best-effort Layer 1 backend: the real OpenAI Privacy Filter
    model if `torch`/`transformers` are installed and the weights can
    be fetched (cached after the first run), otherwise NoOpModelBackend
    so the server still starts and the regex layer (Layer 2) still
    works on its own. Call sites that want the real model mandatory
    should construct OpenAIPrivacyFilterBackend directly instead."""
    try:
        backend = OpenAIPrivacyFilterBackend(min_score=min_score)
        backend._ensure_loaded()
        return backend
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any load failure degrades gracefully
        print(
            f"[cdc_privacy_filter] Layer-1 model unavailable ({exc!r}); "
            "falling back to regex-only (Layer 2) PII detection.",
            file=sys.stderr,
        )
        return NoOpModelBackend()
