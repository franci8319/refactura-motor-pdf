"""Motor PDF del worker: huella, extracción, validación, redacción, estampado y verificación.

Trabaja solo en memoria (bytes de entrada → bytes de salida), sin disco ni estado, para que
lo usen igual el servicio HTTP (servicio.py) y la CLI de pruebas (poc.py).

Regla de oro: si falla la huella, cualquier validación o la verificación de salida, el estado
es needs_review y no se devuelve PDF.
"""

from __future__ import annotations

import re
from pathlib import Path
from dataclasses import dataclass, field, replace
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
# Letra de los importes reescritos y de las filas añadidas, por plantilla ("fuente"): (normal, negrita), cada una
# con su nombre en la página. comic-neue: letra libre (SIL OFL, fuentes/) parecida a la Comic Sans de algunas
# fábricas, que no se puede incrustar.
_FUENTES = Path(__file__).resolve().with_name("fuentes")
FAMILIAS: dict[str, tuple[tuple[fitz.Font, str], tuple[fitz.Font, str]]] = {
    "helvetica": ((FONT_REG, FONT_REG_NAME), (FONT, FONT_NAME)),
}
if (_FUENTES / "ComicNeue-Regular.ttf").exists():
    FAMILIAS["comic-neue"] = ((fitz.Font(fontfile=str(_FUENTES / "ComicNeue-Regular.ttf")), "ComicReg"),
                              (fitz.Font(fontfile=str(_FUENTES / "ComicNeue-Bold.ttf")), "ComicBold"))


def fuente_de(familia: str, negrita: bool) -> tuple[fitz.Font, str]:
    return FAMILIAS[familia][1 if negrita else 0]


def insertar_fuentes(page: fitz.Page, familia: str) -> None:
    for f, nombre in FAMILIAS[familia]:
        page.insert_font(fontname=nombre, fontbuffer=f.buffer)


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
        if con is not None and con["ancla"] not in ("Base Imponible", "Subtotal", "Total", "IVA", "TOTAL"):
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
            t, tc, tl = raw["tabla"], raw.get("totales_columnas"), raw.get("totales_lineas")
            if (tc is None) == (tl is None):
                raise ValueError("modo tabla: totales_columnas o totales_lineas (uno de los dos)")
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
                    # Opcional: regex propia de la columna de cantidad (metraje con unidad, "4,15ML"); si la fila
                    # no tiene cantidad, valen las unidades de col_uds.
                    "cantidad": re.compile(t["cantidad"]) if "cantidad" in t else None,
                    "col_uds": t.get("col_uds"),
                    # Opcional: descuento de fábrica (%) por fila: cuenta en el cuadre y se vacía entero del PDF.
                    "col_dto": t.get("col_dto"),
                    # "aprox": cantidad y precio llevan decimales ocultos; el cuadre de la fila admite su redondeo.
                    "cuadre": t.get("cuadre", "exacto"),
                    # Referencia de la fila: "primera" = el texto de su primera línea (alto_ref); "celda" = toda la
                    # celda de la descripción, hasta la fila siguiente o un hueco (no coge el pie de la página).
                    "ref": t.get("ref", "primera"),
                    # "repetidas": una fila que no cabe en la página sigue en la siguiente repitiendo su precio,
                    # cantidad y total; la primera fila de una página igual que la última de la anterior es la misma.
                    "filas_partidas": t.get("filas_partidas"),
                    # Columnas que se vacían enteras, título incluido (p. ej. el nombre del fabricante): ancho en
                    # pt desde el x0 de su título.
                    "columnas_fuera": [(str(c["titulo"]), float(c["ancho"])) for c in t.get("columnas_fuera", [])],
                },
                # Totales en columnas: valores bajo una fila de títulos. Rangos del borde derecho del valor,
                # relativos al x0 del ancla (un título de esa fila).
                "totales_columnas": None if tc is None else {
                    "ancla": tc["ancla"], "alto": float(tc.get("alto", 22)),
                    "columnas": {k: tuple(float(v) for v in r) for k, r in tc["columnas"].items()},
                    # % de IVA en la fila de valores (rango, como las columnas) o dentro del título de su columna.
                    "iva_pct": tuple(float(v) for v in tc["iva_pct"]) if "iva_pct" in tc else None,
                    "iva_titulo": re.compile(tc["iva_titulo"]) if "iva_titulo" in tc else None,
                },
                # Totales en líneas sueltas ("Sub-total: 1.234,56€"): regex por etiqueta con los grupos "valor" y,
                # en el IVA, "pct"; se reescribe la línea entera cambiando solo esos números. "lineas": filas de los
                # totales que son una línea más del presupuesto (el envío): llevan el % como las de la tabla. Cada
                # una es una regex o {"regex", "tipo"}; tipo "portes": el montador puede poner su precio (sin %).
                "totales_lineas": None if tl is None else {
                    "izquierda": tl.get("alinear", "derecha") == "izquierda",
                    "etiquetas": {k: re.compile(v) for k, v in tl["etiquetas"].items()},
                    "lineas": {k: re.compile(v if isinstance(v, str) else v["regex"])
                               for k, v in tl.get("lineas", {}).items()},
                    "tipos": {k: (t.get("tipo", "extra") if isinstance(t, dict) else None) for k, t in tl.get("lineas", {}).items()},
                },
                "pct": re.compile(raw.get("pct", r"^\s*(\d{1,2}(?:,\d{1,2})?)\s*$")),
            }
            if T["tabla"]["cuadre"] not in ("exacto", "aprox"):
                raise ValueError(f"tabla.cuadre desconocido: {T['tabla']['cuadre']!r}")
            if T["tabla"]["ref"] not in ("primera", "celda"):
                raise ValueError(f"tabla.ref desconocido: {T['tabla']['ref']!r}")
            if T["tabla"]["filas_partidas"] not in (None, "repetidas"):
                raise ValueError(f"tabla.filas_partidas desconocido: {T['tabla']['filas_partidas']!r}")
            if tc is not None and (T["totales_columnas"]["iva_pct"] is None) == (T["totales_columnas"]["iva_titulo"] is None):
                raise ValueError("totales_columnas necesita iva_pct o iva_titulo (uno de los dos)")
            if tl is not None:
                et = T["totales_lineas"]["etiquetas"]
                if set(et) - {"Base Imponible", "Subtotal", "Total", "IVA", "TOTAL"}:
                    raise ValueError(f"totales_lineas: etiqueta desconocida en {sorted(et)}")
                if any("valor" not in rx.groupindex for rx in [*et.values(), *T["totales_lineas"]["lineas"].values()]) \
                        or "pct" not in et.get("IVA", re.compile("(?P<pct>)")).groupindex:
                    raise ValueError("totales_lineas: cada regex necesita el grupo «valor» (y el IVA, «pct»)")
                if set(T["totales_lineas"]["tipos"].values()) - {None, "extra", "portes"}:
                    raise ValueError("totales_lineas.lineas: tipo extra o portes")
        if con is not None and con.get("totales_fijos") and con.get("en_tabla"):
            raise ValueError("conceptos: totales_fijos y en_tabla no van juntos")
        if con is not None and con.get("filas", "lineas") not in ("lineas", "bandas", "rayas"):
            raise ValueError("conceptos.filas: lineas, bandas o rayas")
        if raw.get("fuente", "helvetica") not in FAMILIAS:
            raise ValueError(f"fuente no disponible: {raw.get('fuente')}")
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
                # La zona nunca baja de aquí (pt desde el pie de la hoja): el pie y el nº de página se quedan.
                "margen_inferior": float(cond.get("margen_inferior", 0)),
            },
            # Opcional: líneas que añade el montador ("Otros conceptos": instalación, portes…) en un bloque
            # encima del recuadro de totales, que baja para hacer sitio. "caja" = recuadro de totales relativo
            # al ancla: [x0 − x1_ancla, y0 − y0_ancla, x1 − x1_ancla, y1 − y0_ancla].
            # "totales_fijos": el bloque va en el hueco libre encima del recuadro de totales (el recuadro que
            # contiene el ancla), que no se mueve; "importe_dx": x de los importes respecto a su borde derecho.
            # "en_tabla": cada concepto es una fila más de la tabla de productos (sin código; descripción, uds.,
            # precio e importe), en el hueco libre de la tabla de la página de totales; nada se mueve.
            "conceptos": None if con is None else {
                "ancla": con["ancla"], "totales_fijos": bool(con.get("totales_fijos", False)),
                "en_tabla": bool(con.get("en_tabla", False)),
                # "lineas": filas separadas por líneas (la fila vacía de cierre baja); "bandas": sin líneas, a
                # continuación del último texto, con la banda gris alterna de las filas con precio.
                "filas": con.get("filas", "lineas"),
                "col_uds": con.get("col_uds"),  # título de la columna de uds. si la tabla no lo define
                "caja": None if con.get("totales_fijos") or con.get("en_tabla")
                else tuple(float(v) for v in con["caja"]),
                "importe_dx": None if "importe_dx" not in con else float(con["importe_dx"]),
                "margen_inferior": float(con.get("margen_inferior", 0) if con.get("totales_fijos") or con.get("en_tabla")
                                         else con["margen_inferior"]),
                "max_lineas": int(con.get("max_lineas", 10)),
            },
            "precio": re.compile(raw["precio"]),
            # Opcional: los importes reescritos van en normal o en negrita como el original (si no, en negrita).
            "grosor_original": bool(raw.get("grosor_original", False)),
            "fuente": raw.get("fuente", "helvetica"),
            "logo_pagina": logo["pagina"] - 1,
            "zona_logo_fabrica": tuple(logo["rect"]),  # x0, y0, x1, y1
            "lista_negra": red["lista_negra"],
            # Opcional: líneas de texto que empiezan así (forma de pago entre fábrica y montador…) se quitan
            # enteras, aunque el montador conserve el logo de la fábrica.
            "lineas_fuera": list(red.get("lineas_fuera", [])),
            # Zonas con datos de fábrica en todas las páginas (p. ej. su pie): se vacían enteras.
            "zonas_fabrica_todas": [tuple(r) for r in red.get("zonas_fabrica_todas", [])],
            # Opcional: bloques que se quitan siempre (también conservando el logo de fábrica): el envío o la cuenta
            # bancaria entre fábrica y montador. Zona relativa al ancla (regex sobre cada línea de texto, como el
            # recuadro); "hasta": regex de una línea que la corta por abajo; "trazos": también sus líneas
            # (subrayados). El ancla tiene que aparecer: si no, el formato ha cambiado y no se sigue.
            "bloques_fuera": [{"ancla": re.compile(b["ancla"]), "zona": tuple(float(v) for v in b["zona"]),
                               "hasta": re.compile(b["hasta"]) if "hasta" in b else None,
                               "trazos": bool(b.get("trazos", False))} for b in red.get("bloques_fuera", [])],
            # Opcional: dónde va la marca del montador (logo y datos en la pág. indicada, pie en todas).
            "marca": None if marca is None else {
                "logo_pagina": marca["zona_logo"]["pagina"] - 1,
                "zona_logo": tuple(marca["zona_logo"]["rect"]),
                "logo_derecha": bool(marca.get("logo_derecha", False)),  # alineado a la derecha de su zona
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
                # Opcional: recuadro del destinatario (el cliente que pone el montador: un cliente final o él
                # mismo), que el montador puede cambiar en cada presupuesto. Zona relativa a un ancla: regex que
                # se busca en cada línea de texto (distingue mayúsculas), en cada página que la tenga.
                "recuadro": None if "recuadro" not in marca else {
                    "ancla": re.compile(marca["recuadro"]["ancla"]),
                    "zona": tuple(marca["recuadro"]["zona"]),  # dx0, dy0, dx1, dy1 desde la esquina del ancla
                    "sangria": float(marca["recuadro"].get("sangria", 0)),  # pt desde el borde izquierdo
                },
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
    dto: Decimal | None = None  # modo tabla con columna de descuento: % de fábrica de la fila (0 si no lleva)
    negrita_importe: bool = True  # grosor del original (se respeta con "grosor_original" en la plantilla)
    negrita_total: bool = True
    # Texto pegado detrás del importe en el original ("€+IVA"): se estampa igual.
    sufijo_importe: str = ""
    sufijo_total: str = ""
    # Fila partida entre páginas que repite sus importes en la siguiente: cada repetición (mismos importes,
    # otro sitio), que se reescribe igual que la fila.
    ecos: list[Linea] = field(default_factory=list)
    # Línea suelta de los totales (el envío): un solo texto con el importe dentro ("Envío: 12,00€"), que se
    # reescribe entero cambiando el número (formato con "{valor}"). rect_importe = rect_total.
    en_totales: bool = False
    formato: str | None = None
    izquierda: bool = False  # alineada a la izquierda (en el x0 del original)

    def apariciones(self) -> list[Linea]:
        """La fila y sus repeticiones en páginas siguientes: todos los sitios donde van sus importes."""
        return [self, *self.ecos]


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
    pct_marco: tuple[str, str] | None = None  # % dentro de un título: texto de antes y de después del número
    sufijo: str = ""  # "€" pegado al valor en el original: se estampa igual
    negrita: bool = True
    # Total en una línea suelta: el texto entero con "{valor}" (y "{pct}" en el IVA) en lugar de los números.
    formato: str | None = None
    izquierda: bool = False  # alineado a la izquierda (en el x0 del original)


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
    negrita: bool = True


@dataclass
class Extraccion:
    lineas: list[Linea] = field(default_factory=list)
    totales: list[Total] = field(default_factory=list)
    n_dibujos: int = 0
    errores: list[str] = field(default_factory=list)
    subtotales: list[Subtotal] = field(default_factory=list)
    vaciar: list[tuple[int, tuple]] = field(default_factory=list)  # (página, rect): columna de descuento
    cuadre_aprox: bool = False  # el cuadre de cada fila admite el redondeo de cantidad y precio
    fuera: list[tuple[int, tuple]] = field(default_factory=list)  # (página, rect): columnas que se vacían enteras
    bloques: list[tuple[int, tuple, bool]] = field(default_factory=list)  # (página, rect, trazos): bloques fuera


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
    recuadro: list[str] = field(default_factory=list)  # sustituye el texto del recuadro con los datos del montador

    @property
    def vacia(self) -> bool:
        return not (self.logo or self.datos or self.pie or self.condiciones or self.recuadro)

    @property
    def visual(self) -> bool:
        """Lo que va en zonas de la plantilla (cabecera, recuadro y pie)."""
        return bool(self.logo or self.datos or self.pie or self.recuadro)


MAX_LADO_LOGO = 4000  # px; se comprueba en la cabecera antes de decodificar
TAM_DATOS = (9.0, 6.0)  # pt máx. y mín.: el texto se reduce hasta caber en su zona
TAM_PIE = (7.0, 5.5)  # el pie va en letra normal (FONT_REG), centrado en la página
TAM_RECUADRO = (10.0, 6.0)  # la primera línea, en negrita; el resto, en normal
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
    ex = extract_tabla(doc, T) if T["modo"] == "tabla" else extract_uds(doc, T)
    _bloques_fuera(doc, T, ex)
    return ex


def _misma_fila(a: Span, b: Span, tol: float = 2.5) -> bool:
    return abs(a.bbox[1] - b.bbox[1]) <= tol


def extract_tabla(doc: fitz.Document, T: dict) -> Extraccion:
    """Presupuesto en tabla (sin dibujos): filas con precio en la columna del total, subtotales entre
    páginas y totales en columnas bajo su fila de títulos."""
    C, TC, TL = T["tabla"], T["totales_columnas"], T["totales_lineas"]
    ex = Extraccion(cuadre_aprox=C["cuadre"] == "aprox")
    previa: Linea | None = None  # última fila de la página anterior, si la tabla sigue (filas partidas)
    for page in doc:
        spans = page_spans(page)
        ultima: Linea | None = None  # la última fila de esta página (si es una repetición, la fila a la que pertenece)
        hubo_fin = False

        def cabecera(k: str) -> Span | None:
            return next((s for s in spans if s.text.strip() == C[k]), None) if C[k] else None
        cab = {k: cabecera(k) for k in ("col_cantidad", "col_precio", "col_total", "col_descripcion")}
        fin_desc, col_uds, col_dto = cabecera("col_fin_descripcion"), cabecera("col_uds"), cabecera("col_dto")
        etiquetas_sub = [(tipo, s) for s in spans for tipo, texto in C["subtotales"].items()
                         if s.text.strip().startswith(texto)]
        for tipo, et in etiquetas_sub:
            v = next((s for s in spans if T["precio"].match(s.text) and _misma_fila(s, et, 3)
                      and s.bbox[0] > et.bbox[2]), None)
            if v is None:
                ex.errores.append(f"p{page.number + 1}: subtotal sin importe")
            else:
                ex.subtotales.append(Subtotal(page.number, tipo, v.text.strip(), v.bbox, v.origin, v.size, v.color,
                                              v.bold))
        if all(cab.values()):
            fines = [s.bbox[1] for s in spans if any(s.text.strip().startswith(f) for f in C["fin"])]
            fin, hubo_fin = min(fines, default=page.rect.height), bool(fines)
            y0 = max(cab[k].bbox[3] for k in ("col_cantidad", "col_precio", "col_total"))
            x_desc = cab["col_descripcion"].bbox[0]
            x_cant = min(cab[k].bbox[0] for k in ("col_cantidad", "col_precio")) - 5
            x_max_desc = fin_desc.bbox[0] - 2 if fin_desc else x_cant - 35
            filas = sorted((s for s in spans if T["precio"].match(s.text) and y0 < s.bbox[1] < fin
                            and abs(s.bbox[2] - cab["col_total"].bbox[2]) <= C["tol_x"]
                            and not any(_misma_fila(s, et, 3) for _, et in etiquetas_sub)),
                           key=lambda s: s.bbox[1])
            if col_dto is not None:
                # Toda la columna de descuento, también en filas sin precio (cabecera del producto, sin cargo…).
                ex.vaciar += [(page.number, s.bbox) for s in spans if DTO_CELDA.match(s.text)
                              and y0 < s.bbox[1] < fin and abs(s.bbox[2] - col_dto.bbox[2]) <= C["tol_x"]]
            patron_cant = C["cantidad"] or T["precio"]
            # Columnas que se vacían enteras: su título (con lo que lleve pegado debajo) y su trozo de cada fila.
            columnas_fuera = []
            for titulo, ancho in C["columnas_fuera"]:
                tit = next((s for s in spans if s.text.strip() == titulo), None)
                if tit is None:
                    ex.errores.append(f"p{page.number + 1}: no se encuentra la columna «{titulo}» que se quita")
                    continue
                cx0, cx1 = tit.bbox[0] - 2, tit.bbox[0] + ancho
                columnas_fuera.append((cx0, cx1))
                tope = min((f.bbox[1] for f in filas), default=fin) - 1
                cab_col = _celda(spans, cx0, cx1, tit.bbox[1], tit.bbox[3] - tit.bbox[1], tope)
                ex.fuera.append((page.number, (cx0, tit.bbox[1] - 1, cx1, max(s.bbox[3] for s in cab_col) + 1)))
            for tot in filas:
                def en_col(col: Span, patron: re.Pattern) -> Span | None:
                    return next((s for s in spans if patron.match(s.text) and _misma_fila(s, tot)
                                 and abs(s.bbox[2] - col.bbox[2]) <= C["tol_x"]), None)
                cant, prec = en_col(cab["col_cantidad"], patron_cant), en_col(cab["col_precio"], T["precio"])
                uds = en_col(col_uds, UDS) if cant is None and col_uds is not None else None
                if (cant is None and uds is None) or prec is None:
                    ex.errores.append(f"p{page.number + 1}: fila sin cantidad o sin precio")
                    continue
                cantidad = parse_eur(patron_cant.match(cant.text).group(1)) if cant else Decimal(UDS.match(uds.text).group(1))
                dto = None
                if col_dto is not None:
                    d = en_col(col_dto, DTO_CELDA)
                    dto = parse_dto(d.text) if d else Decimal(0)
                    if dto is None:
                        ex.errores.append(f"p{page.number + 1}: descuento no reconocido ({d.text.strip()[:12]})")
                        continue
                siguiente = next((f.bbox[1] for f in filas if f.bbox[1] > tot.bbox[1] + 1), fin) - 1
                alto = tot.bbox[3] - tot.bbox[1]
                celda = _celda(spans, x_desc - 5, x_max_desc, tot.bbox[1], alto, siguiente)
                if C["ref"] == "celda":
                    ref = " ".join(s.text.strip() for s in celda)
                else:
                    ref = " ".join(s.text.strip() for s in sorted(spans, key=lambda s: (s.bbox[1], s.bbox[0]))
                                   if x_desc - 5 <= s.bbox[0] < x_max_desc
                                   and tot.bbox[1] - 2 <= s.bbox[1] < tot.bbox[1] + C["alto_ref"])
                    # Unidades y medidas (columnas entre la descripción y la cantidad), para reconocer la línea.
                    medio = " ".join(s.text.strip() for s in sorted(spans, key=lambda s: s.bbox[0])
                                     if fin_desc and x_max_desc <= s.bbox[0] < x_cant and _misma_fila(s, tot, 3))
                    if medio:
                        ref = f"{ref} ({medio})"
                for cx0, cx1 in columnas_fuera:
                    trozo = _celda(spans, cx0, cx1, tot.bbox[1], alto, siguiente)
                    fondo = max([tot.bbox[3], *(s.bbox[3] for s in celda + trozo)])
                    ex.fuera.append((page.number, (cx0, tot.bbox[1] - 1.5, cx1, fondo + 1)))
                m_tot, m_prec = T["precio"].match(tot.text), T["precio"].match(prec.text)
                ln = Linea(page.number, C["tipo"], ref or "(sin descripción)", 1,
                           m_tot.group(1), m_tot.group(1), prec.bbox, tot.bbox,
                           prec.origin, tot.origin, tot.size, tot.color, None,
                           cantidad, m_prec.group(1), dto=dto,
                           negrita_importe=prec.bold, negrita_total=tot.bold,
                           sufijo_importe=prec.text[m_prec.end(1):].strip(),
                           sufijo_total=tot.text[m_tot.end(1):].strip())
                if C["filas_partidas"] == "repetidas" and previa is not None and ultima is None and \
                        (ln.total, ln.precio, ln.cantidad, ln.dto) == (previa.total, previa.precio, previa.cantidad, previa.dto):
                    previa.ecos.append(ln)  # la fila que no cabía en la página anterior, con sus importes repetidos
                    ultima = previa
                else:
                    ex.lineas.append(ln)
                    ultima = ln
        # La tabla sigue en la página siguiente si esta tiene filas y no ha llegado a su final.
        previa = ultima if ultima is not None and not hubo_fin else None
        if TL is not None:
            # Totales en líneas sueltas, y las líneas del presupuesto que van entre ellos (el envío).
            for s in spans:
                texto = s.text.strip()
                for label, rx in TL["etiquetas"].items():
                    if (m := rx.match(texto)) is not None:
                        ex.totales.append(Total(page.number, label, m["valor"], m.groupdict().get("pct"), s.bbox,
                                                s.origin, s.size, s.color, negrita=s.bold, formato=_formato(texto, m),
                                                izquierda=TL["izquierda"]))
                for label, rx in TL["lineas"].items():
                    if (m := rx.match(texto)) is not None:
                        ex.lineas.append(Linea(page.number, TL["tipos"][label] or C["tipo"], label, 1, m["valor"], m["valor"], s.bbox,
                                               s.bbox, s.origin, s.origin, s.size, s.color,
                                               negrita_importe=s.bold, negrita_total=s.bold, en_totales=True,
                                               formato=_formato(texto, m), izquierda=TL["izquierda"]))
            continue
        # Totales: valores bajo la fila de títulos (rangos por el borde derecho, relativos al ancla).
        ancla = next((s for s in spans if s.text.strip() == TC["ancla"]), None)
        if ancla is None:
            continue
        bajo = [s for s in spans if ancla.bbox[3] - 1 <= s.bbox[1] <= ancla.bbox[3] + TC["alto"]]

        def valor(r: tuple, patron: re.Pattern) -> tuple[Span, str, str] | None:
            """Span, texto sin "€" y sufijo ("€" si lo llevaba pegado)."""
            for s in bajo:
                texto = s.text.strip()
                sufijo = "€" if texto.endswith("€") else ""
                texto = texto.removesuffix("€").strip()
                if patron.match(texto) and ancla.bbox[0] + r[0] <= s.bbox[2] <= ancla.bbox[0] + r[1]:
                    return s, texto, sufijo
            return None
        pct: tuple[Span, str] | None = None
        marco = None
        if TC["iva_titulo"] is not None:
            for s in spans:
                m = TC["iva_titulo"].search(s.text)
                if m and _misma_fila(s, ancla, 3):
                    pct, marco = (s, m.group(1)), (s.text[:m.start(1)].lstrip(), s.text[m.end(1):].rstrip())
                    break
        elif (v := valor(TC["iva_pct"], T["pct"])) is not None:
            pct = v[0], v[1]
        for label, r in TC["columnas"].items():
            v = valor(r, T["precio"])
            if v is not None:
                s, texto, sufijo = v
                con_pct = label == "IVA" and pct is not None
                ex.totales.append(Total(page.number, label, texto, pct[1] if con_pct else None,
                                        s.bbox, s.origin, s.size, s.color, solo_valor=True,
                                        pct_rect=pct[0].bbox if con_pct else None,
                                        pct_origin=pct[0].origin if con_pct else None,
                                        pct_marco=marco if con_pct else None, sufijo=sufijo,
                                        negrita=s.bold))
    if C["filas_partidas"]:
        resolver_partidas(ex)
    return ex


HUECO_CELDA = 6.0  # pt entre líneas de una misma celda como mucho (entre filas o hasta el pie hay más)


def _celda(spans: list[Span], x0: float, x1: float, y_top: float, alto: float, y_lim: float) -> list[Span]:
    """Texto de una celda: lo que empieza entre x0 y x1 desde la línea de su fila (y_top, de `alto` pt) hacia
    abajo, sin huecos de más de HUECO_CELDA entre líneas y sin pasar de y_lim (la fila siguiente o el final)."""
    fondo, out = y_top + alto, []
    for s in sorted((s for s in spans if x0 <= s.bbox[0] < x1 and y_top - 2 <= s.bbox[1] < y_lim),
                    key=lambda s: (s.bbox[1], s.bbox[0])):
        if s.bbox[1] - fondo > HUECO_CELDA:
            break
        out.append(s)
        fondo = max(fondo, s.bbox[3])
    return out


def _formato(texto: str, m: re.Match) -> str:
    """El texto de un total con "{valor}" y "{pct}" en el sitio de sus números (para reescribirlo igual)."""
    out, i = [], 0
    for g in sorted((g for g in ("valor", "pct") if m.groupdict().get(g) is not None), key=m.start):
        out += [texto[i:m.start(g)].replace("{", "{{").replace("}", "}}"), "{" + g + "}"]
        i = m.end(g)
    out.append(texto[i:].replace("{", "{{").replace("}", "}}"))
    return "".join(out)


def _objetivo_filas(ex: Extraccion) -> Decimal | None:
    """Lo que deben sumar las filas de la tabla según los totales del PDF (sin las líneas de los totales)."""
    sub, base = get_total(ex, "Subtotal"), get_total(ex, "Base Imponible")
    if sub is not None:
        return parse_eur(sub.valor)
    if base is not None:
        return parse_eur(base.valor) - sum((parse_eur(ln.total) for ln in ex.lineas if ln.en_totales), Decimal(0))
    return None


def resolver_partidas(ex: Extraccion) -> None:
    """Filas partidas entre páginas: si juntarlas descuadra la suma de la tabla y separarlas la cuadra, eran filas
    iguales seguidas (otra ventana con el mismo precio) y van por separado. Si no, se quedan juntas y su
    referencia lleva el texto de todas las páginas."""
    partidas = [ln for ln in ex.lineas if ln.ecos]
    if not partidas:
        return
    objetivo = _objetivo_filas(ex)
    suma = sum((parse_eur(ln.total) for ln in ex.lineas if not ln.en_totales), Decimal(0))
    repetido = sum((parse_eur(e.total) for ln in partidas for e in ln.ecos), Decimal(0))
    if objetivo is not None and (suma - objetivo).copy_abs() > TOL and (suma + repetido - objetivo).copy_abs() <= TOL:
        separadas = []
        for ln in ex.lineas:
            separadas += [ln, *ln.ecos]
            ln.ecos = []
        ex.lineas = separadas
        return
    for ln in partidas:
        ln.ref = " ".join([ln.ref, *(e.ref for e in ln.ecos if e.ref != "(sin descripción)")])


def _bloques_fuera(doc: fitz.Document, T: dict, ex: Extraccion) -> None:
    """Zonas de los bloques que se quitan siempre (relativas a su ancla, cortadas por "hasta"). Sin el ancla en
    todo el PDF, el formato ha cambiado: error (su contenido podría quedarse en el PDF del cliente)."""
    for i, b in enumerate(T["bloques_fuera"]):
        antes = len(ex.bloques)
        for page in doc:
            lineas = _lineas_texto(page)
            for r, t in lineas:
                if not b["ancla"].search(t):
                    continue
                z0, z1, z2, z3 = b["zona"]
                zona = fitz.Rect(r.x0 + z0, r.y0 + z1, r.x0 + z2, r.y0 + z3)
                if b["hasta"] is not None:
                    tope = min((r2.y0 for r2, t2 in lineas if r2.y0 > r.y0 + 1 and b["hasta"].search(t2)), default=None)
                    if tope is not None:
                        zona.y1 = min(zona.y1, tope - 1)
                ex.bloques.append((page.number, tuple(zona & page.rect), b["trazos"]))
        if len(ex.bloques) == antes:
            ex.errores.append(f"no se encuentra el bloque {i + 1} de los que se quitan siempre "
                              f"({b['ancla'].pattern[:30]}): el formato ha cambiado")


UDS = re.compile(r"^\s*(\d{1,4})\s*$")
DTO_CELDA = re.compile(r"^[\d\s.,+%-]*\d[\d\s.,+%-]*$")  # cualquier descuento escrito: "47", "47 %", "50+10"
PCT_DTO = re.compile(r"\s*(\d{1,2}(?:,\d{1,2})?)\s*")


def parse_dto(texto: str) -> Decimal | None:
    """% de descuento de una celda; en cascada ("50+10") equivale a 1 − (1 − 0,50)·(1 − 0,10) = 55 %.
    None si no se entiende."""
    f = Decimal(1)
    for parte in texto.replace("%", "").split("+"):
        m = PCT_DTO.fullmatch(parte)
        if m is None:
            return None
        f *= 1 - parse_pct(m.group(1)) / 100
    return ((1 - f) * 100).quantize(CENT, ROUND_HALF_UP)


PRECIO_NEGATIVO = re.compile(r"^\s*-\d{1,3}(?:\.\d{3})*,\d{2}\s*$")


def _caja_de(page: fitz.Page, rect: tuple) -> tuple | None:
    """El recuadro de ancho completo más pequeño que contiene el rect (una fila, un total…)."""
    r = fitz.Rect(rect)
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
                tipo, dibujo, caja = "descuento", None, _caja_de(page, fila.bbox)
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
                                   pagina_dibujo=pagina_dibujo, caja=caja,
                                   negrita_importe=imp.bold, negrita_total=tot.bold))
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
                                        s.bbox, s.origin, s.size, s.color, negrita=s.bold))
    return ex


def get_total(ex: Extraccion, label: str) -> Total | None:
    return next((t for t in ex.totales if t.label == label), None)


def base_fabrica(ex: Extraccion) -> Decimal:
    """Base imponible del PDF: la impresa o, si no la trae, el subtotal de la tabla más las líneas de los totales."""
    base = get_total(ex, "Base Imponible")
    if base is not None:
        return parse_eur(base.valor)
    return parse_eur(get_total(ex, "Subtotal").valor) + \
        sum((parse_eur(ln.total) for ln in ex.lineas if ln.en_totales), Decimal(0))


def validate(ex: Extraccion) -> list[str]:
    """Comprobaciones de la sección 'Detección automática' de la propuesta."""
    err = list(ex.errores)
    if not ex.lineas:
        err.append("no se encontró ninguna línea (falta ancla UDS)")
    for ln in ex.lineas:
        if (parse_eur(ln.importe) * ln.uds - parse_eur(ln.total)).copy_abs() > TOL:
            err.append(f"{ln.ref}: importe × uds ≠ total")
        if ln.cantidad is not None:
            precio, f = parse_eur(ln.precio), 1 - (ln.dto or Decimal(0)) / 100
            # Aproximado: cantidad y precio impresos con 2 decimales de valores que tienen más.
            tol = (precio.copy_abs() + ln.cantidad) * f * Decimal("0.005") + TOL if ex.cuadre_aprox else TOL
            if ((ln.cantidad * precio * f).quantize(CENT, ROUND_HALF_UP) - parse_eur(ln.total)).copy_abs() > tol:
                err.append(f"{ln.ref[:40]}: cantidad × precio ≠ total")
        if ln.tipo == "otro":
            err.append(f"p{ln.pagina + 1}: línea no reconocida ({ln.ref[:40]}); decidir en la plantilla")
        if (ln.tipo == "descuento") != (parse_eur(ln.total) < 0):
            err.append(f"p{ln.pagina + 1}: {ln.ref[:40]}: signo del importe no esperado")
    err += validate_subtotales(ex, [parse_eur(ln.total) for ln in ex.lineas])
    if sum(1 for ln in ex.lineas if ln.tipo == "montaje") > 1:
        err.append("más de una línea de montaje")
    # "Total" (sin IVA) es opcional: algunos formatos solo traen Base Imponible, IVA y TOTAL. Otros traen el
    # subtotal de la tabla y, aparte, líneas en los totales (envío): base = subtotal + esas líneas.
    base, total, iva, total_iva = (get_total(ex, k) for k in ("Base Imponible", "Total", "IVA", "TOTAL"))
    subtotal = get_total(ex, "Subtotal")
    if not ((base or subtotal) and iva and total_iva):
        err.append("faltan totales (Base Imponible / IVA / TOTAL)")
        return err
    suma = sum((parse_eur(ln.total) for ln in ex.lineas), Decimal(0))
    if subtotal is not None:
        filas = sum((parse_eur(ln.total) for ln in ex.lineas if not ln.en_totales), Decimal(0))
        if (filas - parse_eur(subtotal.valor)).copy_abs() > TOL:
            err.append(f"suma de filas {fmt_eur(filas)} ≠ subtotal {subtotal.valor}")
    b = base_fabrica(ex)
    if base is not None and (suma - b).copy_abs() > TOL:
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
                    T: dict | None = None, precios_lineas: dict[int, Decimal] | None = None) -> list[str]:
    """Parámetros que solo se pueden comprobar contra las líneas extraídas."""
    err = []
    if montaje is not None and not tiene_montaje(ex) and (T is None or T["conceptos"] is None):
        err.append("se indicó montaje pero el PDF no tiene línea de instalación donde escribirlo")
    for i in sorted(pct_lineas):
        if not 0 <= i < len(ex.lineas):
            err.append(f"pct_lineas: la línea {i} no existe")
        elif ex.lineas[i].tipo in ("montaje", "descuento"):
            err.append(f"pct_lineas: la línea {i} es {ex.lineas[i].tipo}, que nunca lleva %")
    for i in sorted(precios_lineas or {}):
        if not 0 <= i < len(ex.lineas):
            err.append(f"precios_lineas: la línea {i} no existe")
        elif ex.lineas[i].tipo != "portes":
            err.append(f"precios_lineas: la línea {i} es {ex.lineas[i].tipo}, que no admite precio propio")
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


def tam_texto(lineas: list[str], zona: tuple, tams: tuple[float, float],
              fuentes: list[fitz.Font] | None = None) -> float | None:
    """Mayor tamaño (en pasos de 0,25 pt) con el que todas las líneas caben en la zona; None si no caben.
    fuentes: la de cada línea (por defecto, todas en negrita)."""
    z = fitz.Rect(zona)
    fuentes = fuentes or [FONT] * len(lineas)
    t = tams[0]
    while t >= tams[1]:
        if len(lineas) * t * INTERLINEA <= z.height and \
                max(f.text_length(l, fontsize=t) for l, f in zip(lineas, fuentes)) <= z.width:
            return t
        t -= 0.25
    return None


def fuentes_recuadro(lineas: list[str]) -> list[fitz.Font]:
    return [FONT] + [FONT_REG] * (len(lineas) - 1)


def zona_texto_recuadro(zona: fitz.Rect, T: dict) -> tuple:
    return (zona.x0 + T["marca"]["recuadro"]["sangria"], zona.y0, zona.x1, zona.y1)


def _lineas_texto(page: fitz.Page) -> list[tuple[fitz.Rect, str]]:
    return [(fitz.Rect(l["bbox"]), "".join(s["text"] for s in l["spans"]).strip())
            for b in page.get_text("dict")["blocks"] for l in b.get("lines", [])]


def zona_recuadro(page: fitz.Page, T: dict) -> fitz.Rect | None:
    """Recuadro del destinatario en esta página (relativo a su ancla), o None si la página no lo tiene.
    El ancla es una regex sobre el texto de cada línea: así "PRESUPUESTO 797/2" (p1, con recuadro) no se
    confunde con "Presupuesto:797 /2" (cabecera de las demás páginas, sin recuadro)."""
    R = (T["marca"] or {}).get("recuadro")
    if R is None:
        return None
    a = next((r for r, t in _lineas_texto(page) if R["ancla"].search(t)), None)
    if a is None:
        return None
    z0, z1, z2, z3 = R["zona"]
    return fitz.Rect(a.x0 + z0, a.y0 + z1, a.x0 + z2, a.y0 + z3) & page.rect


def texto_recuadro(pdf: bytes, T: dict) -> list[str] | None:
    """Líneas del recuadro del destinatario tal como vienen (la primera página que lo tenga), para que el
    montador las revise y cambie. Los trozos de una misma línea (código de cliente y nombre) van juntos."""
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        for page in doc:
            z = zona_recuadro(page, T)
            if z is None:
                continue
            spans = [s for b in page.get_text("dict", clip=z)["blocks"] for l in b.get("lines", [])
                     for s in l["spans"] if s["text"].strip() and z.contains(fitz.Point(
                         (s["bbox"][0] + s["bbox"][2]) / 2, (s["bbox"][1] + s["bbox"][3]) / 2))]
            filas: list[list[dict]] = []
            for s in sorted(spans, key=lambda s: (s["origin"][1], s["origin"][0])):
                if filas and abs(filas[-1][0]["origin"][1] - s["origin"][1]) <= 2:
                    filas[-1].append(s)
                else:
                    filas.append([s])
            return [" ".join(" ".join(s["text"].split()) for s in sorted(f, key=lambda s: s["origin"][0]))
                    for f in filas]
    return None


def zonas_recuadro(pdf: bytes, T: dict) -> dict[int, fitz.Rect]:
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        return {p.number: z for p in doc if (z := zona_recuadro(p, T)) is not None}


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
    for nombre, lineas, zona, tams, fuente in (("datos", marca.datos, M["zona_datos"], TAM_DATOS, FONT),
                                               ("pie", marca.pie, M["zona_pie"], TAM_PIE, FONT_REG)):
        if not lineas:
            continue
        raros = sorted({c for l in lineas for c in l if not fuente.has_glyph(ord(c))})
        if raros:
            err.append(f"marca: {nombre} con caracteres no admitidos {raros}")
        elif tam_texto(lineas, zona, tams, [fuente] * len(lineas)) is None:
            err.append(f"marca: el texto de {nombre} no cabe; acórtalo")
    if marca.recuadro and M["recuadro"] is not None:
        fuentes = fuentes_recuadro(marca.recuadro)
        raros = sorted({c for l, f in zip(marca.recuadro, fuentes) for c in l if not f.has_glyph(ord(c))})
        z0, z1, z2, z3 = M["recuadro"]["zona"]
        ancho = (z2 - z0 - M["recuadro"]["sangria"], z3 - z1)
        if raros:
            err.append(f"marca: el texto del recuadro lleva caracteres no admitidos {raros}")
        elif tam_texto(marca.recuadro, (0, 0, *ancho), TAM_RECUADRO, fuentes) is None:
            err.append("marca: el texto del recuadro no cabe; acórtalo o quita alguna línea")
    return err


@dataclass
class Concepto:
    """Línea que añade el montador (instalación, portes, retirada… o un descuento al cliente, en negativo):
    precio final, sin %."""
    texto: str
    importe: Decimal
    # Formatos con los conceptos como filas de la tabla: unidades y precio por unidad (importe = uds × precio).
    uds: int = 1
    precio: Decimal | None = None

    @property
    def precio_ud(self) -> Decimal:
        return self.importe if self.precio is None else self.precio


TITULO_CONCEPTOS = "Otros conceptos"
PAD_CONCEPTOS = 5.0  # pt de margen interior arriba y abajo del bloque
INTERLINEA_CONCEPTOS = 1.6  # paso entre filas, en veces el tamaño de letra de los totales
HUECO_CONCEPTOS = 6.0  # pt entre el bloque nuevo y el recuadro de totales
SANGRIA = 12.0  # pt desde el borde izquierdo del recuadro
TRAZO_FINO = 0.3  # pt: imita la línea finísima (grosor 0) de los recuadros originales


INTERLINEA_FILA = 1.05  # 2.ª línea de una descripción, en veces el tamaño (filas de dos líneas: 22,6 pt frente a 15,2)


@dataclass
class FilaTabla:
    """Un concepto como fila de la tabla: descripción (1 o 2 líneas), uds., precio e importe ya formateados."""
    lineas: list[str]
    uds: str
    precio: str
    importe: str
    y0: float  # borde superior de la fila
    y1: float  # borde inferior
    banda: list[tuple] = field(default_factory=list)  # celdas de la banda gris (modo bandas), si le toca


@dataclass
class TablaConceptos:
    """Dónde y cómo van las filas nuevas, medido en la tabla de la página de totales."""
    x0: float  # extremos de las líneas horizontales de la tabla
    x1: float
    columnas: list[float]  # x de las líneas de columna (se alargan desde y_columnas hasta el fondo)
    y_columnas: float
    y_cierre: float | None  # línea que cerraba la tabla bajo la fila vacía (se quita; None si no se mueve nada)
    alto_cierre: float  # alto de la fila vacía de cierre, que baja debajo de las filas nuevas (0 si no había)
    filas: list[FilaTabla]
    baseline: float  # línea base del texto desde el borde superior de la fila
    interlinea: float
    x_desc: float  # borde izquierdo de la descripción
    x_uds: float  # borde derecho de las uds.
    x_precio: float  # centro del precio o, si en el original van alineados a la derecha, su borde derecho
    x_importe: float  # borde derecho del importe
    size: float
    color: int
    negrita: bool
    grosor: float  # de las líneas originales (0 = la más fina que pinte el dispositivo)
    familia: str = "helvetica"
    precio_derecha: bool = False
    lineas: bool = True  # filas separadas por líneas; si no, bandas (relleno) en las filas que toque
    relleno: tuple | None = None
    # Modo rayas: bajo cada fila nueva, la raya de las de la fábrica (sus trozos x0–x1, alto y color).
    rayas: list[tuple[float, float]] = field(default_factory=list)
    alto_raya: float = 0.0
    color_raya: tuple | None = None


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
    # El bloque va en el hueco libre encima del recuadro de totales, que se queda donde está.
    totales_fijos: bool = False
    # Conceptos como filas de la tabla (plantilla con "en_tabla"): el bloque es la franja de las filas nuevas.
    tabla: TablaConceptos | None = None

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
    if C["en_tabla"]:
        return plan_conceptos_tabla(pdf, ex, T, conceptos)
    ancla = get_total(ex, C["ancla"])
    if ancla is None or any(t.pagina != ancla.pagina for t in ex.totales):
        return None, ["conceptos: los totales no están juntos en una página"]
    size = ancla.size
    paso = size * INTERLINEA_CONCEPTOS
    alto = 2 * PAD_CONCEPTOS + paso * (len(conceptos) + 1)
    if C["totales_fijos"]:
        # El recuadro de totales es el que contiene el ancla; el bloque se apoya encima y nada se mueve.
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            r = _caja_de(doc[ancla.pagina], ancla.rect)
        if r is None:
            return None, ["conceptos: no se encuentra el recuadro de totales en el PDF"]
        caja = fitz.Rect(r)
        bloque = fitz.Rect(caja.x0, caja.y0 - alto, caja.x1, caja.y0)
        desplazamiento = 0.0
    else:
        c0, c1, c2, c3 = C["caja"]
        caja = fitz.Rect(ancla.rect[2] + c0, ancla.rect[1] + c1, ancla.rect[2] + c2, ancla.rect[1] + c3)
        bloque = fitz.Rect(caja.x0, caja.y0, caja.x1, caja.y0 + alto)
        desplazamiento = bloque.height + HUECO_CONCEPTOS
    x_importe = ancla.rect[2] if C["importe_dx"] is None else caja.x1 + C["importe_dx"]
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
        if C["totales_fijos"]:
            # El hueco tiene que estar libre: ni texto, ni imágenes, ni trazos (las líneas tienen alto 0: se
            # ensanchan), salvo los del propio recuadro de totales y los que contienen el bloque entero (el marco).
            cosas = [fitz.Rect(b[:4]) for b in page.get_text("blocks") if b[4].strip()] + \
                [fitz.Rect(i["bbox"]) for i in page.get_image_info()] + \
                [pad(tuple(d["rect"])) for d in dibujos
                 if not pad(tuple(caja), 1).contains(fitz.Rect(d["rect"]))
                 and not pad(tuple(d["rect"]), 1).contains(bloque)]
            if any(c.intersects(bloque) for c in cosas):
                return None, [f"conceptos: no caben {len(conceptos)} líneas añadidas encima de los totales; "
                              "quita alguna o júntalas"]
            trazo = recuadro.get("color") or (0, 0, 0)
            # Grosor 0 = línea finísima del PDF; draw_rect la pintaría de 1 pt: se imita con una fina.
            ancho = recuadro.get("width")
            return BloqueConceptos(ancla.pagina, caja, bloque, desplazamiento, size, ancla.color, tuple(trazo),
                                   1.0 if ancho is None else (ancho or TRAZO_FINO), x_importe, y_titulo, filas,
                                   totales_fijos=True), []
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


def _segmentos(dibujos: list[dict]) -> list[tuple[fitz.Point, fitz.Point, dict]]:
    """Cada línea recta de los dibujos de la página, con el dibujo al que pertenece (color y grosor)."""
    return [(it[1], it[2], d) for d in dibujos for it in d["items"] if it[0] == "l"]


def _fila_modelo(doc: fitz.Document, ex: Extraccion, pagina_totales: int, x0: float, x1: float
                 ) -> tuple[Linea, float, float] | None:
    """La última fila de la fábrica con todo su texto en una sola línea (primero en la página de totales; si no,
    en las anteriores): la línea, el alto de su fila y su línea base desde el borde superior. Las filas
    principales (varias líneas, «Referencia:» encima) no sirven de modelo."""
    for p in sorted({ln.pagina for ln in ex.lineas}, key=lambda p: (p != pagina_totales, -p)):
        page = doc[p]
        hs = sorted({a.y for a, b, _ in _segmentos(page.get_drawings())
                     if abs(a.y - b.y) < 0.5 and min(a.x, b.x) <= x0 + 2 and max(a.x, b.x) >= x1 - 2})
        spans = page_spans(page)
        for ln in reversed([ln for ln in ex.lineas if ln.pagina == p and ln.tipo != "descuento"]):
            sup = max((h for h in hs if h <= ln.rect_total[1] + 0.5), default=None)
            inf = min((h for h in hs if h >= ln.rect_total[3] - 0.5), default=None)
            if sup is None or inf is None:
                continue
            dentro = [sp for sp in spans if sp.bbox[1] > sup - 0.5 and sp.bbox[3] < inf + 0.5]
            if all(abs(sp.origin[1] - ln.origin_total[1]) <= 1 for sp in dentro):
                return ln, inf - sup, ln.origin_total[1] - sup
    return None


def _columna_precio(ex: Extraccion, modelo: Linea, fuente: fitz.Font, size: float) -> tuple[bool, float]:
    """¿Los precios del original van alineados a la derecha (mismo borde derecho)? Si no, centrados en su columna.
    Devuelve eso y el borde derecho o el centro, medidos en la fila modelo."""
    derechas = [ln.rect_importe[2] for ln in ex.lineas if ln.tipo != "descuento" and not ln.en_totales]
    if len(derechas) >= 2 and max(derechas) - min(derechas) <= 1:
        return True, modelo.rect_importe[2]
    texto = modelo.precio.strip() if modelo.precio else modelo.importe
    return False, modelo.rect_importe[2] - fuente.text_length(texto, fontsize=size) / 2


def _filas_texto(conceptos: list[Concepto], fuente: fitz.Font, size: float, ancho: float
                 ) -> tuple[list[list[str]], list[str]]:
    """Descripción de cada concepto partida en 1–2 líneas que quepan en la columna; errores "conceptos:"."""
    err: list[str] = []
    out: list[list[str]] = []
    for c in conceptos:
        texto = " ".join(c.texto.split())
        raros = sorted({ch for ch in texto if not fuente.has_glyph(ord(ch))})
        if not texto:
            err.append("conceptos: hay una línea sin texto")
            continue
        if raros:
            err.append(f"conceptos: «{texto[:30]}» lleva caracteres no admitidos {raros}")
            continue
        lineas = envolver(texto, ancho, size, fuente)
        if len(lineas) > 2 or any(fuente.text_length(l, fontsize=size) > ancho for l in lineas):
            err.append(f"conceptos: «{texto[:30]}…» es demasiado largo; acórtalo")
            continue
        out.append(lineas)
    return out, err


def _plan_bandas(doc: fitz.Document, ex: Extraccion, T: dict, conceptos: list[Concepto], n: int,
                 caja: fitz.Rect, tabla: fitz.Rect, dibujos: list[dict]) -> tuple[BloqueConceptos | None, list[str]]:
    """Tabla sin líneas entre filas: las filas nuevas van a continuación del último texto de la tabla, con el
    paso entre líneas de la descripción, la letra de la última fila de la fábrica y la banda gris alterna de las
    filas con precio (la primera, al revés que la última de la fábrica)."""
    C, TB = T["conceptos"], T["tabla"]
    page = doc[n]
    lineas_fab = [ln for ln in ex.lineas if ln.tipo != "descuento"]
    modelo = ([ln for ln in lineas_fab if ln.pagina == n] or lineas_fab)[-1]
    size, negrita = modelo.size, modelo.negrita_total
    fuente = fuente_de(T["fuente"], negrita)[0]
    spans = page_spans(page)
    titulo_uds = C["col_uds"] or TB["col_uds"]
    cab_uds = next((sp for sp in spans if titulo_uds and sp.text.strip() == titulo_uds), None)
    cab_desc = next((sp for sp in spans if sp.text.strip() == TB["col_descripcion"]), None)
    uds = [sp.bbox[2] for sp in spans if cab_uds is not None and UDS.match(sp.text)
           and abs(sp.bbox[2] - cab_uds.bbox[2]) <= TB["tol_x"] and cab_uds.bbox[3] < sp.bbox[1] < caja.y0]
    if cab_desc is None or cab_uds is None:
        return None, ["conceptos: no se encuentran las columnas de descripción o de uds. de la tabla"]
    # Uds. alineadas a la derecha como las de la fábrica; sin ninguna en la página, en el borde del título.
    x_uds = sorted(uds)[len(uds) // 2] if uds else cab_uds.bbox[2]
    dentro = [sp for sp in spans if tabla.x0 < sp.bbox[0] < tabla.x1 and tabla.y0 < sp.bbox[1] and sp.bbox[3] < caja.y0 - 1]
    x_desc = min((sp.bbox[0] for sp in dentro if abs(sp.bbox[0] - cab_desc.bbox[0]) <= 5), default=cab_desc.bbox[0])
    tops = sorted({round(sp.bbox[1], 1) for sp in dentro if abs(sp.bbox[0] - x_desc) <= 2})
    pasos = [b - a for a, b in zip(tops, tops[1:]) if b - a >= size]
    if not pasos or not dentro:
        return None, ["conceptos: no se reconocen las filas de la tabla"]
    paso = min(pasos)
    columnas = sorted({a.x for a, b, _ in _segmentos(dibujos) if abs(a.x - b.x) < 0.5 and abs(a.y - b.y) > 5
                       and tabla.x0 + 1 < a.x < tabla.x1 - 1 and max(a.y, b.y) >= caja.y0 - 2})
    izquierda = [x for x in columnas if x < x_uds - 10]
    ancho = (izquierda[-1] if izquierda else cab_uds.bbox[0]) - 3 - x_desc
    textos, err = _filas_texto(conceptos, fuente, size, ancho)
    if err:
        return None, err
    precio_derecha, x_precio = _columna_precio(ex, modelo, fuente, size)

    # Banda gris de las filas con precio: celdas, desfase respecto al texto de la fila y color.
    def celdas_de(p: int, ln: Linea) -> list[fitz.Rect]:
        return [fitz.Rect(d["rect"]) for d in doc[p].get_drawings() if d.get("fill") is not None
                and d["rect"].height < size * 2 and d["rect"].width < tabla.width * 0.9
                and d["rect"].y0 < ln.rect_total[3] and d["rect"].y1 > ln.rect_total[1]]
    muestra = next(((ln, c) for ln in reversed(lineas_fab) if (c := celdas_de(ln.pagina, ln))), None)
    relleno = None
    if muestra is not None:
        ln_m, cs = muestra
        relleno = next(d.get("fill") for d in doc[ln_m.pagina].get_drawings()
                       if d.get("fill") is not None and _parecido(fitz.Rect(d["rect"]), cs[0], 0.1))
        dy_banda = cs[0].y0 - ln_m.rect_total[1]
        alto_banda = cs[0].height
        xs = sorted((c.x0, c.x1) for c in cs)
    sombreada = bool(celdas_de(modelo.pagina, modelo))

    top = max(sp.bbox[1] for sp in dentro) + paso  # parte de arriba del texto de la primera fila nueva
    base = modelo.origin_total[1] - modelo.rect_total[1]  # línea base desde la parte de arriba del texto
    filas: list[FilaTabla] = []
    for i, (c, lineas) in enumerate(zip(conceptos, textos)):
        banda = []
        if relleno is not None and (i % 2 == 0) != sombreada:
            banda = [(x0, top + dy_banda, x1, top + dy_banda + alto_banda) for x0, x1 in xs]
        filas.append(FilaTabla(lineas, str(c.uds), fmt_eur(c.precio_ud), fmt_eur(c.importe), top - 1,
                               top + paso * len(lineas) - 1, banda))
        top += paso * len(lineas)
    inicio, fondo = filas[0].y0 - 0.5, filas[-1].y1
    zona = fitz.Rect(tabla.x0 + 1, inicio, tabla.x1 - 1, fondo)
    # Libre salvo el marco (lo contiene todo) y las líneas de columna (la atraviesan de arriba abajo).
    cosas = [fitz.Rect(w[:4]) for w in page.get_text("words") if w[4].strip()] + \
        [fitz.Rect(i["bbox"]) for i in page.get_image_info()] + \
        [pad(tuple(d["rect"])) for d in dibujos if not pad(tuple(d["rect"]), 1).contains(zona)
         and not (d["rect"].width < 1 and d["rect"].y0 <= zona.y0 and d["rect"].y1 >= zona.y1)]
    if fondo > caja.y0 - 1 or any(c.intersects(zona) for c in cosas):
        return None, [f"conceptos: no caben {len(conceptos)} líneas añadidas en la tabla; quita alguna o júntalas"]
    tc = TablaConceptos(tabla.x0, tabla.x1, [], fondo, None, 0.0, filas, base + 1, paso, x_desc, x_uds, x_precio,
                        modelo.rect_total[2], size, modelo.color, negrita, 0.0, T["fuente"], precio_derecha,
                        lineas=False, relleno=relleno)
    return BloqueConceptos(n, caja, fitz.Rect(tabla.x0, inicio, tabla.x1, fondo), 0.0, size, modelo.color,
                           (0, 0, 0), TRAZO_FINO, modelo.rect_total[2], 0.0, [], tabla=tc), []


def plan_conceptos_tabla(pdf: bytes, ex: Extraccion, T: dict, conceptos: list[Concepto]
                         ) -> tuple[BloqueConceptos | None, list[str]]:
    """Conceptos como filas de la tabla de productos, debajo de la última fila de la página de totales: mide
    columnas, altura de fila, letra y alineación en el propio PDF. La fila vacía que cierra la tabla baja debajo
    de las filas nuevas; el recuadro de totales y el marco no se mueven."""
    C, TB = T["conceptos"], T["tabla"]
    ancla = get_total(ex, C["ancla"])
    if ancla is None or any(t.pagina != ancla.pagina for t in ex.totales):
        return None, ["conceptos: los totales no están juntos en una página"]
    if C["filas"] == "rayas":
        return _plan_rayas(pdf, ex, T, conceptos, ancla.pagina)
    n = ancla.pagina
    previas = [ln for ln in ex.lineas if ln.pagina == n and ln.tipo != "descuento"]
    if not previas:
        return None, ["conceptos: no hay filas de la fábrica en la página de los totales"]
    ultima = previas[-1]
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        page = doc[n]
        r = _caja_de(page, ancla.rect)
        if r is None:
            return None, ["conceptos: no se encuentra el recuadro de totales en el PDF"]
        caja = fitz.Rect(r)
        dibujos = page.get_drawings()
        # Marco de la tabla: el recuadro de ancho completo que acaba donde empieza el de totales.
        tabla = next((fitz.Rect(d["rect"]) for d in dibujos if abs(d["rect"].y1 - caja.y0) <= 1.5
                      and d["rect"].width > page.rect.width * 0.7 and d["rect"].y0 < ultima.rect_total[1]), None)
        if tabla is None:
            return None, ["conceptos: no se encuentra el marco de la tabla en el PDF"]
        if C["filas"] == "bandas":
            return _plan_bandas(doc, ex, T, conceptos, n, caja, tabla, dibujos)
        segs = _segmentos(dibujos)
        verticales = [(a.x, max(a.y, b.y)) for a, b, _ in segs if abs(a.x - b.x) < 0.5 and abs(a.y - b.y) > 5
                      and tabla.x0 + 1 < a.x < tabla.x1 - 1 and max(a.y, b.y) < caja.y0 - 1]
        horizontales = [(a.y, min(a.x, b.x), max(a.x, b.x), d) for a, b, d in segs if abs(a.y - b.y) < 0.5
                        and min(a.x, b.x) <= tabla.x0 + 2 and max(a.x, b.x) >= tabla.x1 - 2 and a.y < caja.y0 - 1]
        if not verticales or not horizontales:
            return None, ["conceptos: no se reconocen las líneas de la tabla"]
        y_columnas = max(y for _, y in verticales)
        columnas = sorted({round(x, 2) for x, y in verticales if abs(y - y_columnas) <= 1})
        cierre = next((h for h in horizontales if abs(h[0] - y_columnas) <= 1), None)
        encima = sorted({round(h[0], 2) for h in horizontales if h[0] < y_columnas - 1})
        palabras = [fitz.Rect(w[:4]) for w in page.get_text("words") if w[4].strip()]
        # La fila vacía de cierre (entre la última horizontal y la de cierre) baja debajo de las filas nuevas.
        y_inicio, alto_cierre, y_cierre = y_columnas, 0.0, None
        if cierre is not None and encima:
            franja = fitz.Rect(tabla.x0, encima[-1] + 0.5, tabla.x1, cierre[0] - 0.5)
            if not any(w.intersects(franja) for w in palabras):
                y_inicio, alto_cierre, y_cierre = encima[-1], cierre[0] - encima[-1], cierre[0]
        # Altura, línea base y letra: las de una fila de la fábrica de una sola línea.
        ref = _fila_modelo(doc, ex, n, tabla.x0, tabla.x1)
        if ref is None or ref[2] >= ref[1]:
            return None, ["conceptos: no se reconocen las filas de la tabla"]
        modelo, alto_fila, baseline = ref
        size = modelo.size
        spans = page_spans(page)
        titulo_uds = C["col_uds"] or TB["col_uds"]
        cab_uds = next((sp for sp in spans if titulo_uds and sp.text.strip() == titulo_uds), None)
        cab_desc = next((sp for sp in spans if sp.text.strip() == TB["col_descripcion"]), None)
        uds = [sp.bbox[2] for sp in spans if cab_uds is not None and UDS.match(sp.text)
               and abs(sp.bbox[2] - cab_uds.bbox[2]) <= TB["tol_x"] and cab_uds.bbox[3] < sp.bbox[1] < caja.y0]
        if not uds or cab_desc is None:
            return None, ["conceptos: no se encuentran las columnas de descripción o de uds. de la tabla"]
        x_uds = sorted(uds)[len(uds) // 2]
        fila_modelo = [sp for sp in page_spans(doc[modelo.pagina]) if abs(sp.bbox[1] - modelo.rect_total[1]) <= 2.5
                       and cab_desc.bbox[0] - 5 <= sp.bbox[0] < x_uds - 20]
        x_desc = min((sp.bbox[0] for sp in fila_modelo), default=cab_desc.bbox[0])
        izquierda = [x for x in columnas if x < x_uds - 10]
        if not izquierda:
            return None, ["conceptos: no se encuentran las columnas de la tabla"]
        ancho = izquierda[-1] - 3 - x_desc
        negrita = modelo.negrita_total
        fuente = fuente_de(T["fuente"], negrita)[0]
        precio_derecha, x_precio = _columna_precio(ex, modelo, fuente, size)
        interlinea = size * INTERLINEA_FILA

        textos, err = _filas_texto(conceptos, fuente, size, ancho)
        if err:
            return None, err
        filas: list[FilaTabla] = []
        y = y_inicio
        for c, lineas in zip(conceptos, textos):
            alto = alto_fila + interlinea * (len(lineas) - 1)
            filas.append(FilaTabla(lineas, str(c.uds), fmt_eur(c.precio_ud), fmt_eur(c.importe), y, y + alto))
            y += alto
        fondo = y + alto_cierre
        # Lo que ocuparán las filas por debajo de donde acaban hoy las columnas tiene que estar libre (salvo el
        # marco de la tabla, que lo contiene todo).
        zona = fitz.Rect(tabla.x0 + 1, y_columnas + 1, tabla.x1 - 1, fondo)
        cosas = palabras + [fitz.Rect(i["bbox"]) for i in page.get_image_info()] + \
            [pad(tuple(d["rect"])) for d in dibujos if not pad(tuple(d["rect"]), 1).contains(zona)]
        if fondo > caja.y0 - 0.5 or any(c.intersects(zona) for c in cosas):
            return None, [f"conceptos: no caben {len(conceptos)} líneas añadidas en la tabla; quita alguna o júntalas"]
        ref = min(horizontales, key=lambda h: abs(h[0] - y_inicio))
        trazo = ref[3].get("color") or (0, 0, 0)
        ancho_trazo = ref[3].get("width")
        tc = TablaConceptos(ref[1], ref[2], columnas, y_columnas, y_cierre, alto_cierre, filas, baseline, interlinea,
                            x_desc, x_uds, x_precio, modelo.rect_total[2], size, modelo.color, negrita,
                            1.0 if ancho_trazo is None else ancho_trazo, T["fuente"], precio_derecha)
    return BloqueConceptos(n, caja, fitz.Rect(tabla.x0, y_inicio, tabla.x1, fondo), 0.0, size, modelo.color,
                           tuple(trazo), 1.0 if ancho_trazo is None else (ancho_trazo or TRAZO_FINO),
                           modelo.rect_total[2], 0.0, [], tabla=tc), []


def _es_raya(d: dict) -> bool:
    """Trazo fino horizontal (relleno o línea) que separa filas."""
    return d["rect"].height <= 1.5 and d["rect"].width >= 10


def _plan_rayas(pdf: bytes, ex: Extraccion, T: dict, conceptos: list[Concepto], n: int
                ) -> tuple[BloqueConceptos | None, list[str]]:
    """Tabla sin marco con una raya fina bajo cada fila: las filas nuevas van tras la raya de la última fila de la
    página de totales, cada una con su raya, la letra y los márgenes de la última fila de la fábrica. Lo que hay
    debajo (los totales…) baja lo que ocupan, sin pasar del margen inferior (el pie)."""
    C, TB = T["conceptos"], T["tabla"]
    filas_pag = [a for ln in ex.lineas if not ln.en_totales and ln.tipo != "descuento"
                 for a in ln.apariciones() if a.pagina == n]
    if not filas_pag:
        return None, ["conceptos: no hay filas de la fábrica en la página de los totales"]
    ultima = max(filas_pag, key=lambda a: a.rect_total[1])
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        page = doc[n]
        dibujos = page.get_drawings()
        spans = page_spans(page)
        rayas = [d for d in dibujos if _es_raya(d)]
        debajo = [d for d in rayas if d["rect"].y0 > ultima.rect_total[1]]
        if not debajo:
            return None, ["conceptos: no se reconocen las rayas de la tabla"]
        y_raya = min(d["rect"].y0 for d in debajo)
        raya = [d for d in debajo if d["rect"].y0 - y_raya <= 0.5]
        alto_raya = max(d["rect"].height for d in raya)
        color_raya = raya[0].get("fill") or raya[0].get("color") or (0, 0, 0)
        # Margen entre la raya de encima y el texto de la fila (el mismo abajo), y paso entre líneas.
        encima = [d["rect"].y1 for d in rayas if d["rect"].y1 <= ultima.rect_total[1]]
        margen = min(max(ultima.rect_total[1] - max(encima), 3.0), 12.0) if encima else 5.0
        cab_desc = next((s for s in spans if s.text.strip() == TB["col_descripcion"]), None)
        fin_desc = next((s for s in spans if TB["col_fin_descripcion"] and s.text.strip() == TB["col_fin_descripcion"]), None)
        titulo_uds = C["col_uds"] or TB["col_uds"] or TB["col_cantidad"]
        cab_uds = next((s for s in spans if s.text.strip() == titulo_uds), None)
        if cab_desc is None or fin_desc is None or cab_uds is None:
            return None, ["conceptos: no se encuentran las columnas de descripción o de uds. de la tabla"]
        x_desc = cab_desc.bbox[0]
        tops = sorted({round(s.bbox[1], 1) for s in spans if abs(s.bbox[0] - x_desc) <= 2
                       and abs(s.size - ultima.size) < 0.1})
        pasos = [b - a for a, b in zip(tops, tops[1:]) if ultima.size <= b - a <= ultima.size * 2]
        interlinea = min(pasos) if pasos else ultima.size * INTERLINEA_FILA
        uds = [s.bbox[2] for s in spans if UDS.match(s.text) and abs(s.bbox[2] - cab_uds.bbox[2]) <= TB["tol_x"]
               and cab_uds.bbox[3] < s.bbox[1] < y_raya]
        x_uds = sorted(uds)[len(uds) // 2] if uds else cab_uds.bbox[2]
        negrita = ultima.negrita_total
        fuente = fuente_de(T["fuente"], negrita)[0]
        size = ultima.size
        textos, err = _filas_texto(conceptos, fuente, size, fin_desc.bbox[0] - 3 - x_desc)
        if err:
            return None, err
        precio_derecha, x_precio = _columna_precio(ex, ultima, fuente, size)
        hueco = ultima.rect_total[2] - x_uds - 3  # el importe no puede pisar la cantidad
        largos = [c.texto for c in conceptos
                  if fuente.text_length(fmt_eur(c.importe) + ultima.sufijo_total, fontsize=size) > hueco]
        if largos:
            return None, [f"conceptos: el importe de «{largos[0][:30]}» no cabe en su columna"]
        alto_texto = ultima.rect_total[3] - ultima.rect_total[1]
        y = inicio = y_raya + alto_raya
        filas: list[FilaTabla] = []
        for c, lineas in zip(conceptos, textos):
            alto = 2 * margen + alto_texto + interlinea * (len(lineas) - 1)
            filas.append(FilaTabla(lineas, str(c.uds), fmt_eur(c.precio_ud) + ultima.sufijo_importe,
                                   fmt_eur(c.importe) + ultima.sufijo_total, y, y + alto))
            y += alto + alto_raya
        desplazamiento = y - inicio
        # Lo que hay debajo hasta el margen inferior baja entero; nada puede quedar a caballo de la raya.
        limite = page.rect.height - C["margen_inferior"]
        zona = fitz.Rect(0, inicio + 0.5, page.rect.width, limite)
        cosas = [fitz.Rect(b[:4]) for b in page.get_text("blocks") if b[4].strip()] + \
            [fitz.Rect(i["bbox"]) for i in page.get_image_info()] + \
            [fitz.Rect(d["rect"]) for d in dibujos if d not in raya]
        cosas = [c for c in cosas if c.intersects(zona)]
        if not cosas or any(c.y0 < zona.y0 for c in cosas) or max(c.y1 for c in cosas) + desplazamiento > limite:
            return None, [f"conceptos: no caben {len(conceptos)} líneas añadidas en la página de los totales; "
                          "quita alguna o júntalas"]
        mover = fitz.Rect(0, zona.y0, page.rect.width, max(c.y1 for c in cosas) + 1)
        x0, x1 = min(d["rect"].x0 for d in raya), max(d["rect"].x1 for d in raya)
        tc = TablaConceptos(x0, x1, [], y, None, 0.0, filas, margen + ultima.origin_total[1] - ultima.rect_total[1],
                            interlinea, x_desc, x_uds, x_precio, ultima.rect_total[2], size, ultima.color, negrita,
                            0.0, T["fuente"], precio_derecha, lineas=False,
                            rayas=sorted((d["rect"].x0, d["rect"].x1) for d in raya), alto_raya=alto_raya,
                            color_raya=tuple(color_raya))
    return BloqueConceptos(n, fitz.Rect(), fitz.Rect(x0, inicio, x1, y), desplazamiento, size, ultima.color,
                           (0, 0, 0), TRAZO_FINO, ultima.rect_total[2], 0.0, [], mover=mover, tabla=tc), []


def stamp_conceptos_tabla(page: fitz.Page, b: BloqueConceptos) -> None:
    """Quita la línea de cierre de la tabla y dibuja las filas nuevas, la fila de cierre y las columnas alargadas."""
    t = b.tabla
    if t.y_cierre is not None:
        page.add_redact_annot(fitz.Rect(t.x0 - 1, t.y_cierre - 0.6, t.x1 + 1, t.y_cierre + 0.6), fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
        page.insert_font(fontname=FONT_NAME, fontbuffer=FONT.buffer)
        page.insert_font(fontname=FONT_REG_NAME, fontbuffer=FONT_REG.buffer)
        insertar_fuentes(page, t.familia)
    fondo = b.bloque.y1
    for f in t.filas:
        for r in f.banda:
            page.draw_rect(fitz.Rect(r), color=None, fill=t.relleno, width=0)
        for x0, x1 in t.rayas:
            page.draw_rect(fitz.Rect(x0, f.y1, x1, f.y1 + t.alto_raya), color=None, fill=t.color_raya, width=0)
    if t.lineas:
        _lineas_tabla(page, b, fondo)
    nombre_fuente = fuente_de(t.familia, t.negrita)
    _textos_tabla(page, t, nombre_fuente)


def _lineas_tabla(page: fitz.Page, b: BloqueConceptos, fondo: float) -> None:
    """Separador de cada fila nueva, línea de la fila de cierre y columnas alargadas, con el grosor original."""
    t = b.tabla
    sh = page.new_shape()
    for y in [f.y1 for f in t.filas] + ([fondo] if t.alto_cierre else []):
        sh.draw_line((t.x0, y), (t.x1, y))
    for x in t.columnas:
        sh.draw_line((x, t.y_columnas), (x, fondo))
    if t.grosor == 0:
        # PyMuPDF no escribe "0 w" (deja el grosor por defecto, 1 pt): se pone a mano antes de las líneas.
        sh.draw_cont = "0 w\n" + sh.draw_cont
        sh.finish(color=b.trazo, width=1)
    else:
        sh.finish(color=b.trazo, width=t.grosor)
    sh.commit()


def _textos_tabla(page: fitz.Page, t: TablaConceptos, nombre_fuente: tuple[fitz.Font, str]) -> None:
    """Descripción, uds., precio e importe de cada fila nueva, con la letra y la alineación de la tabla."""
    fuente, nombre = nombre_fuente
    rgb = tuple(((t.color >> s) & 0xFF) / 255 for s in (16, 8, 0))
    for f in t.filas:
        y = f.y0 + t.baseline
        for i, linea in enumerate(f.lineas):
            page.insert_text((t.x_desc, y + i * t.interlinea), linea, fontname=nombre, fontsize=t.size, color=rgb)
        stamp_right(page, f.uds, t.x_uds, y, t.size, t.color, regular=not t.negrita, familia=t.familia)
        if t.precio_derecha:
            stamp_right(page, f.precio, t.x_precio, y, t.size, t.color, regular=not t.negrita, familia=t.familia)
        else:
            page.insert_text((t.x_precio - fuente.text_length(f.precio, fontsize=t.size) / 2, y), f.precio,
                             fontname=nombre, fontsize=t.size, color=rgb)
        stamp_right(page, f.importe, t.x_importe, y, t.size, t.color, regular=not t.negrita, familia=t.familia)


def verify_conceptos_tabla(doc: fitz.Document, b: BloqueConceptos) -> list[str]:
    """Cada fila añadida está en su sitio con su descripción, uds., precio e importe."""
    page = doc[b.pagina]
    for f in b.tabla.filas:
        texto = _normal(page.get_text(clip=fitz.Rect(b.tabla.x0, f.y0, b.tabla.x1, f.y1)))
        if any(_normal(v) not in texto for v in (*f.lineas, f.uds, f.precio, f.importe)):
            return ["conceptos añadidos no estampados"]
    return []


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
    Los totales se estampan aparte, desplazados. Con totales fijos, solo se dibuja el bloque."""
    if b.tabla is not None:
        stamp_conceptos_tabla(page, b)
        return
    if not b.totales_fijos:
        page.add_redact_annot(pad(tuple(b.caja), 2), fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        page.insert_font(fontname=FONT_NAME, fontbuffer=FONT.buffer)
        page.insert_font(fontname=FONT_REG_NAME, fontbuffer=FONT_REG.buffer)
    for r in (b.bloque,) if b.totales_fijos else (b.bloque, b.caja_nueva):
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
    if len(datos) < 2 or tam_texto(una, zona, TAM_PIE, [FONT_REG]) is not None:
        return una
    corte = min(range(1, len(datos)), key=lambda i: abs(len(" · ".join(datos[:i])) - len(" · ".join(datos[i:]))))
    return [" · ".join(datos[:corte]), " · ".join(datos[corte:])]


def marca_efectiva(marca: Marca, T: dict, conservar_fabrica: bool) -> Marca:
    """Aplica pie_por_defecto: si la plantilla lo pide y el montador no tiene pie, sus datos van al pie.
    El texto del recuadro solo se usa en formatos que lo definen (en los demás, el recuadro es del cliente
    final o no existe): donde no, se ignora, como las condiciones en los PDFs que no las traen."""
    M = T["marca"]
    if marca.recuadro and (M is None or M["recuadro"] is None):
        marca = replace(marca, recuadro=[])
    if conservar_fabrica or marca.pie or not marca.datos or M is None or M["pie_por_defecto"] != "datos":
        return marca
    return replace(marca, pie=pie_de_datos(marca.datos, M["zona_pie"]))


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


def envolver(texto: str, ancho: float, size: float, fuente: fitz.Font = FONT_REG) -> list[str]:
    """Cada línea del texto, partida en palabras para que quepa en el ancho; las vacías se mantienen."""
    out: list[str] = []
    for parrafo in texto.split("\n"):
        linea = ""
        for palabra in parrafo.split():
            prueba = f"{linea} {palabra}".strip()
            if linea and fuente.text_length(prueba, fontsize=size) > ancho:
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
                zona = fitz.Rect(a.x0 + z0, a.y0 + z1, a.x0 + z2,
                                 min(a.y0 + z3, page.rect.height - C["margen_inferior"])) & page.rect
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
             iva_pct: Decimal | None = None, precios_lineas: dict[int, Decimal] | None = None) -> dict:
    """Ventanas: coste de fábrica + % (global o de la línea). Montaje: precio del montador, sin %.
    Portes: coste + %, o el precio que ponga el montador (precios_lineas), sin %.

    Conceptos añadidos por el montador: precio final, sin %, suman a la base."""
    precios_lineas = precios_lineas or {}
    lineas = []
    for i, ln in enumerate(ex.lineas):
        if ln.tipo == "descuento":
            p, unit = None, Decimal("0.00")  # oculto al cliente: solo baja el coste del montador
        elif ln.tipo == "montaje":
            p = None
            unit = montaje if montaje is not None else parse_eur(ln.importe)
        elif ln.tipo == "portes" and i in precios_lineas:
            p, unit = None, precios_lineas[i]
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
    # Subtotal de la tabla (filas y conceptos añadidos), sin las líneas de los totales (envío).
    en_totales = sum((l["total"] for l, ln in zip(lineas, ex.lineas) if ln.en_totales), Decimal(0))
    return {"lineas": lineas, "subtotales": subtotales_esperados(ex, [l["total"] for l in lineas]),
            "subtotal": base - en_totales,
            "conceptos": [{"texto": c.texto, "importe": c.importe, "uds": c.uds, "precio": c.precio_ud}
                          for c in conceptos],
            "extra": extra, "base": base, "iva_pct": iva_pct, "iva": iva, "total": base + iva}


def stamp_right(page: fitz.Page, text: str, x1: float, y: float, size: float, color: int,
                regular: bool = False, familia: str = "helvetica") -> None:
    """Texto alineado a la derecha en x1. regular: fuente normal (ya insertada en la página), p. ej. un título."""
    font, nombre = fuente_de(familia, not regular)
    w = font.text_length(text, fontsize=size)
    rgb = tuple(((color >> s) & 0xFF) / 255 for s in (16, 8, 0))
    page.insert_text((x1 - w, y), text, fontname=nombre, fontsize=size, color=rgb)


def stamp_texto(page: fitz.Page, text: str, rect: tuple, y: float, size: float, color: int, izquierda: bool,
                regular: bool = False, familia: str = "helvetica") -> None:
    """Texto en el sitio de otro: desde su x0 (alineado a la izquierda) o hasta su x1 (a la derecha)."""
    if not izquierda:
        stamp_right(page, text, rect[2], y, size, color, regular=regular, familia=familia)
        return
    _, nombre = fuente_de(familia, not regular)
    rgb = tuple(((color >> s) & 0xFF) / 255 for s in (16, 8, 0))
    page.insert_text((rect[0], y), text, fontname=nombre, fontsize=size, color=rgb)


def pad(r: tuple, d: float = 0.5) -> fitz.Rect:
    return fitz.Rect(r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def stamp_bloque(page: fitz.Page, lineas: list[str], zona: tuple, tams: tuple[float, float],
                 derecha: bool, primera_negrita: bool = False, pie: bool = False) -> None:
    """Líneas centradas en vertical en la zona, alineadas a la izquierda o a la derecha.
    primera_negrita: solo la primera en negrita y el resto en normal (si no, todas en negrita).
    pie: todas en normal y centradas en la página (sin salirse de la zona, que no siempre está centrada)."""
    z = fitz.Rect(zona)
    fuentes = fuentes_recuadro(lineas) if primera_negrita else [FONT_REG] * len(lineas) if pie else None
    t = tam_texto(lineas, zona, tams, fuentes)
    assert t is not None  # validate_marca ya lo comprobó
    paso = t * INTERLINEA
    y = z.y0 + (z.height - paso * len(lineas)) / 2 + t
    for i, linea in enumerate(lineas):
        normal = pie or (primera_negrita and i)
        fuente, nombre = (FONT_REG, FONT_REG_NAME) if normal else (FONT, FONT_NAME)
        if derecha:
            stamp_right(page, linea, z.x1, y, t, 0)
        else:
            x = z.x0
            if pie:
                ancho = fuente.text_length(linea, fontsize=t)
                x = min(max(page.rect.width / 2 - ancho / 2, z.x0), z.x1 - ancho)
            page.insert_text((x, y), linea, fontname=nombre, fontsize=t, color=(0, 0, 0))
        y += paso


def lineas_que_empiezan(page: fitz.Page, prefijos: list[str]) -> list[fitz.Rect]:
    """Líneas de texto que empiezan por alguno de los prefijos (sin distinguir mayúsculas), enteras."""
    pre = [p.lower() for p in prefijos]
    if not pre:
        return []
    return [fitz.Rect(line["bbox"]) for b in page.get_text("dict")["blocks"] for line in b.get("lines", [])
            if any("".join(s["text"] for s in line["spans"]).strip().lower().startswith(p) for p in pre)]


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
                     pc: PlanCondiciones | None = None, recuadros: dict[int, fitz.Rect] | None = None) -> bytes:
    """recuadros: zona del recuadro del montador por página (solo si trae texto propio para él)."""
    recuadros = recuadros or {}
    doc = fitz.open(stream=pdf, filetype="pdf")
    labels = {"Base Imponible": "Base Imponible", "Total": "Total", "TOTAL": "TOTAL"}

    fam = T["fuente"]

    def normal(negrita: bool) -> bool:
        """¿Se estampa en letra normal? Solo si la plantilla lo pide y el original no iba en negrita."""
        return T["grosor_original"] and not negrita
    for page in doc:
        n = page.number
        for z in zonas_marca(marca, T, n):
            page.add_redact_annot(fitz.Rect(z), fill=False)
        if n in recuadros:  # su contenido (datos del montador); el borde del recuadro se conserva
            page.add_redact_annot(recuadros[n], fill=False)
        if pc is not None and pc.pagina == n:
            page.add_redact_annot(pc.zona | (pc.borrar or pc.zona), fill=False)
        # Precios y totales: redacción sin relleno para conservar el fondo de las filas.
        for a in (a for l in ex.lineas for a in l.apariciones() if a.pagina == n):
            page.add_redact_annot(pad(a.rect_importe), fill=False)
            page.add_redact_annot(pad(a.rect_total), fill=False)
            if a.caja is not None:
                page.add_redact_annot(fitz.Rect(a.caja), fill=False)
        # Bloques que se quitan siempre (envío, cuenta bancaria…).
        for p, r, _ in ex.bloques:
            if p == n:
                page.add_redact_annot(fitz.Rect(r), fill=False)
        for t in (t for t in ex.totales if t.pagina == n):
            page.add_redact_annot(pad(t.rect), fill=False)
            if t.pct_rect is not None:
                page.add_redact_annot(pad(t.pct_rect), fill=False)
        for st in (st for st in ex.subtotales if st.pagina == n):
            page.add_redact_annot(pad(st.rect), fill=False)
        for pagina, r in ex.vaciar:
            if pagina == n:
                page.add_redact_annot(pad(r), fill=False)
        for r in lineas_que_empiezan(page, T["lineas_fuera"]):
            page.add_redact_annot(pad(tuple(r)), fill=False)
        # Datos de fábrica (salvo que el montador elija conservar su logo y su nombre).
        if not conservar_fabrica:
            if n == T["logo_pagina"]:
                page.add_redact_annot(fitz.Rect(T["zona_logo_fabrica"]), fill=False)
            for z in T["zonas_fabrica_todas"]:
                page.add_redact_annot(fitz.Rect(z), fill=False)
            for p, r in ex.fuera:  # columnas que se quitan (el nombre del fabricante…)
                if p == n:
                    page.add_redact_annot(fitz.Rect(r), fill=False)
            for palabra in T["lista_negra"]:
                for r in page.search_for(palabra):
                    page.add_redact_annot(pad(tuple(r)), fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        # Segunda pasada con los recuadros de descuento y los bloques con trazos: fuera también sus bordes,
        # barras y subrayados.
        segunda = [pad(l.caja, 2) for l in ex.lineas if l.pagina == n and l.caja is not None] + \
            [fitz.Rect(r) for p, r, trazos in ex.bloques if p == n and trazos]
        if segunda:
            for r in segunda:
                page.add_redact_annot(r, fill=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)
        page.insert_font(fontname=FONT_NAME, fontbuffer=FONT.buffer)
        page.insert_font(fontname=FONT_REG_NAME, fontbuffer=FONT_REG.buffer)
        insertar_fuentes(page, fam)
        dy = 0.0
        if bc is not None and bc.pagina == n and bc.mover is not None:
            mover_contenido(doc, page, bc.mover, bc.desplazamiento)
            page.insert_font(fontname=FONT_NAME, fontbuffer=FONT.buffer)
            page.insert_font(fontname=FONT_REG_NAME, fontbuffer=FONT_REG.buffer)
            insertar_fuentes(page, fam)
        if pc is not None and pc.pagina == n:
            stamp_condiciones(page, pc)
        if bc is not None and bc.pagina == n:
            stamp_conceptos(page, bc)
            dy = bc.desplazamiento

        # Lo que estaba en la franja que ha bajado para hacer sitio a los conceptos se escribe más abajo.
        movida = bc.mover if bc is not None and bc.pagina == n else None

        def bajada(rect: tuple) -> float:
            return bc.desplazamiento if movida is not None and movida.contains(fitz.Rect(rect)) else 0.0
        for ln, nuevo in zip(ex.lineas, calc["lineas"]):
            if ln.tipo == "descuento":
                continue
            # Modo tabla: en la columna de precio, el precio por unidad de medida (total / cantidad).
            unit = nuevo["importe"] if ln.cantidad is None else (nuevo["total"] / ln.cantidad).quantize(CENT, ROUND_HALF_UP)
            for a in (a for a in ln.apariciones() if a.pagina == n):
                if a.formato is not None:  # línea suelta de los totales: el texto entero con el número nuevo
                    stamp_texto(page, a.formato.format(valor=fmt_eur(nuevo["total"])), a.rect_total,
                                a.origin_total[1] + bajada(a.rect_total), a.size, a.color, a.izquierda,
                                regular=normal(a.negrita_total), familia=fam)
                    continue
                stamp_right(page, fmt_eur(unit) + a.sufijo_importe, a.rect_importe[2],
                            a.origin_importe[1] + bajada(a.rect_importe), a.size, a.color,
                            regular=normal(a.negrita_importe), familia=fam)
                stamp_right(page, fmt_eur(nuevo["total"]) + a.sufijo_total, a.rect_total[2],
                            a.origin_total[1] + bajada(a.rect_total), a.size, a.color,
                            regular=normal(a.negrita_total), familia=fam)
        for st, valor in zip(ex.subtotales, calc["subtotales"]):
            if st.pagina == n:
                stamp_right(page, fmt_eur(valor), st.rect[2], st.origin[1], st.size, st.color,
                            regular=normal(st.negrita), familia=fam)
        valores = {"IVA": calc["iva"], "TOTAL": calc["total"], "Subtotal": calc["subtotal"]}
        for t in (t for t in ex.totales if t.pagina == n):
            if t.formato is not None:  # total en una línea suelta: el texto entero con los números nuevos
                pct = fmt_pct(calc["iva_pct"], t.iva_pct) if t.iva_pct is not None else ""
                stamp_texto(page, t.formato.format(valor=fmt_eur(valores.get(t.label, calc["base"])), pct=pct),
                            t.rect, t.origin[1] + dy, t.size, t.color, t.izquierda, regular=normal(t.negrita),
                            familia=fam)
                continue
            if t.solo_valor:
                valor = valores.get(t.label, calc["base"])
                stamp_right(page, fmt_eur(valor) + t.sufijo, t.rect[2], t.origin[1] + dy, t.size, t.color,
                            regular=normal(t.negrita), familia=fam)
                if t.pct_rect is not None:
                    pct = fmt_pct(calc["iva_pct"], t.iva_pct)
                    if t.pct_marco is not None:  # % dentro del título de la columna: el título entero, en normal
                        page.insert_font(fontname=FONT_REG_NAME, fontbuffer=FONT_REG.buffer)
                        stamp_right(page, t.pct_marco[0] + pct + t.pct_marco[1], t.pct_rect[2],
                                    t.pct_origin[1] + dy, t.size, t.color, regular=True)
                    else:
                        stamp_right(page, pct, t.pct_rect[2], t.pct_origin[1] + dy, t.size, t.color,
                                    regular=normal(t.negrita), familia=fam)
                continue
            if t.label == "IVA":
                txt = f"I.V.A {fmt_pct(calc['iva_pct'], t.iva_pct)} % : {fmt_eur(calc['iva'])} €"
            else:
                valor = calc["total"] if t.label == "TOTAL" else calc["base"]
                txt = f"{labels[t.label]}: {fmt_eur(valor)} €"
            stamp_right(page, txt, t.rect[2], t.origin[1] + dy, t.size, t.color, regular=normal(t.negrita),
                        familia=fam)

        if marca is not None and not marca.vacia:
            M = T["marca"]
            if marca.logo and n == M["logo_pagina"]:
                pix = logo_pixmap(marca.logo)
                page.insert_image(rect_logo(pix.width, pix.height, M["zona_logo"], M["logo_derecha"]), pixmap=pix)
            if marca.datos and n == M["datos_pagina"]:
                stamp_bloque(page, marca.datos, M["zona_datos"], TAM_DATOS, derecha=True)
            if marca.pie:
                stamp_bloque(page, marca.pie, M["zona_pie"], TAM_PIE, derecha=False, pie=True)
            if marca.recuadro and n in recuadros:
                stamp_bloque(page, marca.recuadro, zona_texto_recuadro(recuadros[n], T), TAM_RECUADRO,
                             derecha=False, primera_negrita=True)

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
    if marca.recuadro:
        # En cada página con recuadro, solo el texto del montador: nada del original.
        con_recuadro = 0
        for page in doc:
            z = zona_recuadro(page, T)
            if z is None:
                continue
            con_recuadro += 1
            resto = _normal(page.get_text(clip=z))
            for linea in marca.recuadro:
                if _normal(linea) not in resto:
                    err.append(f"p{page.number + 1}: texto del recuadro no estampado")
                    break
                resto = resto.replace(_normal(linea), "", 1)
            if resto.strip():
                err.append(f"p{page.number + 1}: quedan restos del texto original en el recuadro")
            if any(fitz.Rect(i["bbox"]).intersects(z) for i in page.get_image_info()):
                err.append(f"p{page.number + 1}: queda una imagen en el recuadro")
        if not con_recuadro:
            err.append("recuadro del montador no encontrado en el PDF generado")
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
        for a in ln.apariciones():  # la fila y sus repeticiones en la página siguiente
            out += [fmt_eur(c["total"])] if a.en_totales else [fmt_eur(unit), fmt_eur(c["total"])]
    out += [fmt_eur(v) for v in calc["subtotales"]]
    out += [fmt_eur(calc[k]) for k in ("base", "base", "iva", "total")]  # Base Imponible, Total, IVA, TOTAL
    if get_total(ex, "Subtotal") is not None:
        out.append(fmt_eur(calc["subtotal"]))
    # Los conceptos no: sus importes solo pueden estar en su bloque, que verify ya no mira; contarlos aquí
    # justificaría un precio de fábrica igual que quedara fuera.
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
    if bc is not None and bc.tabla is not None:
        err += verify_conceptos_tabla(doc, bc)
    elif bc is not None:
        pagina = doc[bc.pagina]
        en_bloque = _normal(pagina.get_text(clip=bc.bloque))
        for linea in [TITULO_CONCEPTOS] + [t for f in bc.filas for t in f[:2]]:
            if _normal(linea) not in en_bloque:
                err.append("conceptos añadidos no estampados")
                break
    if bc is not None:
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
        for a in ln.apariciones():
            campos[(a.pagina, tuple(a.rect_total))] = ln.total
            campos[(a.pagina, tuple(a.rect_importe))] = ln.precio if ln.cantidad is not None else ln.importe
    campos |= {(t.pagina, tuple(t.rect)): t.valor for t in src_ex.totales} | \
        {(st.pagina, tuple(st.rect)): st.valor for st in src_ex.subtotales}
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
    for prefijo in T["lineas_fuera"]:
        if prefijo.lower() in bajo:
            err.append(f"'{prefijo}' sigue en el texto")
    # Columnas y bloques que se quitan: vacíos (los bloques, también de imágenes y, si los tenían, de trazos).
    for p, r in ([] if conservar_fabrica else src_ex.fuera):
        if doc[p].get_text(clip=fitz.Rect(r)).strip():
            err.append(f"p{p + 1}: queda texto en una columna que se quita")
    for p, r, trazos in src_ex.bloques:
        z, page = fitz.Rect(r), doc[p]
        if page.get_text(clip=z).strip() or any(fitz.Rect(i["bbox"]).intersects(z) for i in page.get_image_info()):
            err.append(f"p{p + 1}: queda contenido en un bloque que se quita")
        elif trazos and any(z.contains(d["rect"]) for d in page.get_drawings()):
            err.append(f"p{p + 1}: quedan trazos en un bloque que se quita")

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
    if bc is not None and bc.tabla is not None:
        # Las filas añadidas tienen forma de fila de la tabla: no son líneas de la fábrica.
        out_ex.lineas = [ln for ln in out_ex.lineas
                         if not (ln.pagina == bc.pagina and fitz.Rect(ln.rect_total).intersects(bc.bloque))]
    # Un concepto del montador puede caer a la altura de la columna: lo de su bloque no cuenta.
    if any(not (bc is not None and p == bc.pagina and fitz.Rect(r).intersects(bc.bloque)) for p, r in out_ex.vaciar):
        err.append("queda el descuento de fábrica en su columna")
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
    esperados = {"IVA": calc["iva"], "TOTAL": calc["total"]}
    for label, valor in (("Base Imponible", calc["base"]), ("Total", calc["base"]), ("Subtotal", calc["subtotal"])):
        if get_total(src_ex, label):
            esperados[label] = valor
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
    if not ((base or get_total(ex, "Subtotal")) and iva and total):
        return None
    return {"base": base_fabrica(ex), "iva_pct": parse_pct(iva.iva_pct),
            "iva": parse_eur(iva.valor), "total": parse_eur(total.valor)}


def analizar(pdf: bytes, T: dict) -> Resultado:
    """Paso de identificación/extracción/validación: lo que el montador revisa antes de generar."""
    ex, errores = _analisis(pdf, T)
    datos = {"plantilla": T["id"], "estado": "needs_review" if errores else "ok", "errores": errores,
             "n_dibujos": ex.n_dibujos, "lineas": _lineas_publicas(ex, None),
             "totales_fabrica": _totales_fabrica(ex) if not errores else None,
             # La plantilla sabe dónde añadir conceptos del montador (instalación, portes…).
             "admite_conceptos": T["conceptos"] is not None,
             # Texto del recuadro del destinatario (None si la plantilla no lo define o el PDF no lo tiene).
             "recuadro": None if errores else texto_recuadro(pdf, T)}
    return Resultado(datos, dibujos=[] if errores else recortar_dibujos(pdf, ex))


def generar(pdf: bytes, T: dict, pct: Decimal, modo: str = "markup", montaje: Decimal | None = None,
            pct_lineas: dict[int, Decimal] | None = None, marca: Marca | None = None,
            conservar_fabrica: bool = False, conceptos: list[Concepto] | None = None,
            iva_pct: Decimal | None = None, diagnostico: bool = False,
            precios_lineas: dict[int, Decimal] | None = None) -> Resultado:
    """Analiza de nuevo (sin estado), calcula, redacta, estampa y verifica la salida.

    diagnostico: solo para herramientas internas; con needs_review devuelve igualmente el PDF generado
    (Resultado.ok sigue en False: el servicio no lo entrega nunca).

    conservar_fabrica: deja el logo y el nombre de la fábrica (los precios se eliminan siempre).
    conceptos: líneas añadidas por el montador (sin %), en un bloque encima de los totales.
    precios_lineas: precio del montador para líneas de portes (sin %), por índice de línea."""
    pct_lineas = pct_lineas or {}
    precios_lineas = precios_lineas or {}
    conceptos = list(conceptos or [])
    T = plantilla_efectiva(T, conservar_fabrica)
    marca = marca_efectiva(marca or Marca(), T, conservar_fabrica)
    ex, errores = _analisis(pdf, T)
    bc = pc = None
    if not errores and montaje is not None and not tiene_montaje(ex) and T["conceptos"] is not None:
        # Compatibilidad: montaje sin línea en el PDF = concepto "Instalación".
        conceptos, montaje = [Concepto("Instalación", montaje), *conceptos], None
    if not errores:
        errores = validate_params(ex, montaje, pct_lineas, T, precios_lineas) + validate_marca(marca, T, conservar_fabrica)
    if not errores:
        bc, errores = plan_conceptos(pdf, ex, T, conceptos)
        pc, err_cond = plan_condiciones(pdf, T, marca, conservar_fabrica, bc)
        errores += err_cond
    datos = {"plantilla": T["id"], "pct": pct, "modo": modo, "montaje": montaje,
             "pct_lineas": {str(k): v for k, v in sorted(pct_lineas.items())},
             "precios_lineas": {str(k): v for k, v in sorted(precios_lineas.items())},
             "marca": not marca.vacia, "conservar_fabrica": conservar_fabrica, "n_dibujos": ex.n_dibujos}
    calc = salida = None
    miniaturas: list[bytes] = []
    if not errores:
        calc = calcular(ex, pct, modo, montaje, pct_lineas, conceptos, iva_pct, precios_lineas)
    if calc is not None and calc["base"] < 0:
        errores, calc = ["conceptos: los descuentos dejan el presupuesto en negativo"], None
    recuadros = zonas_recuadro(pdf, T) if calc is not None and marca.recuadro else {}
    if calc is not None and marca.recuadro and not recuadros:
        errores, calc = ["no se encuentra el recuadro del montador para su texto"], None
    if calc is not None:
        salida = redact_and_stamp(pdf, ex, calc, T, marca, conservar_fabrica, bc, pc, recuadros)
        with fitz.open(stream=pdf, filetype="pdf") as orig:
            texto_original = "\n".join(p.get_text() for p in orig)
        errores, miniaturas = verify(ex, calc, salida, T, marca, conservar_fabrica, bc, pc, texto_original)
    datos["lineas"] = _lineas_publicas(ex, calc)
    datos["totales"] = {k: calc[k] for k in ("base", "iva_pct", "iva", "total")} if calc else None
    datos["conceptos"] = calc["conceptos"] if calc else [{"texto": c.texto, "importe": c.importe, "uds": c.uds, "precio": c.precio_ud}
                                                                         for c in conceptos]
    datos["estado"] = "needs_review" if errores else "ok"
    datos["errores"] = errores
    if errores:
        return Resultado(datos, salida if diagnostico else None)  # sin verificación no hay entrega
    return Resultado(datos, salida, miniaturas)


def jsonable(o):
    """Importes como texto con 2 decimales y punto ("1300.00"): sin pérdidas por float."""
    if isinstance(o, Decimal):
        return f"{o:.2f}"
    raise TypeError(type(o))
