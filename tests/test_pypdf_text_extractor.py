"""Tests del extractor de texto, migrados desde pdf-extractext-extraction.

Además cubren el arreglo de concurrencia: el parseo de pypdf es CPU-bound y
tiene que correr fuera del hilo del event loop.
"""

import asyncio
import threading

import pytest

from shared.domain import pypdf_text_extractor as extractor_module
from shared.domain.exceptions import PdfExtractionError
from shared.domain.pypdf_text_extractor import PyPdfTextExtractor

extractor = PyPdfTextExtractor()


@pytest.mark.asyncio
async def test_extracts_text_from_pdf(pdf_bytes):
    text = await extractor.extract_text_from_bytes(pdf_bytes)
    assert "Hello World" in text


@pytest.mark.asyncio
async def test_returns_empty_string_for_pdf_without_text(pdf_without_text):
    assert await extractor.extract_text_from_bytes(pdf_without_text) == ""


@pytest.mark.asyncio
async def test_empty_bytes_raise_value_error():
    with pytest.raises(ValueError, match="no pueden estar vacíos"):
        await extractor.extract_text_from_bytes(b"")


@pytest.mark.asyncio
async def test_corrupted_pdf_raises_domain_error():
    with pytest.raises(PdfExtractionError) as excinfo:
        await extractor.extract_text_from_bytes(b"%PDF-1.4\nbasura que no es un pdf\n")

    error = excinfo.value
    assert error.original_error is not None
    assert str(error.original_error) in error.message


@pytest.mark.asyncio
async def test_error_message_is_unified_across_services():
    """El mensaje usa `{error!s}`: la forma única que comparten los 4 servicios.

    Antes de empaquetar, validation interpolaba `{error!s}` y extraction
    `{str(error)}`; cualquier servicio que comparara el texto fallaba.
    """
    with pytest.raises(PdfExtractionError) as excinfo:
        await extractor.extract_text_from_bytes(b"%PDF-1.4\nbasura\n")

    assert excinfo.value.message.startswith("Error al extraer texto con pypdf: ")


@pytest.mark.asyncio
async def test_parsing_runs_on_a_worker_thread(pdf_bytes):
    """El parseo no puede ejecutarse en el hilo del event loop."""
    loop_thread = threading.current_thread().ident
    seen: dict[str, int | None] = {}

    def fake_sync(_pdf_bytes: bytes) -> str:
        seen["thread"] = threading.current_thread().ident
        return "texto"

    original = extractor_module._extract_text_sync
    extractor_module._extract_text_sync = fake_sync
    try:
        assert await extractor.extract_text_from_bytes(pdf_bytes) == "texto"
    finally:
        extractor_module._extract_text_sync = original

    assert seen["thread"] is not None
    assert seen["thread"] != loop_thread


@pytest.mark.asyncio
async def test_event_loop_keeps_serving_while_extracting(pdf_bytes):
    """Con el bug original (parseo síncrono en el async def) el ticker no avanzaba."""
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0)
            ticks += 1

    results = await asyncio.gather(
        extractor.extract_text_from_bytes(pdf_bytes),
        ticker(),
    )

    assert "Hello World" in results[0]
    assert ticks == 5
