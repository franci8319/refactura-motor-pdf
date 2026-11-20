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


def test_conceptos_suman_a_la_base_sin_pct():
    ex = extraccion(False)
    conceptos = [motor.Concepto("Instalación", Decimal("400.00")), motor.Concepto("Portes", Decimal("50.00"))]
    calc = motor.calcular(ex, Decimal(30), "markup", None, {}, conceptos)
    assert calc["extra"] == Decimal("450.00")
    assert calc["base"] == Decimal("130.00") + Decimal("65.00") + Decimal("450.00")
    assert calc["conceptos"][0] == {"texto": "Instalación", "importe": Decimal("400.00")}


def test_montaje_con_linea_en_el_pdf_se_escribe_en_ella():
    ex = extraccion(False)
    ex.lineas.append(linea("montaje", "300,00"))
    calc = motor.calcular(ex, Decimal(30), "markup", Decimal("400.00"), {})
    assert calc["extra"] == 0 and calc["lineas"][-1]["importe"] == Decimal("400.00")


def test_montaje_sin_linea_ni_conceptos_es_error():
    T = motor.compile_template(EJEMPLO)
    assert motor.validate_params(extraccion(False), Decimal(400), {}, T)


def test_conceptos_en_plantilla_sin_conceptos_es_error_de_conceptos():
    T = motor.compile_template(EJEMPLO)
    bc, err = motor.plan_conceptos(b"", extraccion(False), T, [motor.Concepto("Portes", Decimal(1))])
    assert bc is None and err and all(e.startswith("conceptos:") for e in err)


def test_envolver_parte_por_palabras_y_mantiene_lineas_vacias():
    lineas = motor.envolver("uno dos tres cuatro\n\nfin", motor.FONT_REG.text_length("uno dos", fontsize=10) + 1, 10)
    assert lineas == ["uno dos", "tres", "cuatro", "", "fin"]


def test_limpiar_condiciones_quita_la_fabrica_salvo_si_se_conserva():
    T = {"lista_negra": ["ACME", "ACME Solar"]}
    assert motor.limpiar_condiciones("\n\nquedando acme solar  eximida\n\n", T, False) == "quedando eximida"
    assert motor.limpiar_condiciones("quedando ACME eximida", T, True) == "quedando ACME eximida"
