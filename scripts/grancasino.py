#!/usr/bin/env python3
"""
Scraper de cuotas de La Liga - Casino Gran Madrid Online
Plataforma: Altenar (sb2frontend-altenar2.biahosted.com)

Por defecto exporta todos los mercados que devuelve GetEventDetails (markets + childMarkets)
y todas las cuotas enlazadas por desktopOddIds y mobileOddIds.

Desde la raíz del repositorio:
    python scripts/grancasino.py

Instalación: ver requirements.txt
"""

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Forzar UTF-8 en Windows para poder imprimir caracteres Unicode
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env", interpolate=False)
except ImportError:
    pass

from bookers.odds_schema import (
    CANONICAL_COLUMNS,
    make_match_key,
    make_run_stamp,
    iso_now_utc,
    parse_decimal,
    normalize_row,
)
from bookers.market_labels import (
    format_readable_market_name,
    infer_market_family,
    EXCLUDED_MARKET_FAMILIES,
)
from bookers.persistence import save_csv, save_json, save_mongo
from bookers.supabase_store import save_supabase
from bookers.schedule_filter import within_window, WINDOW_HOURS


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────

BASE_URL = "https://sb2frontend-altenar2.biahosted.com/api/widget"

BOOKMAKER  = "grancasino"
COMPETITION = "LaLiga"

# CSV/JSON en data/ y Mongo son opt-in vía env vars, gestionado en
# `bookers.persistence` (DISABLE_FILE_OUTPUT, MONGO_URI, MONGO_DB, MONGO_COLLECTION).

PARAMS_BASE = {
    "culture": "es-ES",
    "timezoneOffset": "-120",
    "integration": "casinogranmadrid",
    "deviceType": "1",
    "numFormat": "en-GB",
    "countryCode": "ES",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
    "Origin": "https://www.casinogranmadridonline.es",
    "Referer": "https://www.casinogranmadridonline.es/",
}

# IDs de La Liga española
LALIGA_CHAMP_ID = 2941
SPORT_ID = 66
CATEGORY_ID = 501

# Modo filtrado legacy (solo si GRAN_FILTER_MARKETS=1): typeIds + nombres con estas claves.
TARGET_MARKETS = {
    1:   "1x2",
    10:  "Doble oportunidad",
    11:  "Apuesta sin empate",
    18:  "Total goles (Over/Under)",
    29:  "Ambos equipos marcan",
    8:   "Primer gol",
    16:  "Hándicap europeo",
    14:  "Hándicap 1x2",
    166: "Total tiros de esquina",
}
_GRAN_EXTRA_NAME_KEYS = (
    "falta",
    "córner",
    "corner",
    "esquina",
    "saque de banda",
    "tarjeta",
    "amarilla",
    "roja",
    "booking",
)

# Por defecto se incluyen TODOS los mercados del JSON (markets + childMarkets).
# Pon GRAN_FILTER_MARKETS=1 para volver al subconjunto anterior (menos filas / Supabase).
GRAN_FILTER_MARKETS = _env_bool("GRAN_FILTER_MARKETS", False)


def _include_grancasino_market(market: dict) -> bool:
    if market.get("typeId") in TARGET_MARKETS:
        return True
    n = (market.get("name") or "").lower()
    return any(k in n for k in _GRAN_EXTRA_NAME_KEYS)


def _flatten_odd_id_groups(groups: object) -> list[int]:
    """Aplana desktopOddIds / mobileOddIds (listas anidadas de enteros)."""
    out: list[int] = []
    if not groups:
        return out
    if not isinstance(groups, list):
        groups = [groups]
    for group in groups:
        if isinstance(group, list):
            for oid in group:
                if isinstance(oid, (int, float)):
                    out.append(int(oid))
                elif isinstance(oid, str) and oid.strip().lstrip("-").isdigit():
                    out.append(int(oid))
        elif isinstance(group, (int, float)):
            out.append(int(group))
    return out


def _market_odd_ids(market: dict) -> list[int]:
    """Ids de cuota del mercado: desktop + mobile + cualquier otro *OddIds, sin duplicados."""
    seen: set[int] = set()
    ordered: list[int] = []
    for key in ("desktopOddIds", "mobileOddIds"):
        for oid in _flatten_odd_id_groups(market.get(key) or []):
            if oid not in seen:
                seen.add(oid)
                ordered.append(oid)
    for key in sorted(market.keys()):
        if key.endswith("OddIds") and key not in ("desktopOddIds", "mobileOddIds"):
            for oid in _flatten_odd_id_groups(market.get(key) or []):
                if oid not in seen:
                    seen.add(oid)
                    ordered.append(oid)
    return ordered


def _iter_event_markets(event_data: dict):
    """Mercados de primer nivel + childMarkets (Goleador, Remates por jugador, etc.)."""
    seen: set[int] = set()

    def _mid(m: dict) -> Optional[int]:
        v = m.get("id")
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    for m in event_data.get("markets") or []:
        mid = _mid(m)
        if mid is None or mid in seen:
            continue
        seen.add(mid)
        yield m
    for m in event_data.get("childMarkets") or []:
        mid = _mid(m)
        if mid is None or mid in seen:
            continue
        seen.add(mid)
        yield m


# ─── FUNCIONES ────────────────────────────────────────────────────────────────

def get_session() -> requests.Session:
    """Crea una sesión con los headers necesarios."""
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def get_laliga_events(session: requests.Session) -> list[dict]:
    """
    Obtiene todos los eventos disponibles de La Liga.
    Retorna lista de dicts con id, nombre y fecha de cada partido.
    """
    params = {
        **PARAMS_BASE,
        "eventCount": "0",   # 0 = sin límite
        "sportId": "0",
        "champIds": str(LALIGA_CHAMP_ID),
    }

    response = session.get(f"{BASE_URL}/GetEvents", params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    events = []
    for event in data.get("events", []):
        events.append({
            "id":         event["id"],
            "name":       event["name"].strip(),
            "start_date": event.get("startDate"),
            "champ_id":   event.get("champId"),
            "sport_id":   event.get("sportId"),
            "status":     event.get("status", 0),  # 0=próximo, 1=en vivo
        })

    return events


def get_event_details(session: requests.Session, event_id: int) -> Optional[dict]:
    """
    Obtiene todas las cuotas de un evento específico.
    Retorna el JSON completo del evento.
    """
    params = {
        **PARAMS_BASE,
        "eventId": str(event_id),
        # true: incluir líneas no-boost junto a las promocionales cuando el feed las separa.
        "showNonBoosts": "true",
    }

    response = session.get(f"{BASE_URL}/GetEventDetails", params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def extract_odds_from_event(
    event_data: dict,
    *,
    scrape_run_id: str,
    scraped_at_iso: str,
) -> list[dict]:
    """
    Extrae cuotas de un evento y las devuelve como filas canónicas
    (mismo esquema CANONICAL_COLUMNS que los demás scrapers).
    """
    event_id   = str(event_data["id"])
    event_name = event_data["name"].strip()
    start_date = event_data.get("startDate", "") or ""

    odds_map = {o["id"]: o for o in event_data.get("odds", [])}

    competitors = {c["id"]: c["name"] for c in event_data.get("competitors", [])}
    comp_ids    = [c["id"] for c in event_data.get("competitors", [])]
    home_team   = competitors.get(comp_ids[0], "").strip() if len(comp_ids) > 0 else ""
    away_team   = competitors.get(comp_ids[1], "").strip() if len(comp_ids) > 1 else ""
    match_key   = make_match_key(home_team, away_team)

    rows: list[dict] = []

    for market in _iter_event_markets(event_data):
        market_type_id = market.get("typeId")
        market_name    = (market.get("name") or market.get("childName") or "").strip()

        if GRAN_FILTER_MARKETS and not _include_grancasino_market(market):
            continue

        sv_raw = str(market.get("sv", "") or "")
        fam = infer_market_family(market_name, sv_raw, altenar_type_id=market_type_id)
        if fam in EXCLUDED_MARKET_FAMILIES:
            continue

        m_readable = format_readable_market_name(
            fam, market_name, sv_raw, altenar_type_id=market_type_id
        )
        all_odd_ids = _market_odd_ids(market)

        for odd_id in all_odd_ids:
            odd = odds_map.get(odd_id)
            if not odd:
                continue

            odd_status = odd.get("oddStatus", 0)
            price = odd.get("price")

            rows.append(normalize_row({
                "scrape_run_id":  scrape_run_id,
                "scraped_at":     scraped_at_iso,
                "bookmaker":      BOOKMAKER,
                "competition":    COMPETITION,
                "match_key":      match_key,
                "event_id":       event_id,
                "event_name":     event_name,
                "home_team":      home_team,
                "away_team":      away_team,
                "match_time":     start_date,
                "event_url":      "",
                "market_id":      str(market["id"]),
                "market_name":    m_readable,
                "market_type":    str(market_type_id) if market_type_id is not None else "",
                "market_sv":      sv_raw,
                "market_family":  fam,
                "selection_id":   str(odd_id),
                "selection_name": odd.get("name", ""),
                "odds_decimal":   parse_decimal(price),
                "odds_raw":       str(price) if price is not None else "",
                "is_suspended":   odd_status != 0,
            }))

    return rows


# ─── SCRAPER PRINCIPAL ────────────────────────────────────────────────────────

def scrape_laliga_odds(
    delay_between_requests: float = 1.0,
    run_stamp: Optional[str] = None,
    scraped_at: Optional[datetime] = None,
) -> list[dict]:
    """
    Scraper principal. Descarga las cuotas de todos los partidos de LaLiga
    disponibles en Gran Madrid y las persiste en Supabase (+ CSV/JSON/Mongo si
    están habilitados vía env vars). Devuelve las filas canónicas.
    """
    session = get_session()
    run_stamp   = run_stamp or make_run_stamp()
    scraped_at  = scraped_at or datetime.now(tz=timezone.utc)
    scraped_iso = scraped_at.isoformat()

    print(f"[{run_stamp}] Iniciando scraper de La Liga - Gran Madrid Online")
    print(f"{'='*60}")

    print("\n[1/3] Obteniendo lista de partidos de La Liga...")
    events = get_laliga_events(session)
    print(f"      → {len(events)} partidos encontrados:")
    for e in events:
        dt = e['start_date'][:16].replace('T', ' ') if e['start_date'] else "N/A"
        print(f"        • [{e['id']}] {e['name']} — {dt}")

    events = [e for e in events if within_window(e.get("start_date", ""))]
    print(f"      → {len(events)} dentro de la ventana de {WINDOW_HOURS:.0f}h")

    print(f"\n[2/3] Descargando cuotas de cada partido...")
    all_rows: list[dict] = []

    for i, event in enumerate(events, 1):
        event_id   = event["id"]
        event_name = event["name"]
        print(f"      [{i:2d}/{len(events)}] {event_name}...", end=" ", flush=True)

        try:
            details = get_event_details(session, event_id)
            rows = extract_odds_from_event(
                details,
                scrape_run_id=run_stamp,
                scraped_at_iso=scraped_iso,
            )
            all_rows.extend(rows)
            print(f"OK ({len(rows)} cuotas)")
        except requests.RequestException as e:
            print(f"ERROR: {e}")
        except Exception as e:
            print(f"ERROR inesperado: {e}")

        if i < len(events):
            time.sleep(delay_between_requests)

    print(f"\n[3/3] Persistiendo datos...")
    save_json(all_rows, bookmaker=BOOKMAKER, run_stamp=run_stamp)
    save_csv(all_rows,  bookmaker=BOOKMAKER, run_stamp=run_stamp)
    save_supabase(all_rows)
    save_mongo(all_rows, bookmaker=BOOKMAKER)

    df = pd.DataFrame(all_rows, columns=CANONICAL_COLUMNS) if all_rows else pd.DataFrame(columns=CANONICAL_COLUMNS)
    print(f"\n{'='*60}")
    print(f"  Total filas: {len(df)}")
    if len(df):
        print(f"  Mercados únicos: {df['market_name'].nunique()}")
        print(f"  Partidos procesados: {df['match_key'].nunique()}")
    print(f"{'='*60}\n")

    return all_rows


# ─── UTILIDADES EXTRA ─────────────────────────────────────────────────────────

def get_event_ids_only() -> list[int]:
    """Retorna solo los IDs de los partidos disponibles."""
    session = get_session()
    events = get_laliga_events(session)
    return [e["id"] for e in events]


def scrape_single_event(event_id: int) -> pd.DataFrame:
    """Scraper para un único partido por su ID (devuelve filas canónicas)."""
    session = get_session()
    details = get_event_details(session, event_id)
    rows = extract_odds_from_event(
        details,
        scrape_run_id=make_run_stamp(),
        scraped_at_iso=iso_now_utc(),
    )
    return pd.DataFrame(rows, columns=CANONICAL_COLUMNS)


# ─── PUNTO DE ENTRADA ─────────────────────────────────────────────────────────

def run(
    *,
    run_stamp: Optional[str] = None,
    scraped_at_iso: Optional[str] = None,
) -> list[dict]:
    """
    Entry point uniforme (mismo contrato que el resto de scrapers).
    Usado por `scripts/run_all.py` para compartir `run_stamp` y `scraped_at`
    entre todos los scrapers de un ciclo. Devuelve las filas canónicas.
    """
    scraped_at_dt: Optional[datetime] = None
    if scraped_at_iso:
        try:
            scraped_at_dt = datetime.fromisoformat(scraped_at_iso)
        except ValueError:
            scraped_at_dt = None
    return scrape_laliga_odds(
        delay_between_requests=1.0,
        run_stamp=run_stamp,
        scraped_at=scraped_at_dt,
    )


if __name__ == "__main__":
    run()