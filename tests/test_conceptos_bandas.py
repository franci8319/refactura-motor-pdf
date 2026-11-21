"""Conceptos como filas de una tabla sin líneas entre filas (las filas con precio llevan una banda gris alterna):
van a continuación del último texto de la tabla, con la letra de la plantilla ("fuente") y la banda que toque.
Usa una plantilla privada de ../templates/ con conceptos.filas = "bandas" y los PDFs de ../samples/ que
reconozca; si no hay, se salta (el repo público no lleva ninguno)."""

from __future__ import annotations

from decimal import Decimal

import pymupdf as fitz
import pytest

import motor
from plantillas_locales import pares, ultimas

PARES = pares(ultimas(lambda p: (p.get("conceptos") or {}).get("filas") == "bandas"))
IDS = [f"pdf{i}" for i in range(1, len(PARES) + 1)]
pytestmark = pytest.mark.skipif(not PARES, reason="sin plantilla con conceptos en bandas o sin PDFs de ese formato")
casos = pytest.mark.parametrize(("plantilla", "sample"), PARES, ids=IDS)

CONCEPTOS = [motor.Concepto("Instalación", Decimal(400), 1, Decimal(400)),
             motor.Concepto("Retirada puerta antigua", Decimal(100), 2, Decimal(50)),
             motor.Concepto("Descuento", Decimal(-50), 1, Decimal(-50))]


def _ref(plantilla: dict, sample) -> tuple[dict, motor.Extraccion, int, float, float]:
    """Plantilla compilada, extracción, página de totales, fondo del último texto de la tabla y borde superior
    del recuadro de totales (en el original)."""
    T = motor.compile_template(plantilla)
    doc = fitz.open(stream=sample.read_bytes(), filetype="pdf")
    ex = motor.extract(doc, T)
    base = motor.get_total(ex, "Base Imponible")
    caja = motor._caja_de(doc[base.pagina], base.rect)
    ultimo = max(w[3] for w in doc[base.pagina].get_text("words") if w[3] < caja[1] - 1 and w[1] > 200)
    return T, ex, base.pagina, ultimo, caja[1]


def _spans_nuevos(pdf: bytes, pagina: int, desde: float, hasta: float) -> list[dict]:
    page = fitz.open(stream=pdf, filetype="pdf")[pagina]
    return [s for b in page.get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"]
            if s["text"].strip() and desde < s["bbox"][1] and s["bbox"][3] < hasta]


def _generar(plantilla: dict, sample, conceptos=CONCEPTOS) -> motor.Resultado:
    return motor.generar(sample.read_bytes(), motor.compile_template(plantilla), Decimal(30), conceptos=conceptos)


@casos
def test_conceptos_como_filas_tras_el_ultimo_texto(plantilla, sample):
    r = _generar(plantilla, sample)
    assert r.ok, r.datos["errores"]
    _, _, pagina, ultimo, tope = _ref(plantilla, sample)
    textos = [s["text"].strip() for s in _spans_nuevos(r.pdf, pagina, ultimo, tope)]
    for t in ("Instalación", "400,00", "Retirada puerta antigua", "2", "50,00", "100,00", "Descuento", "-50,00"):
        assert t in textos, (t, textos)
    assert motor.TITULO_CONCEPTOS not in fitz.open(stream=r.pdf, filetype="pdf")[pagina].get_text()
    assert r.datos["totales"]["base"] == sum(ln["total"] for ln in r.datos["lineas"]) + 450


@casos
def test_filas_e_importes_con_la_letra_de_la_plantilla(plantilla, sample):
    """Filas nuevas e importes reescritos en la letra libre parecida a la de la fábrica, en normal como el original."""
    r = _generar(plantilla, sample)
    assert r.ok, r.datos["errores"]
    _, ex, pagina, ultimo, tope = _ref(plantilla, sample)
    nuevos = _spans_nuevos(r.pdf, pagina, ultimo, tope)
    assert nuevos and all("Comic" in s["font"] and "Bold" not in s["font"] for s in nuevos), {s["font"] for s in nuevos}
    page = fitz.open(stream=r.pdf, filetype="pdf")[pagina]
    totales = [s for b in page.get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"]
               if any(fitz.Rect(s["bbox"]).intersects(fitz.Rect(l.rect_total)) for l in ex.lineas if l.pagina == pagina)]
    assert totales and all("Comic" in s["font"] for s in totales), {s["font"] for s in totales}


@casos
def test_bandas_alternas_como_las_filas_de_la_tabla(plantilla, sample):
    r = _generar(plantilla, sample)
    assert r.ok, r.datos["errores"]
    T, ex, pagina, ultimo, tope = _ref(plantilla, sample)
    original = fitz.open(stream=sample.read_bytes(), filetype="pdf")[pagina]

    def con_banda(page: fitz.Page, y0: float, y1: float) -> bool:
        return any(d.get("fill") and d["rect"].height < 15 and d["rect"].y0 < y1 and d["rect"].y1 > y0
                   for d in page.get_drawings())
    ultima = [ln for ln in ex.lineas if ln.pagina == pagina][-1]
    sombreada = con_banda(original, ultima.rect_total[1], ultima.rect_total[3])
    salida = fitz.open(stream=r.pdf, filetype="pdf")[pagina]
    filas = sorted({round(s["bbox"][1], 1) for s in _spans_nuevos(r.pdf, pagina, ultimo, tope)
                    if s["text"].strip() in ("400,00", "100,00", "-50,00") and s["bbox"][0] > 480})
    assert len(filas) == 3
    assert [con_banda(salida, y, y + 8) for y in filas] == [not sombreada, sombreada, not sombreada]


@casos
def test_precio_alineado_como_en_la_tabla(plantilla, sample):
    r = _generar(plantilla, sample)
    assert r.ok, r.datos["errores"]
    _, ex, pagina, ultimo, tope = _ref(plantilla, sample)
    derechas = {round(ln.rect_importe[2], 1) for ln in ex.lineas}
    assert max(derechas) - min(derechas) <= 1  # en este formato el precio va alineado a la derecha
    precios = [s for s in _spans_nuevos(r.pdf, pagina, ultimo, tope) if s["text"].strip() in ("400,00", "50,00")
               and 400 < s["bbox"][0] < 480]
    assert len(precios) == 2 and all(abs(s["bbox"][2] - max(derechas)) <= 1 for s in precios)


@casos
def test_filas_sin_hueco_es_error(plantilla, sample):
    T, _, pagina, ultimo, _ = _ref(plantilla, sample)
    doc = fitz.open(stream=sample.read_bytes(), filetype="pdf")
    doc[pagina].draw_rect(fitz.Rect(60, ultimo + 8, 200, ultimo + 18), color=(0, 0, 0))
    r = motor.generar(doc.tobytes(), T, Decimal(30), conceptos=CONCEPTOS[:1])
    assert not r.ok and r.datos["errores"]
    assert all(e.startswith("conceptos:") for e in r.datos["errores"]), r.datos["errores"]


@casos
def test_diez_conceptos_largos_caben_o_error(plantilla, sample):
    texto = "RETIRADA Y TRANSPORTE A VERTEDERO DE PUERTA ANTIGUA"
    conceptos = [motor.Concepto(f"{i} {texto}", Decimal(10), 1, Decimal(10)) for i in range(10)]
    r = _generar(plantilla, sample, conceptos)
    if not r.ok:
        assert all(e.startswith("conceptos:") for e in r.datos["errores"]), r.datos["errores"]
        return
    _, _, pagina, ultimo, tope = _ref(plantilla, sample)
    page = fitz.open(stream=r.pdf, filetype="pdf")[pagina]
    nuevos = [s for b in page.get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"]
              if "RETIRADA" in s["text"] or "VERTEDERO" in s["text"]]
    assert nuevos and all(s["bbox"][3] <= tope for s in nuevos)
