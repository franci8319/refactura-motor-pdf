"""Motor PDF del worker: huella, extracción, validación, redacción, estampado y verificación.

Trabaja solo en memoria (bytes de entrada → bytes de salida), sin disco ni estado, para que
lo usen igual el servicio HTTP (servicio.py) y la CLI de pruebas (poc.py).

Regla de oro: si falla la huella, cualquier validación o la verificación de salida, el estado
es needs_review y no se devuelve PDF.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

import pymupdf as fitz

CENT = Decimal("0.01")
TOL = Decimal("0.01")
MAX_PAGINAS = 60
# Helvetica-Bold (Nimbus Sans) incrustada como CID/Identity-H: el base-14 sin incrustar no codifica "€".
# En producción, fuente propia con licencia de incrustación.
FONT = fitz.Font("hebo")
FONT_NAME = "MontBold"
FONT_REG = fitz.Font("helv")  # Nimbus Sans regular, incrustada igual: texto largo (condiciones)
FONT_REG_NAME = "MontReg"


def compile_template(raw: dict, nombre: str = "plantilla") -> dict:
    """Valida el JSON de plantilla y compila sus regex. Falla pronto si falta una clave."""
    try:
        modo = raw.get("modo", "uds")
        if modo not in ("uds", "tabla"):
            raise ValueError(f"modo desconocido: {modo!r}")
        red = raw["redaccion"]
        logo = red["zona_logo_fabrica"]
        marca = raw.get("marca")
        sin_pos = raw.get("lineas_sin_pos", "otro")
        if sin_pos not in ("otro", "extra"):
            raise ValueError(f"lineas_sin_pos desconocido: {sin_pos!r}")
        cond = raw.get("condiciones")
        con = raw.get("conceptos")
        if con is not None and con["ancla"] not in ("Base Imponible", "Total", "IVA", "TOTAL"):
            raise ValueError(f"conceptos.ancla desconocida: {con['ancla']!r}")
        pie_defecto = (marca or {}).get("pie_por_defecto")
        if pie_defecto not in (None, "datos"):
            raise ValueError(f"pie_por_defecto desconocido: {pie_defecto!r}")
        T = {"modo": modo}
        if modo == "uds":
            a, dib = raw["anclas"], raw["dibujo"]
            T |= {
                "ancla_linea": re.compile(a["linea"]),
                "ancla_pos": re.compile(a["pos"]),
                "ancla_montaje": re.compile(a["montaje"], re.I),
                "ancla_importe": a["col_importe"],
                "ancla_total_col": a["col_total"],
                "totales": re.compile(raw["totales"]),
                # Opcional: líneas en negativo con este texto = descuento de la fábrica al montador. Se ocultan
                # al cliente (recuadro entero fuera del PDF) y solo bajan el coste del montador.
                "descuento": None if "descuentos" not in raw else re.compile(raw["descuentos"]["ancla"], re.I),
                # Dibujo de ventana: imagen que empieza justo bajo la cabecera "Pos." y a la izquierda.
                "dibujo_max_dy": dib["max_dy"],
                "dibujo_max_x0": dib["max_x0"],
            }
        else:
            t, tc = raw["tabla"], raw["totales_columnas"]
            T |= {
                # Tabla de líneas: cada fila con precio en la columna del total; cantidad × precio = total.
                "tabla": {
                    "col_cantidad": t["col_cantidad"], "col_precio": t["col_precio"], "col_total": t["col_total"],
                    "col_descripcion": t["col_descripcion"], "tol_x": float(t.get("tol_x", 20)),
                    "fin": list(t["fin"]), "subtotales": dict(t.get("subtotales", {})),
                    "tipo": t.get("tipo", "extra"), "alto_ref": float(t.get("alto_ref", 14)),
                    # Opcional: la descripción acaba donde empieza esta columna; las de en medio
                    # (unidades, medidas…) se añaden a la referencia entre paréntesis.
                    "col_fin_descripcion": t.get("col_fin_descripcion"),
                },
                # Totales en columnas: valores bajo una fila de títulos. Rangos del borde derecho del valor,
                # relativos al x0 del ancla (un título de esa fila).
                "totales_columnas": {
                    "ancla": tc["ancla"], "alto": float(tc.get("alto", 22)),
                    "columnas": {k: tuple(float(v) for v in r) for k, r in tc["columnas"].items()},
                    "iva_pct": tuple(float(v) for v in tc["iva_pct"]),
                },
                "pct": re.compile(raw.get("pct", r"^\s*(\d{1,2}(?:,\d{1,2})?)\s*$")),
            }
        return T | {
            "id": f"{raw['id']}-v{raw['version']}",
            "huella": raw["huella"],
            # Líneas con precio pero sin cabecera "Pos." (ni montaje): "otro" = sin decidir (needs_review);
            # "extra" = suplemento de fábrica (vidrio, perfil…): lleva el % como una ventana, sin dibujo.
            "lineas_sin_pos": sin_pos,
            # Opcional: texto de condiciones de la fábrica que el montador puede sustituir por el suyo.
            # "zona" relativa al ancla (texto que lo abre): [x0 − x0_ancla, y0 − y0_ancla, x1 − x0_ancla, y1 − y0_ancla].
            "condiciones": None if cond is None else {
                "ancla": str(cond["ancla"]), "zona": tuple(float(v) for v in cond["zona"]),
                "tam": tuple(float(v) for v in cond.get("tam", (6.6, 5.0))),
            },
            # Opcional: líneas que añade el montador ("Otros conceptos": instalación, portes…) en un bloque
            # encima del recuadro de totales, que baja para hacer sitio. "caja" = recuadro de totales relativo
            # al ancla: [x0 − x1_ancla, y0 − y0_ancla, x1 − x1_ancla, y1 − y0_ancla].
            "conceptos": None if con is None else {
                "ancla": con["ancla"], "caja": tuple(float(v) for v in con["caja"]),
                "margen_inferior": float(con["margen_inferior"]), "max_lineas": int(con.get("max_lineas", 10)),
            },
            "precio": re.compile(raw["precio"]),
            "logo_pagina": logo["pagina"] - 1,
            "zona_logo_fabrica": tuple(logo["rect"]),  # x0, y0, x1, y1
            "lista_negra": red["lista_negra"],
            # Zonas con datos de fábrica en todas las páginas (p. ej. su pie): se vacían enteras.
            "zonas_fabrica_todas": [tuple(r) for r in red.get("zonas_fabrica_todas", [])],
            # Opcional: dónde va la marca del montador (logo y datos en la pág. indicada, pie en todas).
            "marca": None if marca is None else {
                "logo_pagina": marca["zona_logo"]["pagina"] - 1,
                "zona_logo": tuple(marca["zona_logo"]["rect"]),
                "logo_derecha": False,
                # Opcional: si el logo de fábrica se conserva y comparte hueco con el del montador,
                # el del montador va aquí (alineado a la derecha).
                "logo_conservando": None if "zona_logo_conservando" not in marca else (
                    marca["zona_logo_conservando"]["pagina"] - 1, tuple(marca["zona_logo_conservando"]["rect"])),
                "datos_pagina": marca["zona_datos"]["pagina"] - 1,
                "zona_datos": tuple(marca["zona_datos"]["rect"]),
                "zona_pie": tuple(marca["zona_pie"]["rect"]),
                # "datos": sin pie propio, los datos de empresa van también en el pie.
                "pie_por_defecto": pie_defecto,
                # false: la cabecera de fábrica ocupa el hueco del logo; conservándola no cabe el del montador.
                "logo_con_fabrica": bool(marca.get("logo_con_fabrica", True)),
            },
        }
    except (KeyError, TypeError, re.error) as e:
        raise ValueError(f"{nombre} no válida: {e!r}") from e


def parse_eur(s: str) -> Decimal:
    return Decimal(s.strip().replace(".", "").replace(",", "."))


def parse_pct(s: str) -> Decimal:
    return Decimal(s.replace(",", "."))


def fmt_eur(d: Decimal) -> str:
    entero, dec = f"{d.quantize(CENT, ROUND_HALF_UP):.2f}".split(".")
    signo = "-" if entero.startswith("-") else ""
    entero = entero.lstrip("-")
    grupos = []
    while entero:
        grupos.insert(0, entero[-3:])
        entero = entero[:-3]
    return f"{signo}{'.'.join(grupos)},{dec}"


@dataclass
class Span:
    text: str
    bbox: tuple[float, float, float, float]
    origin: tuple[float, float]
    size: float
    color: int
    bold: bool


@dataclass
class Linea:
    pagina: int
    tipo: str  # "ventana" | "montaje" | "extra" | "otro"
    ref: str
    uds: int
    importe: str
    total: str
    rect_importe: tuple
    rect_total: tuple
    origin_importe: tuple
    origin_total: tuple
    size: float
    color: int
    dibujo: tuple | None = None
    # Modo tabla: importe = total de la línea (uds = 1) y en la columna de precio va el precio por unidad
    # de medida (metro, m²…), que se recalcula como total / cantidad.
    cantidad: Decimal | None = None
    precio: str | None = None
    pagina_dibujo: int | None = None  # si la ventana empieza en la página anterior (Pos. y dibujo allí)
    caja: tuple | None = None  # descuento: recuadro que se quita entero del PDF del cliente


@dataclass
class Total:
    pagina: int
    label: str
    valor: str
    iva_pct: str | None
    rect: tuple
    origin: tuple
    size: float
    color: int
    solo_valor: bool = False  # modo tabla: el valor va solo, bajo su título de columna
    pct_rect: tuple | None = None  # modo tabla: casilla del % de IVA (se reescribe si el montador lo cambia)
    pct_origin: tuple | None = None


@dataclass
class Subtotal:
    """Suma parcial entre páginas ("Suma y sigue" al pie, "Suma anterior" al principio de la siguiente)."""
    pagina: int
    tipo: str  # "sigue" | "anterior"
    valor: str
    rect: tuple
    origin: tuple
    size: float
    color: int


@dataclass
class Extraccion:
    lineas: list[Linea] = field(default_factory=list)
    totales: list[Total] = field(default_factory=list)
    n_dibujos: int = 0
    errores: list[str] = field(default_factory=list)
    subtotales: list[Subtotal] = field(default_factory=list)


@dataclass
class Resultado:
    """Lo que sale del motor: JSON de resultados y, solo si estado == "ok", el PDF y sus miniaturas
    (generar) o el PNG del dibujo de cada línea (analizar; None si la línea no tiene dibujo)."""
    datos: dict
    pdf: bytes | None = None
    miniaturas: list[bytes] = field(default_factory=list)
    dibujos: list[bytes | None] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.datos["estado"] == "ok"


@dataclass
class Marca:
    """Marca del montador: logo (PNG/JPEG) en lugar del de la cabecera, líneas de datos en el hueco
    del logo de fábrica y, si hay pie, sustituye el pie de todas las páginas. Todo opcional."""
    logo: bytes | None = None
    datos: list[str] = field(default_factory=list)
    pie: list[str] = field(default_factory=list)
    condiciones: str | None = None  # sustituye el texto de condiciones de la fábrica (si el PDF lo trae)

    @property
    def vacia(self) -> bool:
        return not (self.logo or self.datos or self.pie or self.condiciones)

    @property
    def visual(self) -> bool:
        """Lo que va en zonas fijas de la plantilla (cabecera y pie)."""
        return bool(self.logo or self.datos or self.pie)


MAX_LADO_LOGO = 4000  # px; se comprueba en la cabecera antes de decodificar
TAM_DATOS = (9.0, 6.0)  # pt máx. y mín.: el texto se reduce hasta caber en su zona
TAM_PIE = (7.0, 5.5)
INTERLINEA = 1.3


def page_spans(page: fitz.Page) -> list[Span]:
    out = []
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                if s["text"].strip():
                    out.append(Span(s["text"], tuple(s["bbox"]), tuple(s["origin"]),
                                    s["size"], s["color"], "Bold" in s["font"]))
    return out


def check_huella(doc: fitz.Document, T: dict) -> list[str]:
    """Identificación: ¿este PDF tiene el formato que la plantilla espera?"""
    h = T["huella"]
    err = []
    w, ht = h["tam_pagina"]
    for p in doc:
        if abs(p.rect.width - w) > h["tol_pt"] or abs(p.rect.height - ht) > h["tol_pt"]:
            err.append(f"p{p.number + 1}: tamaño {p.rect.width:.0f}×{p.rect.height:.0f} ≠ {w}×{ht}")
    fuentes = {f[3].split("+")[-1] for p in doc for f in p.get_fonts()}
    if extra := fuentes - set(h["fuentes"]):
        err.append(f"fuentes no previstas: {sorted(extra)}")
    p1 = doc[0].get_text()
    todo = "\n".join(p.get_text() for p in doc)
    faltan = [k for k in h["claves_p1"] if k not in p1] + [k for k in h["claves_doc"] if k not in todo]
    if faltan:
        err.append(f"faltan anclas de cabecera: {faltan}")
    return [f"huella sin coincidencia con {T['id']}: {e}" for e in err]


def extract(doc: fitz.Document, T: dict) -> Extraccion:
    return extract_tabla(doc, T) if T["modo"] == "tabla" else extract_uds(doc, T)


def _misma_fila(a: Span, b: Span, tol: float = 2.5) -> bool:
    return abs(a.bbox[1] - b.bbox[1]) <= tol


def extract_tabla(doc: fitz.Document, T: dict) -> Extraccion:
    """Presupuesto en tabla (sin dibujos): filas con precio en la columna del total, subtotales entre
    páginas y totales en columnas bajo su fila de títulos."""
    C, TC = T["tabla"], T["totales_columnas"]
    ex = Extraccion()
    for page in doc:
        spans = page_spans(page)
        cab = {k: next((s for s in spans if s.text.strip() == C[k]), None)
               for k in ("col_cantidad", "col_precio", "col_total", "col_descripcion")}
        fin_desc = C["col_fin_descripcion"] and next((s for s in spans if s.text.strip() == C["col_fin_descripcion"]), None)
        etiquetas_sub = [(tipo, s) for s in spans for tipo, texto in C["subtotales"].items()
                         if s.text.strip().startswith(texto)]
        for tipo, et in etiquetas_sub:
            v = next((s for s in spans if T["precio"].match(s.text) and _misma_fila(s, et, 3)
                      and s.bbox[0] > et.bbox[2]), None)
            if v is None:
                ex.errores.append(f"p{page.number + 1}: subtotal sin importe")
            else:
                ex.subtotales.append(Subtotal(page.number, tipo, v.text.strip(), v.bbox, v.origin, v.size, v.color))
        if all(cab.values()):
            fin = min((s.bbox[1] for s in spans if any(s.text.strip().startswith(f) for f in C["fin"])),
                      default=page.rect.height)
            y0 = max(cab[k].bbox[3] for k in ("col_cantidad", "col_precio", "col_total"))
            x_desc = cab["col_descripcion"].bbox[0]
            x_cant = min(cab[k].bbox[0] for k in ("col_cantidad", "col_precio")) - 5
            x_max_desc = fin_desc.bbox[0] - 2 if fin_desc else x_cant - 35
            filas = sorted((s for s in spans if T["precio"].match(s.text) and y0 < s.bbox[1] < fin
                            and abs(s.bbox[2] - cab["col_total"].bbox[2]) <= C["tol_x"]
                            and not any(_misma_fila(s, et, 3) for _, et in etiquetas_sub)),
                           key=lambda s: s.bbox[1])
            for tot in filas:
                def en_col(k: str) -> Span | None:
                    return next((s for s in spans if T["precio"].match(s.text) and _misma_fila(s, tot)
                                 and abs(s.bbox[2] - cab[k].bbox[2]) <= C["tol_x"]), None)
                cant, prec = en_col("col_cantidad"), en_col("col_precio")
                if cant is None or prec is None:
                    ex.errores.append(f"p{page.number + 1}: fila sin cantidad o sin precio")
                    continue
                ref = " ".join(s.text.strip() for s in sorted(spans, key=lambda s: (s.bbox[1], s.bbox[0]))
                               if x_desc - 5 <= s.bbox[0] < x_max_desc
                               and tot.bbox[1] - 2 <= s.bbox[1] < tot.bbox[1] + C["alto_ref"])
                # Unidades y medidas (columnas entre la descripción y la cantidad), para reconocer la línea.
                medio = " ".join(s.text.strip() for s in sorted(spans, key=lambda s: s.bbox[0])
                                 if fin_desc and x_max_desc <= s.bbox[0] < x_cant and _misma_fila(s, tot, 3))
                if medio:
                    ref = f"{ref} ({medio})"
                ex.lineas.append(Linea(page.number, C["tipo"], ref or "(sin descripción)", 1,
                                       tot.text.strip(), tot.text.strip(), prec.bbox, tot.bbox,
                                       prec.origin, tot.origin, tot.size, tot.color, None,
                                       parse_eur(cant.text), prec.text.strip()))
        # Totales: valores bajo la fila de títulos (rangos por el borde derecho, relativos al ancla).
        ancla = next((s for s in spans if s.text.strip() == TC["ancla"]), None)
        if ancla is None:
            continue
        bajo = [s for s in spans if ancla.bbox[3] - 1 <= s.bbox[1] <= ancla.bbox[3] + TC["alto"]]

        def valor(r: tuple, patron: re.Pattern) -> Span | None:
            return next((s for s in bajo if patron.match(s.text)
                         and ancla.bbox[0] + r[0] <= s.bbox[2] <= ancla.bbox[0] + r[1]), None)
        pct = valor(TC["iva_pct"], T["pct"])
        for label, r in TC["columnas"].items():
            v = valor(r, T["precio"])
            if v is not None:
                con_pct = label == "IVA" and pct is not None
                ex.totales.append(Total(page.number, label, v.text.strip(), pct.text.strip() if con_pct else None,
                                        v.bbox, v.origin, v.size, v.color, solo_valor=True,
                                        pct_rect=pct.bbox if con_pct else None,
                                        pct_origin=pct.origin if con_pct else None))
    return ex


PRECIO_NEGATIVO = re.compile(r"^\s*-\d{1,3}(?:\.\d{3})*,\d{2}\s*$")


def _caja_de(page: fitz.Page, fila: Span) -> tuple | None:
    """El recuadro de ancho completo más pequeño que contiene la fila (para quitar un bloque entero)."""
    r = fitz.Rect(fila.bbox)
    cajas = [fitz.Rect(d["rect"]) for d in page.get_drawings()
             if fitz.Rect(d["rect"]).contains(r) and fitz.Rect(d["rect"]).width > page.rect.width * 0.7]
    return tuple(min(cajas, key=lambda c: c.height)) if cajas else None


def extract_uds(doc: fitz.Document, T: dict) -> Extraccion:
    ex = Extraccion()
    # Ventana partida: su "Pos." y su dibujo al final de una página y la fila UDS al principio de la siguiente.
    arrastre: tuple[Span, int, tuple | None] | None = None
    for page in doc:
        spans = page_spans(page)
        pos_spans = [s for s in spans if T["ancla_pos"].match(s.text.strip())]
        col_importe = next((s for s in spans if s.text.startswith(T["ancla_importe"])), None)
        col_total = next((s for s in spans if s.text.startswith(T["ancla_total_col"])), None)
        imgs = page.get_image_info()

        dibujos = []
        for ps in pos_spans:
            hit = next((i["bbox"] for i in imgs
                        if 0 <= i["bbox"][1] - ps.bbox[3] <= T["dibujo_max_dy"]
                        and i["bbox"][0] <= T["dibujo_max_x0"]), None)
            if hit:
                dibujos.append(tuple(round(v, 1) for v in hit))
        ex.n_dibujos += len(dibujos)

        filas = sorted((s for s in spans if T["ancla_linea"].match(s.text.strip())),
                       key=lambda s: s.bbox[1])
        pos_libres = sorted(pos_spans, key=lambda s: s.bbox[1])
        y_prev = 0.0
        for fila in filas:
            uds = int(T["ancla_linea"].match(fila.text.strip()).group(1))
            negativos = T["descuento"] is not None
            precios = sorted((s for s in spans
                              if (T["precio"].match(s.text) or (negativos and PRECIO_NEGATIVO.match(s.text)))
                              and abs(s.bbox[1] - fila.bbox[1]) < 2 and s.bbox[0] > fila.bbox[2]),
                             key=lambda s: s.bbox[0])
            if len(precios) != 2:
                ex.errores.append(f"p{page.number + 1}: fila UDS con {len(precios)} precios (se esperaban 2)")
                continue
            imp, tot = precios
            if col_importe and col_total and not (imp.bbox[2] < col_total.bbox[0] <= tot.bbox[2]):
                ex.errores.append(f"p{page.number + 1}: precios fuera de las columnas Importe/Total")

            pos = next((p for p in pos_libres if p.bbox[1] < fila.bbox[1]), None)
            pagina_dibujo = caja = None
            negativo = bool(PRECIO_NEGATIVO.match(imp.text))
            if pos:
                pos_libres.remove(pos)
                ref = T["ancla_pos"].match(pos.text.strip()).group(1)
                tipo = "ventana"
                dibujo = next((d for d in dibujos if 0 <= d[1] - pos.bbox[3] <= T["dibujo_max_dy"]), None)
            elif arrastre is not None and y_prev == 0.0:  # primera fila de la página, sin Pos. encima
                pos_prev, pagina_dibujo, dibujo = arrastre
                ref = T["ancla_pos"].match(pos_prev.text.strip()).group(1)
                tipo = "ventana"
            elif negativo:
                ref = " ".join(s.text.strip() for s in spans
                               if y_prev < s.bbox[1] < fila.bbox[1] and not s.bold
                               and fila.bbox[0] - 200 < s.bbox[0] < fila.bbox[2] + 100)
                if not T["descuento"].search(ref):
                    ex.errores.append(f"p{page.number + 1}: línea en negativo no reconocida ({ref[:40]})")
                    continue
                tipo, dibujo, caja = "descuento", None, _caja_de(page, fila)
                if caja is None:
                    ex.errores.append(f"p{page.number + 1}: no se encuentra el recuadro del descuento")
                    continue
            else:
                ref = " ".join(s.text.strip() for s in spans
                               if y_prev < s.bbox[1] < fila.bbox[1] and not s.bold
                               and fila.bbox[0] - 200 < s.bbox[0] < fila.bbox[2] + 100)
                tipo = "montaje" if T["ancla_montaje"].search(ref) else T["lineas_sin_pos"]
                dibujo = None
            ex.lineas.append(Linea(page.number, tipo, ref or "(sin descripción)", uds,
                                   imp.text.strip(), tot.text.strip(), imp.bbox, tot.bbox,
                                   imp.origin, tot.origin, imp.size, imp.color, dibujo,
                                   pagina_dibujo=pagina_dibujo, caja=caja))
            if pagina_dibujo is not None:
                arrastre = None
            y_prev = fila.bbox[3]
        # Una "Pos." sin fila UDS debajo al final de la página pasa a la siguiente.
        arrastre = None
        if pos_libres and (not filas or pos_libres[-1].bbox[1] > filas[-1].bbox[1]):
            ultima = pos_libres[-1]
            dib = next((d for d in dibujos if 0 <= d[1] - ultima.bbox[3] <= T["dibujo_max_dy"]), None)
            arrastre = (ultima, page.number, dib)

        for s in spans:
            m = T["totales"].match(s.text.strip())
            if m:
                label = "IVA" if m["iva"] else m["label"]
                ex.totales.append(Total(page.number, label, m["valor"], m["iva"],
                                        s.bbox, s.origin, s.size, s.color))
    return ex


def get_total(ex: Extraccion, label: str) -> Total | None:
    return next((t for t in ex.totales if t.label == label), None)


def validate(ex: Extraccion) -> list[str]:
    """Comprobaciones de la sección 'Detección automática' de la propuesta."""
    err = list(ex.errores)
    if not ex.lineas:
        err.append("no se encontró ninguna línea (falta ancla UDS)")
    for ln in ex.lineas:
        if (parse_eur(ln.importe) * ln.uds - parse_eur(ln.total)).copy_abs() > TOL:
            err.append(f"{ln.ref}: importe × uds ≠ total")
        if ln.cantidad is not None and \
                ((ln.cantidad * parse_eur(ln.precio)).quantize(CENT, ROUND_HALF_UP) - parse_eur(ln.total)).copy_abs() > TOL:
            err.append(f"{ln.ref[:40]}: cantidad × precio ≠ total")
        if ln.tipo == "otro":
            err.append(f"p{ln.pagina + 1}: línea no reconocida ({ln.ref[:40]}); decidir en la plantilla")
        if (ln.tipo == "descuento") != (parse_eur(ln.total) < 0):
            err.append(f"p{ln.pagina + 1}: {ln.ref[:40]}: signo del importe no esperado")
    err += validate_subtotales(ex, [parse_eur(ln.total) for ln in ex.lineas])
    if sum(1 for ln in ex.lineas if ln.tipo == "montaje") > 1:
        err.append("más de una línea de montaje")
    # "Total" (sin IVA) es opcional: algunos formatos solo traen Base Imponible, IVA y TOTAL.
    base, total, iva, total_iva = (get_total(ex, k) for k in ("Base Imponible", "Total", "IVA", "TOTAL"))
    if not all((base, iva, total_iva)):
        err.append("faltan totales (Base Imponible / IVA / TOTAL)")
        return err
    suma = sum((parse_eur(ln.total) for ln in ex.lineas), Decimal(0))
    b = parse_eur(base.valor)
    if (suma - b).copy_abs() > TOL:
        err.append(f"suma de líneas {fmt_eur(suma)} ≠ base {base.valor}")
    if total and (parse_eur(total.valor) - b).copy_abs() > TOL:
        err.append("Total ≠ Base Imponible")
    if iva.iva_pct is None:
        err.append("IVA sin porcentaje")
        return err
    pct = parse_pct(iva.iva_pct)
    if ((b * pct / 100).quantize(CENT, ROUND_HALF_UP) - parse_eur(iva.valor)).copy_abs() > TOL:
        err.append("IVA ≠ base × %")
    if (b + parse_eur(iva.valor) - parse_eur(total_iva.valor)).copy_abs() > TOL:
        err.append("TOTAL ≠ base + IVA")
    n_ventanas = sum(1 for ln in ex.lineas if ln.tipo == "ventana")
    if n_ventanas != ex.n_dibujos:
        err.append(f"nº de ventanas ({n_ventanas}) ≠ nº de dibujos ({ex.n_dibujos})")
    return err


def tiene_montaje(ex: Extraccion) -> bool:
    return any(ln.tipo == "montaje" for ln in ex.lineas)


def subtotales_esperados(ex: Extraccion, totales_lineas: list[Decimal]) -> list[Decimal]:
    """Lo que debe valer cada subtotal: "sigue" = suma hasta su página; "anterior" = suma de las previas."""
    out = []
    for st in ex.subtotales:
        hasta = st.pagina + (1 if st.tipo == "sigue" else 0)
        out.append(sum((t for ln, t in zip(ex.lineas, totales_lineas) if ln.pagina < hasta), Decimal(0)))
    return out


def validate_subtotales(ex: Extraccion, totales_lineas: list[Decimal]) -> list[str]:
    return [f"p{st.pagina + 1}: subtotal {st.valor} ≠ suma de líneas {fmt_eur(e)}"
            for st, e in zip(ex.subtotales, subtotales_esperados(ex, totales_lineas))
            if (parse_eur(st.valor) - e).copy_abs() > TOL]


def validate_params(ex: Extraccion, montaje: Decimal | None, pct_lineas: dict[int, Decimal],
                    T: dict | None = None) -> list[str]:
    """Parámetros que solo se pueden comprobar contra las líneas extraídas."""
    err = []
    if montaje is not None and not tiene_montaje(ex) and (T is None or T["conceptos"] is None):
        err.append("se indicó montaje pero el PDF no tiene línea de instalación donde escribirlo")
    for i in sorted(pct_lineas):
        if not 0 <= i < len(ex.lineas):
            err.append(f"pct_lineas: la línea {i} no existe")
        elif ex.lineas[i].tipo in ("montaje", "descuento"):
            err.append(f"pct_lineas: la línea {i} es {ex.lineas[i].tipo}, que nunca lleva %")
    return err


def dimensiones_imagen(b: bytes) -> tuple[int, int] | None:
    """Ancho y alto de un PNG o JPEG leyendo solo la cabecera (sin decodificar: evita bombas)."""
    if b.startswith(b"\x89PNG\r\n\x1a\n") and b[12:16] == b"IHDR":
        return int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
    if b.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(b) and b[i] == 0xFF:
            marcador, largo = b[i + 1], int.from_bytes(b[i + 2:i + 4], "big")
            if 0xC0 <= marcador <= 0xCF and marcador not in (0xC4, 0xC8, 0xCC):  # SOFn
                return int.from_bytes(b[i + 7:i + 9], "big"), int.from_bytes(b[i + 5:i + 7], "big")
            i += 2 + largo
    return None


def logo_pixmap(b: bytes) -> fitz.Pixmap:
    """Decodifica y normaliza a RGB: al insertarlo se re-codifica, sin EXIF ni otros metadatos."""
    pix = fitz.Pixmap(b)
    if pix.colorspace is None or pix.colorspace.n != 3:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    return pix


def rect_logo(w: int, h: int, zona: tuple, derecha: bool = False) -> fitz.Rect:
    """Logo ajustado a la zona conservando la proporción, centrado en vertical y alineado a un lado."""
    z = fitz.Rect(zona)
    k = min(z.width / w, z.height / h)
    y0 = z.y0 + (z.height - h * k) / 2
    x0 = z.x1 - w * k if derecha else z.x0
    return fitz.Rect(x0, y0, x0 + w * k, y0 + h * k)


def plantilla_efectiva(T: dict, conservar_fabrica: bool) -> dict:
    """Con el logo de fábrica conservado, el del montador pasa a su zona alternativa (si la hay)."""
    M = T["marca"]
    if not conservar_fabrica or M is None or M["logo_conservando"] is None:
        return T
    pagina, zona = M["logo_conservando"]
    return {**T, "marca": {**M, "logo_pagina": pagina, "zona_logo": zona, "logo_derecha": True}}


def tam_texto(lineas: list[str], zona: tuple, tams: tuple[float, float]) -> float | None:
    """Mayor tamaño (en pasos de 0,25 pt) con el que todas las líneas caben en la zona; None si no caben."""
    z = fitz.Rect(zona)
    t = tams[0]
    while t >= tams[1]:
        if len(lineas) * t * INTERLINEA <= z.height and \
                max(FONT.text_length(l, fontsize=t) for l in lineas) <= z.width:
            return t
        t -= 0.25
    return None


def validate_marca(marca: Marca, T: dict, conservar_fabrica: bool = False) -> list[str]:
    """Errores de configuración de la marca, no del PDF: llevan el prefijo "marca:" para que la app
    pida corregir los ajustes en lugar de dejar el presupuesto en revisión."""
    if not marca.visual:
        return []
    if conservar_fabrica and marca.datos:
        # Los datos van en el hueco del logo de fábrica: si el logo se conserva, no hay sitio.
        return ["marca: con el logo de la fábrica conservado, los datos de empresa no tienen hueco (usa el pie)"]
    M = T["marca"]
    if M is None:
        return [f"marca: la plantilla {T['id']} no define zonas para la marca del montador"]
    err = []
    if conservar_fabrica and marca.logo and not M["logo_con_fabrica"]:
        err.append("marca: con la cabecera del fabricante conservada, este formato no tiene hueco para tu logo")
    if marca.logo is not None:
        dim = dimensiones_imagen(marca.logo)
        if dim is None:
            err.append("marca: el logo tiene que ser PNG o JPEG")
        elif not (0 < dim[0] <= MAX_LADO_LOGO and 0 < dim[1] <= MAX_LADO_LOGO):
            err.append(f"marca: el logo mide {dim[0]}×{dim[1]} px (máximo {MAX_LADO_LOGO} por lado)")
        else:
            try:
                logo_pixmap(marca.logo)
            except (RuntimeError, ValueError):
                err.append("marca: el logo no se puede leer")
    for nombre, lineas, zona, tams in (("datos", marca.datos, M["zona_datos"], TAM_DATOS),
                                       ("pie", marca.pie, M["zona_pie"], TAM_PIE)):
        if not lineas:
            continue
        raros = sorted({c for l in lineas for c in l if not FONT.has_glyph(ord(c))})
        if raros:
            err.append(f"marca: {nombre} con caracteres no admitidos {raros}")
        elif tam_texto(lineas, zona, tams) is None:
            err.append(f"marca: el texto de {nombre} no cabe; acórtalo")
    return err


@dataclass
class Concepto:
    """Línea que añade el montador (instalación, portes, retirada…): precio final, sin %."""
    texto: str
    importe: Decimal


TITULO_CONCEPTOS = "Otros conceptos"
PAD_CONCEPTOS = 5.0  # pt de margen interior arriba y abajo del bloque
INTERLINEA_CONCEPTOS = 1.6  # paso entre filas, en veces el tamaño de letra de los totales
HUECO_CONCEPTOS = 6.0  # pt entre el bloque nuevo y el recuadro de totales
SANGRIA = 12.0  # pt desde el borde izquierdo del recuadro


@dataclass
class BloqueConceptos:
    """Dónde va cada cosa al añadir conceptos: el bloque nuevo ocupa el sitio del recuadro de totales y
    este baja `desplazamiento` pt (redibujado igual, con los totales en las mismas posiciones relativas)."""
    pagina: int
    caja: fitz.Rect  # recuadro de totales original
    bloque: fitz.Rect  # recuadro nuevo de conceptos
    desplazamiento: float
    size: float
    color: int  # del texto (el de los totales)
    trazo: tuple
    ancho_trazo: float
    x_importe: float  # borde derecho de importes (el de los totales)
    y_titulo: float
    filas: list[tuple[str, str, float]]  # texto, importe "1.234,56 €", línea base
    # Si bajo los totales hay contenido (p. ej. las condiciones), esta franja baja también `desplazamiento`.
    mover: fitz.Rect | None = None

    @property
    def caja_nueva(self) -> fitz.Rect:
        return self.caja + (0, self.desplazamiento, 0, self.desplazamiento)


def _parecido(a: fitz.Rect, b: fitz.Rect, tol: float = 3) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def plan_conceptos(pdf: bytes, ex: Extraccion, T: dict, conceptos: list[Concepto]
                   ) -> tuple[BloqueConceptos | None, list[str]]:
    """Comprueba que los conceptos caben y calcula su sitio. Los errores llevan el prefijo "conceptos:":
    son de lo que pide el montador (demasiadas líneas, texto largo), no del PDF."""
    if not conceptos:
        return None, []
    C = T["conceptos"]
    if C is None:
        return None, ["conceptos: este formato de presupuesto no admite líneas añadidas"]
    if len(conceptos) > C["max_lineas"]:
        return None, [f"conceptos: como mucho {C['max_lineas']} líneas añadidas"]
    ancla = get_total(ex, C["ancla"])
    if ancla is None or any(t.pagina != ancla.pagina for t in ex.totales):
        return None, ["conceptos: los totales no están juntos en una página"]
    c0, c1, c2, c3 = C["caja"]
    caja = fitz.Rect(ancla.rect[2] + c0, ancla.rect[1] + c1, ancla.rect[2] + c2, ancla.rect[1] + c3)
    size = ancla.size
    paso = size * INTERLINEA_CONCEPTOS
    bloque = fitz.Rect(caja.x0, caja.y0, caja.x1, caja.y0 + 2 * PAD_CONCEPTOS + paso * (len(conceptos) + 1))
    desplazamiento = bloque.height + HUECO_CONCEPTOS
    x_importe = ancla.rect[2]
    y_titulo = bloque.y0 + PAD_CONCEPTOS + size * 1.1
    filas = [(c.texto, f"{fmt_eur(c.importe)} €", y_titulo + paso * (i + 1)) for i, c in enumerate(conceptos)]

    err = []
    for texto, importe, _ in filas:
        raros = sorted({ch for ch in texto if not FONT.has_glyph(ord(ch))})
        hueco = (x_importe - FONT.text_length(importe, fontsize=size) - 16) - (caja.x0 + SANGRIA * 2)
        if not texto.strip():
            err.append("conceptos: hay una línea sin texto")
        elif raros:
            err.append(f"conceptos: «{texto[:30]}» lleva caracteres no admitidos {raros}")
        elif FONT.text_length(texto, fontsize=size) > hueco:
            err.append(f"conceptos: «{texto[:30]}…» es demasiado largo; acórtalo")
    if err:
        return None, err

    with fitz.open(stream=pdf, filetype="pdf") as doc:
        page = doc[ancla.pagina]
        dibujos = page.get_drawings()
        recuadro = next((d for d in dibujos if _parecido(d["rect"], caja)), None)
        if recuadro is None:
            return None, ["conceptos: no se encuentra el recuadro de totales en el PDF"]
        # En el recuadro solo pueden estar los totales: se vacía entero y se redibuja más abajo.
        totales = [t for t in ex.totales if t.pagina == ancla.pagina]
        def lineas(texto: str) -> list[str]:
            return sorted(_normal(l) for l in texto.splitlines() if l.strip())
        if not all(pad(tuple(caja), 1).contains(fitz.Rect(t.rect)) for t in totales) or \
                lineas(page.get_text(clip=caja)) != lineas("\n".join(
                    page.get_text(clip=fitz.Rect(t.rect)) for t in totales)):
            return None, ["conceptos: el recuadro de totales tiene algo más que los totales"]
        limite = page.rect.height - C["margen_inferior"]
        # Lo que hay bajo el recuadro de totales hasta el límite inferior (el pie queda fuera).
        debajo = fitz.Rect(0, caja.y1 + 1, page.rect.width, limite)
        cosas = [fitz.Rect(b[:4]) for b in page.get_text("blocks") if b[4].strip()] + \
            [fitz.Rect(i["bbox"]) for i in page.get_image_info()] + \
            [fitz.Rect(d["rect"]) for d in dibujos if d is not recuadro]
        cosas = [c for c in cosas if c.intersects(debajo)]
        mover = None
        if any(c.y0 < caja.y1 + 1 for c in cosas):
            ocupado = True  # algo empieza a la altura del recuadro: no se puede bajar sin partirlo
        elif cosas:
            # Hay contenido debajo: baja con el recuadro si sigue cabiendo antes del pie.
            fondo_contenido = max(c.y1 for c in cosas)
            ocupado = fondo_contenido + desplazamiento > limite
            mover = None if ocupado else fitz.Rect(0, caja.y1 + 1, page.rect.width, fondo_contenido + 1)
        else:
            ocupado = caja.y1 + desplazamiento > limite
        if ocupado:
            return None, [f"conceptos: no caben {len(conceptos)} líneas añadidas en la página de los totales; "
                          "quita alguna o júntalas"]
    trazo = recuadro.get("color") or (0, 0, 0)
    return BloqueConceptos(ancla.pagina, caja, bloque, desplazamiento, size, ancla.color, tuple(trazo),
                           recuadro.get("width") or 1.0, x_importe, y_titulo, filas, mover), []


def mover_contenido(doc: fitz.Document, page: fitz.Page, zona: fitz.Rect, dy: float) -> None:
    """Baja `dy` pt lo que hay en `zona` (ya sin datos de fábrica: después de la primera redacción).
    Se copia la página, se borra de verdad todo lo de fuera de la zona en la copia y se pega más abajo:
    el PDF no guarda nada oculto fuera del recorte."""
    tmp = fitz.open()
    tmp.insert_pdf(doc, from_page=page.number, to_page=page.number)
    tp = tmp[0]
    w, h = tp.rect.width, tp.rect.height
    for r in (fitz.Rect(0, 0, w, zona.y0), fitz.Rect(0, zona.y1, w, h),
              fitz.Rect(0, zona.y0, zona.x0, zona.y1), fitz.Rect(zona.x1, zona.y0, w, zona.y1)):
        if not r.is_empty:
            tp.add_redact_annot(r, fill=False)
    tp.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE, graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                        text=fitz.PDF_REDACT_TEXT_REMOVE)
    page.add_redact_annot(zona, fill=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE, graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    page.show_pdf_page(zona + (0, dy, 0, dy), tmp, 0, clip=zona)
    tmp.close()


def stamp_conceptos(page: fitz.Page, b: BloqueConceptos) -> None:
    """Vacía el recuadro de totales (trazos incluidos), dibuja el bloque de conceptos y el recuadro movido.
    Los totales se estampan aparte, desplazados."""
    page.add_redact_annot(pad(tuple(b.caja), 2), fill=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    page.insert_font(fontname=FONT_NAME, fontbuffer=FONT.buffer)
    for r in (b.bloque, b.caja_nueva):
        page.draw_rect(r, color=b.trazo, width=b.ancho_trazo)
    rgb = tuple(((b.color >> s) & 0xFF) / 255 for s in (16, 8, 0))
    page.insert_text((b.caja.x0 + SANGRIA, b.y_titulo), TITULO_CONCEPTOS, fontname=FONT_NAME,
                     fontsize=b.size, color=rgb)
    for texto, importe, y in b.filas:
        page.insert_text((b.caja.x0 + SANGRIA * 2, y), texto, fontname=FONT_NAME, fontsize=b.size, color=rgb)
        stamp_right(page, importe, b.x_importe, y, b.size, b.color)


def pie_de_datos(datos: list[str], zona: tuple) -> list[str]:
    """Datos de empresa como pie: en una línea si caben; si no, repartidos en dos."""
    una = [" · ".join(datos)]
    if len(datos) < 2 or tam_texto(una, zona, TAM_PIE) is not None:
        return una
    corte = min(range(1, len(datos)), key=lambda i: abs(len(" · ".join(datos[:i])) - len(" · ".join(datos[i:]))))
    return [" · ".join(datos[:corte]), " · ".join(datos[corte:])]


def marca_efectiva(marca: Marca, T: dict, conservar_fabrica: bool) -> Marca:
    """Aplica pie_por_defecto: si la plantilla lo pide y el montador no tiene pie, sus datos van al pie."""
    M = T["marca"]
    if conservar_fabrica or marca.pie or not marca.datos or M is None or M["pie_por_defecto"] != "datos":
        return marca
    return Marca(marca.logo, marca.datos, pie_de_datos(marca.datos, M["zona_pie"]), marca.condiciones)


INTERLINEA_CONDICIONES = 1.4


@dataclass
class PlanCondiciones:
    """Dónde va el texto de condiciones del montador: la zona se vacía y se escribe línea a línea."""
    pagina: int
    zona: fitz.Rect
    lineas: list[str]  # "" = línea en blanco (separación entre párrafos)
    size: float
    borrar: fitz.Rect | None = None  # zona original (si los conceptos la bajan, se borra esta y se escribe en zona)


def limpiar_condiciones(texto: str, T: dict, conservar_fabrica: bool) -> str:
    """Normaliza espacios y, salvo que se conserve la fábrica, quita su nombre (como en el texto original)."""
    if not conservar_fabrica:
        for palabra in sorted(T["lista_negra"], key=len, reverse=True):
            texto = re.sub(re.escape(palabra), "", texto, flags=re.I)
    lineas = [" ".join(l.split()) for l in texto.replace("\r\n", "\n").split("\n")]
    while lineas and not lineas[-1]:
        lineas.pop()
    while lineas and not lineas[0]:
        lineas.pop(0)
    return "\n".join(lineas)


def envolver(texto: str, ancho: float, size: float) -> list[str]:
    """Cada línea del texto, partida en palabras para que quepa en el ancho; las vacías se mantienen."""
    out: list[str] = []
    for parrafo in texto.split("\n"):
        linea = ""
        for palabra in parrafo.split():
            prueba = f"{linea} {palabra}".strip()
            if linea and FONT_REG.text_length(prueba, fontsize=size) > ancho:
                out.append(linea)
                linea = palabra
            else:
                linea = prueba
        out.append(linea)
    return out


def plan_condiciones(pdf: bytes, T: dict, marca: Marca, conservar_fabrica: bool,
                     bc: "BloqueConceptos | None" = None) -> tuple[PlanCondiciones | None, list[str]]:
    """Solo si el montador tiene condiciones y el PDF trae las de la fábrica (donde no, no se añade nada)."""
    C = T["condiciones"]
    if not marca.condiciones or C is None:
        return None, []
    texto = limpiar_condiciones(marca.condiciones, T, conservar_fabrica)
    if not texto:
        return None, []
    raros = sorted({c for c in texto if c != "\n" and not FONT_REG.has_glyph(ord(c))})
    if raros:
        return None, [f"marca: las condiciones llevan caracteres no admitidos {raros}"]
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        for page in reversed(doc):
            hits = page.search_for(C["ancla"])
            if hits:
                a = hits[0]
                z0, z1, z2, z3 = C["zona"]
                zona = fitz.Rect(a.x0 + z0, a.y0 + z1, a.x0 + z2, a.y0 + z3) & page.rect
                n = page.number
                borrar = fitz.Rect(zona)
                if bc is not None and bc.mover is not None and bc.pagina == n and zona.intersects(bc.mover):
                    zona.y0 += bc.desplazamiento  # los conceptos bajan lo de debajo de los totales
                break
        else:
            return None, []
    t = C["tam"][0]
    while t >= C["tam"][1]:
        lineas = envolver(texto, zona.width, t)
        if len(lineas) * t * INTERLINEA_CONDICIONES <= zona.height:
            return PlanCondiciones(n, zona, lineas, t, borrar), []
        t -= 0.25
    return None, ["marca: las condiciones no caben en la página; acórtalas"]


def stamp_condiciones(page: fitz.Page, pc: PlanCondiciones) -> None:
    page.insert_font(fontname=FONT_REG_NAME, fontbuffer=FONT_REG.buffer)
    y = pc.zona.y0 + pc.size
    for linea in pc.lineas:
        if linea:
            page.insert_text((pc.zona.x0, y), linea, fontname=FONT_REG_NAME, fontsize=pc.size, color=(0, 0, 0))
        y += pc.size * INTERLINEA_CONDICIONES


def precio_cliente(coste: Decimal, pct: Decimal, modo: str) -> Decimal:
    p = pct / 100
    v = coste * (1 + p) if modo == "markup" else coste / (1 - p)
    return v.quantize(CENT, ROUND_HALF_UP)


def fmt_pct(v: Decimal, como: str | None = None) -> str:
    """% de IVA con los decimales del original ("21" / "21,0"); sin original, sin ceros sobrantes."""
    if como and "," in como:
        return f"{v:.{len(como.split(',')[1])}f}".replace(".", ",")
    n = v.normalize()
    return str(int(n)) if n == n.to_integral() else f"{n:f}".replace(".", ",")


def calcular(ex: Extraccion, pct: Decimal, modo: str, montaje: Decimal | None,
             pct_lineas: dict[int, Decimal], conceptos: list[Concepto] | None = None,
             iva_pct: Decimal | None = None) -> dict:
    """Ventanas: coste de fábrica + % (global o de la línea). Montaje: precio del montador, sin %.

    Conceptos añadidos por el montador: precio final, sin %, suman a la base."""
    lineas = []
    for i, ln in enumerate(ex.lineas):
        if ln.tipo == "descuento":
            p, unit = None, Decimal("0.00")  # oculto al cliente: solo baja el coste del montador
        elif ln.tipo == "montaje":
            p = None
            unit = montaje if montaje is not None else parse_eur(ln.importe)
        else:
            p = pct_lineas.get(i, pct)
            unit = precio_cliente(parse_eur(ln.importe), p, modo)
        lineas.append({"ref": ln.ref, "tipo": ln.tipo, "uds": ln.uds, "pct": p,
                       "importe": unit, "total": unit * ln.uds})
    conceptos = conceptos or []
    extra = sum((c.importe for c in conceptos), Decimal(0))
    base = sum((l["total"] for l in lineas), Decimal(0)) + extra
    # % de IVA: el que elija el montador o, si no, el del PDF.
    iva_pct = iva_pct if iva_pct is not None else parse_pct(get_total(ex, "IVA").iva_pct)
    iva = (base * iva_pct / 100).quantize(CENT, ROUND_HALF_UP)
    return {"lineas": lineas, "subtotales": subtotales_esperados(ex, [l["total"] for l in lineas]),
            "conceptos": [{"texto": c.texto, "importe": c.importe} for c in conceptos],
            "extra": extra, "base": base, "iva_pct": iva_pct, "iva": iva, "total": base + iva}


def stamp_right(page: fitz.Page, text: str, x1: float, y: float, size: float, color: int) -> None:
    w = FONT.text_length(text, fontsize=size)
    rgb = tuple(((color >> s) & 0xFF) / 255 for s in (16, 8, 0))
    page.insert_text((x1 - w, y), text, fontname=FONT_NAME, fontsize=size, color=rgb)


def pad(r: tuple, d: float = 0.5) -> fitz.Rect:
    return fitz.Rect(r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def stamp_bloque(page: fitz.Page, lineas: list[str], zona: tuple, tams: tuple[float, float],
                 derecha: bool) -> None:
    """Líneas centradas en vertical en la zona, alineadas a la izquierda o a la derecha."""
    z = fitz.Rect(zona)
    t = tam_texto(lineas, zona, tams)
    assert t is not None  # validate_marca ya lo comprobó
    paso = t * INTERLINEA
    y = z.y0 + (z.height - paso * len(lineas)) / 2 + t
    for linea in lineas:
        if derecha:
            stamp_right(page, linea, z.x1, y, t, 0)
        else:
            page.insert_text((z.x0, y), linea, fontname=FONT_NAME, fontsize=t, color=(0, 0, 0))
        y += paso


def zonas_marca(marca: Marca | None, T: dict, n: int) -> list[tuple]:
    """Zonas de la página n que se vacían para la marca del montador."""
    if marca is None or marca.vacia:
        return []
    M = T["marca"]
    zonas = []
    if marca.logo and n == M["logo_pagina"]:
        zonas.append(M["zona_logo"])
    if marca.datos and n == M["datos_pagina"]:
        zonas.append(M["zona_datos"])
    if marca.pie:
        zonas.append(M["zona_pie"])
    return zonas


def redact_and_stamp(pdf: bytes, ex: Extraccion, calc: dict, T: dict, marca: Marca | None = None,
                     conservar_fabrica: bool = False, bc: BloqueConceptos | None = None,
                     pc: PlanCondiciones | None = None) -> bytes:
    doc = fitz.open(stream=pdf, filetype="pdf")
    labels = {"Base Imponible": "Base Imponible", "Total": "Total", "TOTAL": "TOTAL"}
    for page in doc:
        n = page.number
        for z in zonas_marca(marca, T, n):
            page.add_redact_annot(fitz.Rect(z), fill=False)
        if pc is not None and pc.pagina == n:
            page.add_redact_annot(pc.zona | (pc.borrar or pc.zona), fill=False)
        # Precios y totales: redacción sin relleno para conservar el fondo de las filas.
        for ln in (l for l in ex.lineas if l.pagina == n):
            page.add_redact_annot(pad(ln.rect_importe), fill=False)
            page.add_redact_annot(pad(ln.rect_total), fill=False)
            if ln.caja is not None:
                page.add_redact_annot(fitz.Rect(ln.caja), fill=False)
        for t in (t for t in ex.totales if t.pagina == n):
            page.add_redact_annot(pad(t.rect), fill=False)
            if t.pct_rect is not None:
                page.add_redact_annot(pad(t.pct_rect), fill=False)
        for st in (st for st in ex.subtotales if st.pagina == n):
            page.add_redact_annot(pad(st.rect), fill=False)
        # Datos de fábrica (salvo que el montador elija conservar su logo y su nombre).
        if not conservar_fabrica:
            if n == T["logo_pagina"]:
                page.add_redact_annot(fitz.Rect(T["zona_logo_fabrica"]), fill=False)
            for z in T["zonas_fabrica_todas"]:
                page.add_redact_annot(fitz.Rect(z), fill=False)
            for palabra in T["lista_negra"]:
                for r in page.search_for(palabra):
                    page.add_redact_annot(pad(tuple(r)), fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        cajas = [l.caja for l in ex.lineas if l.pagina == n and l.caja is not None]
        if cajas:
            # Segunda pasada solo con los recuadros de descuento: fuera también sus bordes y su barra.
            for c in cajas:
                page.add_redact_annot(pad(c, 2), fill=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)
        page.insert_font(fontname=FONT_NAME, fontbuffer=FONT.buffer)
        dy = 0.0
        if bc is not None and bc.pagina == n and bc.mover is not None:
            mover_contenido(doc, page, bc.mover, bc.desplazamiento)
            page.insert_font(fontname=FONT_NAME, fontbuffer=FONT.buffer)
        if pc is not None and pc.pagina == n:
            stamp_condiciones(page, pc)
        if bc is not None and bc.pagina == n:
            stamp_conceptos(page, bc)
            dy = bc.desplazamiento

        for ln, nuevo in zip(ex.lineas, calc["lineas"]):
            if ln.pagina != n or ln.tipo == "descuento":
                continue
            # Modo tabla: en la columna de precio, el precio por unidad de medida (total / cantidad).
            unit = nuevo["importe"] if ln.cantidad is None else (nuevo["total"] / ln.cantidad).quantize(CENT, ROUND_HALF_UP)
            stamp_right(page, fmt_eur(unit), ln.rect_importe[2], ln.origin_importe[1], ln.size, ln.color)
            stamp_right(page, fmt_eur(nuevo["total"]), ln.rect_total[2], ln.origin_total[1], ln.size, ln.color)
        for st, valor in zip(ex.subtotales, calc["subtotales"]):
            if st.pagina == n:
                stamp_right(page, fmt_eur(valor), st.rect[2], st.origin[1], st.size, st.color)
        for t in (t for t in ex.totales if t.pagina == n):
            if t.solo_valor:
                valor = {"IVA": calc["iva"], "TOTAL": calc["total"]}.get(t.label, calc["base"])
                stamp_right(page, fmt_eur(valor), t.rect[2], t.origin[1] + dy, t.size, t.color)
                if t.pct_rect is not None:
                    stamp_right(page, fmt_pct(calc["iva_pct"], t.iva_pct), t.pct_rect[2], t.pct_origin[1] + dy,
                                t.size, t.color)
                continue
            if t.label == "IVA":
                txt = f"I.V.A {fmt_pct(calc['iva_pct'], t.iva_pct)} % : {fmt_eur(calc['iva'])} €"
            else:
                valor = calc["total"] if t.label == "TOTAL" else calc["base"]
                txt = f"{labels[t.label]}: {fmt_eur(valor)} €"
            stamp_right(page, txt, t.rect[2], t.origin[1] + dy, t.size, t.color)

        if marca is not None and not marca.vacia:
            M = T["marca"]
            if marca.logo and n == M["logo_pagina"]:
                pix = logo_pixmap(marca.logo)
                page.insert_image(rect_logo(pix.width, pix.height, M["zona_logo"], M["logo_derecha"]), pixmap=pix)
            if marca.datos and n == M["datos_pagina"]:
                stamp_bloque(page, marca.datos, M["zona_datos"], TAM_DATOS, derecha=True)
            if marca.pie:
                stamp_bloque(page, marca.pie, M["zona_pie"], TAM_PIE, derecha=False)

    # Limpieza de todo lo que no sea contenido visible.
    doc.set_metadata({})
    doc.del_xml_metadata()
    doc.set_toc([])
    for name in doc.embfile_names():
        doc.embfile_del(name)
    for page in doc:
        for a in list(page.annots()):
            page.delete_annot(a)
        for w in list(page.widgets()):
            page.delete_widget(w)
    # tobytes siempre reescribe el archivo entero (nunca incremental).
    out = doc.tobytes(garbage=4, clean=True, deflate=True)
    doc.close()
    return out


def money_regex(valor: str) -> re.Pattern:
    """Coincide con el importe en cualquier formato habitual, sin ser parte de otro número."""
    d = parse_eur(valor)
    plano = f"{d:.2f}"
    variantes = {valor, plano.replace(".", ","), plano, f"{d:,.2f}"}
    alt = "|".join(re.escape(v) for v in variantes)
    return re.compile(rf"(?<![\d.,])(?:{alt})(?![\d,]|\.\d)")


def _normal(s: str) -> str:
    return " ".join(s.split())


def verify_marca(doc: fitz.Document, marca: Marca, T: dict) -> list[str]:
    """La marca está donde debe: un único logo (el nuevo) en su zona y cada línea de texto en su página."""
    if marca.vacia:
        return []
    M, err = T["marca"], []
    if marca.logo:
        zona = fitz.Rect(M["zona_logo"])
        imgs = [fitz.Rect(i["bbox"]) for i in doc[M["logo_pagina"]].get_image_info()
                if fitz.Rect(i["bbox"]).intersects(zona)]
        if len(imgs) != 1 or not pad(tuple(zona), 1).contains(imgs[0]):
            err.append(f"zona del logo del montador con {len(imgs)} imágenes (se esperaba solo el nuevo)")
    textos = [_normal(p.get_text()) for p in doc]
    for linea in marca.datos:
        if _normal(linea) not in textos[M["datos_pagina"]]:
            err.append("datos del montador no estampados")
            break
    for i, texto in enumerate(textos):
        if any(_normal(l) not in texto for l in marca.pie):
            err.append(f"p{i + 1}: pie del montador no estampado")
    return err


def verify_zonas_fabrica(doc: fitz.Document, T: dict, marca: Marca | None) -> list[str]:
    """Zonas de fábrica vacías: en la del logo solo puede quedar el logo del montador (si comparten
    hueco) y en las de todas las páginas solo el pie del montador (si comparten hueco)."""
    err = []
    marca = marca or Marca()
    zona_logo = fitz.Rect(T["zona_logo_fabrica"])
    propia = pad(T["marca"]["zona_logo"], 1) if marca.logo and T["marca"] else None
    if any(fitz.Rect(i["bbox"]).intersects(zona_logo) and not (propia and propia.contains(fitz.Rect(i["bbox"])))
           for i in doc[T["logo_pagina"]].get_image_info()):
        err.append("logo de fábrica sigue en la cabecera")
    M = T["marca"]
    for page in doc:
        n = page.number
        # Lo del montador que puede compartir hueco con la fábrica en esta página.
        propias = list(marca.pie) + (list(marca.datos) if M and n == M["datos_pagina"] else [])
        logo_propio = pad(M["zona_logo"], 1) if M and marca.logo and n == M["logo_pagina"] else None
        for z in T["zonas_fabrica_todas"]:
            r = fitz.Rect(z)
            resto = _normal(page.get_text(clip=r))
            for linea in propias:
                resto = resto.replace(_normal(linea), "", 1)
            if resto.strip():
                err.append(f"p{n + 1}: queda texto de fábrica en una zona de fábrica")
            if any(fitz.Rect(i["bbox"]).intersects(r) and not (logo_propio and logo_propio.contains(fitz.Rect(i["bbox"])))
                   for i in page.get_image_info()):
                err.append(f"p{n + 1}: queda una imagen en una zona de fábrica")
    return err


def importes_estampados(ex: Extraccion, calc: dict) -> list[str]:
    """Todo importe que escribe Refactura en el PDF (para no confundirlo con un precio de fábrica)."""
    out = []
    for ln, c in zip(ex.lineas, calc["lineas"]):
        if ln.tipo == "descuento":
            continue
        unit = c["importe"] if ln.cantidad is None else (c["total"] / ln.cantidad).quantize(CENT, ROUND_HALF_UP)
        out += [fmt_eur(unit), fmt_eur(c["total"])]
    out += [fmt_eur(v) for v in calc["subtotales"]]
    out += [fmt_eur(calc[k]) for k in ("base", "base", "iva", "total")]  # Base Imponible, Total, IVA, TOTAL
    out += [fmt_eur(c["importe"]) for c in calc["conceptos"]]
    return out


def verify(src_ex: Extraccion, calc: dict, pdf: bytes, T: dict, marca: Marca | None = None,
           conservar_fabrica: bool = False, bc: BloqueConceptos | None = None,
           pc: PlanCondiciones | None = None, texto_original: str | None = None) -> tuple[list[str], list[bytes]]:
    """Verificación de salida obligatoria (sección 'Redacción real y verificación de salida').

    Devuelve los errores y una miniatura PNG por página para la pantalla de descarga.
    """
    err = []
    doc = fitz.open(stream=pdf, filetype="pdf")
    texto = "\n".join(p.get_text() for p in doc)
    if bc is not None:
        pagina = doc[bc.pagina]
        en_bloque = _normal(pagina.get_text(clip=bc.bloque))
        for linea in [TITULO_CONCEPTOS] + [t for f in bc.filas for t in f[:2]]:
            if _normal(linea) not in en_bloque:
                err.append("conceptos añadidos no estampados")
                break
        # Los importes del montador pueden coincidir con un precio de fábrica: fuera del bloque se buscan.
        texto = "\n".join(p.get_text() if p.number != bc.pagina else " ".join(
            w[4] for w in p.get_text("words") if not fitz.Rect(w[:4]).intersects(bc.bloque)) for p in doc)

    # El montaje es precio del montador, no de fábrica: puede seguir apareciendo.
    fabrica = [ln for ln in src_ex.lineas if ln.tipo != "montaje"]
    originales = {ln.importe for ln in fabrica} | {ln.total for ln in fabrica} \
        | {ln.precio for ln in fabrica if ln.precio} | {t.valor for t in src_ex.totales} \
        | {st.valor for st in src_ex.subtotales}
    # Un importe nuevo puede coincidir por casualidad con un precio de fábrica (p. ej. un precio por metro
    # recalculado igual al de otra línea). Se cuentan las apariciones: las justificadas son las que escribe
    # Refactura y las que el original ya tenía fuera de sus casillas de precio (metros, medidas…).
    campos: dict[tuple, str] = {}
    for ln in fabrica:
        campos[tuple(ln.rect_total)] = ln.total
        campos[tuple(ln.rect_importe)] = ln.precio if ln.cantidad is not None else ln.importe
    campos |= {tuple(t.rect): t.valor for t in src_ex.totales} | {tuple(st.rect): st.valor for st in src_ex.subtotales}
    escritos = " ".join(importes_estampados(src_ex, calc))
    for v in sorted(originales):
        rx = money_regex(v)
        if not rx.search(texto):
            continue
        en_campos = sum(1 for c in campos.values() if parse_eur(c) == parse_eur(v))
        fuera_de_campos = max(0, len(rx.findall(texto_original)) - en_campos) if texto_original is not None else 0
        if len(rx.findall(texto)) > len(rx.findall(escritos)) + fuera_de_campos:
            err.append(f"precio de fábrica {v} sigue en el texto")

    bajo = texto.lower()
    for palabra in ([] if conservar_fabrica else T["lista_negra"]):
        if palabra.lower() in bajo:
            err.append(f"dato de fábrica '{palabra}' sigue en el texto")

    meta = {k: v for k, v in doc.metadata.items() if k not in ("format", "encryption") and v}
    if meta:
        err.append(f"metadatos no vacíos: {meta}")
    if doc.xref_xml_metadata():
        err.append("XMP presente")
    if doc.embfile_count():
        err.append("adjuntos presentes")
    if doc.get_ocgs():
        err.append("capas (OCG) presentes")
    if any(list(p.annots()) or list(p.widgets()) for p in doc):
        err.append("anotaciones o formularios presentes")
    if not conservar_fabrica:
        err += verify_zonas_fabrica(doc, T, marca)
    if marca is not None:
        err += verify_marca(doc, marca, T)
    if pc is not None:
        # En la zona solo pueden quedar las condiciones del montador, enteras y en orden.
        esperado = _normal(" ".join(l for l in pc.lineas if l))
        if _normal(doc[pc.pagina].get_text(clip=pc.zona)) != esperado:
            err.append("condiciones del montador no estampadas o con restos del texto original")

    out_ex = extract(doc, T)
    visibles = [c for ln, c in zip(src_ex.lineas, calc["lineas"]) if ln.tipo != "descuento"]
    for ln in (l for l in src_ex.lineas if l.tipo == "descuento"):
        if _normal(ln.ref) and _normal(ln.ref) in _normal(doc[ln.pagina].get_text()):
            err.append(f"p{ln.pagina + 1}: el descuento de fábrica sigue en el PDF")
    if len(out_ex.lineas) != len(visibles):
        err.append(f"nº de líneas en salida {len(out_ex.lineas)} ≠ {len(visibles)}")
    for got, exp in zip(out_ex.lineas, visibles):
        if (parse_eur(got.total) - exp["total"]).copy_abs() > TOL or \
                (parse_eur(got.importe) - exp["importe"]).copy_abs() > TOL:
            err.append(f"{exp['ref']}: estampado {got.importe}/{got.total} ≠ calculado")
    esperados = {"Base Imponible": calc["base"], "IVA": calc["iva"], "TOTAL": calc["total"]}
    if get_total(src_ex, "Total"):
        esperados["Total"] = calc["base"]
    for label, exp in esperados.items():
        t = get_total(out_ex, label)
        if not t or (parse_eur(t.valor) - exp).copy_abs() > TOL:
            err.append(f"total '{label}' estampado {t.valor if t else '—'} ≠ {fmt_eur(exp)}")
    iva_out = get_total(out_ex, "IVA")
    if iva_out is None or iva_out.iva_pct is None or parse_pct(iva_out.iva_pct) != calc["iva_pct"]:
        err.append("% de IVA estampado ≠ el elegido")
    suma = sum((parse_eur(l.total) for l in out_ex.lineas), Decimal(0)) + calc["extra"]
    if (suma - calc["base"]).copy_abs() > TOL:
        err.append("suma de líneas estampadas ≠ base estampada")
    if len(out_ex.subtotales) != len(src_ex.subtotales) or any(
            (parse_eur(st.valor) - e).copy_abs() > TOL for st, e in zip(out_ex.subtotales, calc["subtotales"])):
        err.append("subtotales estampados ≠ calculados")
    for got, exp in zip(out_ex.lineas, visibles):
        if got.cantidad is not None and \
                ((got.cantidad * parse_eur(got.precio)) - exp["total"]).copy_abs() > got.cantidad * Decimal("0.005") + TOL:
            err.append(f"{exp['ref'][:40]}: precio por unidad estampado no cuadra con el total")
    if out_ex.n_dibujos != src_ex.n_dibujos:
        err.append(f"nº de dibujos en salida {out_ex.n_dibujos} ≠ original {src_ex.n_dibujos}")

    miniaturas = [p.get_pixmap(dpi=50).tobytes("png") for p in doc]
    doc.close()
    return err, miniaturas


def _abrir(pdf: bytes) -> tuple[fitz.Document | None, list[str]]:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except (fitz.FileDataError, RuntimeError, ValueError):
        return None, ["PDF no legible"]
    if doc.needs_pass:
        doc.close()
        return None, ["PDF cifrado"]
    if doc.page_count == 0 or doc.page_count > MAX_PAGINAS:
        n = doc.page_count
        doc.close()
        return None, [f"nº de páginas no admitido ({n}; máximo {MAX_PAGINAS})"]
    return doc, []


def _analisis(pdf: bytes, T: dict) -> tuple[Extraccion, list[str]]:
    """Huella + extracción + validación. Con la huella fallida no se sigue: el formato es otro."""
    doc, err = _abrir(pdf)
    if doc is None:
        return Extraccion(), err
    with doc:
        huella = check_huella(doc, T)
        if huella:
            return Extraccion(), huella
        ex = extract(doc, T)
    return ex, validate(ex)


DPI_DIBUJO = 144  # nítido en móvil con zoom; ~10–20 KB por dibujo


def recortar_dibujos(pdf: bytes, ex: Extraccion) -> list[bytes | None]:
    """PNG de la zona de dibujo de cada línea, para que el montador reconozca cada ventana al revisar."""
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        return [doc[ln.pagina if ln.pagina_dibujo is None else ln.pagina_dibujo]
                .get_pixmap(clip=fitz.Rect(ln.dibujo), dpi=DPI_DIBUJO).tobytes("png")
                if ln.dibujo else None for ln in ex.lineas]


def _lineas_publicas(ex: Extraccion, calc: dict | None) -> list[dict]:
    """Líneas para la app: valores numéricos, sin coordenadas internas salvo el dibujo."""
    out = []
    for i, ln in enumerate(ex.lineas):
        d = {"indice": i, "pagina": ln.pagina + 1, "tipo": ln.tipo, "ref": ln.ref, "uds": ln.uds,
             "coste_importe": parse_eur(ln.importe), "coste_total": parse_eur(ln.total),
             "dibujo": list(ln.dibujo) if ln.dibujo else None}
        if calc:
            c = calc["lineas"][i]
            d |= {"pct": c["pct"], "importe": c["importe"], "total": c["total"]}
        out.append(d)
    return out


def _totales_fabrica(ex: Extraccion) -> dict | None:
    base, iva, total = (get_total(ex, k) for k in ("Base Imponible", "IVA", "TOTAL"))
    if not (base and iva and total):
        return None
    return {"base": parse_eur(base.valor), "iva_pct": parse_pct(iva.iva_pct),
            "iva": parse_eur(iva.valor), "total": parse_eur(total.valor)}


def analizar(pdf: bytes, T: dict) -> Resultado:
    """Paso de identificación/extracción/validación: lo que el montador revisa antes de generar."""
    ex, errores = _analisis(pdf, T)
    datos = {"plantilla": T["id"], "estado": "needs_review" if errores else "ok", "errores": errores,
             "n_dibujos": ex.n_dibujos, "lineas": _lineas_publicas(ex, None),
             "totales_fabrica": _totales_fabrica(ex) if not errores else None,
             # La plantilla sabe dónde añadir conceptos del montador (instalación, portes…).
             "admite_conceptos": T["conceptos"] is not None}
    return Resultado(datos, dibujos=[] if errores else recortar_dibujos(pdf, ex))


def generar(pdf: bytes, T: dict, pct: Decimal, modo: str = "markup", montaje: Decimal | None = None,
            pct_lineas: dict[int, Decimal] | None = None, marca: Marca | None = None,
            conservar_fabrica: bool = False, conceptos: list[Concepto] | None = None,
            iva_pct: Decimal | None = None) -> Resultado:
    """Analiza de nuevo (sin estado), calcula, redacta, estampa y verifica la salida.

    conservar_fabrica: deja el logo y el nombre de la fábrica (los precios se eliminan siempre).
    conceptos: líneas añadidas por el montador (sin %), en un bloque encima de los totales."""
    pct_lineas = pct_lineas or {}
    conceptos = list(conceptos or [])
    T = plantilla_efectiva(T, conservar_fabrica)
    marca = marca_efectiva(marca or Marca(), T, conservar_fabrica)
    ex, errores = _analisis(pdf, T)
    bc = pc = None
    if not errores and montaje is not None and not tiene_montaje(ex) and T["conceptos"] is not None:
        # Compatibilidad: montaje sin línea en el PDF = concepto "Instalación".
        conceptos, montaje = [Concepto("Instalación", montaje), *conceptos], None
    if not errores:
        errores = validate_params(ex, montaje, pct_lineas, T) + validate_marca(marca, T, conservar_fabrica)
    if not errores:
        bc, errores = plan_conceptos(pdf, ex, T, conceptos)
        pc, err_cond = plan_condiciones(pdf, T, marca, conservar_fabrica, bc)
        errores += err_cond
    datos = {"plantilla": T["id"], "pct": pct, "modo": modo, "montaje": montaje,
             "pct_lineas": {str(k): v for k, v in sorted(pct_lineas.items())},
             "marca": not marca.vacia, "conservar_fabrica": conservar_fabrica, "n_dibujos": ex.n_dibujos}
    calc = salida = None
    miniaturas: list[bytes] = []
    if not errores:
        calc = calcular(ex, pct, modo, montaje, pct_lineas, conceptos, iva_pct)
        salida = redact_and_stamp(pdf, ex, calc, T, marca, conservar_fabrica, bc, pc)
        with fitz.open(stream=pdf, filetype="pdf") as orig:
            texto_original = "\n".join(p.get_text() for p in orig)
        errores, miniaturas = verify(ex, calc, salida, T, marca, conservar_fabrica, bc, pc, texto_original)
    datos["lineas"] = _lineas_publicas(ex, calc)
    datos["totales"] = {k: calc[k] for k in ("base", "iva_pct", "iva", "total")} if calc else None
    datos["conceptos"] = calc["conceptos"] if calc else [{"texto": c.texto, "importe": c.importe} for c in conceptos]
    datos["estado"] = "needs_review" if errores else "ok"
    datos["errores"] = errores
    if errores:
        return Resultado(datos)  # sin verificación no hay entrega
    return Resultado(datos, salida, miniaturas)


def jsonable(o):
    """Importes como texto con 2 decimales y punto ("1300.00"): sin pérdidas por float."""
    if isinstance(o, Decimal):
        return f"{o:.2f}"
    raise TypeError(type(o))
