"""
Filtro temporal: determina si un partido debe scrapearse según cuánto
tiempo falta para el inicio.

Ventana configurable con la variable de entorno MATCH_WINDOW_HOURS
(default: 48).  Si el formato de fecha no se puede parsear, el partido
se incluye para no filtrar por error.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

WINDOW_HOURS: float = float(os.environ.get("MATCH_WINDOW_HOURS", "48"))


def within_window(match_time_iso: str, *, window_hours: float = WINDOW_HOURS) -> bool:
    """
    Devuelve True si el partido empieza dentro de las próximas `window_hours` horas.

    - Si `match_time_iso` está vacío o no es parseable → True (no filtra).
    - Partidos ya comenzados (delta < 0) → True (pueden estar en curso).
    - Partidos a más de `window_hours` horas → False (descartados).
    """
    if not match_time_iso:
        return True
    try:
        s = match_time_iso.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta_h = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
        # Incluye partidos ya iniciados (hasta 3h después del comienzo)
        # y partidos hasta `window_hours` en el futuro.
        return delta_h >= -3 and delta_h <= window_hours
    except Exception:
        return True  # formato desconocido → no filtrar
