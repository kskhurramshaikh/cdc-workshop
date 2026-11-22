"""
Thin PDF-text extraction helper. Prefers the system `pdftotext` (poppler)
for layout-preserving extraction (what the regex patterns were tuned
against); falls back to pypdf if poppler isn't installed on the
workshop laptop.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def extract_text(pdf_path: Path) -> str:
    if shutil.which("pdftotext"):
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout

    # Fallback: pypdf (pure Python, no system dependency)
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)
