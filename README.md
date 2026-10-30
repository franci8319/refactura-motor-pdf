# Motor PDF de Refactura

Servicio HTTP sin estado que convierte un presupuesto PDF de fábrica en un presupuesto para el cliente final:
identifica el formato, extrae líneas y precios, **elimina de verdad** los precios y datos de la fábrica
(redacción con PyMuPDF, no se tapan), estampa los precios nuevos y verifica el resultado antes de entregarlo.

Es el motor de [refactura.es](https://refactura.es). Se publica bajo AGPL-3.0 porque usa PyMuPDF.

## Cómo funciona

- **Entra:** la URL firmada de un PDF y una plantilla JSON (anclas de texto y regiones; no se incluye ninguna en este repo).
- **Sale:** JSON de resultados y, si todo cuadra, el PDF generado, que se sube por PUT a otra URL firmada.
- **Regla de oro:** si falla la huella, cualquier validación o la verificación de salida, la respuesta es
  `"estado": "needs_review"` y no se entrega PDF.
- Sin disco ni base de datos: todo ocurre en memoria.

| Ruta | Qué hace |
| --- | --- |
| `GET /salud` | `{"ok": true}` |
| `POST /v1/analizar` | `{pdf_url, plantilla}` → huella, líneas (con el dibujo de cada ventana en PNG), totales y validación |
| `POST /v1/generar` | `{pdf_url, salida_url, plantilla, pct, modo?, montaje?, pct_lineas?}` → redacción, estampado, verificación y subida |

Importes en JSON como texto con punto decimal (`"1300.00"`). Detalle en la cabecera de `servicio.py`.

## Ejecutar

```
pip install -r requirements.txt
WORKER_TOKEN=<32+ caracteres> WORKER_HOSTS=<host-del-almacenamiento> \
  uvicorn servicio:crear_app --factory --port 8080
```

| Variable | Uso |
| --- | --- |
| `WORKER_TOKEN` | Token Bearer compartido con la app (mínimo 32 caracteres) |
| `WORKER_HOSTS` | Hosts permitidos para las URLs de descarga y subida, separados por comas |
| `WORKER_MAX_MB` | Tamaño máximo del PDF (opcional, 20 por defecto) |

Docker: `docker build -t motor-pdf .` y `docker run -p 8080:8080 -e WORKER_TOKEN=… -e WORKER_HOSTS=… motor-pdf`.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest tests -q
```

Los tests que necesitan PDFs reales se saltan en este repo: los PDFs de ejemplo contienen datos de clientes
y no se publican.

## Contribuciones

No se aceptan pull requests. Puedes abrir una incidencia si encuentras un error.

## Licencia

Copyright (C) 2026 Refactura (refactura.es).

Este programa es software libre: puedes redistribuirlo y/o modificarlo bajo los términos de la
GNU Affero General Public License publicada por la Free Software Foundation, versión 3. Se distribuye
SIN NINGUNA GARANTÍA. Texto completo en [`LICENSE`](LICENSE).
