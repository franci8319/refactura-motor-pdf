# Worker PDF sin estado. Construir desde worker/: docker build -t worker-pdf .
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY motor.py servicio.py ./
USER nobody
# WORKER_TOKEN y WORKER_HOSTS llegan por el entorno del despliegue; PORT lo fija Cloud Run/Fly.
CMD ["sh", "-c", "exec uvicorn servicio:crear_app --factory --host 0.0.0.0 --port ${PORT:-8080} --no-access-log"]
