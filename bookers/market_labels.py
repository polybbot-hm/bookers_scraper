"""
Clasificación de mercados: slug estable (`market_family`) + nombre legible en español.

Sirve para distinguir Over/Under de goles vs córners vs faltas vs saques de banda, etc.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Familias de mercado que NO se recogen (mucho volumen, sin edge para los modelos de nicho).
# Aplica a TODOS los scrapers antes de añadir filas.
EXCLUDED_MARKET_FAMILIES: frozenset[str] = frozenset({
    "goals_team",     # goles por jugador / equipo (props)
    "first_goal",     # primer goleador
    "goals_ou",       # total goles O/U del partido
    "match_1x2",      # 1X2 estándar
    "handicap_eu",    # hándicap europeo
    "handicap_1x2",   # hándicap 1X2
    "btts",           # ambos marcan
    "double_chance",  # doble oportunidad
    "draw_no_bet",    # apuesta sin empate
})

# Altenar (Gran Casino): typeId → familia (los que ya filtrábamos en TARGET_MARKETS).
ALTENAR_TYPE_FAMILY: dict[int, str] = {
    1:   "match_1x2",
    10:  "double_chance",
    11:  "draw_no_bet",
    18:  "goals_ou",
    29:  "btts",
    8:   "first_goal",
    16:  "handicap_eu",
    14:  "handicap_1x2",
    166: "corners_ou",
}

FAMILY_LABEL_ES: dict[str, str] = {
    "match_1x2":       "1X2",
    "double_chance":   "Doble oportunidad",
    "draw_no_bet":     "Sin empate",
    "goals_ou":        "Goles — Total más/menos",
    "goals_team":      "Goles — Equipo",
    "btts":            "Ambos marcan",
    "first_goal":      "Primer gol",
    "handicap_eu":     "Hándicap europeo",
    "handicap_1x2":    "Hándicap 1X2",
    "corners_ou":      "Córners — Total más/menos",
    "corners":         "Córners",
    "cards":           "Tarjetas",
    "fouls":           "Faltas",
    "fouls_ou":        "Faltas — Total más/menos",
    "fouls_team":      "Faltas — Por equipo",
    "fouls_team_ou":   "Faltas — Por equipo más/menos",
    "throw_ins":       "Saques de banda",
    "throw_ins_ou":    "Saques de banda — Total más/menos",
    "shots":           "Remates",
    "shots_on_target": "Remates a puerta",
    "other":           "Otro mercado",
}


def _ascii_lower(s: str) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_s = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_s.lower()


def _has_ou_signal(blob: str, raw: str, sv: str) -> bool:
    if "mas" in blob or "menos" in blob:
        return True
    if re.search(r"\d+[.,]\d+", f"{raw} {sv}"):
        return True
    return False


def _keyword_family(raw_name: str, market_sv: str) -> Optional[str]:
    """Clasificación por texto (Retabet + mercados extra de Altenar)."""
    blob = _ascii_lower(f"{raw_name} {market_sv}")
    if not blob.strip():
        return None

    if "saque de banda" in blob or "saques de banda" in blob:
        return "throw_ins_ou" if _has_ou_signal(blob, raw_name, market_sv) else "throw_ins"

    if "tarjeta" in blob or "amarilla" in blob or "roja" in blob or "booking" in blob:
        return "cards"

    foul_kw = (
        "falta" in blob
        or "faltas" in blob
        or "infraccion" in blob
    )
    if foul_kw:
        # Retabet / casas: "Faltas de equipo", "1 Total de faltas", líneas por bando, etc.
        team_ctx = (
            "de equipo" in blob
            or "del equipo" in blob
            or "por equipo" in blob
            or re.search(r"faltas?\s+equipo", blob) is not None
            or re.search(r"equipo\s+[12]\b", blob) is not None
            or re.search(r"\b[12]\s+total\s+de\s+faltas", blob) is not None
        )
        ou = _has_ou_signal(blob, raw_name, market_sv)
        if team_ctx:
            return "fouls_team_ou" if ou else "fouls_team"
        return "fouls_ou" if ou else "fouls"

    if "corner" in blob or "corne" in blob or "esquina" in blob:
        return "corners_ou" if _has_ou_signal(blob, raw_name, market_sv) else "corners"

    # "remate" (Retabet/Gran Casino) vs "tiro" (22bet/Kirolbet). "tiros a puerta",
    # "tiros entre los tres palos" y similares → remates a puerta.
    is_shot = "remate" in blob or "tiro" in blob or "tiros" in blob
    on_target = "puerta" in blob or "tres palos" in blob or "palos" in blob
    if is_shot and on_target:
        return "shots_on_target"
    if is_shot:
        return "shots"

    if "primer" in blob and "gol" in blob:
        return "first_goal"

    if "ambos" in blob and "marcan" in blob:
        return "btts"

    if "gol" in blob or "goles" in blob or "marcaran" in blob:
        if _has_ou_signal(blob, raw_name, market_sv):
            return "goals_ou"
        return "goals_team"

    if "handicap" in blob or "hándicap" in (raw_name or "").lower():
        return "handicap_eu"

    if re.search(r"1\s*-\s*x\s*-\s*2", blob) or "1x2" in blob.replace(" ", ""):
        return "match_1x2"

    return None


def infer_market_family(
    raw_market_name: str,
    market_sv: str,
    *,
    altenar_type_id: Optional[int] = None,
) -> str:
    """
    Slug estable para `market_family`. Prioriza palabras clave en nombre+sv;
    si no hay match, usa el mapa de typeId de Altenar; si no, `other`.
    """
    kw = _keyword_family(raw_market_name or "", market_sv or "")
    if kw:
        return kw
    if altenar_type_id is not None and altenar_type_id in ALTENAR_TYPE_FAMILY:
        return ALTENAR_TYPE_FAMILY[altenar_type_id]
    return "other"


def format_readable_market_name(
    family_slug: str,
    raw_market_name: str,
    market_sv: str,
    *,
    altenar_type_id: Optional[int] = None,
) -> str:
    """Nombre legible: categoría + título API + línea/sv si no está ya en el título."""
    label = FAMILY_LABEL_ES.get(family_slug, FAMILY_LABEL_ES["other"])
    raw = (raw_market_name or "").strip()
    sv = (market_sv or "").strip()

    parts: list[str] = [label]
    if raw and _ascii_lower(raw) != _ascii_lower(label):
        parts.append(raw)
    if sv and sv not in raw:
        parts.append(f"línea/sv: {sv}")
    if family_slug == "other" and altenar_type_id is not None:
        parts.append(f"typeId={altenar_type_id}")
    return " — ".join(parts)
