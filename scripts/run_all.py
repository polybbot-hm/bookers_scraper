"""
Entry point del cron: ejecuta todos los scrapers secuencialmente con un
único `scrape_run_id` compartido, para que sea trivial agrupar todas las
cuotas del mismo ciclo en Supabase/Mongo.

Uso en local:
    python scripts/run_all.py

En Railway se ejecuta como comando del cron job (ver railway.json).

Variables de entorno relevantes (todas opcionales salvo las de Supabase):

    SUPABASE_DB_URL              postgres://...  (necesario para subir a Supabase)
    SUPABASE_TABLE               odds_history    (default)
    MONGO_URI                    mongodb+srv://... (opt-in; si no se define, se salta)
    MONGO_DB                     sports_odds     (default)
    MONGO_COLLECTION             odds_history    (default)
    DISABLE_FILE_OUTPUT          1   (Railway: no generar CSV/JSON locales)
    HEADLESS                     1   (Railway/contenedor: obligatorio)

    RETABET_DETAIL_VIA_PAGE      1   (default: URL real + scroll/expandir)
    RETABET_CLICK_MARKET_TABS    1   (default: recorrer pestañas de mercados)
    GRAN_FILTER_MARKETS          0   (default: todos los mercados Altenar)
    TWENTYTWOBET_RATE_MIN        1.0 (pausa mínima entre requests de 22bet)
    TWENTYTWOBET_RATE_MAX        2.0 (pausa máxima entre requests de 22bet)
    TWENTYTWOBET_MAX_MATCHES     (vacío=todos; útil para smoke tests)

    SCRAPERS   lista separada por comas (default: retabet,grancasino,kirolbet,twentytwobet)
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from dotenv import load_dotenv
    # interpolate=False: si la contraseña lleva $, sin esto dotenv la trocea y falla el login.
    load_dotenv(_REPO_ROOT / ".env", interpolate=False)
except ImportError:
    pass  # python-dotenv es opcional; en Railway las env vars ya vienen inyectadas

from bookers.odds_schema import iso_now_utc, make_run_stamp


# Import perezoso para que un fallo de import de un scraper no rompa los demás.
def _load_scraper(name: str) -> Callable:
    if name == "retabet":
        from scripts import retabet
        return retabet.run
    if name == "grancasino":
        from scripts import grancasino
        return grancasino.run
    if name == "kirolbet":
        from scripts import kirolbet
        return kirolbet.run
    if name in ("twentytwobet", "22bet"):
        from scripts import twentytwobet
        return twentytwobet.run
    raise ValueError(f"Scraper desconocido: {name}")


def main() -> int:
    scrapers_env = os.environ.get(
        "SCRAPERS",
        "retabet,grancasino,kirolbet,twentytwobet",
    )
    scrapers = [s.strip() for s in scrapers_env.split(",") if s.strip()]

    run_stamp = make_run_stamp()
    scraped_at = iso_now_utc()
    print(f"[run_all] run_stamp={run_stamp} scraped_at={scraped_at}")
    print(f"[run_all] scrapers={scrapers}")

    failures = 0
    for name in scrapers:
        print(f"\n{'#' * 70}\n# {name}\n{'#' * 70}")
        t0 = time.perf_counter()
        try:
            run_fn = _load_scraper(name)
            run_fn(run_stamp=run_stamp, scraped_at_iso=scraped_at)
        except Exception as e:
            failures += 1
            print(f"[run_all] {name} FALLÓ: {e}")
            traceback.print_exc()
        finally:
            print(f"[run_all] {name} terminó en {time.perf_counter() - t0:.1f}s")

    print(f"\n[run_all] Terminado. {len(scrapers) - failures}/{len(scrapers)} OK.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
