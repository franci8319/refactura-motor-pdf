"""Regresión con todos los formatos reales: cada PDF de ../samples/ lo reconoce una sola plantilla de
../templates/ (la última versión de cada una) y analiza y genera bien. Una plantilla nueva no puede romper
ni confundirse con las anteriores. Sin plantillas o sin PDFs, se salta (el repo público no lleva ninguno)."""

from __future__ import annotations

from decimal import Decimal

import pymupdf as fitz
import pytest

import motor
from plantillas_locales import pdfs, ultimas
from test_servicio import MARCA, png

PLANTILLAS = ultimas(lambda p: True)
PDFS = pdfs()
IDS = [f"pdf{i}" for i in range(1, len(PDFS) + 1)]
pytestmark = pytest.mark.skipif(not (PLANTILLAS and PDFS), reason="sin plantillas o sin PDFs reales")


def reconocen(pdf) -> list[dict]:
    with fitz.open(pdf) as doc:
        return [T for T in map(motor.compile_template, PLANTILLAS) if not motor.check_huella(doc, T)]


@pytest.mark.parametrize("pdf", PDFS, ids=IDS)
def test_cada_pdf_lo_reconoce_una_sola_plantilla(pdf):
    assert len([T["id"] for T in reconocen(pdf)]) == 1


@pytest.mark.parametrize("pdf", PDFS, ids=IDS)
def test_cada_pdf_analiza_y_genera(pdf):
    encontradas = reconocen(pdf)
    assert encontradas, "ninguna plantilla reconoce el PDF"
    T = encontradas[0]
    a = motor.analizar(pdf.read_bytes(), T)
    assert a.ok, a.datos["errores"]
    marca = motor.Marca(logo=png(), datos=MARCA["datos"], pie=MARCA["pie"])
    g = motor.generar(pdf.read_bytes(), T, Decimal(30), marca=marca)
    assert g.ok, g.datos["errores"]


def _negrita(doc: fitz.Document, pagina: int, rect: tuple) -> bool:
    """¿Va en negrita el texto de esa casilla? (el span que más se solapa con ella)"""
    r = fitz.Rect(rect)
    s = max(motor.page_spans(doc[pagina]), key=lambda s: (fitz.Rect(s.bbox) & r).get_area())
    assert (fitz.Rect(s.bbox) & r).get_area() > 0, "no hay texto en la casilla"
    return s.bold


@pytest.mark.parametrize("pdf", PDFS, ids=IDS)
def test_precios_conservan_el_grosor(pdf):
    """Con "grosor_original", cada importe que reescribe Refactura va en negrita o en normal como el original al
    que sustituye; sin ella, en negrita."""
    T = reconocen(pdf)[0]
    g = motor.generar(pdf.read_bytes(), T, Decimal(30))
    assert g.ok, g.datos["errores"]
    orig, out = fitz.open(pdf), fitz.open(stream=g.pdf, filetype="pdf")
    ex_o, ex_s = motor.extract(orig, T), motor.extract(out, T)
    pares = [(a.pagina, a.rect_importe, b.rect_importe) for a, b in
             zip((l for l in ex_o.lineas if l.tipo != "descuento"), ex_s.lineas)]
    pares += [(a.pagina, a.rect_total, b.rect_total) for a, b in
              zip((l for l in ex_o.lineas if l.tipo != "descuento"), ex_s.lineas)]
    pares += [(t.pagina, t.rect, motor.get_total(ex_s, t.label).rect) for t in ex_o.totales]
    pares += [(a.pagina, a.rect, b.rect) for a, b in zip(ex_o.subtotales, ex_s.subtotales)]
    # Sin "grosor_original" en la plantilla, todo en negrita como siempre.
    distintos = [(p + 1, tuple(round(v) for v in ro)) for p, ro, rs in pares
                 if (_negrita(orig, p, ro) if T["grosor_original"] else True) != _negrita(out, p, rs)]
    assert not distintos, f"grosor distinto al original en {distintos[:5]} (de {len(distintos)})"
