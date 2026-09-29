"""Fixtures compartidas por los tests del paquete."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _build_pdf(content: bytes) -> bytes:
    """Arma un PDF de una página con la tabla xref y el startxref correctos.

    pypdf no tolera un xref desalineado, así que los offsets se calculan sobre
    los bytes finales en vez de escribirlos a mano.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []

    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref_offset}\n%%EOF\n".encode()

    return bytes(out)


TEXT_STREAM = b"BT /F1 12 Tf 100 700 Td (Hello World) Tj ET"

PDF_WITH_TEXT = _build_pdf(TEXT_STREAM)
PDF_WITHOUT_TEXT = _build_pdf(b"")


@pytest.fixture
def pdf_bytes() -> bytes:
    """PDF de una página con texto extraíble."""
    return PDF_WITH_TEXT


@pytest.fixture
def pdf_without_text() -> bytes:
    """PDF de una página cuyo stream de contenido está vacío."""
    return PDF_WITHOUT_TEXT
