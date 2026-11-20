"""Tests unitarios del motor sin PDFs reales: totales opcionales, líneas extra y pie por defecto."""

from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import motor  # noqa: E402

EJEMPLO = json.loads(Path(__file__).with_name("plantilla_ejemplo.json").read_text(encoding="utf-8"))


def linea(tipo: str, importe: str, uds: int = 1, total: str | None = None) -> motor.Linea:
    r = (0, 0, 1, 1)
    return motor.Linea(0, tipo, "x", uds, importe, total or importe, r, r, (0, 0), (0, 0), 9, 0,
                       r if tipo == "ventana" else None)


def total(label: str, valor: str, iva: str | None = None) -> motor.Total:
    return motor.Total(0, label, valor, iva, (0, 0, 1, 1), (0, 0), 9, 0)


def extraccion(con_total: bool) -> motor.Extraccion:
    ex = motor.Extraccion(lineas=[linea("ventana", "100,00"), linea("extra", "25,00", 2, "50,00")], n_dibujos=1)
    ex.totales = [total("Base Imponible", "150,00"), total("IVA", "31,50", "21"), total("TOTAL", "181,50")]
    if con_total:
        ex.totales.append(total("Total", "150,00"))
    return ex


@pytest.mark.parametrize("con_total", [True, False])
def test_total_sin_iva_es_opcional(con_total):
    assert motor.validate(extraccion(con_total)) == []


def test_total_distinto_de_base_es_error():
    ex = extraccion(True)
    ex.totales[-1] = total("Total", "149,00")
    assert "Total ≠ Base Imponible" in motor.validate(ex)


def test_linea_extra_lleva_pct_y_no_cuenta_como_ventana():
    ex = extraccion(False)
    calc = motor.calcular(ex, Decimal(30), "markup", None, {})
    assert [l["total"] for l in calc["lineas"]] == [Decimal("130.00"), Decimal("65.00")]
    assert calc["lineas"][1]["pct"] == Decimal(30)


def test_linea_otro_sigue_siendo_needs_review():
    ex = extraccion(False)
    ex.lineas[1].tipo = "otro"
    assert any("línea no reconocida" in e for e in motor.validate(ex))


@pytest.mark.parametrize("campo,valor", [("lineas_sin_pos", "ventana"), ("pie_por_defecto", "logo")])
def test_plantilla_con_opcion_desconocida(campo, valor):
    raw = copy.deepcopy(EJEMPLO)
    if campo == "lineas_sin_pos":
        raw[campo] = valor
    else:
        raw["marca"][campo] = valor
    with pytest.raises(ValueError):
        motor.compile_template(raw)


def plantilla(pie_por_defecto: str | None) -> dict:
    raw = copy.deepcopy(EJEMPLO)
    if pie_por_defecto:
        raw["marca"]["pie_por_defecto"] = pie_por_defecto
    return motor.compile_template(raw)


DATOS = ["Ventanas Ejemplo S.L.", "NIF B12345678"]


def test_pie_por_defecto_usa_los_datos():
    m = motor.marca_efectiva(motor.Marca(datos=DATOS), plantilla("datos"), conservar_fabrica=False)
    assert m.pie == ["Ventanas Ejemplo S.L. · NIF B12345678"] and m.datos == DATOS


@pytest.mark.parametrize("caso", ["sin_opcion", "pie_propio", "conservar"])
def test_pie_por_defecto_no_aplica(caso):
    marca = motor.Marca(datos=DATOS, pie=["Mi pie"] if caso == "pie_propio" else [])
    T = plantilla(None if caso == "sin_opcion" else "datos")
    m = motor.marca_efectiva(marca, T, conservar_fabrica=caso == "conservar")
    assert m.pie == marca.pie


def test_pie_de_datos_largo_en_dos_lineas():
    datos = ["Una razón social bastante larga S.L.", "NIF B12345678", "Calle con nombre muy largo 123, 18000 Granada",
             "Tel. 958 000 000", "correo-largo@empresa-de-ejemplo.es", "www.empresa-de-ejemplo.es"]
    zona = (60, 792, 300, 812)
    lineas = motor.pie_de_datos(datos, zona)
    assert len(lineas) == 2 and " · ".join(lineas) == " · ".join(datos)
