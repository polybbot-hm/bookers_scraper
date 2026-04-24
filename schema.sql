-- ──────────────────────────────────────────────────────────────
-- Esquema de Supabase para historial de cuotas de apuestas
-- Ejecutar una sola vez en el SQL editor de Supabase
-- ──────────────────────────────────────────────────────────────

create table if not exists public.odds_history (
  id             bigserial primary key,
  scrape_run_id  text        not null,
  scraped_at     timestamptz not null,
  bookmaker      text        not null,
  competition    text        not null,
  match_key      text        not null,
  event_id       text        not null,
  event_name     text,
  home_team      text,
  away_team      text,
  match_time     text,
  event_url      text,
  market_id      text,
  market_name    text,
  market_type    text,
  market_sv      text,
  market_family  text        not null default '',
  selection_id   text,
  selection_name text,
  odds_decimal   numeric(12, 4),
  odds_raw       text,
  is_suspended   boolean     not null default false,
  inserted_at    timestamptz not null default now()
);

-- Índices para consultas típicas (evolución de una cuota, comparativa entre casas, etc.)
create index if not exists idx_odds_history_run
  on public.odds_history (scrape_run_id);

create index if not exists idx_odds_history_bookmaker_scraped_at
  on public.odds_history (bookmaker, scraped_at desc);

create index if not exists idx_odds_history_match_key_scraped_at
  on public.odds_history (match_key, scraped_at desc);

create index if not exists idx_odds_history_event_scraped_at
  on public.odds_history (bookmaker, event_id, scraped_at);

-- Índice clave para series temporales de una cuota concreta:
-- "evolución de la cuota X del partido Y en la casa Z"
create index if not exists idx_odds_history_selection_time
  on public.odds_history (bookmaker, event_id, selection_id, scraped_at);

create index if not exists idx_odds_history_market_family
  on public.odds_history (market_family);

-- ──────────────────────────────────────────────────────────────
-- Row Level Security (Supabase avisa si la tabla queda sin RLS)
-- ──────────────────────────────────────────────────────────────
-- Con RLS activado y sin políticas para `anon` / `authenticated`,
-- la API REST de Supabase no puede leer ni escribir esta tabla
-- (comportamiento seguro para datos que sólo tocan tus scrapers).
--
-- La conexión Postgres directa (SUPABASE_DB_URL con usuario postgres)
-- sigue pudiendo INSERT porque ese rol es propietario / bypass RLS
-- salvo que uses FORCE ROW LEVEL SECURITY (no lo hacemos aquí).
alter table public.odds_history enable row level security;
