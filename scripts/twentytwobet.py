"""
Scraper de cuotas de LaLiga en 22bet — API pública LineFeed.

Sin navegador: se consumen directamente los endpoints JSON (`GetChampZip` +
`GetGameZip`) y se enumeran todos los subgames (mercados) de cada partido.
Produce filas canónicas (`bookers.odds_schema.CANONICAL_COLUMNS`) y las
persiste por los mismos canales que el resto de scrapers.

Arquitectura de la API (resumen):
  GET GetChampZip?champ=127733           -> Value.G[] partidos de LaLiga
  GET GetGameZip?id={match_ci}&isSubGames=true
      -> Value.SG[] subgames (cada subgame = un grupo de mercado con CI propio)
      -> Value.E[]  outcomes de la "línea principal" (1X2, etc.)
  GET GetGameZip?id={subgame_ci}         -> Value.E[] outcomes del subgame

Cada outcome (E) codifica:
  GS -> game group (4=Total, 1=1x2, 2=hándicap, …)
  G  -> game type dentro del grupo
  T  -> tipo de selección (1=home, 2=draw, 3=away, 9=over, 10=under, …)
  P  -> línea (O/U, hándicap)
  C  -> cuota decimal
  N  -> a veces nombre del jugador / texto de selección

Desde la raíz del repositorio:
    python scripts/twentytwobet.py
    python scripts/twentytwobet.py --max-matches 3 --verbose
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env", interpolate=False)
except ImportError:
    pass

from bookers.market_labels import (
    EXCLUDED_MARKET_FAMILIES,
    format_readable_market_name,
    infer_market_family,
)
from bookers.odds_schema import (
    iso_now_utc,
    make_match_key,
    make_run_stamp,
    normalize_row,
    parse_decimal,
)
from bookers.persistence import save_csv, save_json, save_mongo
from bookers.supabase_store import save_supabase


# ─────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────
BASE_URL        = "https://22bet92.com/service-api/LineFeed"
LALIGA_CHAMP_ID = 127733
BOOKMAKER       = "22bet"
COMPETITION     = "LaLiga"

# 22bet tiene un volumen enorme de subgames (>50 por partido). Solo guardamos
# los mercados de faltas y saques de banda, que son el objetivo del modelo.
# Cualquier family que no esté aquí se descarta antes de construir la fila.
_ALLOWED_FAMILIES: frozenset[str] = frozenset({
    "fouls",
    "fouls_ou",
    "fouls_team",
    "fouls_team_ou",
    "throw_ins",
    "throw_ins_ou",
})

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": "https://22bet92.com/",
    "Origin": "https://22bet92.com",
}

PARAMS_BASE = {
    "lng": "es_ES",
    "tf": 3000000,
    "tz": 2,
    "country": 78,
    "partner": 151,
    "gr": 151,
    "mode": 4,
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: Optional[int]) -> Optional[int]:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


RATE_MIN = _env_float("TWENTYTWOBET_RATE_MIN", 1.0)
RATE_MAX = _env_float("TWENTYTWOBET_RATE_MAX", 2.0)
MAX_MATCHES = _env_int("TWENTYTWOBET_MAX_MATCHES", None)

TIMEOUT = 20
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0


# ─────────────────────────────────────────────────────────────
# Decodificación de selecciones (best-effort)
# ─────────────────────────────────────────────────────────────
SELECTION_LABELS: dict[tuple[int, int], dict[int, str]] = {
    (1, 1):  {1: "home", 2: "draw", 3: "away"},
    (4, 17): {9: "over", 10: "under"},
    (2, 2):  {7: "home", 8: "away"},
}

GENERIC_T_LABELS: dict[int, str] = {
    1:  "home",
    2:  "draw",
    3:  "away",
    4:  "home_or_draw",
    5:  "home_or_away",
    6:  "draw_or_away",
    7:  "home",
    8:  "away",
    9:  "over",
    10: "under",
    180: "yes",
    181: "no",
}


log = logging.getLogger("scrape_22bet")


# ─────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────
def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def _polite_sleep(min_sleep: float, max_sleep: float) -> None:
    time.sleep(random.uniform(min_sleep, max_sleep))


def _get(
    session: requests.Session,
    endpoint: str,
    extra_params: Optional[dict] = None,
) -> Optional[dict]:
    url = f"{BASE_URL}/{endpoint}"
    params = dict(PARAMS_BASE)
    if extra_params:
        params.update(extra_params)

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if (
                e.response is not None
                and 400 <= e.response.status_code < 500
                and e.response.status_code != 429
            ):
                log.warning("HTTP %s en %s (no se reintenta)", status, url)
                return None
            last_exc = e
            log.info("HTTP %s en %s (intento %d/%d)", status, url, attempt + 1, MAX_RETRIES)
        except Exception as e:
            last_exc = e
            log.info("Error en %s: %s (intento %d/%d)", url, e, attempt + 1, MAX_RETRIES)
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF ** attempt)
    log.warning("Falló %s tras %d intentos: %s", url, MAX_RETRIES, last_exc)
    return None


# ─────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────
def fetch_laliga_events(session: requests.Session) -> list[dict]:
    """Descubre todos los partidos upcoming de LaLiga."""
    data = _get(session, "GetChampZip", {"champ": LALIGA_CHAMP_ID, "groupChamps": "true"})
    if not data:
        return []
    val = data.get("Value") or {}
    raw = val.get("G") if isinstance(val, dict) else []
    if not raw:
        log.warning("GetChampZip no devolvió partidos (Value vacío).")
        return []
    games: list[dict] = []
    for g in raw:
        if g.get("LI") and g["LI"] != LALIGA_CHAMP_ID:
            continue
        home = g.get("O1", "")
        away = g.get("O2", "")
        if home.lower() in {"locales", "visitantes"} or away.lower() in {"locales", "visitantes"}:
            continue
        games.append({
            "ci":         g.get("CI"),
            "home":       home,
            "away":       away,
            "start_unix": g.get("S"),
        })
    log.info("Partidos LaLiga encontrados: %d", len(games))
    return games


def fetch_subgames_index(session: requests.Session, match_ci: int) -> dict:
    data = _get(session, "GetGameZip", {"id": match_ci, "isSubGames": "true", "grMode": 4})
    return (data or {}).get("Value") or {}


def fetch_subgame_events(session: requests.Session, subgame_ci: int) -> list[dict]:
    data = _get(session, "GetGameZip", {"id": subgame_ci, "isSubGames": "false", "grMode": 4})
    val = (data or {}).get("Value") or {}
    return val.get("E") or []


# ─────────────────────────────────────────────────────────────
# PARSEO
# ─────────────────────────────────────────────────────────────
def _unix_to_iso(s: Optional[int]) -> str:
    if s is None:
        return ""
    try:
        s_int = int(s)
    except (TypeError, ValueError):
        return ""
    if s_int > 10_000_000_000:
        s_int //= 1000
    try:
        return datetime.fromtimestamp(s_int, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _decode_selection(gs: Optional[int], g: Optional[int], t: Optional[int]) -> str:
    if t is None:
        return "unknown"
    key = (gs or -1, g or -1)
    specific = SELECTION_LABELS.get(key)
    if specific and t in specific:
        return specific[t]
    if t in GENERIC_T_LABELS:
        return GENERIC_T_LABELS[t]
    return f"T{t}"


def _outcome_to_row(
    ev: dict,
    event_info: dict,
    subgame_meta: dict,
    *,
    scrape_run_id: str,
    scraped_at_iso: str,
    is_main_line: bool = False,
) -> Optional[dict]:
    """Convierte un outcome crudo (Value.E[i]) en fila canónica, o None si no es válida."""
    price = ev.get("C")
    if price is None:
        return None
    try:
        odds = float(price)
    except (TypeError, ValueError):
        return None
    if odds <= 1.0:
        return None

    gs = ev.get("GS")
    g  = ev.get("G")
    t  = ev.get("T")
    line = ev.get("P")

    # En 22bet el nombre legible del grupo viene en `TG` ("Saques de esquina",
    # "Total" …). `N` suele ser un id numérico (150190, 222786) — lo dejamos
    # como fallback sólo si no hay TG.
    tg_raw     = subgame_meta.get("TG") if subgame_meta else None
    mname_raw  = subgame_meta.get("N") if subgame_meta else None
    subgame_ci = subgame_meta.get("CI") if subgame_meta else None

    tg_str = str(tg_raw).strip() if tg_raw is not None else ""
    market_name = tg_str or (str(mname_raw) if mname_raw is not None else f"gs{gs}_g{g}")
    fam = infer_market_family(market_name, "", altenar_type_id=None)
    if fam in EXCLUDED_MARKET_FAMILIES:
        return None
    # 22bet: solo faltas y saques de banda (whitelist estricta).
    if fam not in _ALLOWED_FAMILIES:
        return None
    # La "línea principal" (Value.E del partido) son los mercados principales
    # del partido (1X2 base, totales de goles, hándicap, BTTS, …). Si la
    # inferencia por keyword no encuentra familia para esos outcomes es porque
    # efectivamente son esos principales — los descartamos.
    if is_main_line and fam == "other":
        return None

    m_readable = format_readable_market_name(fam, market_name, "", altenar_type_id=None)

    selection = _decode_selection(gs, g, t)
    outcome_name = ev.get("N")
    if outcome_name and selection.startswith("T"):
        selection = str(outcome_name)[:64]

    market_id = str(subgame_ci) if subgame_ci is not None else f"gs{gs}_g{g}"
    sv_line = "" if line is None else str(line)
    # selection_id debe ser único dentro del partido para poder seguir una cuota en el tiempo.
    sel_id_parts = [str(market_id), str(gs or ""), str(g or ""), str(t or ""), sv_line]
    selection_id = "_".join(p for p in sel_id_parts if p)

    return normalize_row({
        "scrape_run_id":  scrape_run_id,
        "scraped_at":     scraped_at_iso,
        "bookmaker":      BOOKMAKER,
        "competition":    COMPETITION,
        "match_key":      event_info["match_key"],
        "event_id":       event_info["event_id"],
        "event_name":     event_info["event_name"],
        "home_team":      event_info["home_team"],
        "away_team":      event_info["away_team"],
        "match_time":     event_info["start_iso"],
        "event_url":      "",
        "market_id":      market_id,
        "market_name":    m_readable,
        "market_type":    f"GS{gs}_G{g}" if gs is not None or g is not None else "",
        "market_sv":      sv_line,
        "market_family":  fam,
        "selection_id":   selection_id,
        "selection_name": selection,
        "odds_decimal":   parse_decimal(odds),
        "odds_raw":       str(price),
        "is_suspended":   False,
    })


def parse_event_markets(
    session: requests.Session,
    game: dict,
    *,
    scrape_run_id: str,
    scraped_at_iso: str,
    rate_min: float,
    rate_max: float,
) -> list[dict]:
    match_ci  = game["ci"]
    start_iso = _unix_to_iso(game["start_unix"])
    home      = game["home"]
    away      = game["away"]

    event_info = {
        "event_id":   str(match_ci),
        "event_name": f"{home} vs {away}",
        "home_team":  home,
        "away_team":  away,
        "match_key":  make_match_key(home, away),
        "start_iso":  start_iso,
    }

    _polite_sleep(rate_min, rate_max)
    val = fetch_subgames_index(session, match_ci)
    if not val:
        log.info("  -> [missing] %s: GetGameZip vacío", event_info["event_name"])
        return []

    rows: list[dict] = []

    # (1) Línea principal: outcomes directamente en Value.E del partido.
    main_events = val.get("E") or []
    main_meta = {"TG": "Main", "N": "Main line", "CI": None}
    for ev in main_events:
        row = _outcome_to_row(
            ev, event_info, main_meta,
            scrape_run_id=scrape_run_id, scraped_at_iso=scraped_at_iso,
            is_main_line=True,
        )
        if row:
            rows.append(row)

    # (2) Subgames: un subgame = un grupo de mercado con su propio CI.
    subgames = val.get("SG") or []
    log.info(
        "  -> %s: %d outcomes en línea principal, %d subgames por enumerar",
        event_info["event_name"], len(main_events), len(subgames),
    )
    for sg in subgames:
        sg_ci = sg.get("CI")
        if sg_ci is None:
            continue
        _polite_sleep(rate_min, rate_max)
        try:
            sg_events = fetch_subgame_events(session, sg_ci)
        except Exception as exc:
            log.warning("    subgame CI=%s error: %s", sg_ci, exc)
            continue
        for ev in sg_events:
            row = _outcome_to_row(
                ev, event_info, sg,
                scrape_run_id=scrape_run_id, scraped_at_iso=scraped_at_iso,
            )
            if row:
                rows.append(row)

    log.info("  -> [ok] %s: %d cuotas totales", event_info["event_name"], len(rows))
    return rows


# ─────────────────────────────────────────────────────────────
# MAIN / ENTRY POINT
# ─────────────────────────────────────────────────────────────
def _configure_logging(verbose: bool) -> None:
    if logging.getLogger().handlers:
        return  # ya configurado por otra parte (run_all.py, tests)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler()],
    )


def run(
    *,
    run_stamp: Optional[str] = None,
    scraped_at_iso: Optional[str] = None,
    max_matches: Optional[int] = None,
    rate_min: Optional[float] = None,
    rate_max: Optional[float] = None,
    verbose: bool = False,
) -> list[dict]:
    """
    Ejecuta el scraper y persiste en todos los backends habilitados.
    Mismo contrato que el resto: run_stamp + scraped_at_iso compartidos.
    """
    _configure_logging(verbose)
    run_stamp = run_stamp or make_run_stamp()
    scraped_at_iso = scraped_at_iso or iso_now_utc()

    rmin = rate_min if rate_min is not None else RATE_MIN
    rmax = rate_max if rate_max is not None else RATE_MAX
    if rmax < rmin:
        rmax = rmin
    limit = max_matches if max_matches is not None else MAX_MATCHES

    session = _build_session()
    games = fetch_laliga_events(session)
    if not games:
        log.warning("No se encontraron partidos de LaLiga — nada que subir.")
        save_json([], bookmaker=BOOKMAKER, run_stamp=run_stamp)
        save_csv([],  bookmaker=BOOKMAKER, run_stamp=run_stamp)
        return []

    if limit is not None:
        games = games[:limit]
        log.info("Limitado a %d partidos.", len(games))

    all_rows: list[dict] = []
    stats = {"ok": 0, "empty": 0, "error": 0}
    for i, game in enumerate(games):
        try:
            rows = parse_event_markets(
                session, game,
                scrape_run_id=run_stamp, scraped_at_iso=scraped_at_iso,
                rate_min=rmin, rate_max=rmax,
            )
        except Exception as exc:
            log.exception("Error procesando %s vs %s: %s", game.get("home"), game.get("away"), exc)
            stats["error"] += 1
            continue
        if rows:
            all_rows.extend(rows)
            stats["ok"] += 1
        else:
            stats["empty"] += 1
            # 22bet solo publica mercados de faltas/saques para partidos próximos
            # (generalmente el partido del día o del día siguiente). En cuanto
            # encontramos un partido sin datos, los siguientes tampoco los tendrán.
            log.warning(
                "22bet: %s devolvió 0 cuotas de faltas/saques — abortando el resto.",
                f"{game.get('home')} vs {game.get('away')}",
            )
            break

    log.info(
        "Resumen: ok=%d empty=%d error=%d | total_rows=%d",
        stats["ok"], stats["empty"], stats["error"], len(all_rows),
    )

    save_json(all_rows, bookmaker=BOOKMAKER, run_stamp=run_stamp)
    save_csv(all_rows,  bookmaker=BOOKMAKER, run_stamp=run_stamp)
    save_supabase(all_rows)
    save_mongo(all_rows, bookmaker=BOOKMAKER)

    print(f"\n🏁 22bet: {len(games)} partidos, {len(all_rows)} cuotas (run {run_stamp})")
    return all_rows


# ─────────────────────────────────────────────────────────────
# CLI — ejecutar como script suelto
# ─────────────────────────────────────────────────────────────
def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scraper de TODOS los mercados de LaLiga en 22bet.")
    parser.add_argument("--max-matches", type=int, default=None, help="Límite de partidos (smoke tests).")
    parser.add_argument("--verbose", action="store_true", help="Logs nivel DEBUG.")
    parser.add_argument("--rate-min", type=float, default=None, help=f"Pausa mínima entre requests (default: {RATE_MIN}s).")
    parser.add_argument("--rate-max", type=float, default=None, help=f"Pausa máxima entre requests (default: {RATE_MAX}s).")
    parser.add_argument("--list-only", action="store_true", help="Sólo lista partidos y CI, no fetcha mercados.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    if args.list_only:
        session = _build_session()
        games = fetch_laliga_events(session)
        for g in games:
            start_iso = _unix_to_iso(g["start_unix"]) or "sin fecha"
            print(f"CI={g['ci']:>11}  {g['home']:<28} vs {g['away']:<28}  {start_iso}")
        return 0

    run(
        max_matches=args.max_matches,
        rate_min=args.rate_min,
        rate_max=args.rate_max,
        verbose=args.verbose,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
