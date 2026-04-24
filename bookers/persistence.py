"""
Helpers de persistencia compartidos por TODOS los scrapers.

Cada scraper produce filas canónicas (ver `odds_schema.CANONICAL_COLUMNS`) y
usa SIEMPRE estas funciones para guardarlas, de modo que:

- El nombre de fichero sigue un patrón consistente (`<bookmaker>_laliga_odds_<run_stamp>.{csv,json}`).
- La variable `DISABLE_FILE_OUTPUT` desactiva el volcado a disco (Railway).
- MongoDB es opt-in con `MONGO_URI` y es idéntico para los 4 scrapers.

`save_supabase` vive aparte en `supabase_store.py` porque tiene lógica propia
(construcción de DSN desde variables separadas, batching, etc.).
"""

from __future__ import annotations

import csv
import json
import os
from typing import Iterable, Optional

from .odds_schema import CANONICAL_COLUMNS
from .paths import data_path, ensure_data_dir


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _file_output_enabled() -> bool:
    return not _env_bool("DISABLE_FILE_OUTPUT", False)


def _output_prefix(bookmaker: str, prefix: Optional[str]) -> str:
    return prefix or f"{bookmaker}_laliga_odds"


def save_json(
    rows: Iterable[dict],
    *,
    bookmaker: str,
    run_stamp: str,
    prefix: Optional[str] = None,
) -> Optional[str]:
    """Guarda `rows` como JSON en `data/<prefix>_<run_stamp>.json`.

    Devuelve la ruta o None si el volcado a disco está desactivado.
    """
    if not _file_output_enabled():
        return None
    rows_list = list(rows)
    ensure_data_dir()
    path = data_path(f"{_output_prefix(bookmaker, prefix)}_{run_stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows_list, f, ensure_ascii=False, indent=2)
    print(f"[{bookmaker}] JSON guardado: {path} ({len(rows_list)} filas)")
    return str(path)


def save_csv(
    rows: Iterable[dict],
    *,
    bookmaker: str,
    run_stamp: str,
    prefix: Optional[str] = None,
) -> Optional[str]:
    """Guarda `rows` como CSV canónico en `data/<prefix>_<run_stamp>.csv`.

    Usa siempre `CANONICAL_COLUMNS` en el mismo orden. Devuelve la ruta o None
    si el volcado a disco está desactivado.
    """
    if not _file_output_enabled():
        return None
    rows_list = list(rows)
    ensure_data_dir()
    path = data_path(f"{_output_prefix(bookmaker, prefix)}_{run_stamp}.csv")
    if not rows_list:
        print(f"[{bookmaker}] CSV vacío: no se escribió {path}")
        return str(path)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CANONICAL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows_list)
    print(f"[{bookmaker}] CSV guardado: {path} ({len(rows_list)} filas)")
    return str(path)


def save_mongo(
    rows: Iterable[dict],
    *,
    bookmaker: str,
    uri: Optional[str] = None,
    db_name: Optional[str] = None,
    collection_name: Optional[str] = None,
) -> int:
    """
    Inserta filas canónicas en MongoDB (append-only).

    Opt-in: sólo se ejecuta si `MONGO_URI` está definido. Si falla la
    conexión o pymongo no está instalado, avisa y devuelve 0 sin lanzar.
    Devuelve el número de documentos insertados.
    """
    uri = uri if uri is not None else os.environ.get("MONGO_URI", "")
    if not uri:
        return 0

    db_name = db_name or os.environ.get("MONGO_DB", "sports_odds")
    collection_name = collection_name or os.environ.get("MONGO_COLLECTION", "odds_history")

    rows_list = list(rows)
    if not rows_list:
        print(f"[{bookmaker}] Mongo: sin filas.")
        return 0

    try:
        from pymongo import MongoClient
        from pymongo.errors import BulkWriteError
    except ImportError:
        print(f"[{bookmaker}] pymongo no instalado. pip install pymongo")
        return 0

    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
    except Exception as e:
        print(f"[{bookmaker}] Mongo: no se pudo conectar ({e}).")
        client.close()
        return 0

    try:
        collection = client[db_name][collection_name]
        collection.create_index("bookmaker")
        collection.create_index("scrape_run_id")
        collection.create_index("match_key")
        collection.create_index("event_id")
        collection.create_index("selection_id")
        collection.create_index("scraped_at")
        collection.create_index(
            [("bookmaker", 1), ("event_id", 1), ("selection_id", 1), ("scraped_at", 1)]
        )
        try:
            result = collection.insert_many(rows_list, ordered=False)
            run_id = rows_list[0].get("scrape_run_id", "?")
            count = len(result.inserted_ids)
            print(
                f"[{bookmaker}] Mongo [{db_name}.{collection_name}]: "
                f"{count} cuotas insertadas (run {run_id})"
            )
            return count
        except BulkWriteError as e:
            print(f"[{bookmaker}] Mongo BulkWriteError: {e.details}")
            return 0
    finally:
        client.close()
