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
from .patterns import ACCOUNT_NO_RE, NAME_PATTERNS, STRUCTURED_PATTERNS


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
                if category == "ACCOUNT_NO":
                    # Only pseudonymise the captured digit/dash run, not
                    # the "Account No:" label itself.
                    start, end = m.start(1), m.end(1)
                else:
                    start, end = m.start(), m.end()
                if overlaps(start, end):
                    continue
                spans.append(Span(start, end, category, text[start:end]))
                claimed.append((start, end))

        # 2. Known fund / broker names
        for category, pattern in NAME_PATTERNS:
            for m in pattern.finditer(text):
                start, end = m.start(), m.end()
                if overlaps(start, end):
                    continue
                spans.append(Span(start, end, category, text[start:end]))
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

    def redact(self, text: str) -> str:
        """Return text with every detected PII span replaced by a stable
        pseudonym token, e.g. [IBAN_001], [NTN_002], [BROKER_003]."""
        spans = self._collect_spans(text)
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
