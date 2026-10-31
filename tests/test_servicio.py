"""Tests del servicio HTTP con un almacenamiento falso (httpx.MockTransport).

Los que necesitan PDFs reales usan samples/ y una plantilla real de templates/ (ambas carpetas
privadas, fuera de este repo) y se saltan si faltan. El resto usa tests/plantilla_ejemplo.json.
"""

from __future__ import annotations

import base64
import json
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pymupdf as fitz
import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import servicio  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
REALES = sorted((ROOT / "templates").glob("*.json"))
# La última versión real (la que define zonas de marca); si no hay, la de ejemplo.
PLANTILLA = json.loads((REALES[-1] if REALES else Path(__file__).with_name("plantilla_ejemplo.json"))
                       .read_text(encoding="utf-8"))
ID_PLANTILLA = f"{PLANTILLA['id']}-v{PLANTILLA['version']}"
SAMPLES = sorted((ROOT / "samples").glob("*.pdf")) if REALES else []
# ids sin nombres de cliente (los nombres de archivo los llevan)
IDS = [f"pdf{i}" for i in range(1, len(SAMPLES) + 1)]
con_samples = pytest.mark.skipif(not SAMPLES, reason="sin PDFs en samples/ o sin plantilla real en templates/")

TOKEN = "t" * 40
HOST = "almacen.example"
PDF_URL = f"https://{HOST}/tmp/original.pdf?token=firma"
SALIDA_URL = f"https://{HOST}/tmp/salida.pdf?token=firma"
AUTH = {"authorization": f"Bearer {TOKEN}"}


class Almacen:
    """Sirve un PDF en GET y guarda lo que llega por PUT."""

    def __init__(self, pdf: bytes = b""):
        self.pdf = pdf
        self.subidas: list[bytes] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, content=self.pdf)
        if req.method == "PUT":
            assert req.headers["content-type"] == "application/pdf"
            self.subidas.append(req.content)
            return httpx.Response(200, json={"Key": "tmp/salida.pdf"})
        return httpx.Response(405)


def cliente(almacen: Almacen, max_bytes: int = 20 * 1024 * 1024) -> TestClient:
    cfg = servicio.Config(TOKEN, frozenset({HOST}), max_bytes)
    return TestClient(servicio.crear_app(cfg, httpx.MockTransport(almacen)))


def generar(c: TestClient, **extra) -> httpx.Response:
    cuerpo = {"pdf_url": PDF_URL, "salida_url": SALIDA_URL, "plantilla": PLANTILLA, "pct": 30} | extra
    return c.post("/v1/generar", json=cuerpo, headers=AUTH)


def test_salud():
    assert cliente(Almacen()).get("/salud").json() == {"ok": True}


@pytest.mark.parametrize("cabeceras", [{}, {"authorization": "Bearer otro"}, {"authorization": TOKEN}])
def test_sin_token_valido_401(cabeceras):
    r = cliente(Almacen()).post("/v1/analizar", json={"pdf_url": PDF_URL, "plantilla": PLANTILLA},
                                headers=cabeceras)
    assert r.status_code == 401


@pytest.mark.parametrize("url", ["http://almacen.example/x.pdf", "https://otro.example/x.pdf",
                                 "https://almacen.example.evil.com/x.pdf", "file:///etc/passwd"])
def test_url_no_permitida_400_sin_eco(url):
    almacen = Almacen(b"%PDF")
    r = generar(cliente(almacen), pdf_url=url)
    assert r.status_code == 400
    assert url not in r.text
    assert not almacen.subidas


def test_plantilla_no_valida_400():
    r = generar(cliente(Almacen()), plantilla={"id": "x"})
    assert r.status_code == 400 and "no válida" in r.json()["error"]


@pytest.mark.parametrize("extra", [{"pct": -1}, {"pct": "30.123"}, {"pct": 100, "modo": "margen"},
                                   {"pct_lineas": {"0": 150}, "modo": "margen"}, {"montaje": -5},
                                   {"modo": "otro"}, {"campo_raro": 1}])
def test_parametros_no_validos_400(extra):
    r = generar(cliente(Almacen()), **extra)
    assert r.status_code == 400, r.text


def test_pdf_demasiado_grande_413():
    almacen = Almacen(b"%PDF-" + b"0" * 2000)
    assert generar(cliente(almacen, max_bytes=1000)).status_code == 413


def test_descarga_fallida_502():
    c = TestClient(servicio.crear_app(servicio.Config(TOKEN, frozenset({HOST})),
                                      httpx.MockTransport(lambda req: httpx.Response(403))))
    assert generar(c).status_code == 502


def test_pdf_corrupto_needs_review_sin_subida():
    almacen = Almacen(b"esto no es un pdf")
    r = generar(cliente(almacen))
    assert r.status_code == 200
    d = r.json()
    assert d["estado"] == "needs_review" and d["entregado"] is False and "miniaturas" not in d
    assert not almacen.subidas


def test_huella_distinta_needs_review():
    doc = fitz.open()
    doc.new_page(width=595, height=842).insert_text((50, 50), "Otro documento")
    almacen = Almacen(doc.tobytes())
    d = generar(cliente(almacen)).json()
    assert d["estado"] == "needs_review" and any("huella" in e for e in d["errores"])
    assert not almacen.subidas


@con_samples
@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_analizar_ok(sample):
    r = cliente(Almacen(sample.read_bytes())).post(
        "/v1/analizar", json={"pdf_url": PDF_URL, "plantilla": PLANTILLA}, headers=AUTH)
    d = r.json()
    assert r.status_code == 200 and d["estado"] == "ok", d["errores"]
    assert d["plantilla"] == ID_PLANTILLA
    ventanas = [ln for ln in d["lineas"] if ln["tipo"] == "ventana"]
    assert len(ventanas) == d["n_dibujos"] and all(ln["dibujo"] for ln in ventanas)
    assert all(base64.b64decode(ln["dibujo_png"]).startswith(b"\x89PNG") for ln in ventanas)
    suma = sum(Decimal(ln["coste_total"]) for ln in d["lineas"])
    assert abs(suma - Decimal(d["totales_fabrica"]["base"])) <= Decimal("0.01")  # la fábrica redondea


@con_samples
@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_generar_ok_sube_pdf_verificado(sample):
    almacen = Almacen(sample.read_bytes())
    r = generar(cliente(almacen), montaje="400.50")
    d = r.json()
    assert r.status_code == 200 and d["estado"] == "ok", d["errores"]
    assert d["entregado"] is True and len(almacen.subidas) == 1

    salida = fitz.open(stream=almacen.subidas[0], filetype="pdf")
    assert len(d["miniaturas"]) == salida.page_count
    assert base64.b64decode(d["miniaturas"][0]).startswith(b"\x89PNG")
    texto = "".join(p.get_text() for p in salida).lower()
    assert not any(n.lower() in texto for n in PLANTILLA["redaccion"]["lista_negra"])

    montaje = next(ln for ln in d["lineas"] if ln["tipo"] == "montaje")
    assert montaje["importe"] == "400.50" and montaje["pct"] is None
    assert sum(Decimal(ln["total"]) for ln in d["lineas"]) == Decimal(d["totales"]["base"])


@con_samples
def test_pct_por_linea():
    almacen = Almacen(SAMPLES[0].read_bytes())
    d = generar(cliente(almacen), pct=30, pct_lineas={"0": "50"}).json()
    assert d["estado"] == "ok", d["errores"]
    l0, l1 = d["lineas"][0], d["lineas"][1]
    assert l0["pct"] == "50.00" and float(l0["importe"]) == pytest.approx(float(l0["coste_importe"]) * 1.5, abs=0.01)
    assert l1["pct"] == "30.00"


@con_samples
@pytest.mark.parametrize("pct_lineas", [{"99": 10}, "montaje"])
def test_pct_lineas_invalido_needs_review(pct_lineas):
    sample = SAMPLES[0].read_bytes()
    if pct_lineas == "montaje":
        lineas = cliente(Almacen(sample)).post(
            "/v1/analizar", json={"pdf_url": PDF_URL, "plantilla": PLANTILLA}, headers=AUTH).json()["lineas"]
        pct_lineas = {str(next(ln["indice"] for ln in lineas if ln["tipo"] == "montaje")): 10}
    almacen = Almacen(sample)
    d = generar(cliente(almacen), pct_lineas=pct_lineas).json()
    assert d["estado"] == "needs_review" and any("pct_lineas" in e for e in d["errores"])
    assert not almacen.subidas


def png(w: int = 400, h: int = 100) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, (0, 118, 111))
    return pix.tobytes("png")


MARCA = {"logo": base64.b64encode(png()).decode("ascii"),
         "datos": ["Ventanas Ejemplo S.L.", "NIF B12345678", "Tel. 958 000 000 · info@ejemplo.es"],
         "pie": ["Ventanas Ejemplo S.L. · C/ Mayor 12, 18001 Granada"]}


@pytest.mark.parametrize("marca", [{"logo": "no es base64!"}, {"datos": ["x"] * 7}, {"pie": [""]},
                                   {"otro": 1}])
def test_marca_no_valida_400(marca):
    almacen = Almacen()
    assert generar(cliente(almacen), marca=marca).status_code == 400
    assert not almacen.subidas


@con_samples
@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_generar_con_marca(sample):
    almacen = Almacen(sample.read_bytes())
    d = generar(cliente(almacen), marca=MARCA).json()
    assert d["estado"] == "ok", d["errores"]
    assert d["marca"] is True and len(almacen.subidas) == 1

    salida = fitz.open(stream=almacen.subidas[0], filetype="pdf")
    zonas = PLANTILLA["marca"]
    zona_logo = fitz.Rect(zonas["zona_logo"]["rect"])
    imgs = [i for i in salida[0].get_image_info() if fitz.Rect(i["bbox"]).intersects(zona_logo)]
    assert len(imgs) == 1  # solo el logo nuevo
    texto = " ".join(salida[0].get_text().split())
    assert all(l in texto for l in MARCA["datos"])
    for p in salida:
        assert MARCA["pie"][0] in " ".join(p.get_text().split())


@con_samples
@pytest.mark.parametrize("caso", ["sin_zonas", "logo_gif", "no_cabe"])
def test_marca_imposible_needs_review(caso):
    plantilla, marca = PLANTILLA, dict(MARCA)
    if caso == "sin_zonas":
        plantilla = {k: v for k, v in PLANTILLA.items() if k != "marca"}
    elif caso == "logo_gif":
        marca["logo"] = base64.b64encode(b"GIF89a" + b"0" * 20).decode("ascii")
    else:
        marca["datos"] = ["M" * 200]
    almacen = Almacen(SAMPLES[0].read_bytes())
    d = generar(cliente(almacen), plantilla=plantilla, marca=marca).json()
    assert d["estado"] == "needs_review" and not almacen.subidas
    assert d["errores"] and all(e.startswith("marca:") for e in d["errores"])


@con_samples
@pytest.mark.parametrize("sample", SAMPLES, ids=IDS)
def test_conservar_fabrica(sample):
    """Logo y nombre de la fábrica se quedan; los precios se eliminan igual; la marca (sin datos) se aplica."""
    original = fitz.open(stream=sample.read_bytes(), filetype="pdf")
    almacen = Almacen(sample.read_bytes())
    marca = {"logo": MARCA["logo"], "pie": MARCA["pie"]}
    d = generar(cliente(almacen), conservar_fabrica=True, marca=marca).json()
    assert d["estado"] == "ok", d["errores"]
    assert d["conservar_fabrica"] is True and len(almacen.subidas) == 1

    salida = fitz.open(stream=almacen.subidas[0], filetype="pdf")
    red = PLANTILLA["redaccion"]
    zona = fitz.Rect(red["zona_logo_fabrica"]["rect"])
    pag = red["zona_logo_fabrica"]["pagina"] - 1

    def imgs(doc):
        return [i for i in doc[pag].get_image_info() if fitz.Rect(i["bbox"]).intersects(zona)]
    assert len(imgs(salida)) == len(imgs(original)) > 0
    texto_o = "".join(p.get_text() for p in original).lower()
    texto_s = "".join(p.get_text() for p in salida).lower()
    for n in red["lista_negra"]:
        assert (n.lower() in texto_s) == (n.lower() in texto_o)
    for p in salida:
        assert MARCA["pie"][0] in " ".join(p.get_text().split())


@con_samples
def test_conservar_fabrica_con_datos_es_error_de_marca():
    almacen = Almacen(SAMPLES[0].read_bytes())
    d = generar(cliente(almacen), conservar_fabrica=True, marca=MARCA).json()
    assert d["estado"] == "needs_review" and not almacen.subidas
    assert d["errores"] and all(e.startswith("marca:") for e in d["errores"])
