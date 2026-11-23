"""Tabla con tarifa, descuento de fábrica e importe neto: cantidad (metraje o uds), columna de descuento que
se vacía, % de IVA en el título de su columna, TOTAL con «€» y otros conceptos encima del recuadro de
totales, que no se mueve. Usa una plantilla privada de ../templates/ con "tabla.col_dto" y los PDFs de
../samples/ que reconozca; si no hay, se salta (el repo público no lleva ninguno)."""

from __future__ import annotations

import copy
from decimal import ROUND_HALF_UP, Decimal

import pymupdf as fitz
import pytest

import motor
from plantillas_locales import pares, ultimas
from test_servicio import Almacen, MARCA, cliente, generar

PARES = pares(ultimas(lambda p: "col_dto" in p.get("tabla", {})))
IDS = [f"pdf{i}" for i in range(1, len(PARES) + 1)]
pytestmark = pytest.mark.skipif(not PARES, reason="sin plantilla con columna de descuento o sin PDFs de ese formato")
casos = pytest.mark.parametrize(("plantilla", "sample"), PARES, ids=IDS)


def _con_recuadro(plantilla: dict) -> dict:
    """La plantilla con los conceptos en un recuadro encima de los totales (versión anterior): sigue cubierto."""
    p = copy.deepcopy(plantilla)
    p["conceptos"] = {"ancla": "Base Imponible", "totales_fijos": True, "importe_dx": -6, "max_lineas": 10}
    return p


def _generar(plantilla: dict, sample, **extra) -> tuple[dict, fitz.Document | None]:
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=plantilla, **extra).json()
    return d, fitz.open(stream=almacen.subidas[0], filetype="pdf") if almacen.subidas else None


@casos
def test_lineas_con_cantidad_y_descuento(plantilla, sample):
    T = motor.compile_template(plantilla)
    ex, err = motor._analisis(sample.read_bytes(), T)
    assert err == []
    assert ex.lineas and all(ln.tipo == "extra" and ln.cantidad > 0 for ln in ex.lineas)
    assert any(ln.dto for ln in ex.lineas) and ex.vaciar
    suma = sum((motor.parse_eur(ln.total) for ln in ex.lineas), Decimal(0))
    assert suma == motor.parse_eur(motor.get_total(ex, "Base Imponible").valor)


@casos
@pytest.mark.parametrize("modo", ["markup", "margen"])
def test_generar_sin_descuento_ni_datos_de_fabrica(plantilla, sample, modo):
    d, salida = _generar(plantilla, sample, pct=30, modo=modo, marca=MARCA)
    assert d["estado"] == "ok", d["errores"]
    texto = "".join(p.get_text() for p in salida).lower()
    assert not any(n.lower() in texto for n in plantilla["redaccion"]["lista_negra"])
    ex = motor.extract(salida, motor.compile_template(plantilla))
    assert ex.vaciar == [] and ex.errores == []
    assert [motor.parse_eur(ln.total) for ln in ex.lineas] == [Decimal(ln["total"]) for ln in d["lineas"]]
    for ln in ex.lineas:
        assert ln.dto == 0
        assert abs(ln.cantidad * motor.parse_eur(ln.precio) - motor.parse_eur(ln.total)) <= \
            ln.cantidad * Decimal("0.005") + Decimal("0.01")


@casos
def test_iva_elegido_se_reescribe_en_el_titulo(plantilla, sample):
    d, salida = _generar(plantilla, sample, pct=30, iva_pct="10")
    assert d["estado"] == "ok", d["errores"]
    iva = motor.get_total(motor.extract(salida, motor.compile_template(plantilla)), "IVA")
    assert motor.parse_pct(iva.iva_pct) == 10
    base = Decimal(d["totales"]["base"])
    assert Decimal(d["totales"]["total"]) == base + (base / 10).quantize(Decimal("0.01"), ROUND_HALF_UP)


@casos
def test_importes_largos_se_releen(plantilla, sample):
    """Los totales van centrados: con un 150 % cambian de ancho y se tienen que seguir releyendo."""
    d, _ = _generar(plantilla, sample, pct=150)
    assert d["estado"] == "ok", d["errores"]


@casos
def test_conceptos_encima_sin_mover_los_totales(plantilla, sample):
    plantilla = _con_recuadro(plantilla)
    conceptos = [{"texto": "Instalación", "importe": "400"}, {"texto": "Descuento", "importe": "-50"}]
    d, salida = _generar(plantilla, sample, pct=30, conceptos=conceptos)
    assert d["estado"] == "ok", d["errores"]
    T = motor.compile_template(plantilla)
    original = motor.extract(fitz.open(stream=sample.read_bytes(), filetype="pdf"), T)
    nuevo = motor.extract(salida, T)
    for t in original.totales:
        t2 = motor.get_total(nuevo, t.label)
        assert t2 is not None and abs(t2.origin[1] - t.origin[1]) <= 0.5 and abs(t2.rect[2] - t.rect[2]) <= 1
    pagina = motor.get_total(original, "Base Imponible").pagina
    assert motor.TITULO_CONCEPTOS in salida[pagina].get_text()
    assert Decimal(d["totales"]["base"]) == sum(Decimal(ln["total"]) for ln in d["lineas"]) + 350


@casos
def test_concepto_largo_no_cuenta_como_descuento(plantilla, sample):
    """Un concepto largo llega a la altura de la columna de descuento: no es un resto del descuento."""
    T = motor.compile_template(_con_recuadro(plantilla))
    ex, _ = motor._analisis(sample.read_bytes(), T)
    size = motor.get_total(ex, "Base Imponible").size
    texto = "Instalación"
    while motor.FONT.text_length(texto + " completa", fontsize=size) < 445:
        texto += " completa"
    r = motor.generar(sample.read_bytes(), T, Decimal(30), conceptos=[motor.Concepto(texto, Decimal(400))])
    assert r.ok, r.datos["errores"]


@casos
def test_conceptos_sin_hueco_es_error(plantilla, sample):
    """Algo en el hueco encima de los totales: los conceptos no se montan encima."""
    T = motor.compile_template(_con_recuadro(plantilla))
    ex, _ = motor._analisis(sample.read_bytes(), T)
    base = motor.get_total(ex, "Base Imponible")
    doc = fitz.open(stream=sample.read_bytes(), filetype="pdf")
    y = base.rect[1] - 40
    doc[base.pagina].draw_rect(fitz.Rect(60, y, 200, y + 10), color=(0, 0, 0))
    r = motor.generar(doc.tobytes(), T, Decimal(30), conceptos=[motor.Concepto("Instalación", Decimal(400))])
    assert not r.ok and r.datos["errores"]
    assert all(e.startswith("conceptos:") for e in r.datos["errores"]), r.datos["errores"]


@casos
def test_descuento_en_cascada_se_lee_y_se_quita(plantilla, sample):
    """Descuentos en cascada ("47+0", "50+10"): cuentan como uno solo en el cuadre y no quedan en el PDF,
    tampoco en las filas sin precio."""
    plantilla = copy.deepcopy(plantilla)
    plantilla["huella"]["fuentes"].append("Helvetica")  # el texto de prueba se escribe con Helvetica
    T = motor.compile_template(plantilla)
    doc = fitz.open(stream=sample.read_bytes(), filetype="pdf")
    ex = motor.extract(doc, T)

    def linea_de(p: int, r: tuple):
        return next((ln for ln in ex.lineas if ln.pagina == p and abs(ln.rect_total[1] - r[1]) <= 2.5), None)
    con_precio = next((p, r, linea_de(p, r)) for p, r in ex.vaciar if linea_de(p, r) and linea_de(p, r).dto)
    sin_precio = next((p, r) for p, r in ex.vaciar if linea_de(p, r) is None)
    cambios = [(con_precio[0], con_precio[1], f"{motor.fmt_pct(con_precio[2].dto)}+0"), (*sin_precio, "50+10")]
    for p, r, texto in cambios:
        doc[p].add_redact_annot(motor.pad(r))
        doc[p].apply_redactions()
        doc[p].insert_text((r[0], r[3] - 1.5), texto, fontsize=7, fontname="helv")
    pdf = doc.tobytes()
    ex2, err = motor._analisis(pdf, T)
    assert err == []
    assert linea_de(con_precio[0], con_precio[1]) is not None
    r = motor.generar(pdf, T, Decimal(30))
    assert r.ok, r.datos["errores"]
    texto = "".join(p.get_text() for p in fitz.open(stream=r.pdf, filetype="pdf"))
    assert "50+10" not in texto and "+0" not in texto


def test_descuento_en_cascada_equivale_al_producto():
    assert motor.parse_dto("50+10") == Decimal("55.00")
    assert motor.parse_dto("47") == Decimal("47")
    assert motor.parse_dto("47 %") == Decimal("47")
    assert motor.parse_dto("40+11,67") == Decimal("47.00")
    assert motor.parse_dto("N/A") is None and motor.parse_dto("150") is None


@casos
def test_conservando_logo_de_fabrica_el_del_montador_va_arriba_a_la_derecha(plantilla, sample):
    d, salida = _generar(plantilla, sample, pct=30, conservar_fabrica=True, marca={"logo": MARCA["logo"]})
    assert d["estado"] == "ok", d["errores"]
    zona = motor.pad(tuple(plantilla["marca"]["zona_logo_conservando"]["rect"]), 1)
    imgs = [fitz.Rect(i["bbox"]) for i in salida[0].get_image_info()]
    assert len(imgs) == 2 and sum(1 for r in imgs if zona.contains(r)) == 1


# --- Conceptos como filas de la tabla ("en_tabla") ---

CONCEPTOS_TABLA = [
    {"texto": "Instalación", "importe": "400.00", "uds": 1, "precio": "400.00"},
    {"texto": "Retirada puerta antigua", "importe": "100.00", "uds": 2, "precio": "50.00"},
    {"texto": "Descuento", "importe": "-50.00", "uds": 1, "precio": "-50.00"},
]
LARGO = "RETIRADA Y TRANSPORTE A VERTEDERO DE PUERTA ANTIGUA EXISTENTE"  # 60 caracteres, no cabe en una línea


def _referencias(plantilla: dict, sample) -> tuple[dict, motor.Extraccion, motor.Linea, motor.Total]:
    """Plantilla compilada, extracción del original, última línea de la página de totales y la Base Imponible."""
    T = motor.compile_template(plantilla)
    ex = motor.extract(fitz.open(stream=sample.read_bytes(), filetype="pdf"), T)
    base = motor.get_total(ex, "Base Imponible")
    return T, ex, [ln for ln in ex.lineas if ln.pagina == base.pagina][-1], base


def _nuevas(salida: fitz.Document, ultima: motor.Linea, base: motor.Total) -> list[motor.Span]:
    """Spans entre la última línea de producto y el recuadro de totales: las filas añadidas."""
    tope = motor._caja_de(salida[base.pagina], base.rect)[1]
    return [s for s in motor.page_spans(salida[base.pagina]) if ultima.rect_total[3] < s.bbox[1] < tope]


@casos
def test_conceptos_como_filas_de_la_tabla(plantilla, sample):
    d, salida = _generar(plantilla, sample, pct=30, conceptos=CONCEPTOS_TABLA)
    assert d["estado"] == "ok", d["errores"]
    T, original, ultima, base = _referencias(plantilla, sample)
    nuevo = motor.extract(salida, T)
    for t in original.totales:
        t2 = motor.get_total(nuevo, t.label)
        assert t2 is not None and abs(t2.origin[1] - t.origin[1]) <= 0.5 and abs(t2.rect[2] - t.rect[2]) <= 1
    assert motor.TITULO_CONCEPTOS not in salida[base.pagina].get_text()
    textos = [s.text.strip() for s in _nuevas(salida, ultima, base)]
    for t in ("Instalación", "400,00", "Retirada puerta antigua", "2", "50,00", "100,00", "Descuento", "-50,00"):
        assert t in textos, (t, textos)
    assert Decimal(d["totales"]["base"]) == sum(Decimal(ln["total"]) for ln in d["lineas"]) + 450


@casos
def test_filas_con_la_letra_de_la_tabla(plantilla, sample):
    d, salida = _generar(plantilla, sample, pct=30, conceptos=CONCEPTOS_TABLA)
    assert d["estado"] == "ok", d["errores"]
    _, _, ultima, base = _referencias(plantilla, sample)
    nuevas = _nuevas(salida, ultima, base)
    assert nuevas and all(abs(s.size - ultima.size) <= 0.1 and s.bold == ultima.negrita_total for s in nuevas)


@casos
def test_descripcion_larga_ocupa_dos_lineas(plantilla, sample):
    _, _, ultima, base = _referencias(plantilla, sample)
    assert motor.FONT_REG.text_length(LARGO, fontsize=ultima.size) > 240  # no cabe en la columna
    c = {"texto": LARGO, "importe": "80.00", "uds": 1, "precio": "80.00"}
    d, salida = _generar(plantilla, sample, pct=30, conceptos=[c])
    assert d["estado"] == "ok", d["errores"]
    desc = sorted((s for s in _nuevas(salida, ultima, base) if s.bbox[0] < 300), key=lambda s: s.bbox[1])
    assert len(desc) == 2 and " ".join(s.text.strip() for s in desc) == LARGO


@casos
def test_descripcion_de_tres_lineas_es_error(plantilla, sample):
    T = motor.compile_template(plantilla)
    texto = "PALABRA " * 19
    r = motor.generar(sample.read_bytes(), T, Decimal(30), conceptos=[motor.Concepto(texto, Decimal(1), 1, Decimal(1))])
    assert not r.ok and r.datos["errores"]
    assert all(e.startswith("conceptos:") for e in r.datos["errores"]), r.datos["errores"]


@casos
def test_filas_empiezan_debajo_si_el_cierre_tiene_texto(plantilla, sample):
    """Si la fila vacía que cierra la tabla tiene algo escrito, las filas nuevas van debajo, sin pisarlo."""
    T, _, ultima, base = _referencias(plantilla, sample)
    doc = fitz.open(stream=sample.read_bytes(), filetype="pdf")
    # Con una fuente que ya usa la página (una nueva no pasaría la huella).
    arial = next(f[4] for f in doc[base.pagina].get_fonts() if f[3].endswith("Arial"))
    doc[base.pagina].insert_text((80, ultima.origin_total[1] + 13), "X", fontsize=6, fontname=f"/{arial}")
    r = motor.generar(doc.tobytes(), T, Decimal(30), conceptos=[motor.Concepto("Instalación", Decimal(400))])
    assert r.ok, r.datos["errores"]
    spans = motor.page_spans(fitz.open(stream=r.pdf, filetype="pdf")[base.pagina])
    x = next(s for s in spans if s.text.strip() == "X")
    inst = next(s for s in spans if s.text.strip() == "Instalación")
    assert x.bbox[3] <= inst.bbox[1]


@casos
def test_concepto_con_precio_igual_a_uno_de_fabrica(plantilla, sample):
    _, _, ultima, _ = _referencias(plantilla, sample)
    precio = f"{motor.parse_eur(ultima.precio):.2f}"  # el de la última línea de fábrica de la página de totales
    c = {"texto": "Portes", "importe": precio, "uds": 1, "precio": precio}
    d, _ = _generar(plantilla, sample, pct=30, conceptos=[c])
    assert d["estado"] == "ok", d["errores"]


@casos
def test_concepto_con_caracteres_raros_es_error(plantilla, sample):
    T = motor.compile_template(plantilla)
    r = motor.generar(sample.read_bytes(), T, Decimal(30),
                      conceptos=[motor.Concepto("Instalación \U0001F600", Decimal(400))])
    assert not r.ok and all(e.startswith("conceptos:") for e in r.datos["errores"]), r.datos["errores"]


@casos
def test_diez_conceptos_largos_caben_o_error(plantilla, sample):
    T, _, ultima, base = _referencias(plantilla, sample)
    conceptos = [motor.Concepto(f"{i} {LARGO[:55]}", Decimal(10), 1, Decimal(10)) for i in range(10)]
    r = motor.generar(sample.read_bytes(), T, Decimal(30), conceptos=conceptos)
    if not r.ok:
        assert all(e.startswith("conceptos:") for e in r.datos["errores"]), r.datos["errores"]
        return
    salida = fitz.open(stream=r.pdf, filetype="pdf")
    caja = motor._caja_de(salida[base.pagina], base.rect)
    assert all(s.bbox[3] <= caja[1] for s in _nuevas(salida, ultima, base))


@casos
def test_filas_sin_hueco_es_error(plantilla, sample):
    """Algo en el hueco donde irían las filas: no se dibujan encima."""
    T, _, ultima, base = _referencias(plantilla, sample)
    doc = fitz.open(stream=sample.read_bytes(), filetype="pdf")
    y = ultima.rect_total[3] + 20
    doc[base.pagina].draw_rect(fitz.Rect(60, y, 200, y + 10), color=(0, 0, 0))
    r = motor.generar(doc.tobytes(), T, Decimal(30), conceptos=[motor.Concepto("Instalación", Decimal(400))])
    assert not r.ok and r.datos["errores"]
    assert all(e.startswith("conceptos:") for e in r.datos["errores"]), r.datos["errores"]


@casos
def test_lineas_nuevas_con_el_grosor_de_la_tabla(plantilla, sample):
    """Las líneas de las filas añadidas tienen el mismo grosor que las de la fábrica (finísimas: grosor 0)."""
    d, salida = _generar(plantilla, sample, pct=30, conceptos=CONCEPTOS_TABLA)
    assert d["estado"] == "ok", d["errores"]
    _, _, ultima, base = _referencias(plantilla, sample)
    original = fitz.open(stream=sample.read_bytes(), filetype="pdf")[base.pagina].get_drawings()
    grosores = {d["width"] for d in original if d["rect"].height < 0.5 and d["rect"].width > 400}
    tope = motor._caja_de(salida[base.pagina], base.rect)[1]
    nuevas = [d for d in salida[base.pagina].get_drawings()
              if ultima.rect_total[3] + 5 < d["rect"].y0 and d["rect"].y1 < tope - 1]
    assert nuevas and {d["width"] for d in nuevas} <= grosores, ({d["width"] for d in nuevas}, grosores)


@casos
@pytest.mark.parametrize("uds", [1, 2])
def test_fuga_de_precio_igual_a_un_concepto_se_detecta(plantilla, sample, uds):
    """Un precio de fábrica que quedara fuera de las filas añadidas se detecta aunque un concepto valga lo mismo:
    los importes de los conceptos solo pueden estar en sus filas."""
    T, ex, ultima, _ = _referencias(plantilla, sample)
    pdf = sample.read_bytes()
    ex, _ = motor._analisis(pdf, T)
    precio = motor.parse_eur(ultima.precio)  # un precio de fábrica de la página de totales
    conceptos = [motor.Concepto("Portes", precio * uds, uds, precio)]
    bc, err = motor.plan_conceptos(pdf, ex, T, conceptos)
    assert not err, err
    calc = motor.calcular(ex, Decimal(30), "markup", None, {}, conceptos)
    salida = fitz.open(stream=motor.redact_and_stamp(pdf, ex, calc, T, None, False, bc, None), filetype="pdf")
    salida[0].insert_text((300, 200), ultima.precio.strip(), fontsize=7)  # la «fuga» (verify no mira la huella)
    texto_original = "\n".join(p.get_text() for p in fitz.open(stream=pdf, filetype="pdf"))
    errores, _ = motor.verify(ex, calc, salida.tobytes(), T, None, False, bc, None, texto_original)
    assert errores, "la fuga no se ha detectado"


@casos
def test_ultima_fila_principal_toma_el_modelo_de_una_fila_de_una_linea(plantilla, sample):
    """Si en la página de totales solo queda la fila principal (negrita, varias líneas), las filas nuevas toman
    altura, línea base y letra de una fila de una sola línea (aunque esté en otra página)."""
    T = motor.compile_template(plantilla)
    pdf = sample.read_bytes()
    ex, _ = motor._analisis(pdf, T)
    base = motor.get_total(ex, "Base Imponible")
    en_pagina = [ln for ln in ex.lineas if ln.pagina == base.pagina]
    assert en_pagina[0].negrita_total  # la fila principal va en negrita
    ex.lineas = [ln for ln in ex.lineas if ln.pagina != base.pagina] + en_pagina[:1]
    conceptos = [motor.Concepto("Instalación", Decimal(400), 1, Decimal(400))]
    bc, err = motor.plan_conceptos(pdf, ex, T, conceptos)
    assert not err, err
    fila = bc.tabla.filas[0]
    assert not bc.tabla.negrita and bc.tabla.baseline < fila.y1 - fila.y0
    calc = motor.calcular(ex, Decimal(30), "markup", None, {}, conceptos)
    salida = fitz.open(stream=motor.redact_and_stamp(pdf, ex, calc, T, None, False, bc, None), filetype="pdf")
    assert motor.verify_conceptos_tabla(salida, bc) == []


# --- Recuadro con los datos del montador ("marca.recuadro") ---

RECUADRO = ["Comercial: ANA", "MÓVIL: 600 000 000", "comercial@ejemplo.es"]


def _texto_fuera(page: fitz.Page, marco: fitz.Rect, zona: fitz.Rect) -> list[str]:
    """Palabras del recuadro original que quedan fuera de la zona que se sustituye (código de cliente…)."""
    return [w[4] for w in page.get_text("words") if marco.contains(fitz.Rect(w[:4])) and not fitz.Rect(w[:4]).intersects(zona)]


@casos
def test_texto_propio_en_el_recuadro_del_montador(plantilla, sample):
    if "recuadro" not in plantilla["marca"]:
        pytest.skip("plantilla sin recuadro del montador")
    T = motor.compile_template(plantilla)
    with fitz.open(stream=sample.read_bytes(), filetype="pdf") as orig:
        zonas = {p.number: motor.zona_recuadro(p, T) for p in orig}
        assert all(zonas.values())
        marcos = {n: next(fitz.Rect(d["rect"]) for d in orig[n].get_drawings() if fitz.Rect(d["rect"]).contains(z))
                  for n, z in zonas.items()}
        fuera = {n: _texto_fuera(orig[n], marcos[n], z) for n, z in zonas.items()}
        dentro = {n: orig[n].get_text(clip=z).split() for n, z in zonas.items()}
    d, salida = _generar(plantilla, sample, pct=30, marca={"recuadro": RECUADRO})
    assert d["estado"] == "ok", d["errores"]
    for n, z in zonas.items():
        page = salida[n]
        texto = page.get_text(clip=z)
        assert all(l in texto for l in RECUADRO)
        assert not any(w in texto.split() for w in dentro[n] if w not in " ".join(RECUADRO))
        assert _texto_fuera(page, marcos[n], z) == fuera[n]  # el código de cliente se conserva
        assert any(dr["rect"] == marcos[n] for dr in page.get_drawings())  # y el borde del recuadro
    # La primera línea, en negrita; el resto, en normal.
    spans = [s for b in salida[0].get_text("dict", clip=zonas[0])["blocks"] for l in b["lines"] for s in l["spans"]]
    negrita = {s["text"].strip(): bool(s["flags"] & 16) or "Bold" in s["font"] for s in spans if s["text"].strip()}
    assert negrita[RECUADRO[0]] and not negrita[RECUADRO[1]]


@casos
@pytest.mark.parametrize(("lineas", "error"), [
    (["Comercial: ANA " * 5], "no cabe"),  # 75 caracteres: pasa el contrato, no cabe a 6 pt
    (["a"] * 6 + ["b"], None),  # el contrato admite como mucho 6 líneas
    (["Comercial: ANA ☎"], "caracteres no admitidos"),
])
def test_texto_del_recuadro_que_no_vale(plantilla, sample, lineas, error):
    if "recuadro" not in plantilla["marca"]:
        pytest.skip("plantilla sin recuadro del montador")
    almacen = Almacen(sample.read_bytes())
    r = generar(cliente(almacen), plantilla=plantilla, marca={"recuadro": lineas})
    if error is None:
        assert r.status_code == 400
        return
    d = r.json()
    assert d["estado"] == "needs_review" and any(e.startswith("marca:") and error in e for e in d["errores"])
    assert not almacen.subidas


@casos
def test_recuadro_se_ignora_en_plantillas_que_no_lo_definen(plantilla, sample):
    p = copy.deepcopy(plantilla)
    p["marca"].pop("recuadro", None)
    d, salida = _generar(p, sample, pct=30, marca={"recuadro": RECUADRO})
    assert d["estado"] == "ok", d["errores"]
    assert not any(l in salida[0].get_text() for l in RECUADRO)
