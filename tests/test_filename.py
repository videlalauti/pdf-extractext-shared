"""Tests del validador de nombre de archivo."""

import pytest

from shared.domain.filename import has_pdf_extension


@pytest.mark.parametrize(
    "filename",
    [
        "documento.pdf",
        "reporte.pdf",
        "documento.PDF",
        "DoCuMeNtO.Pdf",
        "con espacios y ñ.pdf",
        "2026-01-01.pdf",
    ],
)
def test_accepts_pdf_extension(filename):
    assert has_pdf_extension(filename) is True


@pytest.mark.parametrize(
    "filename",
    [
        "documento",
        "documento.txt",
        "documento.pdf.txt",
        "pdf",
        ".pdf.bak",
        "documento.pd",
    ],
)
def test_rejects_non_pdf_extension(filename):
    assert has_pdf_extension(filename) is False


@pytest.mark.parametrize("filename", [None, ""])
def test_rejects_empty_filename(filename):
    assert has_pdf_extension(filename) is False
