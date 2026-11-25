"""Pedido de tienda en línea en tabla: importes con sufijo («€+IVA»), filas que siguen en la página siguiente
repitiendo sus importes, totales en líneas sueltas (con alguna línea más que lleva el %, como el envío), columnas
y bloques que se quitan enteros, y conceptos como filas con su raya debajo (lo de debajo baja para hacer sitio).
Usa una plantilla privada de ../templates/ con "totales_lineas" y los PDFs de ../samples/ que reconozca; si no
hay, se salta (el repo público no lleva ninguno). Los tests sin PDF construyen la extracción a mano."""

from __future__ import annotations

import re
from decimal import Decimal

import pymupdf as fitz
import pytest

import motor
from plantillas_locales import pares, ultimas

PARES = pares(ultimas(lambda p: "totales_lineas" in p))
IDS = [f"pdf{i}" for i in range(1, len(PARES) + 1)]
casos = pytest.mark.parametrize(("plantilla", "sample"), PARES, ids=IDS)
con_pdf = pytest.mark.skipif(not PARES, reason="sin plantilla con totales en líneas o sin PDFs de ese formato")

CONCEPTOS = [motor.Concepto("Instalación", Decimal(400), 1, Decimal(400)),
             motor.Concepto("Retirada de marcos antiguos", Decimal(100), 2, Decimal(50)),
             motor.Concepto("Descuento", Decimal(-50), 1, Decimal(-50))]


def _analisis(plantilla: dict, pdf: bytes) -> tuple[dict, motor.Extraccion]:
    T = motor.compile_template(plantilla)
    ex, err = motor._analisis(pdf, T)
    assert not err, err
    return T, ex


def _filas(ex: motor.Extraccion) -> list[motor.Linea]:
    return [ln for ln in ex.lineas if not ln.en_totales]


def _abrir(pdf: bytes) -> fitz.Document:
    return fitz.open(stream=pdf, filetype="pdf")


@con_pdf
@casos
def test_fila_partida_entre_paginas_es_una_sola_linea(plantilla, sample):
    _, ex = _analisis(plantilla, sample.read_bytes())
    partidas = [ln for ln in ex.lineas if ln.ecos]
    assert partidas, "el PDF de prueba tiene una fila que sigue en la página siguiente"
    for ln in partidas:
        for e in ln.ecos:
            assert e.pagina == ln.pagina + 1
            assert (e.total, e.precio, e.cantidad) == (ln.total, ln.precio, ln.cantidad)
    assert sum(motor.parse_eur(ln.total) for ln in _filas(ex)) == motor.parse_eur(motor.get_total(ex, "Subtotal").valor)


@con_pdf
@casos
def test_la_referencia_lleva_toda_la_celda_sin_el_pie(plantilla, sample):
    pdf = sample.read_bytes()
    _, ex = _analisis(plantilla, pdf)
    doc = _abrir(pdf)
    for ln in _filas(ex):
        spans = motor.page_spans(doc[ln.pagina])
        segunda = [s for s in spans if s.bbox[0] < ln.rect_importe[0] - 150
                   and ln.rect_total[3] + 1 < s.bbox[1] < ln.rect_total[3] + 14]
        assert segunda and segunda[0].text.strip() in ln.ref, (ln.ref, segunda)
    for ln in (ln for ln in _filas(ex) if ln.ecos):
        siguiente = doc[ln.ecos[0].pagina]
        primera = min((s for s in motor.page_spans(siguiente) if abs(s.bbox[1] - ln.ecos[0].rect_total[1]) <= 2
                       and s.bbox[0] < ln.ecos[0].rect_importe[0] - 150), key=lambda s: s.bbox[0])
        assert primera.text.strip() in ln.ref
    negras = [n.lower() for n in plantilla["redaccion"]["lista_negra"]]
    assert not any(n in ln.ref.lower() for ln in _filas(ex) for n in negras)


@con_pdf
@casos
def test_importes_con_su_sufijo_tambien_en_la_fila_partida(plantilla, sample):
    pdf = sample.read_bytes()
    T, ex = _analisis(plantilla, pdf)
    r = motor.generar(pdf, T, Decimal(30))
    assert r.ok, r.datos["errores"]
    out = _abrir(r.pdf)
    ex_s = motor.extract(out, T)
    assert [(ln.sufijo_importe, ln.sufijo_total, len(ln.ecos)) for ln in ex.lineas] == \
        [(ln.sufijo_importe, ln.sufijo_total, len(ln.ecos)) for ln in ex_s.lineas]
    assert any(ln.sufijo_total for ln in ex.lineas)
    for ln, c in ((ln, c) for ln, c in zip(ex.lineas, r.datos["lineas"]) if not ln.en_totales):
        for e in [ln, *ln.ecos]:
            # El texto que acaba donde acababa el original, en su misma línea (el importe nuevo puede ser más ancho).
            s = next(s for s in motor.page_spans(out[e.pagina]) if abs(s.bbox[2] - e.rect_total[2]) <= 1
                     and abs(s.origin[1] - e.origin_total[1]) <= 1)
            assert s.text.strip() == motor.fmt_eur(c["total"]) + ln.sufijo_total


@con_pdf
@casos
def test_totales_en_su_sitio_y_con_el_iva_elegido(plantilla, sample):
    pdf = sample.read_bytes()
    T, ex = _analisis(plantilla, pdf)
    r = motor.generar(pdf, T, Decimal(30), iva_pct=Decimal(10))
    assert r.ok, r.datos["errores"]
    ex_s = motor.extract(_abrir(r.pdf), T)
    for t in ex.totales:
        s = motor.get_total(ex_s, t.label)
        assert s is not None and s.formato == t.formato, t.label
        assert abs(s.rect[0] - t.rect[0]) <= 0.5 and abs(s.origin[1] - t.origin[1]) <= 0.5, t.label
    assert motor.parse_pct(motor.get_total(ex_s, "IVA").iva_pct) == 10
    sub = motor.parse_eur(motor.get_total(ex_s, "Subtotal").valor)
    assert sub == sum(c["total"] for ln, c in zip(ex.lineas, r.datos["lineas"]) if not ln.en_totales)


def _con_linea_de_totales(pdf: bytes, T: dict, ex: motor.Extraccion, valor: Decimal) -> bytes:
    """El PDF con un importe en la línea de los totales que lleva el % (p. ej. portes) y los totales cuadrados."""
    linea = next(ln for ln in ex.lineas if ln.en_totales)
    sub = motor.parse_eur(motor.get_total(ex, "Subtotal").valor)
    iva = motor.get_total(ex, "IVA")
    pct = motor.parse_pct(iva.iva_pct)
    base = sub + valor
    v_iva = (base * pct / 100).quantize(motor.CENT)
    nuevos = [(linea.pagina, linea.rect_total, linea.origin_total, linea.formato.format(valor=motor.fmt_eur(valor)))]
    nuevos += [(iva.pagina, iva.rect, iva.origin, iva.formato.format(valor=motor.fmt_eur(v_iva), pct=iva.iva_pct))]
    t = motor.get_total(ex, "TOTAL")
    nuevos += [(t.pagina, t.rect, t.origin, t.formato.format(valor=motor.fmt_eur(base + v_iva)))]
    doc = _abrir(pdf)
    for p, rect, _, _ in nuevos:
        doc[p].add_redact_annot(motor.pad(rect), fill=False)
    for p in {n[0] for n in nuevos}:
        doc[p].apply_redactions(text=fitz.PDF_REDACT_TEXT_REMOVE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        doc[p].insert_font(fontname="F0", fontbuffer=fitz.Font("helv").buffer)
    for p, rect, origin, texto in nuevos:
        doc[p].insert_text((rect[0], origin[1]), texto, fontname="F0", fontsize=10)
    return doc.tobytes()


def _con_fuente_de_prueba(plantilla: dict) -> dict:
    """Los PDFs retocados en los tests llevan una fuente más."""
    return {**plantilla, "huella": {**plantilla["huella"], "fuentes": plantilla["huella"]["fuentes"] + ["Helvetica", "Nimbus Sans Regular"]}}


@con_pdf
@casos
def test_la_linea_de_los_totales_lleva_el_pct(plantilla, sample):
    pdf = sample.read_bytes()
    T, ex = _analisis(plantilla, pdf)
    assert [ln.ref for ln in ex.lineas if ln.en_totales] == list(plantilla["totales_lineas"]["lineas"])
    plantilla = _con_fuente_de_prueba(plantilla)
    pdf = _con_linea_de_totales(pdf, T, ex, Decimal(10))
    T, ex = _analisis(plantilla, pdf)
    i = next(i for i, ln in enumerate(ex.lineas) if ln.en_totales)
    assert motor.parse_eur(ex.lineas[i].total) == 10
    r = motor.generar(pdf, T, Decimal(30))
    assert r.ok, r.datos["errores"]
    assert r.datos["lineas"][i]["pct"] == 30 and r.datos["lineas"][i]["total"] == Decimal("13.00")
    ex_s = motor.extract(_abrir(r.pdf), T)
    assert motor.parse_eur(ex_s.lineas[i].total) == Decimal("13.00")
    base = r.datos["totales"]["base"]
    assert motor.parse_eur(motor.get_total(ex_s, "Subtotal").valor) == base - Decimal("13.00")


def _portes(plantilla: dict) -> list[str]:
    """Líneas de los totales que son portes (el montador puede poner su precio)."""
    return [k for k, v in plantilla["totales_lineas"]["lineas"].items() if isinstance(v, dict) and v.get("tipo") == "portes"]


@con_pdf
@casos
@pytest.mark.parametrize("coste", [Decimal(0), Decimal(10)])
def test_portes_con_el_precio_del_montador_sin_pct(plantilla, sample, coste):
    if not _portes(plantilla):
        pytest.skip("la plantilla no tiene portes")
    pdf = sample.read_bytes()
    T, ex = _analisis(plantilla, pdf)
    if coste:
        plantilla = _con_fuente_de_prueba(plantilla)
        pdf = _con_linea_de_totales(pdf, T, ex, coste)
        T, ex = _analisis(plantilla, pdf)
    i = next(i for i, ln in enumerate(ex.lineas) if ln.tipo == "portes")
    r = motor.generar(pdf, T, Decimal(30), precios_lineas={i: Decimal("25.50")})
    assert r.ok, r.datos["errores"]
    assert r.datos["lineas"][i]["pct"] is None and r.datos["lineas"][i]["total"] == Decimal("25.50")
    ex_s = motor.extract(_abrir(r.pdf), T)
    assert motor.parse_eur(ex_s.lineas[i].total) == Decimal("25.50")
    assert ex_s.lineas[i].formato.format(valor="25,50") in _abrir(r.pdf)[ex_s.lineas[i].pagina].get_text()
    filas = sum(c["total"] for ln, c in zip(ex.lineas, r.datos["lineas"]) if not ln.en_totales)
    assert r.datos["totales"]["base"] == filas + Decimal("25.50")
    # Sin precio, los portes llevan el % como las demás líneas.
    r = motor.generar(pdf, T, Decimal(30))
    assert r.ok and r.datos["lineas"][i]["pct"] == 30 and r.datos["lineas"][i]["total"] == coste * Decimal("1.3")


@con_pdf
@casos
def test_precio_de_portes_por_http(plantilla, sample):
    from test_servicio import Almacen, cliente, generar
    if not _portes(plantilla):
        pytest.skip("la plantilla no tiene portes")
    _, ex = _analisis(plantilla, sample.read_bytes())
    i = next(i for i, ln in enumerate(ex.lineas) if ln.tipo == "portes")
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), plantilla=plantilla, precios_lineas={str(i): "12.30"}).json()
    assert d["estado"] == "ok", d["errores"]
    assert d["lineas"][i]["pct"] is None and d["lineas"][i]["total"] == "12.30" and almacen.subidas
    r = generar(cliente(Almacen(sample.read_bytes())), plantilla=plantilla, precios_lineas={str(i): "-1"})
    assert r.status_code == 400  # parámetros no válidos


@con_pdf
@casos
def test_precio_propio_solo_en_portes(plantilla, sample):
    pdf = sample.read_bytes()
    T, ex = _analisis(plantilla, pdf)
    i = next(i for i, ln in enumerate(ex.lineas) if ln.tipo != "portes")
    r = motor.generar(pdf, T, Decimal(30), precios_lineas={i: Decimal(10)})
    assert not r.ok and any("precio propio" in e for e in r.datos["errores"])
    r = motor.generar(pdf, T, Decimal(30), precios_lineas={99: Decimal(10)})
    assert not r.ok and any("no existe" in e for e in r.datos["errores"])


@con_pdf
@casos
def test_columnas_fuera_vacias_salvo_conservando_la_fabrica(plantilla, sample):
    pdf = sample.read_bytes()
    T, ex = _analisis(plantilla, pdf)
    assert ex.fuera
    orig = _abrir(pdf)
    assert all(orig[p].get_text(clip=fitz.Rect(r)).strip() for p, r in ex.fuera)
    r = motor.generar(pdf, T, Decimal(30))
    assert r.ok, r.datos["errores"]
    out = _abrir(r.pdf)
    assert not [(p, r) for p, r in ex.fuera if out[p].get_text(clip=fitz.Rect(r)).strip()]
    for c in plantilla["tabla"]["columnas_fuera"]:
        assert c["titulo"] not in "".join(p.get_text() for p in out)
    r = motor.generar(pdf, T, Decimal(30), conservar_fabrica=True)
    assert r.ok, r.datos["errores"]
    out = _abrir(r.pdf)
    assert all(out[p].get_text(clip=fitz.Rect(r)).strip() for p, r in ex.fuera)


@con_pdf
@casos
@pytest.mark.parametrize("conservar", [False, True])
def test_bloques_fuera_vacios_trazos_incluidos(plantilla, sample, conservar):
    pdf = sample.read_bytes()
    T, ex = _analisis(plantilla, pdf)
    assert len({tuple(b[1]) for b in ex.bloques}) >= len(plantilla["redaccion"]["bloques_fuera"])
    orig = _abrir(pdf)
    assert any(d for p, z, trazos in ex.bloques if trazos
               for d in orig[p].get_drawings() if fitz.Rect(z).contains(d["rect"]))
    r = motor.generar(pdf, T, Decimal(30), conservar_fabrica=conservar)
    assert r.ok, r.datos["errores"]
    out = _abrir(r.pdf)
    for p, z, trazos in ex.bloques:
        z = fitz.Rect(z)
        assert not out[p].get_text(clip=z).strip()
        if trazos:
            assert not [d for d in out[p].get_drawings() if z.contains(d["rect"])]


@con_pdf
@casos
def test_sin_el_ancla_de_un_bloque_no_se_admite(plantilla, sample):
    """Si el PDF cambia y el bloque no aparece, su contenido (p. ej. el coste del envío) podría quedarse."""
    pdf = sample.read_bytes()
    plantilla = _con_fuente_de_prueba(plantilla)
    T = motor.compile_template(plantilla)
    ancla = re.compile(plantilla["redaccion"]["bloques_fuera"][0]["ancla"])
    doc = _abrir(pdf)
    for page in doc:
        for r, t in motor._lineas_texto(page):
            if ancla.search(t):
                page.add_redact_annot(r, fill=False)
        page.apply_redactions(text=fitz.PDF_REDACT_TEXT_REMOVE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    a = motor.analizar(doc.tobytes(), T)
    assert not a.ok and any("bloque" in e for e in a.datos["errores"]), a.datos["errores"]


@con_pdf
@casos
def test_conceptos_como_filas_con_raya_y_los_totales_bajan(plantilla, sample):
    pdf = sample.read_bytes()
    T, ex = _analisis(plantilla, pdf)
    r = motor.generar(pdf, T, Decimal(30), conceptos=CONCEPTOS)
    assert r.ok, r.datos["errores"]
    out = _abrir(r.pdf)
    ex_s = motor.extract(out, T)
    sub = motor.get_total(ex, "Subtotal")
    bajadas = {round(motor.get_total(ex_s, t.label).rect[1] - t.rect[1], 1) for t in ex.totales}
    bajadas |= {round(b.rect_total[1] - a.rect_total[1], 1) for a, b in
                zip((ln for ln in ex.lineas if ln.en_totales), (ln for ln in ex_s.lineas if ln.en_totales))}
    assert len(bajadas) == 1 and next(iter(bajadas)) > 20, bajadas
    ultima = [ln for ln in _filas(ex) if ln.pagina == sub.pagina][-1]
    nuevo_sub = motor.get_total(ex_s, "Subtotal")
    page = out[sub.pagina]
    franja = fitz.Rect(0, ultima.rect_total[3] + 1, page.rect.width, nuevo_sub.rect[1] - 1)
    textos = [s.text.strip() for s in motor.page_spans(page) if franja.contains(fitz.Rect(s.bbox))]
    suf = ultima.sufijo_total
    for t in ("Instalación", "400,00" + suf, "Retirada de marcos antiguos", "2", "50,00" + suf, "100,00" + suf,
              "Descuento", "-50,00" + suf):
        assert t in textos, (t, textos)
    rayas = lambda doc: [d for d in doc[sub.pagina].get_drawings()
                         if d["rect"].height <= 1.5 and d["rect"].width > 20 and d.get("fill") is not None]
    assert len(rayas(out)) == len(rayas(_abrir(pdf))) + 3 * len({round(d["rect"].x0) for d in rayas(out)})
    filas = sum(c["total"] for ln, c in zip(ex.lineas, r.datos["lineas"]) if not ln.en_totales)
    assert motor.parse_eur(nuevo_sub.valor) == filas + 450
    assert r.datos["totales"]["base"] == sum(c["total"] for c in r.datos["lineas"]) + 450


@con_pdf
@casos
def test_conceptos_que_no_caben_en_la_pagina_son_error_de_conceptos(plantilla, sample):
    plantilla = {**plantilla, "conceptos": {**plantilla["conceptos"], "max_lineas": 60}}
    T = motor.compile_template(plantilla)
    muchos = [motor.Concepto(f"Concepto {i}", Decimal(10)) for i in range(60)]
    r = motor.generar(sample.read_bytes(), T, Decimal(30), conceptos=muchos)
    assert not r.ok and r.datos["errores"] and all(e.startswith("conceptos:") for e in r.datos["errores"])


@con_pdf
@casos
def test_importe_de_concepto_que_pisaria_la_cantidad_es_error(plantilla, sample):
    T = motor.compile_template(plantilla)
    enorme = [motor.Concepto("Instalación", Decimal("999999.99") * 12, 12, Decimal("999999.99"))]
    r = motor.generar(sample.read_bytes(), T, Decimal(30), conceptos=enorme)
    assert not r.ok and any(e.startswith("conceptos:") and "no cabe" in e for e in r.datos["errores"])


def _linea(pagina: int, total: str, y: float) -> motor.Linea:
    return motor.Linea(pagina, "extra", f"fila {pagina}", 1, total, total, (400, y, 430, y + 10),
                       (530, y, 560, y + 10), (400, y + 8), (530, y + 8), 10, 0, cantidad=Decimal(1), precio=total)


@pytest.mark.parametrize(("subtotal", "n"), [("20,00", 2), ("10,00", 1)])
def test_dos_filas_iguales_en_paginas_seguidas_solo_se_juntan_si_cuadra(subtotal, n):
    """La primera fila de una página con los mismos importes que la última de la anterior es la misma fila,
    salvo que juntarlas descuadre la suma: entonces eran dos filas iguales."""
    a = _linea(0, "10,00", 700)
    a.ecos.append(_linea(1, "10,00", 100))
    ex = motor.Extraccion(lineas=[a], totales=[motor.Total(1, "Subtotal", subtotal, None, (450, 300, 560, 310),
                                                          (450, 308), 10, 0)])
    motor.resolver_partidas(ex)
    assert len(ex.lineas) == n
    assert [ln.pagina for ln in ex.lineas] == ([0, 1] if n == 2 else [0])
    assert all(not ln.ecos for ln in ex.lineas) == (n == 2)
