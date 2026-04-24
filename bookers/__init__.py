"""Código compartido entre scrapers (esquema canónico, rutas, persistencia)."""

from .market_labels import (
    EXCLUDED_MARKET_FAMILIES,
    format_readable_market_name,
    infer_market_family,
)
from .odds_schema import (
    CANONICAL_COLUMNS,
    canonical_team,
    empty_row,
    iso_now_utc,
    make_match_key,
    make_run_stamp,
    normalize_row,
    parse_decimal,
    slugify_team,
)
from .paths import data_path, ensure_data_dir
from .persistence import save_csv, save_json, save_mongo
from .supabase_store import save_supabase

__all__ = [
    "CANONICAL_COLUMNS",
    "EXCLUDED_MARKET_FAMILIES",
    "canonical_team",
    "data_path",
    "empty_row",
    "ensure_data_dir",
    "format_readable_market_name",
    "infer_market_family",
    "iso_now_utc",
    "make_match_key",
    "make_run_stamp",
    "normalize_row",
    "parse_decimal",
    "save_csv",
    "save_json",
    "save_mongo",
    "save_supabase",
    "slugify_team",
]
