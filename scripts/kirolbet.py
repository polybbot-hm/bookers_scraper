"""
Scraper de cuotas de LaLiga (1ª División) en Kirolbet — apuestas.kirolbet.es

Usa Playwright para recorrer el listado de partidos y, para cada uno, evaluar
JS que extrae todos los `marketGroup` con sus odds. Produce filas en el
esquema canónico compartido (`bookers.odds_schema.CANONICAL_COLUMNS`) y las
persiste por los mismos canales que el resto de scrapers.

Desde la raíz del repositorio:
    python scripts/kirolbet.py

Instalación: ver requirements.txt y `playwright install chromium`
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Optional

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

from playwright.async_api import async_playwright

from bookers.market_labels import (
    EXCLUDED_MARKET_FAMILIES,
    format_readable_market_name,
    infer_market_family,
)
from bookers.odds_schema import (
    CANONICAL_COLUMNS,
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
BASE_URL        = "https://apuestas.kirolbet.es"
LEAGUE_URL      = f"{BASE_URL}/esp/Sport/Competicion/1"   # 1ª División
BOOKMAKER       = "kirolbet"
COMPETITION     = "LaLiga"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


HEADLESS = _env_bool("HEADLESS", False)


# ─────────────────────────────────────────────────────────────
# JS inyectado en la página (listado y detalle)
# ─────────────────────────────────────────────────────────────
_EXTRACT_EVENT_LINKS_JS = r"""
() => {
  const seen = new Set();
  const out  = [];
  document.querySelectorAll('a[href*="/Sport/Evento/"]').forEach(a => {
    const m = a.href.match(/\/Evento\/(\d+)/);
    if (!m) return;
    const id = m[1];
    if (seen.has(id)) return;
    seen.add(id);
    out.push({ eventId: id, url: a.href, text: a.innerText.trim() });
  });
  return out;
}
"""

_EXTRACT_MARKETS_JS = r"""
() => {
  const markets = document.querySelectorAll('.marketGroup');
  const result = [];
  for (const m of markets) {
    const nameEl = m.querySelector('.apuesta_evento');
    const marketName = nameEl ? nameEl.innerText.replace(/\s+/g, ' ').trim() : '';

    const clsMatch = m.className.match(/it_(\d+)_(\d+)/);
    const eventId  = clsMatch ? clsMatch[1] : null;
    const marketId = clsMatch ? clsMatch[2] : null;

    const oddLinks = m.querySelectorAll('a[class*="it_"][class*="_F"]');
    const odds = [];
    for (const a of oddLinks) {
      const cls = a.className.match(/it_(\d+)_(\d+)_F/);
      const pron = a.querySelector('.pron');
      const coef = a.querySelector('.coef');
      odds.push({
        marketId: cls ? cls[1] : null,
        oddId:    cls ? cls[2] : null,
        label:    (a.getAttribute('title') || (pron ? pron.innerText : '')).trim(),
        value:    coef ? coef.innerText.trim() : null,
        isMain:   a.getAttribute('ip') === '1'
      });
    }
    if (odds.length > 0) {
      result.push({ eventId, marketId, marketName, odds });
    }
  }
  return result;
}
"""


# ─────────────────────────────────────────────────────────────
# PARSEO DE TÍTULO DE PARTIDO
# ─────────────────────────────────────────────────────────────
_VS_RE = re.compile(r"(.+?)\s+VS\.\s+(.+?)(?:\s*\(\+\s*(\d+)\))?$", re.IGNORECASE)


def _split_home_away(text: str) -> tuple[str, str]:
    """'BETIS VS. REAL MADRID (+ 418)' -> ('BETIS', 'REAL MADRID')."""
    m = _VS_RE.match((text or "").strip())
    if not m:
        return text or "", ""
    return m.group(1).strip(), m.group(2).strip()


# ─────────────────────────────────────────────────────────────
# SCRAPE
# ─────────────────────────────────────────────────────────────
async def _scrape() -> list[dict]:
    """Devuelve la lista cruda de partidos con sus mercados (sin normalizar)."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(locale="es-ES", user_agent=USER_AGENT)
        page = await ctx.new_page()

        print(f"[kirolbet] Abriendo {LEAGUE_URL}")
        await page.goto(LEAGUE_URL, wait_until="domcontentloaded")
        await page.wait_for_selector('a[href*="/Sport/Evento/"]', timeout=15000)
        events = await page.evaluate(_EXTRACT_EVENT_LINKS_JS)
        print(f"[kirolbet] {len(events)} partidos encontrados")

        all_data: list[dict] = []
        for i, ev in enumerate(events, 1):
            home, away = _split_home_away(ev["text"])
            print(f"  [{i}/{len(events)}] {home} vs {away}")
            try:
                await page.goto(ev["url"], wait_until="domcontentloaded")
                await page.wait_for_selector(".marketGroup", timeout=15000)
                await page.wait_for_timeout(800)
                markets = await page.evaluate(_EXTRACT_MARKETS_JS)
            except Exception as e:
                print(f"      !! Error: {e}")
                markets = []

            all_data.append({
                "eventId": ev["eventId"],
                "url":     ev["url"],
                "home":    home,
                "away":    away,
                "markets": markets,
            })

        await browser.close()
    return all_data


# ─────────────────────────────────────────────────────────────
# CONVERSIÓN A FILAS CANÓNICAS
# ─────────────────────────────────────────────────────────────
def events_to_canonical_rows(
    events: list[dict],
    *,
    scrape_run_id: str,
    scraped_at_iso: str,
) -> list[dict]:
    rows: list[dict] = []
    for ev in events:
        event_id   = str(ev.get("eventId") or "")
        home       = ev.get("home") or ""
        away       = ev.get("away") or ""
        event_name = f"{home} vs {away}".strip()
        match_key  = make_match_key(home, away)
        event_url  = ev.get("url") or ""

        for mkt in ev.get("markets", []) or []:
            market_id   = str(mkt.get("marketId") or "")
            market_name = (mkt.get("marketName") or "").strip()

            fam = infer_market_family(market_name, "", altenar_type_id=None)
            if fam in EXCLUDED_MARKET_FAMILIES:
                continue

            m_readable = format_readable_market_name(fam, market_name, "", altenar_type_id=None)

            for odd in mkt.get("odds", []) or []:
                odd_id    = str(odd.get("oddId") or "")
                raw_value = odd.get("value")
                rows.append(normalize_row({
                    "scrape_run_id":  scrape_run_id,
                    "scraped_at":     scraped_at_iso,
                    "bookmaker":      BOOKMAKER,
                    "competition":    COMPETITION,
                    "match_key":      match_key,
                    "event_id":       event_id,
                    "event_name":     event_name,
                    "home_team":      home,
                    "away_team":      away,
                    "match_time":     "",
                    "event_url":      event_url,
                    "market_id":      market_id,
                    "market_name":    m_readable,
                    "market_type":    "",
                    "market_sv":      "",
                    "market_family":  fam,
                    "selection_id":   f"{market_id}_{odd_id}" if market_id or odd_id else "",
                    "selection_name": (odd.get("label") or "").strip(),
                    "odds_decimal":   parse_decimal(raw_value),
                    "odds_raw":       str(raw_value) if raw_value is not None else "",
                    "is_suspended":   False,
                }))
    return rows


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def run(
    *,
    run_stamp: Optional[str] = None,
    scraped_at_iso: Optional[str] = None,
) -> list[dict]:
    """Ejecuta el scraper y persiste en todos los backends habilitados."""
    run_stamp = run_stamp or make_run_stamp()
    scraped_at_iso = scraped_at_iso or iso_now_utc()

    events = asyncio.run(_scrape())
    rows = events_to_canonical_rows(
        events,
        scrape_run_id=run_stamp,
        scraped_at_iso=scraped_at_iso,
    )

    save_json(rows, bookmaker=BOOKMAKER, run_stamp=run_stamp)
    save_csv(rows,  bookmaker=BOOKMAKER, run_stamp=run_stamp)
    save_supabase(rows)
    save_mongo(rows, bookmaker=BOOKMAKER)

    print(f"\n🏁 Kirolbet: {len(events)} partidos, {len(rows)} cuotas (run {run_stamp})")
    return rows


if __name__ == "__main__":
    run()
