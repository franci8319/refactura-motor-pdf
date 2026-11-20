"""Formato con líneas extra, pie de fábrica en todas las páginas y logo de fábrica en el hueco del
logo del montador. Usa una plantilla privada de ../templates/ con "lineas_sin_pos": "extra" y los
PDFs de ../samples/ que reconozca; si no hay, se salta (el repo público no lleva ninguno)."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from pathlib import Path

import pymupdf as fitz
import pytest

from test_servicio import Almacen, MARCA, cliente, generar

ROOT = Path(__file__).resolve().parents[2]
PLANTILLAS = [p for p in (json.loads(f.read_text(encoding="utf-8")) for f in sorted((ROOT / "templates").glob("*.json")))
              if p.get("lineas_sin_pos") == "extra"]
PLANTILLA = PLANTILLAS[-1] if PLANTILLAS else None


def reconocidos() -> list[Path]:
    if PLANTILLA is None:
        return []
    import motor
    T = motor.compile_template(PLANTILLA)
    out = []
    for f in sorted((ROOT / "samples").rglob("*.pdf")):
        with fitz.open(f) as doc:
            if not motor.check_huella(doc, T):
                out.append(f)
    return out


SAMPLES = reconocidos()
IDS = [f"pdf{i}" for i in range(1, len(SAMPLES) + 1)]
pytestmark = pytest.mark.skipif(not SAMPLES, reason="sin plantilla con líneas extra o sin PDFs de ese formato")


@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_extras_llevan_pct(sample):
    d = generar(cliente(Almacen(sample.read_bytes())), plantilla=PLANTILLA).json()
    assert d["estado"] == "ok", d["errores"]
    extras = [ln for ln in d["lineas"] if ln["tipo"] == "extra"]
    assert extras and all(ln["pct"] == "30.00" and ln["dibujo"] is None for ln in extras)
    assert sum(Decimal(ln["total"]) for ln in d["lineas"]) == Decimal(d["totales"]["base"])


@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
@pytest.mark.parametrize("con_pie", [True, False])
def test_marca_sustituye_logo_y_pie_de_fabrica(sample, con_pie):
    marca = MARCA if con_pie else {"logo": MARCA["logo"], "datos": MARCA["datos"]}
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=PLANTILLA, marca=marca).json()
    assert d["estado"] == "ok", d["errores"]

    salida = fitz.open(stream=almacen.subidas[0], filetype="pdf")
    red = PLANTILLA["redaccion"]
    zona = fitz.Rect(red["zona_logo_fabrica"]["rect"])
    imgs = [i for i in salida[0].get_image_info() if fitz.Rect(i["bbox"]).intersects(zona)]
    assert len(imgs) == 1  # solo el logo del montador
    texto = "".join(p.get_text() for p in salida).lower()
    assert not any(n.lower() in texto for n in red["lista_negra"])
    pie = MARCA["pie"][0] if con_pie else " · ".join(MARCA["datos"])
    for p in salida:
        assert pie in " ".join(p.get_text().split())


@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_sin_marca_quita_logo_y_pie_de_fabrica(sample):
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=PLANTILLA).json()
    assert d["estado"] == "ok", d["errores"]
    salida = fitz.open(stream=almacen.subidas[0], filetype="pdf")
    zona = fitz.Rect(PLANTILLA["redaccion"]["zona_logo_fabrica"]["rect"])
    assert not [i for i in salida[0].get_image_info() if fitz.Rect(i["bbox"]).intersects(zona)]
    for p in salida:
        for z in PLANTILLA["redaccion"]["zonas_fabrica_todas"]:
            assert not p.get_text(clip=fitz.Rect(z)).strip()
    assert base64.b64decode(d["miniaturas"][0]).startswith(b"\x89PNG")


@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_montaje_sin_linea_va_como_concepto_instalacion(sample):
    """Este formato no trae instalación: el montaje se añade como primer concepto y suma a la base."""
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=PLANTILLA, pct=38, montaje="4000.00",
                conceptos=[{"texto": "Portes", "importe": "150"}]).json()
    assert d["estado"] == "ok", d["errores"]
    assert [c["texto"] for c in d["conceptos"]] == ["Instalación", "Portes"]
    lineas = sum(Decimal(ln["total"]) for ln in d["lineas"])
    assert lineas + Decimal("4150.00") == Decimal(d["totales"]["base"])
    salida = fitz.open(stream=almacen.subidas[0], filetype="pdf")
    texto = " ".join("".join(p.get_text() for p in salida).split())
    assert "Otros conceptos" in texto and "4.000,00 €" in texto and "150,00 €" in texto


@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_conservando_logo_de_fabrica_el_del_montador_va_aparte(sample):
    if "zona_logo_conservando" not in PLANTILLA["marca"]:
        pytest.skip("la plantilla no define zona_logo_conservando")
    original = fitz.open(stream=sample.read_bytes(), filetype="pdf")
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=PLANTILLA, conservar_fabrica=True,
                marca={"logo": MARCA["logo"], "pie": MARCA["pie"]}).json()
    assert d["estado"] == "ok", d["errores"]
    salida = fitz.open(stream=almacen.subidas[0], filetype="pdf")
    fab = fitz.Rect(PLANTILLA["redaccion"]["zona_logo_fabrica"]["rect"])
    alt = fitz.Rect(PLANTILLA["marca"]["zona_logo_conservando"]["rect"])

    def imgs(doc, zona):
        return [i["bbox"] for i in doc[0].get_image_info() if fitz.Rect(i["bbox"]).intersects(zona)]
    assert imgs(salida, fab) == imgs(original, fab) != []  # el de fábrica, intacto
    assert len(imgs(salida, alt)) == 1  # el del montador, en su zona


CONDICIONES = ("Le rogamos que revise medidas y cantidades.\nMuy importante, revisar mano de apertura.\n\n"
               "Acepto el presupuesto\n\n\nSello/DNI, Firma y Fecha")


def _plantilla_con_condiciones() -> dict:
    p = next((t for t in reversed(PLANTILLAS) if "condiciones" in t), None)
    if p is None:
        pytest.skip("ninguna plantilla define condiciones")
    return p


@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_condiciones_del_montador_sustituyen_las_de_fabrica(sample):
    plantilla = _plantilla_con_condiciones()
    almacen = Almacen(sample.read_bytes())
    nombre = plantilla["redaccion"]["lista_negra"][-1]  # un nombre de fábrica: se quita del texto del montador
    d = generar(cliente(almacen), plantilla=plantilla,
                marca={"condiciones": CONDICIONES + f"\nQuedando {nombre} eximida."}).json()
    assert d["estado"] == "ok", d["errores"]
    salida = fitz.open(stream=almacen.subidas[0], filetype="pdf")
    ultima = " ".join(salida[-1].get_text().split())
    assert "Muy importante, revisar mano de apertura." in ultima and "Quedando eximida." in ultima
    original = " ".join(fitz.open(stream=sample.read_bytes(), filetype="pdf")[-1].get_text().split())
    frase = "No se admitirán reclamaciones"
    assert frase in original and frase not in ultima  # el texto de la fábrica ya no está


@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_condiciones_demasiado_largas_son_error_de_marca(sample):
    plantilla = _plantilla_con_condiciones()
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=plantilla, marca={"condiciones": "\n".join(["Línea"] * 200)}).json()
    assert d["estado"] == "needs_review" and not almacen.subidas
    assert d["errores"] and all(e.startswith("marca:") for e in d["errores"])
