"""
Persistencia en Supabase (Postgres) para las filas canónicas.

Conexión directa con psycopg2 (inserts masivos).

Configuración (elige UNA de las dos formas):

1) Variables separadas (recomendado si la contraseña tiene $, @, :, etc.):
       SUPABASE_DB_USER=postgres.TU_PROJECT_REF
       SUPABASE_DB_PASSWORD=tu_contraseña_en_claro
       SUPABASE_DB_HOST=aws-1-eu-west-1.pooler.supabase.com
       SUPABASE_DB_PORT=6543          (opcional, default 6543)
       SUPABASE_DB_NAME=postgres      (opcional)
   Si están las tres primeras, se construye la URI con urllib.parse.quote
   y no hace falta escapar caracteres especiales a mano.

2) Una sola URI:
       SUPABASE_DB_URL=postgresql://postgres.TU_REF:PASSWORD@host:6543/postgres

Si no hay credenciales válidas, `save_supabase` avisa y devuelve 0.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Iterable, Optional
from urllib.parse import quote

from .odds_schema import CANONICAL_COLUMNS

# Columnas en el mismo orden que el INSERT a `odds_history` (ver README: DDL en Supabase)
_INSERT_COLUMNS: list[str] = CANONICAL_COLUMNS
_TABLE = os.environ.get("SUPABASE_TABLE", "odds_history")


def _dsn_from_split_env() -> Optional[str]:
    """DSN desde SUPABASE_DB_USER / PASSWORD / HOST (evita errores de parseo de la URI)."""
    user = os.environ.get("SUPABASE_DB_USER", "").strip()
    password = os.environ.get("SUPABASE_DB_PASSWORD", "")
    if password:
        password = password.strip("\r\n")
    host = os.environ.get("SUPABASE_DB_HOST", "").strip()
    if not (user and password and host):
        return None
    port = os.environ.get("SUPABASE_DB_PORT", "6543").strip() or "6543"
    dbname = os.environ.get("SUPABASE_DB_NAME", "postgres").strip() or "postgres"
    u = quote(user, safe="")
    p = quote(password, safe="")
    return f"postgresql://{u}:{p}@{host}:{port}/{dbname}?sslmode=require"


def _get_dsn() -> Optional[str]:
    """
    Devuelve el DSN de Postgres o None si no está configurado / es inválido.

    - Rechaza la URL HTTPS del proyecto (error frecuente al copiar del panel).
    - Añade sslmode=require si falta y el host es Supabase (evita fallos en Railway).

    Prioridad: variables separadas (SUPABASE_DB_USER + PASSWORD + HOST) si están
    completas; si no, SUPABASE_DB_URL / DATABASE_URL.
    """
    split = _dsn_from_split_env()
    if split:
        return split

    raw = (os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not raw:
        return None
    # Quitar comillas si alguien pega la URI entre comillas en Railway
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1]

    lower = raw.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        print(
            "[supabase] SUPABASE_DB_URL debe ser postgresql://... "
            "(Database → Connection string → URI), no la URL https del proyecto."
        )
        return None
    if not (lower.startswith("postgresql://") or lower.startswith("postgres://")):
        print("[supabase] SUPABASE_DB_URL debe empezar por postgresql:// o postgres://")
        return None

    if "supabase" in lower and "sslmode=" not in lower:
        raw += "&sslmode=require" if "?" in raw else "?sslmode=require"

    return raw


def _qualified_table(table: str) -> str:
    """Nombre de tabla seguro para SQL (evita inyección vía env)."""
    t = table.strip()
    if "." in t:
        schema, name = t.split(".", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", name
        ):
            raise ValueError(f"Nombre de tabla inválido: {table!r}")
        return f"{schema}.{name}"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t):
        raise ValueError(f"Nombre de tabla inválido: {table!r}")
    return t


def _coerce_row(row: dict) -> tuple:
    """
    Convierte una fila canónica (dict) en una tupla en el orden de columnas,
    aplicando conversiones mínimas:
      - scraped_at str ISO → datetime (psycopg2 ya lo parsea, pero lo hacemos
        explícito para evitar sorpresas con zonas horarias).
      - is_suspended → bool nativo.
      - odds_decimal → float o None.
    """
    values: list = []
    for col in _INSERT_COLUMNS:
        v = row.get(col)
        if col == "scraped_at" and isinstance(v, str) and v:
            try:
                v = datetime.fromisoformat(v)
            except ValueError:
                pass
        elif col == "is_suspended":
            v = bool(v) if v is not None else False
        elif col == "odds_decimal":
            if v is None or v == "":
                v = None
            else:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    v = None
        elif v is None:
            v = ""
        values.append(v)
    return tuple(values)


def save_supabase(
    rows: Iterable[dict],
    *,
    table: str = _TABLE,
    batch_size: int = 500,
) -> int:
    """
    Inserta las filas canónicas en la tabla de Supabase (append-only).

    Devuelve el número de filas insertadas. Si no hay DSN configurado o
    psycopg2 no está instalado, imprime aviso y devuelve 0 sin lanzar.
    """
    rows_list = list(rows)
    if not rows_list:
        print("[supabase] No hay filas para enviar.")
        return 0

    dsn = _get_dsn()
    if not dsn:
        print("[supabase] SUPABASE_DB_URL no configurado. Saltando subida.")
        return 0

    try:
        import psycopg2
        from psycopg2.extras import execute_values
    except ImportError:
        print("[supabase] psycopg2 no instalado. pip install psycopg2-binary")
        return 0

    try:
        qtable = _qualified_table(table)
    except ValueError as e:
        print(f"[supabase] {e}")
        return 0

    cols_sql = ", ".join(_INSERT_COLUMNS)
    template = "(" + ", ".join(["%s"] * len(_INSERT_COLUMNS)) + ")"
    sql = f"INSERT INTO {qtable} ({cols_sql}) VALUES %s"

    conn = None
    inserted = 0
    try:
        conn = psycopg2.connect(dsn, connect_timeout=10)
        conn.autocommit = False
        with conn.cursor() as cur:
            for start in range(0, len(rows_list), batch_size):
                batch = rows_list[start : start + batch_size]
                tuples = [_coerce_row(r) for r in batch]
                execute_values(cur, sql, tuples, template=template, page_size=batch_size)
                inserted += len(tuples)
        conn.commit()
        run_id = rows_list[0].get("scrape_run_id", "?")
        print(
            f"[supabase] {inserted} cuotas insertadas en {table} (run {run_id})"
        )
        return inserted
    except Exception as e:
        if conn is not None:
            conn.rollback()
        print(f"[supabase] Error subiendo datos: {e}")
        return 0
    finally:
        if conn is not None:
            conn.close()
