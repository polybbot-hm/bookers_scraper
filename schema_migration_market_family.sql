-- Ejecutar en Supabase SQL Editor si la tabla odds_history ya existía sin market_family.

alter table public.odds_history
  add column if not exists market_family text not null default '';

create index if not exists idx_odds_history_market_family
  on public.odds_history (market_family);
