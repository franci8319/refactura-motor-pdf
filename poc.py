"""Fase 0 – CLI de pruebas del motor PDF (motor.py) sobre archivos locales.

Para cada PDF de fábrica:
  1. Comprueba la huella de la plantilla (tamaño de página, fuentes y anclas de cabecera).
  2. Extrae líneas (ventanas y otros conceptos), precios y totales, y valida la aritmética.
  3. Redacta de verdad precios, totales y datos de fábrica (apply_redactions).
  4. Estampa precios nuevos con el % indicado (markup por defecto) y recalcula base, IVA y total.
     La línea de montaje (INSTALACION A OBRA) no lleva %: la escribe el montador (--montaje)
     o, si no se indica, se conserva el importe del PDF.
  5. Verifica la salida re-extrayendo el PDF generado.

Uso:
  python worker/poc.py                      # todos los PDFs de samples/
  python worker/poc.py ruta.pdf --pct 30 --montaje 400
  python worker/poc.py ruta.pdf --pct 30 --modo margen
  python worker/poc.py --plantilla templates/<fabrica>-v1.json
  python worker/poc.py ruta.pdf --logo logo.png --dato "Mi Empresa S.L." --dato "NIF B1234" --pie "..."

Salida en worker/out/<nombre>/: salida.pdf, resultado.json y miniaturas PNG.
Código de salida 1 si algún PDF queda en needs_review.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from motor import CENT, Marca, compile_template, fmt_eur, generar, jsonable, parse_pct

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path(__file__).resolve().parent / "out"

# Plantilla por defecto para la PoC: la última de templates/ (carpeta privada, fuera de este repo).
# En el producto llega por HTTP (JSON versionado en Supabase) y nunca vive en el repo público del worker.
DEFAULT_TEMPLATE = next(iter(sorted((ROOT / "templates").glob("*.json"), reverse=True)), None)


def load_template(path: Path) -> dict:
    return compile_template(json.loads(path.read_text(encoding="utf-8")), f"plantilla {path.name}")


def process(src: Path, pct: Decimal, modo: str, montaje: Decimal | None, T: dict, marca: Marca) -> dict:
    out = OUT_DIR / src.stem
    out.mkdir(parents=True, exist_ok=True)
    # No dejar salida ni miniaturas viejas junto a un needs_review.
    for viejo in [out / "salida.pdf", *out.glob("p*.png")]:
        viejo.unlink(missing_ok=True)
    r = generar(src.read_bytes(), T, pct, modo, montaje, marca=marca)
    if r.ok:
        (out / "salida.pdf").write_bytes(r.pdf)
        for i, png in enumerate(r.miniaturas, 1):
            (out / f"p{i}.png").write_bytes(png)
    res = {"archivo": src.name, **r.datos}
    (out / "resultado.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=jsonable),
                                        encoding="utf-8")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="*", type=Path)
    ap.add_argument("--pct", type=Decimal, default=Decimal(30))
    ap.add_argument("--modo", choices=("markup", "margen"), default="markup")
    ap.add_argument("--montaje", type=lambda s: parse_pct(s).quantize(CENT, ROUND_HALF_UP),
                    help="precio unitario de montaje del montador, sin %% (p. ej. 400 o 400,50)")
    ap.add_argument("--plantilla", type=Path, default=DEFAULT_TEMPLATE, required=DEFAULT_TEMPLATE is None,
                    help="JSON de plantilla")
    ap.add_argument("--logo", type=Path, help="logo del montador (PNG o JPEG)")
    ap.add_argument("--dato", action="append", default=[], help="línea de datos del montador (repetible)")
    ap.add_argument("--pie", action="append", default=[], help="línea del pie del montador (repetible)")
    args = ap.parse_args()
    try:
        T = load_template(args.plantilla)
    except (OSError, ValueError) as e:
        ap.error(str(e))
    if args.modo == "margen" and not (0 <= args.pct < 100):
        ap.error("en modo margen el % debe estar entre 0 y 100")
    if args.montaje is not None and args.montaje < 0:
        ap.error("el montaje no puede ser negativo")
    pdfs = args.pdfs or sorted((ROOT / "samples").glob("*.pdf"))
    marca = Marca(args.logo.read_bytes() if args.logo else None, args.dato, args.pie)

    fallos = 0
    for src in pdfs:
        r = process(src, args.pct, args.modo, args.montaje, T, marca)
        print(f"\n== {src.name}: {r['estado'].upper()} ({len(r['lineas'])} líneas, {r['n_dibujos']} dibujos)")
        for ln in r["lineas"]:
            extra = f" -> {fmt_eur(ln['total'])}" if "total" in ln else ""
            print(f"   p{ln['pagina']} {ln['tipo']:<7} {ln['ref'][:45]:<45} x{ln['uds']} "
                  f"{fmt_eur(ln['coste_total']):>10}{extra}")
        if t := r["totales"]:
            print(f"   base {fmt_eur(t['base'])}  IVA {t['iva_pct']}% {fmt_eur(t['iva'])}  "
                  f"TOTAL {fmt_eur(t['total'])}")
        for e in r["errores"]:
            print(f"   ! {e}")
        fallos += r["estado"] != "ok"
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
