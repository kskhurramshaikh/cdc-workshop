"""
ContextMap — holds the real-value <-> pseudonym mapping for one redaction
session (one document, one tool call, or one whole workshop run — caller's
choice). This is the only place real PII values live in memory; it is never
sent to Claude. The MCP server keeps it server-side and uses it to
de-redact Claude's output before writing the final FMS file.
"""

from dataclasses import dataclass, field


@dataclass
class ContextMap:
    """Bidirectional map between real values and stable pseudonyms.

    Pseudonyms are deterministic within a single ContextMap instance: the
    same real value always gets the same pseudonym, so Claude can reason
    about relationships (e.g. "this bill's NTN matches that liability
    export row") without ever seeing the real number.
    """

    _real_to_pseudo: dict[str, str] = field(default_factory=dict)
    _pseudo_to_real: dict[str, str] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    def pseudonymise(self, category: str, real_value: str) -> str:
        """Return the pseudonym for real_value under category, creating a
        new one (incrementing that category's counter) on first sight."""
        key = f"{category}:{real_value.strip()}"
        if key in self._real_to_pseudo:
            return self._real_to_pseudo[key]

        n = self._counters.get(category, 0) + 1
        self._counters[category] = n
        pseudo = f"[{category}_{n:03d}]"

        self._real_to_pseudo[key] = pseudo
        self._pseudo_to_real[pseudo] = real_value.strip()
        return pseudo

    def resolve(self, pseudonym: str) -> str | None:
        """Real value for a pseudonym token, or None if unknown."""
        return self._pseudo_to_real.get(pseudonym)

    def all_pseudonyms(self) -> list[str]:
        return list(self._pseudo_to_real.keys())

    def as_dict(self) -> dict[str, str]:
        """pseudonym -> real_value, for de-redaction / audit export."""
        return dict(self._pseudo_to_real)

    def __len__(self) -> int:
        return len(self._pseudo_to_real)
