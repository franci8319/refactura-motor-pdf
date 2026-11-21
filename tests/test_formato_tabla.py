"""Presupuesto en tabla (sin dibujos): cantidad × precio = total por fila, subtotales entre páginas y
totales en columnas. Usa una plantilla privada de ../templates/ con "modo": "tabla" y los PDFs de
../samples/ que reconozca; si no hay, se salta (el repo público no lleva ninguno)."""

from __future__ import annotations

from decimal import Decimal

import pymupdf as fitz
import pytest

import motor
from plantillas_locales import pares, ultimas
from test_servicio import Almacen, MARCA, cliente, generar

PARES = pares(ultimas(lambda p: p.get("modo") == "tabla"))
IDS = [f"pdf{i}" for i in range(1, len(PARES) + 1)]
pytestmark = pytest.mark.skipif(not PARES, reason="sin plantilla en modo tabla o sin PDFs de ese formato")


@pytest.mark.parametrize(("plantilla", "sample"), PARES, ids=IDS)
def test_analizar_tabla(plantilla, sample):
    r = cliente(Almacen(sample.read_bytes())).post(
        "/v1/analizar", json={"pdf_url": "https://almacen.example/tmp/original.pdf?token=firma", "plantilla": plantilla},
        headers={"authorization": f"Bearer {'t' * 40}"})
    d = r.json()
    assert d["estado"] == "ok", d["errores"]
    assert d["lineas"] and d["n_dibujos"] == 0 and all(ln["uds"] == 1 and ln["dibujo"] is None for ln in d["lineas"])
    assert sum(Decimal(ln["coste_total"]) for ln in d["lineas"]) == Decimal(d["totales_fabrica"]["base"])


@pytest.mark.parametrize(("plantilla", "sample"), PARES, ids=IDS)
@pytest.mark.parametrize("modo", ["markup", "margen"])
def test_generar_tabla_cuadra_y_sin_datos_de_fabrica(plantilla, sample, modo):
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=plantilla, pct=30, modo=modo,
                marca={"logo": MARCA["logo"], "datos": MARCA["datos"]}).json()
    assert d["estado"] == "ok", d["errores"]
    assert sum(Decimal(ln["total"]) for ln in d["lineas"]) == Decimal(d["totales"]["base"])
    salida = fitz.open(stream=almacen.subidas[0], filetype="pdf")
    texto = "".join(p.get_text() for p in salida).lower()
    assert not any(n.lower() in texto for n in plantilla["redaccion"]["lista_negra"])
    # Releer la salida con la misma plantilla: filas, subtotales y totales cuadran con lo calculado.
    T = motor.compile_template(plantilla)
    ex = motor.extract(salida, T)
    assert [motor.parse_eur(ln.total) for ln in ex.lineas] == [Decimal(ln["total"]) for ln in d["lineas"]]
    assert motor.validate_subtotales(ex, [motor.parse_eur(ln.total) for ln in ex.lineas]) == []
    assert motor.parse_eur(motor.get_total(ex, "TOTAL").valor) == Decimal(d["totales"]["total"])


@pytest.mark.parametrize(("plantilla", "sample"), PARES, ids=IDS)
def test_tabla_logo_con_cabecera_de_fabrica_es_error_de_marca(plantilla, sample):
    if plantilla["marca"].get("logo_con_fabrica", True):
        pytest.skip("esta plantilla tiene hueco para el logo con la cabecera de fábrica conservada")
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=plantilla, conservar_fabrica=True, marca={"logo": MARCA["logo"]}).json()
    assert d["estado"] == "needs_review" and not almacen.subidas
    assert d["errores"] and all(e.startswith("marca:") for e in d["errores"])


@pytest.mark.parametrize(("plantilla", "sample"), PARES, ids=IDS)
def test_importe_nuevo_igual_a_un_precio_de_fabrica_no_es_fuga(plantilla, sample):
    """Con un 20 %, un precio por metro recalculado coincide con el original de otra línea (106,20)."""
    T = motor.compile_template(plantilla)
    for pct in (Decimal(5), Decimal(20), Decimal("37.5")):
        r = motor.generar(sample.read_bytes(), T, pct)
        assert r.ok, (pct, r.datos["errores"])


@pytest.mark.parametrize(("plantilla", "sample"), PARES, ids=IDS)
def test_precio_de_fabrica_que_se_queda_se_detecta(plantilla, sample):
    """El PDF original (sin redactar) como si fuera la salida: todos sus precios cuentan como fuga."""
    T = motor.compile_template(plantilla)
    pdf = sample.read_bytes()
    ex, _ = motor._analisis(pdf, T)
    calc = motor.calcular(ex, Decimal(20), "markup", None, {})
    texto = "\n".join(p.get_text() for p in fitz.open(stream=pdf, filetype="pdf"))
    err, _ = motor.verify(ex, calc, pdf, T, texto_original=texto)
    assert any("precio de fábrica" in e for e in err)
