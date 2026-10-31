"""Servicio HTTP del worker PDF: sin estado, sin disco, sin secretos ni plantillas en el código.

Entra PDF + plantilla JSON, sale PDF + JSON de resultados. El PDF nunca pasa por la app:
el worker lo descarga de una URL firmada del bucket temporal y sube el generado a otra.

  GET  /salud         → {"ok": true}
  POST /v1/analizar   {pdf_url, plantilla}
                      → huella, extracción y validación (lo que el montador revisa); cada línea
                        lleva "dibujo_png" (PNG en base64 del dibujo de la ventana, o null)
  POST /v1/generar    {pdf_url, salida_url, plantilla, pct, modo?, montaje?, pct_lineas?, marca?, conservar_fabrica?}
                      → redacción, estampado, verificación de salida y subida del PDF (PUT a salida_url)
                        marca = {logo?: PNG/JPEG en base64, datos?: [líneas], pie?: [líneas]}; necesita
                        que la plantilla defina sus zonas ("marca")

Respuestas 200 con "estado": "ok" | "needs_review". En needs_review no se sube nada.
Importes en JSON como texto con punto decimal ("1300.00"); en la entrada también se aceptan números.

Variables de entorno (obligatorias salvo indicación):
  WORKER_TOKEN       token Bearer compartido con la app (mín. 32 caracteres)
  WORKER_HOSTS       hosts permitidos para las URLs, separados por comas (p. ej. abc.supabase.co)
  WORKER_MAX_MB      tamaño máximo del PDF descargado (opcional, 20 por defecto)

Arranque: uvicorn servicio:crear_app --factory --app-dir worker --port 8080
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import urlsplit

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import motor

# Logs sin datos personales: solo plantilla, estado, recuentos y tiempos. Nunca URLs, refs ni errores.
log = logging.getLogger("worker")

# PyMuPDF no es seguro entre hilos: un proceso del motor a la vez por instancia.
_MOTOR = threading.Lock()


@dataclass(frozen=True)
class Config:
    token: str
    hosts: frozenset[str]
    max_bytes: int = 20 * 1024 * 1024
    timeout_s: float = 30.0

    @classmethod
    def desde_entorno(cls) -> Config:
        token = os.environ.get("WORKER_TOKEN", "")
        hosts = frozenset(h.strip().lower() for h in os.environ.get("WORKER_HOSTS", "").split(",") if h.strip())
        if len(token) < 32:
            raise RuntimeError("WORKER_TOKEN ausente o demasiado corto (mín. 32 caracteres)")
        if not hosts:
            raise RuntimeError("WORKER_HOSTS vacío: indicar el host del almacenamiento")
        return cls(token, hosts, int(float(os.environ.get("WORKER_MAX_MB", "20")) * 1024 * 1024))


class ErrorPeticion(Exception):
    def __init__(self, status: int, mensaje: str):
        super().__init__(mensaje)
        self.status = status
        self.mensaje = mensaje


# % de beneficio: global o por línea. El tope de margen (<100) depende del modo, va en el modelo.
Pct = Annotated[Decimal, Field(ge=0, le=1000, decimal_places=2)]
MAX_CUERPO = 1024 * 1024  # JSON con plantilla; el PDF va por URL


class PeticionAnalizar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pdf_url: str
    plantilla: dict


MAX_LOGO = 300 * 1024
Linea = Annotated[str, Field(min_length=1, max_length=200)]


class PeticionMarca(BaseModel):
    """Marca del montador. logo: PNG o JPEG en base64; datos y pie: líneas de texto ya compuestas."""
    model_config = ConfigDict(extra="forbid")
    logo: str | None = Field(default=None, max_length=(MAX_LOGO + 2) // 3 * 4)
    datos: list[Linea] = Field(default_factory=list, max_length=6)
    pie: list[Linea] = Field(default_factory=list, max_length=3)

    def a_motor(self) -> motor.Marca:
        logo = None
        if self.logo is not None:
            try:
                logo = base64.b64decode(self.logo, validate=True)
            except ValueError as e:
                raise ErrorPeticion(400, "marca.logo: base64 no válido") from e
        return motor.Marca(logo, list(self.datos), list(self.pie))


class PeticionGenerar(PeticionAnalizar):
    salida_url: str
    pct: Pct
    modo: Literal["markup", "margen"] = "markup"
    montaje: Decimal | None = Field(default=None, ge=0, le=1_000_000, decimal_places=2)
    pct_lineas: dict[int, Pct] = Field(default_factory=dict)
    marca: PeticionMarca | None = None
    conservar_fabrica: bool = False  # deja el logo y el nombre de la fábrica; los precios se eliminan siempre

    @model_validator(mode="after")
    def _pcts(self) -> PeticionGenerar:
        if self.modo == "margen" and any(p >= 100 for p in (self.pct, *self.pct_lineas.values())):
            raise ValueError("en modo margen el % debe ser menor que 100")
        return self


def _comprobar_url(url: str, cfg: Config, campo: str) -> None:
    partes = urlsplit(url)
    if partes.scheme != "https" or (partes.hostname or "").lower() not in cfg.hosts:
        # Sin repetir la URL: lleva el token de firma.
        raise ErrorPeticion(400, f"{campo}: solo https a un host permitido")


def _descargar(cliente: httpx.Client, url: str, cfg: Config) -> bytes:
    try:
        with cliente.stream("GET", url) as r:
            if r.status_code != 200:
                raise ErrorPeticion(502, f"descarga del PDF: HTTP {r.status_code}")
            if int(r.headers.get("content-length") or 0) > cfg.max_bytes:
                raise ErrorPeticion(413, "PDF demasiado grande")
            datos = bytearray()
            for trozo in r.iter_bytes():
                datos += trozo
                if len(datos) > cfg.max_bytes:
                    raise ErrorPeticion(413, "PDF demasiado grande")
            return bytes(datos)
    except httpx.HTTPError as e:
        raise ErrorPeticion(502, f"descarga del PDF: {type(e).__name__}") from e


def _subir(cliente: httpx.Client, url: str, pdf: bytes) -> None:
    try:
        r = cliente.put(url, content=pdf, headers={"content-type": "application/pdf"})
    except httpx.HTTPError as e:
        raise ErrorPeticion(502, f"subida del PDF: {type(e).__name__}") from e
    if not 200 <= r.status_code < 300:
        raise ErrorPeticion(502, f"subida del PDF: HTTP {r.status_code}")


def _plantilla(raw: dict) -> dict:
    try:
        return motor.compile_template(raw)
    except ValueError as e:
        raise ErrorPeticion(400, str(e)) from e


def _con_motor(fn, *args):
    with _MOTOR:
        return fn(*args)


def _respuesta(datos: dict, status: int = 200) -> JSONResponse:
    # Decimal → "1300.00" con el mismo criterio que el motor.
    return JSONResponse(json.loads(json.dumps(datos, ensure_ascii=False, default=motor.jsonable)), status)


def crear_app(cfg: Config | None = None, transporte: httpx.BaseTransport | None = None) -> Starlette:
    """Fábrica de la app. Sin cfg lee el entorno y no arranca si falta configuración."""
    if cfg is None:
        cfg = Config.desde_entorno()
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cliente = httpx.Client(transport=transporte, timeout=cfg.timeout_s, follow_redirects=False)
    token = f"Bearer {cfg.token}".encode()

    async def leer(request: Request, modelo: type[BaseModel]):
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), token):
            raise ErrorPeticion(401, "no autorizado")
        cuerpo = await request.body()
        if len(cuerpo) > MAX_CUERPO:
            raise ErrorPeticion(413, "petición demasiado grande")
        try:
            return modelo.model_validate_json(cuerpo)
        except ValidationError as e:
            # Solo campo y motivo, nunca el valor recibido (las URLs llevan el token de firma).
            motivos = [f"{'.'.join(str(p) for p in err['loc']) or '(cuerpo)'}: {err['msg']}" for err in e.errors()]
            raise ErrorPeticion(400, f"petición no válida: {'; '.join(motivos)}") from e

    def analizar(p: PeticionAnalizar) -> dict:
        T = _plantilla(p.plantilla)
        _comprobar_url(p.pdf_url, cfg, "pdf_url")
        pdf = _descargar(cliente, p.pdf_url, cfg)
        r = _con_motor(motor.analizar, pdf, T)
        dibujos = r.dibujos or [None] * len(r.datos["lineas"])
        for ln, png in zip(r.datos["lineas"], dibujos):
            ln["dibujo_png"] = base64.b64encode(png).decode("ascii") if png else None
        return r.datos

    def generar(p: PeticionGenerar) -> dict:
        T = _plantilla(p.plantilla)
        _comprobar_url(p.pdf_url, cfg, "pdf_url")
        _comprobar_url(p.salida_url, cfg, "salida_url")
        marca = p.marca.a_motor() if p.marca else None
        pdf = _descargar(cliente, p.pdf_url, cfg)
        r = _con_motor(motor.generar, pdf, T, p.pct, p.modo, p.montaje, p.pct_lineas, marca,
                       p.conservar_fabrica)
        if not r.ok:
            return {**r.datos, "entregado": False}
        _subir(cliente, p.salida_url, r.pdf)
        return {**r.datos, "entregado": True,
                "miniaturas": [base64.b64encode(png).decode("ascii") for png in r.miniaturas]}

    def endpoint(nombre: str, modelo: type[BaseModel], fn):
        async def manejar(request: Request) -> JSONResponse:
            t0 = time.perf_counter()
            try:
                peticion = await leer(request, modelo)
                datos = await anyio.to_thread.run_sync(fn, peticion)
            except ErrorPeticion as e:
                log.info("%s status=%d ms=%.0f", nombre, e.status, (time.perf_counter() - t0) * 1000)
                return _respuesta({"error": e.mensaje}, e.status)
            log.info("%s plantilla=%s estado=%s lineas=%d errores=%d ms=%.0f", nombre, datos["plantilla"],
                     datos["estado"], len(datos["lineas"]), len(datos["errores"]),
                     (time.perf_counter() - t0) * 1000)
            return _respuesta(datos)
        return manejar

    async def salud(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @asynccontextmanager
    async def ciclo(_: Starlette):
        yield
        cliente.close()

    app = Starlette(routes=[
        Route("/salud", salud, methods=["GET"]),
        Route("/v1/analizar", endpoint("analizar", PeticionAnalizar, analizar), methods=["POST"]),
        Route("/v1/generar", endpoint("generar", PeticionGenerar, generar), methods=["POST"]),
    ], lifespan=ciclo)
    return app
