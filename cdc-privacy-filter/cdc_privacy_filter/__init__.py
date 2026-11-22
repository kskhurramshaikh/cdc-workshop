"""CDC Privacy Filter — by Blockverse.

Local, network-free PII redaction/de-redaction for Pakistani financial
documents. Wraps a Pakistani-finance regex layer (IBAN, CNIC, NTN,
account numbers, known fund/broker names) around a pluggable model
backend for free-text names the regex layer can't reliably catch.

Typical usage (inside the MCP server, never inside Claude's context):

    from cdc_privacy_filter import PrivacyFilter, ContextMap

    ctx = ContextMap()                 # one per document / tool call
    pf = PrivacyFilter(context_map=ctx)

    redacted_text = pf.redact(real_pdf_text)   # -> safe to return to Claude
    ...
    final_text = pf.deredact(claude_output)    # -> real values restored,
                                                #    written to the FMS file
"""

from .context_map import ContextMap
from .filter import PrivacyFilter
from .model_backend import (
    ModelBackend,
    NoOpModelBackend,
    OpenAIPrivacyFilterBackend,
    Span,
    default_model_backend,
)

__all__ = [
    "ContextMap",
    "PrivacyFilter",
    "ModelBackend",
    "NoOpModelBackend",
    "OpenAIPrivacyFilterBackend",
    "default_model_backend",
    "Span",
]

__version__ = "0.1.0"
