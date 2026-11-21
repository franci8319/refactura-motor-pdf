"""Plantillas privadas de ../templates/ y PDFs de ../samples/ para los tests de formatos reales.
El repo público no lleva ninguno: sin ellos, esos tests se saltan."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf as fitz

import motor

ROOT = Path(__file__).resolve().parents[2]


def ultimas(filtro) -> list[dict]:
    """Última versión de cada plantilla de ../templates/ que cumple el filtro."""
    por_id: dict[str, dict] = {}
    for f in (ROOT / "templates").glob("*.json"):
        p = json.loads(f.read_text(encoding="utf-8"))
        if filtro(p) and p["version"] > por_id.get(p["id"], {}).get("version", 0):
            por_id[p["id"]] = p
    return [por_id[k] for k in sorted(por_id)]


def pdfs() -> list[Path]:
    """PDFs de ../samples/ (salvo diagnostico/, que es temporal)."""
    base = ROOT / "samples"
    return [f for f in sorted(base.rglob("*.pdf")) if "diagnostico" not in f.relative_to(base).parts]


def pares(plantillas: list[dict]) -> list[tuple[dict, Path]]:
    """Cada PDF de ../samples/ con la plantilla que lo reconoce."""
    out = []
    for plantilla in plantillas:
        T = motor.compile_template(plantilla)
        for f in pdfs():
            with fitz.open(f) as doc:
                if not motor.check_huella(doc, T):
                    out.append((plantilla, f))
    return out
