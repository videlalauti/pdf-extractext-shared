"""Dominio compartido: reglas de negocio puras del pipeline PDF ExtractExt."""

from shared.domain.constants import MAX_PDF_SIZE_BYTES
from shared.domain.exceptions import (
    DomainError,
    InvalidPdfFormatError,
    PdfExtractionError,
    PdfTooLargeError,
)
from shared.domain.filename import has_pdf_extension
from shared.domain.pdf_validator import PdfValidationResult, PdfValidator

__all__ = [
    "MAX_PDF_SIZE_BYTES",
    "DomainError",
    "InvalidPdfFormatError",
    "PdfExtractionError",
    "PdfTooLargeError",
    "PdfValidationResult",
    "PdfValidator",
    "PyPdfTextExtractor",
    "has_pdf_extension",
]


def __getattr__(name: str):
    """Import diferido de PyPdfTextExtractor: `pypdf` es un extra opcional.

    Importarlo en el `__init__` obligaría a instalar pypdf en validation,
    persistence y summary, que nunca extraen texto.
    """
    if name == "PyPdfTextExtractor":
        from shared.domain.pypdf_text_extractor import PyPdfTextExtractor

        return PyPdfTextExtractor

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
