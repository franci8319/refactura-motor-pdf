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
