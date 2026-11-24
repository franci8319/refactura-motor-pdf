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


@pytest.mark.parametrize("pdf", PDFS, ids=IDS)
def test_pie_del_montador_centrado_y_en_letra_normal(pdf):
    """En todas las plantillas, el pie del montador va en letra normal y centrado en la página, sin salirse de
    su zona (que no siempre está centrada)."""
    T = reconocen(pdf)[0]
    if T["marca"] is None:
        pytest.skip("plantilla sin zonas de marca")
    g = motor.generar(pdf.read_bytes(), T, Decimal(30), marca=motor.Marca(pie=MARCA["pie"]))
    assert g.ok, g.datos["errores"]
    zona = motor.pad(T["marca"]["zona_pie"], 1)
    with fitz.open(stream=g.pdf, filetype="pdf") as out:
        for page in out:
            spans = [s for b in page.get_text("dict", clip=zona)["blocks"] for l in b.get("lines", [])
                     for s in l["spans"] if s["text"].strip() == MARCA["pie"][0]]
            assert len(spans) == 1, f"p{page.number + 1}: pie no encontrado"
            s, r = spans[0], fitz.Rect(spans[0]["bbox"])
            assert not s["flags"] & 16 and "Bold" not in s["font"], f"p{page.number + 1}: pie en negrita ({s['font']})"
            assert zona.contains(r)
            centrado = abs((r.x0 + r.x1) / 2 - page.rect.width / 2) <= 1
            assert centrado or r.x0 <= zona.x0 + 1 or r.x1 >= zona.x1 - 1, f"p{page.number + 1}: pie sin centrar"


NUEVO_RECUADRO = ["Comercial: PRUEBA", "MÓVIL: 600 000 000", "prueba@ejemplo.es"]


@pytest.mark.parametrize("pdf", PDFS, ids=IDS)
def test_recuadro_del_destinatario_se_lee_y_se_sustituye(pdf):
    """Donde la plantilla define el recuadro del destinatario: analizar devuelve su texto y, con un texto nuevo,
    en cada página que lo tiene solo queda ese texto; en las demás páginas no cambia nada."""
    T = reconocen(pdf)[0]
    if (T["marca"] or {}).get("recuadro") is None:
        pytest.skip("plantilla sin recuadro del destinatario")
    b = pdf.read_bytes()
    a = motor.analizar(b, T)
    assert a.ok and a.datos["recuadro"], "el recuadro no se lee"
    g = motor.generar(b, T, Decimal(30), marca=motor.Marca(recuadro=NUEVO_RECUADRO))
    assert g.ok, g.datos["errores"]
    with fitz.open(stream=b, filetype="pdf") as orig, fitz.open(stream=g.pdf, filetype="pdf") as out:
        for po, ps in zip(orig, out):
            z = motor.zona_recuadro(po, T)
            if z is None:  # sin recuadro: ni rastro del texto nuevo
                assert motor.zona_recuadro(ps, T) is None
                assert not any(l in ps.get_text() for l in NUEVO_RECUADRO)
                continue
            assert ps.get_text(clip=z).split() == " ".join(NUEVO_RECUADRO).split()
            assert not any(l in ps.get_text(clip=z) for l in a.datos["recuadro"])


@pytest.mark.parametrize("pdf", PDFS, ids=IDS)
def test_sin_texto_nuevo_el_recuadro_queda_como_venia(pdf):
    T = reconocen(pdf)[0]
    if (T["marca"] or {}).get("recuadro") is None:
        pytest.skip("plantilla sin recuadro del destinatario")
    b = pdf.read_bytes()
    g = motor.generar(b, T, Decimal(30), marca=motor.Marca(pie=MARCA["pie"]))
    assert g.ok, g.datos["errores"]
    assert motor.texto_recuadro(g.pdf, T) == motor.texto_recuadro(b, T)


def test_el_ancla_del_recuadro_distingue_mayusculas():
    """La cabecera "Presupuesto:797 /2" de las páginas 2+ no es el ancla "PRESUPUESTO 797/2" del recuadro."""
    T = motor.compile_template({**PLANTILLAS[0], "marca": {**(PLANTILLAS[0].get("marca") or {}),
                                "zona_logo": {"pagina": 1, "rect": [0, 0, 1, 1]}, "zona_datos": {"pagina": 1, "rect": [0, 0, 1, 1]},
                                "zona_pie": {"rect": [0, 0, 1, 1]},
                                "recuadro": {"ancla": r"^PRESUPUESTO \d", "zona": [0, 0, 100, 20]}}})
    doc = fitz.open()
    for texto in ("PRESUPUESTO 797/2", "Presupuesto:797 /2"):
        doc.new_page().insert_text((50, 100), texto)
    assert motor.zona_recuadro(doc[0], T) is not None and motor.zona_recuadro(doc[1], T) is None
