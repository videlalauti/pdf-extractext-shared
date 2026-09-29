"""Tests del validador de PDF, migrados desde pdf-extractext-validation."""

import pytest

from shared.domain.constants import MAX_PDF_SIZE_BYTES
from shared.domain.exceptions import InvalidPdfFormatError, PdfTooLargeError
from shared.domain.pdf_validator import PdfValidator

validator = PdfValidator(max_size_bytes=MAX_PDF_SIZE_BYTES)

PDF_HEADER = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"


def test_valid_pdf_is_valid():
    result = validator.validate(PDF_HEADER)
    assert result.is_valid is True
    assert result.error is None


def test_empty_file_is_invalid():
    result = validator.validate(b"")
    assert result.is_valid is False
    assert result.error is not None


def test_non_pdf_bytes_are_invalid():
    result = validator.validate(b"not a pdf")
    assert result.is_valid is False
    assert result.error is not None


def test_file_exceeding_max_size_is_invalid():
    oversized = PDF_HEADER + b"x" * (MAX_PDF_SIZE_BYTES + 1)
    result = validator.validate(oversized)
    assert result.is_valid is False
    assert result.error is not None


def test_file_at_exact_max_size_is_valid():
    exact = b"%PDF-" + b"x" * (MAX_PDF_SIZE_BYTES - 5)
    assert len(exact) == MAX_PDF_SIZE_BYTES
    result = validator.validate(exact)
    assert result.is_valid is True
    assert result.error is None


def test_validate_or_raise_returns_result_for_valid_pdf():
    result = validator.validate_or_raise(PDF_HEADER)
    assert result.is_valid is True


def test_validate_or_raise_raises_too_large_with_sizes():
    oversized = PDF_HEADER + b"x" * (MAX_PDF_SIZE_BYTES + 1)
    with pytest.raises(PdfTooLargeError) as excinfo:
        validator.validate_or_raise(oversized)

    assert excinfo.value.max_size_bytes == MAX_PDF_SIZE_BYTES
    assert excinfo.value.actual_size_bytes == len(oversized)


def test_validate_or_raise_raises_invalid_format():
    with pytest.raises(InvalidPdfFormatError):
        validator.validate_or_raise(b"not a pdf")


def test_validator_requires_an_explicit_limit():
    with pytest.raises(TypeError):
        PdfValidator()
