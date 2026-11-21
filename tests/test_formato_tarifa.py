"""Tabla con tarifa, descuento de fábrica e importe neto: cantidad (metraje o uds), columna de descuento que
se vacía, % de IVA en el título de su columna, TOTAL con «€» y otros conceptos encima del recuadro de
totales, que no se mueve. Usa una plantilla privada de ../templates/ con "tabla.col_dto" y los PDFs de
../samples/ que reconozca; si no hay, se salta (el repo público no lleva ninguno)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pymupdf as fitz
import pytest

import motor
from plantillas_locales import pares, ultimas
from test_servicio import Almacen, MARCA, cliente, generar

PARES = pares(ultimas(lambda p: "col_dto" in p.get("tabla", {})))
IDS = [f"pdf{i}" for i in range(1, len(PARES) + 1)]
pytestmark = pytest.mark.skipif(not PARES, reason="sin plantilla con columna de descuento o sin PDFs de ese formato")
casos = pytest.mark.parametrize(("plantilla", "sample"), PARES, ids=IDS)


def _generar(plantilla: dict, sample, **extra) -> tuple[dict, fitz.Document | None]:
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=plantilla, **extra).json()
    return d, fitz.open(stream=almacen.subidas[0], filetype="pdf") if almacen.subidas else None


@casos
def test_lineas_con_cantidad_y_descuento(plantilla, sample):
    T = motor.compile_template(plantilla)
    ex, err = motor._analisis(sample.read_bytes(), T)
    assert err == []
    assert ex.lineas and all(ln.tipo == "extra" and ln.cantidad > 0 for ln in ex.lineas)
    assert any(ln.dto for ln in ex.lineas) and ex.vaciar
    suma = sum((motor.parse_eur(ln.total) for ln in ex.lineas), Decimal(0))
    assert suma == motor.parse_eur(motor.get_total(ex, "Base Imponible").valor)


@casos
@pytest.mark.parametrize("modo", ["markup", "margen"])
def test_generar_sin_descuento_ni_datos_de_fabrica(plantilla, sample, modo):
    d, salida = _generar(plantilla, sample, pct=30, modo=modo, marca=MARCA)
    assert d["estado"] == "ok", d["errores"]
    texto = "".join(p.get_text() for p in salida).lower()
    assert not any(n.lower() in texto for n in plantilla["redaccion"]["lista_negra"])
    ex = motor.extract(salida, motor.compile_template(plantilla))
    assert ex.vaciar == [] and ex.errores == []
    assert [motor.parse_eur(ln.total) for ln in ex.lineas] == [Decimal(ln["total"]) for ln in d["lineas"]]
    for ln in ex.lineas:
        assert ln.dto == 0
        assert abs(ln.cantidad * motor.parse_eur(ln.precio) - motor.parse_eur(ln.total)) <= \
            ln.cantidad * Decimal("0.005") + Decimal("0.01")


@casos
def test_iva_elegido_se_reescribe_en_el_titulo(plantilla, sample):
    d, salida = _generar(plantilla, sample, pct=30, iva_pct="10")
    assert d["estado"] == "ok", d["errores"]
    iva = motor.get_total(motor.extract(salida, motor.compile_template(plantilla)), "IVA")
    assert motor.parse_pct(iva.iva_pct) == 10
    base = Decimal(d["totales"]["base"])
    assert Decimal(d["totales"]["total"]) == base + (base / 10).quantize(Decimal("0.01"), ROUND_HALF_UP)


@casos
def test_importes_largos_se_releen(plantilla, sample):
    """Los totales van centrados: con un 150 % cambian de ancho y se tienen que seguir releyendo."""
    d, _ = _generar(plantilla, sample, pct=150)
    assert d["estado"] == "ok", d["errores"]


@casos
def test_conceptos_encima_sin_mover_los_totales(plantilla, sample):
    conceptos = [{"texto": "Instalación", "importe": "400"}, {"texto": "Descuento", "importe": "-50"}]
    d, salida = _generar(plantilla, sample, pct=30, conceptos=conceptos)
    assert d["estado"] == "ok", d["errores"]
    T = motor.compile_template(plantilla)
    original = motor.extract(fitz.open(stream=sample.read_bytes(), filetype="pdf"), T)
    nuevo = motor.extract(salida, T)
    for t in original.totales:
        t2 = motor.get_total(nuevo, t.label)
        assert t2 is not None and abs(t2.origin[1] - t.origin[1]) <= 0.5 and abs(t2.rect[2] - t.rect[2]) <= 1
    pagina = motor.get_total(original, "Base Imponible").pagina
    assert motor.TITULO_CONCEPTOS in salida[pagina].get_text()
    assert Decimal(d["totales"]["base"]) == sum(Decimal(ln["total"]) for ln in d["lineas"]) + 350


@casos
def test_concepto_largo_no_cuenta_como_descuento(plantilla, sample):
    """Un concepto largo llega a la altura de la columna de descuento: no es un resto del descuento."""
    T = motor.compile_template(plantilla)
    ex, _ = motor._analisis(sample.read_bytes(), T)
    size = motor.get_total(ex, "Base Imponible").size
    texto = "Instalación"
    while motor.FONT.text_length(texto + " completa", fontsize=size) < 445:
        texto += " completa"
    r = motor.generar(sample.read_bytes(), T, Decimal(30), conceptos=[motor.Concepto(texto, Decimal(400))])
    assert r.ok, r.datos["errores"]


@casos
def test_conceptos_sin_hueco_es_error(plantilla, sample):
    """Algo en el hueco encima de los totales: los conceptos no se montan encima."""
    T = motor.compile_template(plantilla)
    ex, _ = motor._analisis(sample.read_bytes(), T)
    base = motor.get_total(ex, "Base Imponible")
    doc = fitz.open(stream=sample.read_bytes(), filetype="pdf")
    y = base.rect[1] - 40
    doc[base.pagina].draw_rect(fitz.Rect(60, y, 200, y + 10), color=(0, 0, 0))
    r = motor.generar(doc.tobytes(), T, Decimal(30), conceptos=[motor.Concepto("Instalación", Decimal(400))])
    assert not r.ok and r.datos["errores"]
    assert all(e.startswith("conceptos:") for e in r.datos["errores"]), r.datos["errores"]


@casos
def test_conservando_logo_de_fabrica_el_del_montador_va_arriba_a_la_derecha(plantilla, sample):
    d, salida = _generar(plantilla, sample, pct=30, conservar_fabrica=True, marca={"logo": MARCA["logo"]})
    assert d["estado"] == "ok", d["errores"]
    zona = motor.pad(tuple(plantilla["marca"]["zona_logo_conservando"]["rect"]), 1)
    imgs = [fitz.Rect(i["bbox"]) for i in salida[0].get_image_info()]
    assert len(imgs) == 2 and sum(1 for r in imgs if zona.contains(r)) == 1
