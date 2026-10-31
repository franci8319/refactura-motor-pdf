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


def compile_template(raw: dict, nombre: str = "plantilla") -> dict:
    """Valida el JSON de plantilla y compila sus regex. Falla pronto si falta una clave."""
    try:
        a, red, dib = raw["anclas"], raw["redaccion"], raw["dibujo"]
        logo = red["zona_logo_fabrica"]
        marca = raw.get("marca")
        return {
            "id": f"{raw['id']}-v{raw['version']}",
            "huella": raw["huella"],
            "ancla_linea": re.compile(a["linea"]),
            "ancla_pos": re.compile(a["pos"]),
            "ancla_montaje": re.compile(a["montaje"], re.I),
            "ancla_importe": a["col_importe"],
            "ancla_total_col": a["col_total"],
            "precio": re.compile(raw["precio"]),
            "totales": re.compile(raw["totales"]),
            # Dibujo de ventana: imagen que empieza justo bajo la cabecera "Pos." y a la izquierda.
            "dibujo_max_dy": dib["max_dy"],
            "dibujo_max_x0": dib["max_x0"],
            "logo_pagina": logo["pagina"] - 1,
            "zona_logo_fabrica": tuple(logo["rect"]),  # x0, y0, x1, y1
            "lista_negra": red["lista_negra"],
            # Opcional: dónde va la marca del montador (logo y datos en la pág. indicada, pie en todas).
            "marca": None if marca is None else {
                "logo_pagina": marca["zona_logo"]["pagina"] - 1,
                "zona_logo": tuple(marca["zona_logo"]["rect"]),
                "datos_pagina": marca["zona_datos"]["pagina"] - 1,
                "zona_datos": tuple(marca["zona_datos"]["rect"]),
                "zona_pie": tuple(marca["zona_pie"]["rect"]),
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
    tipo: str  # "ventana" | "montaje" | "otro"
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


@dataclass
class Extraccion:
    lineas: list[Linea] = field(default_factory=list)
    totales: list[Total] = field(default_factory=list)
    n_dibujos: int = 0
    errores: list[str] = field(default_factory=list)


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

    @property
    def vacia(self) -> bool:
        return not (self.logo or self.datos or self.pie)


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
    ex = Extraccion()
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
            precios = sorted((s for s in spans
                              if T["precio"].match(s.text) and abs(s.bbox[1] - fila.bbox[1]) < 2
                              and s.bbox[0] > fila.bbox[2]),
                             key=lambda s: s.bbox[0])
            if len(precios) != 2:
                ex.errores.append(f"p{page.number + 1}: fila UDS con {len(precios)} precios (se esperaban 2)")
                continue
            imp, tot = precios
            if col_importe and col_total and not (imp.bbox[2] < col_total.bbox[0] <= tot.bbox[2]):
                ex.errores.append(f"p{page.number + 1}: precios fuera de las columnas Importe/Total")

            pos = next((p for p in pos_libres if p.bbox[1] < fila.bbox[1]), None)
            if pos:
                pos_libres.remove(pos)
                ref = T["ancla_pos"].match(pos.text.strip()).group(1)
                tipo = "ventana"
                dibujo = next((d for d in dibujos if 0 <= d[1] - pos.bbox[3] <= T["dibujo_max_dy"]), None)
            else:
                ref = " ".join(s.text.strip() for s in spans
                               if y_prev < s.bbox[1] < fila.bbox[1] and not s.bold
                               and fila.bbox[0] - 200 < s.bbox[0] < fila.bbox[2] + 100)
                tipo = "montaje" if T["ancla_montaje"].search(ref) else "otro"
                dibujo = None
            ex.lineas.append(Linea(page.number, tipo, ref or "(sin descripción)", uds,
                                   imp.text.strip(), tot.text.strip(), imp.bbox, tot.bbox,
                                   imp.origin, tot.origin, imp.size, imp.color, dibujo))
            y_prev = fila.bbox[3]

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
        if ln.tipo == "otro":
            err.append(f"p{ln.pagina + 1}: línea no reconocida ({ln.ref[:40]}); decidir en la plantilla")
    if sum(1 for ln in ex.lineas if ln.tipo == "montaje") > 1:
        err.append("más de una línea de montaje")
    base, total, iva, total_iva = (get_total(ex, k) for k in ("Base Imponible", "Total", "IVA", "TOTAL"))
    if not all((base, total, iva, total_iva)):
        err.append("faltan totales (Base Imponible / Total / IVA / TOTAL)")
        return err
    suma = sum((parse_eur(ln.total) for ln in ex.lineas), Decimal(0))
    b = parse_eur(base.valor)
    if (suma - b).copy_abs() > TOL:
        err.append(f"suma de líneas {fmt_eur(suma)} ≠ base {base.valor}")
    if (parse_eur(total.valor) - b).copy_abs() > TOL:
        err.append("Total ≠ Base Imponible")
    pct = parse_pct(iva.iva_pct)
    if ((b * pct / 100).quantize(CENT, ROUND_HALF_UP) - parse_eur(iva.valor)).copy_abs() > TOL:
        err.append("IVA ≠ base × %")
    if (b + parse_eur(iva.valor) - parse_eur(total_iva.valor)).copy_abs() > TOL:
        err.append("TOTAL ≠ base + IVA")
    n_ventanas = sum(1 for ln in ex.lineas if ln.tipo == "ventana")
    if n_ventanas != ex.n_dibujos:
        err.append(f"nº de ventanas ({n_ventanas}) ≠ nº de dibujos ({ex.n_dibujos})")
    return err


def validate_params(ex: Extraccion, montaje: Decimal | None, pct_lineas: dict[int, Decimal]) -> list[str]:
    """Parámetros que solo se pueden comprobar contra las líneas extraídas."""
    err = []
    if montaje is not None and not any(ln.tipo == "montaje" for ln in ex.lineas):
        err.append("se indicó montaje pero el PDF no tiene línea de instalación donde escribirlo")
    for i in sorted(pct_lineas):
        if not 0 <= i < len(ex.lineas):
            err.append(f"pct_lineas: la línea {i} no existe")
        elif ex.lineas[i].tipo == "montaje":
            err.append(f"pct_lineas: la línea {i} es el montaje, que nunca lleva %")
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


def rect_logo(w: int, h: int, zona: tuple) -> fitz.Rect:
    """Logo ajustado a la zona conservando la proporción, alineado a la izquierda y centrado en vertical."""
    z = fitz.Rect(zona)
    k = min(z.width / w, z.height / h)
    y0 = z.y0 + (z.height - h * k) / 2
    return fitz.Rect(z.x0, y0, z.x0 + w * k, y0 + h * k)


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
    if marca.vacia:
        return []
    if conservar_fabrica and marca.datos:
        # Los datos van en el hueco del logo de fábrica: si el logo se conserva, no hay sitio.
        return ["marca: con el logo de la fábrica conservado, los datos de empresa no tienen hueco (usa el pie)"]
    M = T["marca"]
    if M is None:
        return [f"marca: la plantilla {T['id']} no define zonas para la marca del montador"]
    err = []
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


def precio_cliente(coste: Decimal, pct: Decimal, modo: str) -> Decimal:
    p = pct / 100
    v = coste * (1 + p) if modo == "markup" else coste / (1 - p)
    return v.quantize(CENT, ROUND_HALF_UP)


def calcular(ex: Extraccion, pct: Decimal, modo: str, montaje: Decimal | None,
             pct_lineas: dict[int, Decimal]) -> dict:
    """Ventanas: coste de fábrica + % (global o de la línea). Montaje: precio del montador, sin %."""
    lineas = []
    for i, ln in enumerate(ex.lineas):
        if ln.tipo == "montaje":
            p = None
            unit = montaje if montaje is not None else parse_eur(ln.importe)
        else:
            p = pct_lineas.get(i, pct)
            unit = precio_cliente(parse_eur(ln.importe), p, modo)
        lineas.append({"ref": ln.ref, "tipo": ln.tipo, "uds": ln.uds, "pct": p,
                       "importe": unit, "total": unit * ln.uds})
    base = sum((l["total"] for l in lineas), Decimal(0))
    iva_pct = parse_pct(get_total(ex, "IVA").iva_pct)
    iva = (base * iva_pct / 100).quantize(CENT, ROUND_HALF_UP)
    return {"lineas": lineas, "base": base, "iva_pct": iva_pct, "iva": iva, "total": base + iva}


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
                     conservar_fabrica: bool = False) -> bytes:
    doc = fitz.open(stream=pdf, filetype="pdf")
    labels = {"Base Imponible": "Base Imponible", "Total": "Total", "TOTAL": "TOTAL"}
    for page in doc:
        n = page.number
        for z in zonas_marca(marca, T, n):
            page.add_redact_annot(fitz.Rect(z), fill=False)
        # Precios y totales: redacción sin relleno para conservar el fondo de las filas.
        for ln in (l for l in ex.lineas if l.pagina == n):
            page.add_redact_annot(pad(ln.rect_importe), fill=False)
            page.add_redact_annot(pad(ln.rect_total), fill=False)
        for t in (t for t in ex.totales if t.pagina == n):
            page.add_redact_annot(pad(t.rect), fill=False)
        # Datos de fábrica (salvo que el montador elija conservar su logo y su nombre).
        if not conservar_fabrica:
            if n == T["logo_pagina"]:
                page.add_redact_annot(fitz.Rect(T["zona_logo_fabrica"]), fill=False)
            for palabra in T["lista_negra"]:
                for r in page.search_for(palabra):
                    page.add_redact_annot(pad(tuple(r)), fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        page.insert_font(fontname=FONT_NAME, fontbuffer=FONT.buffer)

        for ln, nuevo in zip(ex.lineas, calc["lineas"]):
            if ln.pagina != n:
                continue
            stamp_right(page, fmt_eur(nuevo["importe"]), ln.rect_importe[2], ln.origin_importe[1], ln.size, ln.color)
            stamp_right(page, fmt_eur(nuevo["total"]), ln.rect_total[2], ln.origin_total[1], ln.size, ln.color)
        for t in (t for t in ex.totales if t.pagina == n):
            if t.label == "IVA":
                txt = f"I.V.A {t.iva_pct} % : {fmt_eur(calc['iva'])} €"
            else:
                valor = calc["total"] if t.label == "TOTAL" else calc["base"]
                txt = f"{labels[t.label]}: {fmt_eur(valor)} €"
            stamp_right(page, txt, t.rect[2], t.origin[1], t.size, t.color)

        if marca is not None and not marca.vacia:
            M = T["marca"]
            if marca.logo and n == M["logo_pagina"]:
                pix = logo_pixmap(marca.logo)
                page.insert_image(rect_logo(pix.width, pix.height, M["zona_logo"]), pixmap=pix)
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


def verify(src_ex: Extraccion, calc: dict, pdf: bytes, T: dict,
           marca: Marca | None = None, conservar_fabrica: bool = False) -> tuple[list[str], list[bytes]]:
    """Verificación de salida obligatoria (sección 'Redacción real y verificación de salida').

    Devuelve los errores y una miniatura PNG por página para la pantalla de descarga.
    """
    err = []
    doc = fitz.open(stream=pdf, filetype="pdf")
    texto = "\n".join(p.get_text() for p in doc)

    # El montaje es precio del montador, no de fábrica: puede seguir apareciendo.
    fabrica = [ln for ln in src_ex.lineas if ln.tipo != "montaje"]
    originales = {ln.importe for ln in fabrica} | {ln.total for ln in fabrica} \
        | {t.valor for t in src_ex.totales}
    for v in sorted(originales):
        if money_regex(v).search(texto):
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
    zona_logo = fitz.Rect(T["zona_logo_fabrica"])
    if not conservar_fabrica and             any(fitz.Rect(i["bbox"]).intersects(zona_logo) for i in doc[T["logo_pagina"]].get_image_info()):
        err.append("logo de fábrica sigue en la cabecera")
    if marca is not None:
        err += verify_marca(doc, marca, T)

    out_ex = extract(doc, T)
    if len(out_ex.lineas) != len(calc["lineas"]):
        err.append(f"nº de líneas en salida {len(out_ex.lineas)} ≠ {len(calc['lineas'])}")
    for got, exp in zip(out_ex.lineas, calc["lineas"]):
        if (parse_eur(got.total) - exp["total"]).copy_abs() > TOL or \
                (parse_eur(got.importe) - exp["importe"]).copy_abs() > TOL:
            err.append(f"{exp['ref']}: estampado {got.importe}/{got.total} ≠ calculado")
    esperados = {"Base Imponible": calc["base"], "Total": calc["base"], "IVA": calc["iva"], "TOTAL": calc["total"]}
    for label, exp in esperados.items():
        t = get_total(out_ex, label)
        if not t or (parse_eur(t.valor) - exp).copy_abs() > TOL:
            err.append(f"total '{label}' estampado {t.valor if t else '—'} ≠ {fmt_eur(exp)}")
    suma = sum((parse_eur(l.total) for l in out_ex.lineas), Decimal(0))
    if (suma - calc["base"]).copy_abs() > TOL:
        err.append("suma de líneas estampadas ≠ base estampada")
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
        return [doc[ln.pagina].get_pixmap(clip=fitz.Rect(ln.dibujo), dpi=DPI_DIBUJO).tobytes("png")
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
             "totales_fabrica": _totales_fabrica(ex) if not errores else None}
    return Resultado(datos, dibujos=[] if errores else recortar_dibujos(pdf, ex))


def generar(pdf: bytes, T: dict, pct: Decimal, modo: str = "markup", montaje: Decimal | None = None,
            pct_lineas: dict[int, Decimal] | None = None, marca: Marca | None = None,
            conservar_fabrica: bool = False) -> Resultado:
    """Analiza de nuevo (sin estado), calcula, redacta, estampa y verifica la salida.

    conservar_fabrica: deja el logo y el nombre de la fábrica (los precios se eliminan siempre)."""
    pct_lineas = pct_lineas or {}
    marca = marca or Marca()
    ex, errores = _analisis(pdf, T)
    if not errores:
        errores = validate_params(ex, montaje, pct_lineas) + validate_marca(marca, T, conservar_fabrica)
    datos = {"plantilla": T["id"], "pct": pct, "modo": modo, "montaje": montaje,
             "pct_lineas": {str(k): v for k, v in sorted(pct_lineas.items())},
             "marca": not marca.vacia, "conservar_fabrica": conservar_fabrica, "n_dibujos": ex.n_dibujos}
    calc = salida = None
    miniaturas: list[bytes] = []
    if not errores:
        calc = calcular(ex, pct, modo, montaje, pct_lineas)
        salida = redact_and_stamp(pdf, ex, calc, T, marca, conservar_fabrica)
        errores, miniaturas = verify(ex, calc, salida, T, marca, conservar_fabrica)
    datos["lineas"] = _lineas_publicas(ex, calc)
    datos["totales"] = {k: calc[k] for k in ("base", "iva_pct", "iva", "total")} if calc else None
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
