"""
Scraper de cuotas de LaLiga - Retabet
Usa Playwright para superar el F5 BIG-IP JS Challenge.

Por defecto, para cada partido abre la URL del evento, recorre las pestañas de mercados
(Principales, Partido, Estadísticas Partido, Córners, etc.), en cada una hace scroll y
clic en `span.bets__tit` para desplegar bloques, y fusiona mercados por `data-mid`.
Si falla o RETABET_DETAIL_VIA_PAGE=0, usa POST /api/render/LoadPageByUrl.

Desde la raíz del repositorio:
    python scripts/retabet.py

Instalación: ver requirements.txt y `playwright install chromium`
"""

import os
import re
import sys
import json
import time
import asyncio
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Resolver imports del paquete `bookers` al ejecutar como script
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

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, BrowserContext

try:
    from playwright_stealth import Stealth as _PlaywrightStealth
except ImportError:
    _PlaywrightStealth = None  # type: ignore[misc, assignment]

from bookers.odds_schema import (
    CANONICAL_COLUMNS,
    make_match_key,
    make_run_stamp,
    iso_now_utc,
    parse_decimal,
    normalize_row,
)
from bookers.paths import data_path, ensure_data_dir
from bookers.market_labels import (
    format_readable_market_name,
    infer_market_family,
    EXCLUDED_MARKET_FAMILIES,
)
from bookers.persistence import save_csv, save_json, save_mongo
from bookers.supabase_store import save_supabase


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


# ─────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────
BASE_URL        = "https://apuestas.retabet.es"
LALIGA_PATH     = "/deportes/futbol/espana/laliga/1"
API_RENDER      = f"{BASE_URL}/api/render/LoadPageByUrl"
DELAY_REQUESTS  = 1.2   # segundos entre peticiones a la API
# Local: ventana visible; en contenedor/Railway: headless obligatorio.
HEADLESS        = _env_bool("HEADLESS", False)
# Detalle por URL real + scroll + clic en títulos de mercado (más bloques en el DOM que LoadPageByUrl solo).
RETABET_DETAIL_VIA_PAGE = _env_bool("RETABET_DETAIL_VIA_PAGE", True)
# Clic en pestañas horizontales del partido (faltas suelen estar en "Estadísticas Partido", no en "Principales").
RETABET_CLICK_MARKET_TABS = _env_bool("RETABET_CLICK_MARKET_TABS", True)
# Depuración: RETABET_DEBUG_SAVE_HTML=1 guarda el HTML crudo de UN partido (ver RETABET_DEBUG_EVENT_INDEX).
RETABET_DEBUG_SAVE_HTML = _env_bool("RETABET_DEBUG_SAVE_HTML", False)

# Textos de pestañas tal como en la barra de Retabet (orden no crítico; se fusiona por data-mid).
_RETABET_MARKET_TAB_LABELS: tuple[str, ...] = (
    "Principales",
    "Partido",
    "Goles",
    "Estadísticas Equipos",
    "Estadísticas Partido",
    "Estadísticas Jugadores",
    "Comparaciones",
    "Jugadores",
    "Apuestas Múltiples",
    "Córners",
    "Tarjetas",
    "Equipos",
)

BOOKMAKER       = "retabet"
COMPETITION     = "LaLiga"

# Salida: data/retabet_laliga_odds_<run_stamp>.{json,csv}; se desactiva con
# DISABLE_FILE_OUTPUT=1 en Railway (se maneja dentro de `save_csv/save_json`).
# MongoDB es opt-in con MONGO_URI en el entorno (ver `bookers.persistence`).


# ─────────────────────────────────────────────────────────────
# MODELOS
# ─────────────────────────────────────────────────────────────
@dataclass
class Odd:
    option_id: str
    name: str
    value: str
    value_decimal: Optional[float] = None

    def __post_init__(self):
        try:
            self.value_decimal = float(self.value.replace(",", "."))
        except (ValueError, AttributeError):
            self.value_decimal = None


@dataclass
class Market:
    market_id: str
    name: str
    odds: list[Odd] = field(default_factory=list)


@dataclass
class Event:
    event_id:   str
    home_team:  str
    away_team:  str
    match_time: str
    url:        str
    markets:    list[Market] = field(default_factory=list)
    scraped_at: str = field(default_factory=lambda: datetime.now().isoformat())


# ─────────────────────────────────────────────────────────────
# PARSERS HTML
# ─────────────────────────────────────────────────────────────
def parse_event_list(html: str) -> list[dict]:
    """
    Parsea el HTML de /laliga/1 y devuelve la lista de eventos.
    Cada dict tiene: event_id, url, home_team, away_team, match_time,
                     odds_preview (cuotas 1X2 de la lista)
    """
    soup = BeautifulSoup(html, "lxml")
    events = []

    for li in soup.select("li.jev[data-i]"):
        event_id = li.get("data-i", "")
        data_u   = li.get("data-u", "")
        if not event_id or not data_u:
            continue

        # Equipos
        spans = li.select(".event__players-name li span")
        home  = spans[0].get_text(strip=True) if len(spans) > 0 else ""
        away  = spans[1].get_text(strip=True) if len(spans) > 1 else ""

        # Hora del partido (regex en todo el texto del li)
        import re
        all_text   = li.get_text()
        time_match = re.search(r"\d{1,2}:\d{2}", all_text)
        match_time = time_match.group(0) if time_match else ""

        # Preview de cuotas 1X2 que ya aparecen en la lista
        odds_preview = []
        for bet in li.select("li.jo.betbox[data-i]"):
            odds_preview.append({
                "option_id": bet.get("data-i", ""),
                "name":  bet.select_one(".jqt").get_text(strip=True) if bet.select_one(".jqt") else "",
                "value": bet.select_one(".jpr").get_text(strip=True) if bet.select_one(".jpr") else "",
            })

        events.append({
            "event_id":     event_id,
            "url":          f"{BASE_URL}{data_u}",
            "path":         data_u,          # relativa, para la API
            "home_team":    home,
            "away_team":    away,
            "match_time":   match_time,
            "odds_preview": odds_preview,
        })

    return events


def parse_event_detail(html: str, event_info: dict) -> Event:
    """
    Parsea el HTML de la página de un evento y extrae todos los mercados y cuotas.
    """
    soup = BeautifulSoup(html, "lxml")

    event = Event(
        event_id   = event_info["event_id"],
        home_team  = event_info["home_team"],
        away_team  = event_info["away_team"],
        match_time = event_info["match_time"],
        url        = event_info["url"],
    )

    # Cada mercado: div.bets__wrapper.jbet[data-mid]
    for market_div in soup.select("div.bets__wrapper.jbet[data-mid]"):
        market_id = market_div.get("data-mid", "")

        # Nombre del mercado: priorizar el título VISIBLE (span.bets__tit), que es lo que
        # muestra la web ("Faltas durante el partido", etc.). `data-fmkn` a veces es un
        # código interno sin la palabra "Faltas" y entonces no clasificábamos ni guardábamos bien.
        market_name = ""
        tit_el = market_div.select_one("span.bets__tit")
        if tit_el:
            raw_tit = " ".join(tit_el.get_text(" ", strip=True).split())
            for line in raw_tit.splitlines():
                line = line.strip()
                if line:
                    market_name = line
                    break
            if not market_name and raw_tit:
                market_name = raw_tit

        if not market_name:
            name_el = market_div.select_one("span.mt[data-fmkn]")
            if name_el:
                market_name = (
                    name_el.get_text(" ", strip=True)
                    or name_el.get("data-fmkn", "")
                    or ""
                ).strip()

        market = Market(market_id=market_id, name=market_name)

        # Cuotas: li.jopc[data-i]
        for li in market_div.select("li.jopc[data-i]"):
            option_id  = li.get("data-i", "")
            name_span  = li.select_one(".jqt")
            odd_span   = li.select_one(".jpr")

            if name_span and odd_span:
                market.odds.append(Odd(
                    option_id = option_id,
                    name      = name_span.get_text(strip=True),
                    value     = odd_span.get_text(strip=True),
                ))

        if market.odds:
            event.markets.append(market)

    return event


# ─────────────────────────────────────────────────────────────
# SCRAPER PRINCIPAL (async con Playwright)
# ─────────────────────────────────────────────────────────────
async def scrape_laliga() -> list[Event]:
    ensure_data_dir()
    async with async_playwright() as p:
        # ── 1. Lanzar browser y resolver el F5 JS Challenge ──────────────
        print("[*] Iniciando browser y resolviendo F5 challenge...")
        # Intentar usar Chrome real (channel="chrome"); si no está instalado,
        # caer a Chromium normal.
        launch_args = dict(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--start-maximized",
            ],
            slow_mo=50,
        )
        try:
            browser = await p.chromium.launch(channel="chrome", **launch_args)
            print("[*] Usando Chrome (canal estable).")
        except Exception:
            browser = await p.chromium.launch(**launch_args)
            print("[*] Chrome no disponible, usando Chromium.")

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="es-ES",
            viewport={"width": 1366, "height": 768},
            java_script_enabled=True,
            ignore_https_errors=True,
        )

        page = await context.new_page()

        # playwright-stealth (pip: playwright-stealth): opcional; sin él Retabet puede
        # igual funcionar, pero el F5 puede ser más estricto en algunos entornos.
        if _PlaywrightStealth is not None:
            stealth = _PlaywrightStealth(
                navigator_languages_override=("es-ES", "es"),
                navigator_platform_override="Win32",
            )
            await stealth.apply_stealth_async(context)
        else:
            print(
                "[!] playwright-stealth no instalado — continuo sin Stealth. "
                "Instala con: pip install playwright-stealth"
            )

        # Navegar a la página de LaLiga.
        # El challenge F5 BIG-IP hace una o más redirecciones internas;
        # usamos "domcontentloaded" para no bloquear, luego esperamos networkidle.
        print("[*] Navegando a LaLiga (esperando resolucion del challenge)...")
        try:
            await page.goto(
                f"{BASE_URL}{LALIGA_PATH}",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception:
            pass  # El challenge puede causar timeout en goto; seguimos igualmente

        # Esperar a que el challenge F5 se ejecute, ponga cookies y haga el redirect.
        # networkidle espera hasta que no hay peticiones de red por 500ms.
        print("[*] Esperando networkidle (challenge resolviendo)...")
        try:
            await page.wait_for_load_state("networkidle", timeout=90000)
        except Exception:
            pass

        # Breve pausa por si el challenge usa setTimeout para el redirect final
        await asyncio.sleep(3)

        # Esperar el contenido real de la página
        print("[*] Esperando contenido de eventos...")
        try:
            await page.wait_for_selector("li.jev[data-i]", timeout=60000)
            print("[*] Challenge F5 superado. Cookies de sesion obtenidas.")
        except Exception:
            _dbg = str(data_path("retabet_debug.png"))
            await page.screenshot(path=_dbg, full_page=True)
            page_text = await page.content()
            print(f"[!] No se encontro 'li.jev[data-i]'. Captura guardada en {_dbg}")
            print(f"[!] URL actual: {page.url}")
            print(f"[!] Primeros 500 chars del HTML: {page_text[:500]}")
            await browser.close()
            return []

        # ── 2. Obtener lista de eventos via API (o HTML de la propia página) ─
        print("[*] Obteniendo lista de partidos de LaLiga...")
        try:
            list_html = await _api_post(context, LALIGA_PATH)
        except Exception as e:
            print(f"    [!] API falló ({e}), usando HTML de la página cargada...")
            list_html = await page.content()
        event_list = parse_event_list(list_html)
        print(f"    → {len(event_list)} partidos encontrados:")
        for ev in event_list:
            print(f"      · {ev['home_team']} vs {ev['away_team']} ({ev['match_time']}) — ID {ev['event_id']}")

        # ── 3. Scraping de cada evento ────────────────────────────────────
        all_events: list[Event] = []
        total = len(event_list)

        for i, ev_info in enumerate(event_list, 1):
            match_name = f"{ev_info['home_team']} vs {ev_info['away_team']}"
            print(f"\n[{i}/{total}] Scraping: {match_name}")
            try:
                if RETABET_DETAIL_VIA_PAGE and RETABET_CLICK_MARKET_TABS:
                    event, html = await _scrape_event_from_tabs(page, context, ev_info)
                else:
                    html = await _fetch_event_detail_html(page, context, ev_info["path"])
                    event = parse_event_detail(html, ev_info)
                if RETABET_DEBUG_SAVE_HTML:
                    try:
                        dbg_idx = int(os.environ.get("RETABET_DEBUG_EVENT_INDEX", "1") or "1")
                    except ValueError:
                        dbg_idx = 1
                    if i == max(1, dbg_idx):
                        ensure_data_dir()
                        eid = ev_info.get("event_id", "unknown")
                        out_html = data_path(f"retabet_debug_match_{eid}.html")
                        out_html.write_text(html, encoding="utf-8")
                        low = html.lower()
                        n_faltas = low.count("faltas")
                        n_falta = low.count("falta")
                        jb = len(
                            BeautifulSoup(html, "lxml").select(
                                "div.bets__wrapper.jbet[data-mid]"
                            )
                        )
                        with_f = [m.name for m in event.markets if "falta" in (m.name or "").lower()]
                        print(
                            f"    [debug] HTML guardado: {out_html} "
                            f"({len(html)} bytes; 'faltas' en bruto: {n_faltas}; "
                            f"'falta': {n_falta}; bloques jbet[data-mid]: {jb})"
                        )
                        print(
                            f"    [debug] parse_event_detail: {len(event.markets)} mercados; "
                            f"con 'falta' en nombre: {len(with_f)}"
                        )
                        if with_f:
                            print(f"    [debug] Ejemplos: {with_f[:8]}")
                all_events.append(event)
                print(f"    → {len(event.markets)} mercados extraídos")
                await asyncio.sleep(DELAY_REQUESTS)
            except Exception as e:
                print(f"    ⚠ Error: {e}")

        await browser.close()
        return all_events


async def _api_post(context: BrowserContext, path: str) -> str:
    """
    Usa la API POST /api/render/LoadPageByUrl con las cookies de sesión actuales.
    Devuelve el HTML del widget pre-renderizado.
    """
    response = await context.request.post(
        API_RENDER,
        headers={
            "Content-Type":     "application/json",
            "Accept":           "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin":           BASE_URL,
            "Referer":          f"{BASE_URL}{LALIGA_PATH}",
        },
        data=json.dumps({"url": path}),
        timeout=30000,
    )
    if response.status != 200:
        raise RuntimeError(f"API devolvió status {response.status} para {path}")
    body = await response.text()
    # La API puede devolver JSON envolviendo el HTML
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            # Buscar campo que contenga HTML
            for key in ("html", "content", "data", "body", "result"):
                if key in parsed and isinstance(parsed[key], str):
                    return parsed[key]
    except (json.JSONDecodeError, TypeError):
        pass
    return body


_RETABET_EXPAND_SCROLL_JS = r"""
() => {
  const doc = document.documentElement;
  const body = document.body;
  const height = () => Math.max(
    doc ? doc.scrollHeight : 0,
    body ? body.scrollHeight : 0,
    doc ? doc.clientHeight : 0
  );
  for (let y = 0; y < height(); y += 500) {
    window.scrollTo(0, y);
  }
  window.scrollTo(0, height());
  document.querySelectorAll("div.bets__wrapper.jbet span.bets__tit").forEach((el) => {
    try {
      el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    } catch (_e) {}
  });
  for (let y = 0; y < height(); y += 500) {
    window.scrollTo(0, y);
  }
  window.scrollTo(0, height());
}
"""


async def _retabet_load_event_page(page, path: str) -> None:
    """Navega al partido, espera bloques de cuotas, scroll + expandir mercados colapsados."""
    full_url = f"{BASE_URL}{path}" if path.startswith("/") else path
    await page.goto(full_url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_selector("div.bets__wrapper.jbet[data-mid]", timeout=45000)
    except Exception:
        pass
    await page.evaluate(_RETABET_EXPAND_SCROLL_JS)
    extra = float(os.environ.get("RETABET_DETAIL_EXTRA_WAIT", "1.2") or "1.2")
    await asyncio.sleep(extra)


async def _retabet_click_tab_by_name(page, label: str) -> bool:
    """Clic en una pestaña de la barra (tab / link / texto exacto)."""
    if not (label or "").strip():
        return False
    label = label.strip()
    rx_exact = re.compile(r"^\s*" + re.escape(label) + r"\s*$", re.I)

    makers = [
        lambda: page.get_by_role("tab", name=label, exact=True),
        lambda: page.get_by_role("tab", name=rx_exact),
        lambda: page.get_by_role("link", name=label, exact=True),
        lambda: page.get_by_role("link", name=rx_exact),
        lambda: page.get_by_role("button", name=label, exact=True),
        lambda: page.get_by_text(label, exact=True),
    ]
    for make in makers:
        try:
            loc = make()
            if await loc.count() == 0:
                continue
            el = loc.first
            await el.scroll_into_view_if_needed(timeout=5000)
            await el.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


async def _scrape_event_from_tabs(
    page,
    context: BrowserContext,
    ev_info: dict,
) -> tuple[Event, str]:
    """
    Carga el partido, parsea la pestaña inicial, luego recorre el resto de pestañas
    y fusiona mercados por market_id (data-mid). Devuelve (Event, último HTML).
    """
    if not RETABET_DETAIL_VIA_PAGE:
        html = await _api_post(context, ev_info["path"])
        return parse_event_detail(html, ev_info), html

    try:
        await _retabet_load_event_page(page, ev_info["path"])
    except Exception as e:
        print(f"    [!] Detalle por página falló ({e}); usando LoadPageByUrl.")
        html = await _api_post(context, ev_info["path"])
        return parse_event_detail(html, ev_info), html

    by_mid: dict[str, Market] = {}

    def _merge_current(html: str) -> None:
        evp = parse_event_detail(html, ev_info)
        for m in evp.markets:
            by_mid[m.market_id] = m

    _merge_current(await page.content())

    tab_delay = float(os.environ.get("RETABET_TAB_CLICK_DELAY", "0.55") or "0.55")
    for tab_label in _RETABET_MARKET_TAB_LABELS:
        if tab_label.strip().lower() == "principales":
            continue
        clicked = await _retabet_click_tab_by_name(page, tab_label)
        if not clicked:
            continue
        await asyncio.sleep(tab_delay)
        try:
            await page.evaluate(_RETABET_EXPAND_SCROLL_JS)
        except Exception:
            pass
        await asyncio.sleep(max(0.2, tab_delay * 0.45))
        _merge_current(await page.content())

    event = Event(
        event_id   = ev_info["event_id"],
        home_team  = ev_info["home_team"],
        away_team  = ev_info["away_team"],
        match_time = ev_info["match_time"],
        url        = ev_info["url"],
        markets    = list(by_mid.values()),
    )
    return event, await page.content()


async def _fetch_event_detail_html(
    page,
    context: BrowserContext,
    path: str,
) -> str:
    """
    HTML de una sola carga del partido (sin recorrer pestañas).
    Si falla o RETABET_DETAIL_VIA_PAGE=0, usa LoadPageByUrl.
    """
    if not RETABET_DETAIL_VIA_PAGE:
        return await _api_post(context, path)

    try:
        await _retabet_load_event_page(page, path)
        return await page.content()
    except Exception as e:
        print(f"    [!] Detalle por página falló ({e}); usando LoadPageByUrl.")
        return await _api_post(context, path)


# ─────────────────────────────────────────────────────────────
# SALIDA
# ─────────────────────────────────────────────────────────────
def print_summary(events: list[Event]):
    print("\n" + "=" * 65)
    print("  CUOTAS LALIGA — apuestas.retabet.es")
    print("=" * 65)
    for ev in events:
        print(f"\n🏟  {ev.home_team} vs {ev.away_team}  [{ev.match_time}]  (ID {ev.event_id})")
        for mkt in ev.markets:
            line = f"   {mkt.name[:40]:40s} | "
            line += "  |  ".join(f"{o.name}: {o.value}" for o in mkt.odds[:4])
            if len(mkt.odds) > 4:
                line += f"  (+{len(mkt.odds)-4} más)"
            print(line)


# ─────────────────────────────────────────────────────────────
# CONVERSIÓN A FILAS CANÓNICAS (esquema compartido entre scrapers)
# ─────────────────────────────────────────────────────────────
def events_to_canonical_rows(
    events: list[Event],
    *,
    scrape_run_id: str,
    scraped_at_iso: str,
) -> list[dict]:
    """
    Aplana la estructura Event→Market→Odd en filas canónicas (esquema CANONICAL_COLUMNS).
    Una fila = una cuota individual en un snapshot temporal.
    """
    rows: list[dict] = []
    for ev in events:
        match_key = make_match_key(ev.home_team, ev.away_team)
        event_name = f"{ev.home_team} vs {ev.away_team}"
        for mkt in ev.markets:
            m_sv = ""
            fam = infer_market_family(mkt.name, m_sv, altenar_type_id=None)
            if fam in EXCLUDED_MARKET_FAMILIES:
                continue
            m_readable = format_readable_market_name(fam, mkt.name, m_sv, altenar_type_id=None)
            for odd in mkt.odds:
                rows.append(normalize_row({
                    "scrape_run_id":  scrape_run_id,
                    "scraped_at":     scraped_at_iso,
                    "bookmaker":      BOOKMAKER,
                    "competition":    COMPETITION,
                    "match_key":      match_key,
                    "event_id":       str(ev.event_id),
                    "event_name":     event_name,
                    "home_team":      ev.home_team,
                    "away_team":      ev.away_team,
                    "match_time":     ev.match_time,
                    "event_url":      ev.url,
                    "market_id":      str(mkt.market_id),
                    "market_name":    m_readable,
                    "market_type":    "",
                    "market_sv":      m_sv,
                    "market_family":  fam,
                    "selection_id":   str(odd.option_id),
                    "selection_name": odd.name,
                    "odds_decimal":   parse_decimal(odd.value),
                    "odds_raw":       odd.value,
                    "is_suspended":   False,  # Retabet no expone estado explícito
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
    """
    Ejecuta el scraper y persiste en todos los backends habilitados.
    Devuelve las filas canónicas. Pensada para ser llamada desde `run_all.py`
    con un `run_stamp` compartido entre scrapers.
    """
    run_stamp = run_stamp or make_run_stamp()
    scraped_at_iso = scraped_at_iso or iso_now_utc()

    events = asyncio.run(scrape_laliga())
    print_summary(events)

    rows = events_to_canonical_rows(
        events,
        scrape_run_id=run_stamp,
        scraped_at_iso=scraped_at_iso,
    )

    save_json(rows, bookmaker=BOOKMAKER, run_stamp=run_stamp)
    save_csv(rows,  bookmaker=BOOKMAKER, run_stamp=run_stamp)
    save_supabase(rows)
    save_mongo(rows, bookmaker=BOOKMAKER)

    print(f"\n🏁 Retabet: {len(events)} partidos, {len(rows)} cuotas (run {run_stamp})")
    return rows


if __name__ == "__main__":
    run()