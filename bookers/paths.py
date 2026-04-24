"""Rutas del repositorio: salida CSV/JSON bajo `data/`."""
from __future__ import annotations

from pathlib import Path

# Raíz del repo: .../scrapers_bookers/ (carpeta que contiene `bookers/` y `data/`)
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def data_path(*parts: str) -> Path:
    """Ruta bajo `data/`, p.ej. data_path('retabet_laliga_odds_2026.json')."""
    return DATA_DIR.joinpath(*parts)
