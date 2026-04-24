# syntax=docker/dockerfile:1.7
# Imagen oficial de Microsoft con Playwright + browsers preinstalados.
# Mantener esta versión sincronizada con `playwright==X.Y.Z` de requirements.txt
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HEADLESS=1 \
    DISABLE_FILE_OUTPUT=1 \
    TZ=Europe/Madrid

WORKDIR /app

# Dependencias de Python primero (capa cacheable)
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Código del repositorio
COPY bookers ./bookers
COPY scripts ./scripts

# Carpeta data/ efímera (se usa sólo si DISABLE_FILE_OUTPUT=0)
RUN mkdir -p data

# El cron de Railway llamará a `python scripts/run_all.py`
CMD ["python", "scripts/run_all.py"]
