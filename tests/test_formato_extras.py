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
