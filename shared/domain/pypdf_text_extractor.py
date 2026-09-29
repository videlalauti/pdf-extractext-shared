"""Extracción de texto con pypdf, compartida entre los microservicios."""

import asyncio
from io import BytesIO

from pypdf import PdfReader

from shared.domain.exceptions import PdfExtractionError


def _extract_text_sync(pdf_bytes: bytes) -> str:
    """Parseo de pypdf: trabajo CPU-bound y bloqueante.

    Vive fuera del `async def` a propósito; el llamador lo despacha con
    `asyncio.to_thread` para no bloquear el event loop del servicio.
    """
    pdf_stream = BytesIO(pdf_bytes)
    reader = PdfReader(pdf_stream)

    extracted_texts = []
    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            extracted_texts.append(page_text)

    return "\n".join(extracted_texts)


class PyPdfTextExtractor:
    """Procesa el PDF en memoria con BytesIO, sin crear archivos temporales."""

    async def extract_text_from_bytes(self, pdf_bytes: bytes) -> str:
        """Extrae texto de todas las páginas, o vacío si no hay texto extraíble."""
        if not pdf_bytes:
            raise ValueError("Los bytes del PDF no pueden estar vacíos")

        try:
            # PdfReader parsea tensos de KB de xref de forma síncrona: hacerlo en el
            # hilo del event loop congelaría todas las requests concurrentes.
            return await asyncio.to_thread(_extract_text_sync, pdf_bytes)
        except Exception as error:
            raise PdfExtractionError(
                message=f"Error al extraer texto con pypdf: {error!s}",
                original_error=error,
            ) from error
