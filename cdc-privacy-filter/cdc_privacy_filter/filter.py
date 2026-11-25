"""
PrivacyFilter — "CDC Privacy Filter by Blockverse"

Redacts Pakistani financial PII out of document text before it is ever
returned to Claude, and de-redacts Claude's (pseudonym-laden) output back
to real values before it is written to a final FMS file. All of this
runs locally; nothing here makes a network call.
"""

from __future__ import annotations

from .context_map import ContextMap
from .model_backend import ModelBackend, NoOpModelBackend, Span
from .patterns import (
    ACCOUNT_NO_RE,
    NAME_CANON,
    NAME_PATTERNS,
    STRUCTURED_PATTERNS,
    find_contextual_person_spans,
)


class PrivacyFilter:
    def __init__(
        self,
        context_map: ContextMap | None = None,
        model_backend: ModelBackend | None = None,
    ):
        self.context_map = context_map if context_map is not None else ContextMap()
        self.model_backend = model_backend if model_backend is not None else NoOpModelBackend()

    # -- redaction ----------------------------------------------------

    def _collect_spans(self, text: str) -> list[Span]:
        spans: list[Span] = []
        claimed: list[tuple[int, int]] = []

        def overlaps(a_start: int, a_end: int) -> bool:
            return any(a_start < c_end and c_start < a_end for c_start, c_end in claimed)

        # 1. Structured identifiers, in priority order (IBAN before the
        #    looser NTN/CNIC bare-digit fallbacks, etc.)
        for category, pattern in STRUCTURED_PATTERNS:
            for m in pattern.finditer(text):
                if m.lastindex is not None:
                    # Pattern has a capturing group — e.g. ACCOUNT_NO_RE's
                    # "Account No: 123" / bare-fallback alternation, or
                    # PHONE_LABELED_RE's "Phone #: 123-4" — so only
                    # pseudonymise the captured value, not the label text
                    # itself. lastindex is whichever group actually
                    # participated in the match (the right one when the
                    # pattern is an alternation of several groups).
                    g = m.lastindex
                    start, end = m.start(g), m.end(g)
                else:
                    start, end = m.start(), m.end()
                if overlaps(start, end):
                    continue
                spans.append(Span(start, end, category, text[start:end]))
                claimed.append((start, end))

        # 2. Known fund / broker names. Canonicalise the matched text to
        #    its one fixed spelling from KNOWN_FUND_NAMES/KNOWN_BROKER_NAMES
        #    before it reaches the ContextMap: the match itself is already
        #    case-insensitive, but without this the *pseudonym* would
        #    depend on which casing variant appeared in this particular
        #    document, splitting one real broker/fund across two tokens.
        #    This only changes what gets pseudonymised, not the span's
        #    start/end — the original text is still replaced in place.
        for category, pattern in NAME_PATTERNS:
            for m in pattern.finditer(text):
                start, end = m.start(), m.end()
                if overlaps(start, end):
                    continue
                matched = text[start:end]
                canonical = NAME_CANON.get(category, {}).get(matched.lower(), matched)
                spans.append(Span(start, end, category, canonical))
                claimed.append((start, end))

        # 2.5. Context-anchored person names (Layer 2b) — short/common
        #      Pakistani names (e.g. "Ahmed Muhammad") that Layer 1 (the
        #      model) misses ENTIRELY when they sit right after specific
        #      institutional phrases ("PAY TO THE ORDER OF", "CDC TRUSTEE
        #      ... FUND ACCOUNT", etc.) — see patterns.py's
        #      find_contextual_person_spans() docstring for the full
        #      root-cause writeup from the live UC2 workshop. Runs after
        #      known fund/broker names (step 2) so a legitimate fund or
        #      broker name sitting in one of these same slots is claimed
        #      there first and never reaches here as a false PERSON tag;
        #      runs before the model (step 3) so this deterministic catch
        #      always wins on overlap, same "Layer 2 wins" convention as
        #      every other pattern in this file.
        for start, end, candidate in find_contextual_person_spans(text):
            if overlaps(start, end):
                continue
            spans.append(Span(start, end, "PERSON", candidate))
            claimed.append((start, end))

        # 3. Model backend (free-text names/addresses the regex layer
        #    can't reliably catch) — lowest priority, skip overlaps.
        for span in self.model_backend.detect(text):
            if overlaps(span.start, span.end):
                continue
            spans.append(span)
            claimed.append((span.start, span.end))

        spans.sort(key=lambda s: s.start)
        return spans

    def collect_spans(self, text: str) -> list[Span]:
        """Public entry point for Layer 1 + Layer 2 PII span detection,
        with no ContextMap side effects — used by the Render-hosted
        detection service, which has no business holding real-value
        pseudonym mappings. The caller (the local MCP server, which does
        own a ContextMap) turns these spans into pseudonym substitutions
        itself via its own context_map.pseudonymise()."""
        return self._collect_spans(text)

    def apply_spans(self, text: str, spans: list[Span]) -> str:
        """Substitute pseudonyms for already-detected spans, via this
        filter's own ContextMap. Split out from redact() so spans found
        remotely (e.g. by the Render-hosted detection service, which
        holds no ContextMap of its own) can be applied locally without
        ever sending pseudonym mappings off this machine. Spans must be
        pre-sorted by start (as both _collect_spans() and the remote
        /detect endpoint already return them)."""
        if not spans:
            return text

        out: list[str] = []
        cursor = 0
        for span in spans:
            out.append(text[cursor:span.start])
            pseudo = self.context_map.pseudonymise(span.category, span.text)
            out.append(pseudo)
            cursor = span.end
        out.append(text[cursor:])
        return "".join(out)

    def redact(self, text: str) -> str:
        """Return text with every detected PII span replaced by a stable
        pseudonym token, e.g. [IBAN_001], [NTN_002], [BROKER_003]."""
        return self.apply_spans(text, self._collect_spans(text))

    # -- de-redaction ---------------------------------------------------

    def deredact(self, text: str) -> str:
        """Restore real values for every pseudonym token this filter's
        ContextMap knows about. Unknown tokens (shouldn't happen if the
        same ContextMap produced them) are left untouched."""
        result = text
        for pseudo in self.context_map.all_pseudonyms():
            real = self.context_map.resolve(pseudo)
            if real is not None:
                result = result.replace(pseudo, real)
        return result
