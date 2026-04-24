"""
Esquema canónico y utilidades compartidas por todos los scrapers de cuotas.

Todos los scrapers producen filas con las mismas columnas para que:
  - JSON / CSV / MongoDB tengan la misma estructura
  - Un modelo predictivo pueda cargar datos de cualquier casa con el mismo código
  - Se puedan comparar cuotas entre casas usando `match_key`
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional


# ─────────────────────────────────────────────────────────────
# Columnas canónicas (orden fijo en CSV / JSON)
# ─────────────────────────────────────────────────────────────
CANONICAL_COLUMNS: list[str] = [
    # Metadatos de la captura
    "scrape_run_id",
    "scraped_at",
    "bookmaker",
    "competition",
    # Identificación del partido
    "match_key",
    "event_id",
    "event_name",
    "home_team",
    "away_team",
    "match_time",
    "event_url",
    # Mercado
    "market_id",
    "market_name",
    "market_type",
    "market_sv",
    # Slug estable p.ej. goals_ou, corners_ou, fouls_ou, fouls_team_ou, throw_ins (filtrar en SQL)
    "market_family",
    # Selección / cuota
    "selection_id",
    "selection_name",
    "odds_decimal",
    "odds_raw",
    "is_suspended",
]


# ─────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────
def slugify_team(name: str) -> str:
    """
    Slug simple: quita tildes, puntos y caracteres especiales.
    NO elimina prefijos/sufijos — para eso se usa `canonical_team`.
    """
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_name).strip("-").lower()
    return slug


# Mapa directo: cualquier variante conocida → nombre canónico.
# Añade entradas aquí cuando aparezca una nueva forma de escribir un equipo.
_TEAM_ALIASES = {
    # Real Madrid
    "real-madrid":          "real-madrid",
    # Atlético de Madrid
    "atletico-de-madrid":   "atletico-madrid",
    "atletico-madrid":      "atletico-madrid",
    # Barcelona
    "f-c-barcelona":        "barcelona",
    "fc-barcelona":         "barcelona",
    "barcelona":            "barcelona",
    # Betis
    "betis":                "betis",
    "real-betis":           "betis",
    # Mallorca
    "mallorca":             "mallorca",
    "rcd-mallorca":         "mallorca",
    # Alavés
    "alaves":               "alaves",
    "deportivo-alaves":     "alaves",
    # Getafe
    "getafe":               "getafe",
    "getafe-cf":            "getafe",
    # Valencia
    "valencia":             "valencia",
    "valencia-cf":          "valencia",
    # Girona
    "girona":               "girona",
    "girona-fc":            "girona",
    # Athletic Club (Bilbao)
    "athletic-club":        "athletic-club",
    # Rayo
    "rayo-vallecano":       "rayo-vallecano",
    # Real Sociedad
    "real-sociedad":        "real-sociedad",
    # Oviedo
    "oviedo":               "oviedo",
    "real-oviedo":          "oviedo",
    # Elche
    "elche":                "elche",
    # Osasuna
    "osasuna":              "osasuna",
    "ca-osasuna":           "osasuna",
    # Sevilla
    "sevilla":              "sevilla",
    "sevilla-fc":           "sevilla",
    # Villarreal
    "villarreal":           "villarreal",
    "villarreal-cf":        "villarreal",
    # Celta
    "celta":                "celta",
    "rc-celta":             "celta",
    "celta-de-vigo":        "celta",
    "celta-vigo":           "celta",
    # Espanyol
    "espanyol":             "espanyol",
    "rcd-espanyol":         "espanyol",
    # Levante
    "levante":              "levante",
}


def canonical_team(name: str) -> str:
    """
    Devuelve el nombre canónico del equipo.
    Si el alias no está en la tabla, devuelve el slug (así no falla
    con equipos nuevos, pero deberás añadirlo al mapa para cruzar bien).
    """
    slug = slugify_team(name)
    return _TEAM_ALIASES.get(slug, slug)


def make_match_key(home: str, away: str) -> str:
    """Clave normalizada compartible entre casas, p.ej. 'betis_vs_real-madrid'."""
    return f"{canonical_team(home)}_vs_{canonical_team(away)}"


def iso_now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def make_run_stamp() -> str:
    """YYYYMMDD_HHMMSS_microsegundos, único por ejecución."""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def parse_decimal(raw: Optional[str]) -> Optional[float]:
    """Convierte '3,86' o '1.95' o 3.86 → float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).replace(",", "."))
    except (ValueError, AttributeError):
        return None


def empty_row() -> dict:
    """Plantilla con todas las columnas canónicas en None / strings vacíos."""
    row = {col: "" for col in CANONICAL_COLUMNS}
    row["odds_decimal"] = None
    row["is_suspended"] = False
    return row


def normalize_row(row: dict) -> dict:
    """
    Devuelve un dict con exactamente las columnas canónicas en orden,
    rellenando ausentes. Se usa como último paso antes de guardar.
    """
    canonical = empty_row()
    for k, v in row.items():
        if k in canonical:
            canonical[k] = v
    return canonical
