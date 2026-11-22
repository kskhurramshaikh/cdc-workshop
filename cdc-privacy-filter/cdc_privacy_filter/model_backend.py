"""
Pluggable model backend hook.

The regex layer (patterns.py) catches structured Pakistani financial
identifiers deterministically — IBANs, NTNs, CNICs, account numbers,
known fund/broker names. It will NOT catch free-text personal names,
addresses, or signatures that appear inconsistently across documents
(e.g. "BILAL RASHEED" signing a broker bill).

For production, wrap a local NER/PII model here (the workshop notes
reference the OpenAI Privacy Filter — a small, local, Apache-2.0 model —
as the intended backend). It must run entirely on CDC's network and
return character spans; it never needs network egress.

This module ships a NoOpModelBackend so the regex layer is fully
functional and testable on its own during the workshop build-out. Swap
in a real backend by implementing `detect()` the same way.
"""

from __future__ import annotations

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
    """Default backend: detects nothing. The regex layer alone covers the
    structured identifiers this project cares about. Swap this out for a
    real local model (e.g. a Presidio/GLiNER-style NER pipeline, or the
    OpenAI Privacy Filter model referenced in the project brief) once it
    is installed on CDC's network — same `detect()` signature."""

    def detect(self, text: str) -> list[Span]:
        return []
