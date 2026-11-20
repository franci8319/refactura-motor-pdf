"""Presupuesto en tabla (sin dibujos): cantidad × precio = total por fila, subtotales entre páginas y
totales en columnas. Usa una plantilla privada de ../templates/ con "modo": "tabla" y los PDFs de
../samples/ que reconozca; si no hay, se salta (el repo público no lleva ninguno)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pymupdf as fitz
import pytest

import motor
from test_servicio import Almacen, MARCA, cliente, generar

ROOT = Path(__file__).resolve().parents[2]
PLANTILLAS = [p for p in (json.loads(f.read_text(encoding="utf-8")) for f in sorted((ROOT / "templates").glob("*.json")))
              if p.get("modo") == "tabla"]
PLANTILLA = PLANTILLAS[-1] if PLANTILLAS else None


def reconocidos() -> list[Path]:
    if PLANTILLA is None:
        return []
    T = motor.compile_template(PLANTILLA)
    out = []
    for f in sorted((ROOT / "samples").rglob("*.pdf")):
        with fitz.open(f) as doc:
            if not motor.check_huella(doc, T):
                out.append(f)
    return out


SAMPLES = reconocidos()
IDS = [f"pdf{i}" for i in range(1, len(SAMPLES) + 1)]
pytestmark = pytest.mark.skipif(not SAMPLES, reason="sin plantilla en modo tabla o sin PDFs de ese formato")


@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_analizar_tabla(sample):
    r = cliente(Almacen(sample.read_bytes())).post(
        "/v1/analizar", json={"pdf_url": "https://almacen.example/tmp/original.pdf?token=firma", "plantilla": PLANTILLA},
        headers={"authorization": f"Bearer {'t' * 40}"})
    d = r.json()
    assert d["estado"] == "ok", d["errores"]
    assert d["lineas"] and d["n_dibujos"] == 0 and all(ln["uds"] == 1 and ln["dibujo"] is None for ln in d["lineas"])
    assert sum(Decimal(ln["coste_total"]) for ln in d["lineas"]) == Decimal(d["totales_fabrica"]["base"])


@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
@pytest.mark.parametrize("modo", ["markup", "margen"])
def test_generar_tabla_cuadra_y_sin_datos_de_fabrica(sample, modo):
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=PLANTILLA, pct=30, modo=modo,
                marca={"logo": MARCA["logo"], "datos": MARCA["datos"]}).json()
    assert d["estado"] == "ok", d["errores"]
    assert sum(Decimal(ln["total"]) for ln in d["lineas"]) == Decimal(d["totales"]["base"])
    salida = fitz.open(stream=almacen.subidas[0], filetype="pdf")
    texto = "".join(p.get_text() for p in salida).lower()
    assert not any(n.lower() in texto for n in PLANTILLA["redaccion"]["lista_negra"])
    # Releer la salida con la misma plantilla: filas, subtotales y totales cuadran con lo calculado.
    T = motor.compile_template(PLANTILLA)
    ex = motor.extract(salida, T)
    assert [motor.parse_eur(ln.total) for ln in ex.lineas] == [Decimal(ln["total"]) for ln in d["lineas"]]
    assert motor.validate_subtotales(ex, [motor.parse_eur(ln.total) for ln in ex.lineas]) == []
    assert motor.parse_eur(motor.get_total(ex, "TOTAL").valor) == Decimal(d["totales"]["total"])


@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_tabla_logo_con_cabecera_de_fabrica_es_error_de_marca(sample):
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=PLANTILLA, conservar_fabrica=True, marca={"logo": MARCA["logo"]}).json()
    assert d["estado"] == "needs_review" and not almacen.subidas
    assert d["errores"] and all(e.startswith("marca:") for e in d["errores"])
