# scrapers_bookers

Scrapers de cuotas de LaLiga para **Retabet**, **Casino Gran Madrid (Altenar)**,
**Kirolbet** y **22bet**. Todos producen filas con el mismo **esquema canónico
plano** (`bookers/odds_schema.py`) y se persisten como historial append-only:

- **Supabase (Postgres)** — fuente principal, consultable vía SQL.
- **CSV / JSON** en `data/` — sólo en local (desactivado en Railway con `DISABLE_FILE_OUTPUT=1`).
- **MongoDB** — opcional (opt-in con `MONGO_URI`).

Todos los scrapers comparten la misma estructura:

1. Listan los eventos de LaLiga en su fuente.
2. Para cada evento, extraen todos los mercados y sus odds.
3. Clasifican cada mercado en una `market_family` (`fouls_ou`, `throw_ins_ou`,
   `corners_ou`, etc.) y **descartan** las familias de mercado principal que
   no aportan edge (ver `EXCLUDED_MARKET_FAMILIES` en `bookers/market_labels.py`:
   `match_1x2`, `goals_ou`, `handicap_eu`, `handicap_1x2`, `btts`,
   `double_chance`, `draw_no_bet`, `first_goal`, `goals_team`).
4. Normalizan las filas al esquema canónico y las persisten en Supabase + CSV/JSON + Mongo.

## Estructura

```
scrapers_bookers/
├── bookers/
│   ├── __init__.py
│   ├── odds_schema.py       # Esquema canónico + normalización de equipos
│   ├── market_labels.py     # market_family + filtros + nombres legibles
│   ├── paths.py             # Rutas bajo data/
│   ├── persistence.py       # save_csv / save_json / save_mongo comunes
│   └── supabase_store.py    # save_supabase (psycopg2 + execute_values)
├── scripts/
│   ├── retabet.py           # Playwright + stealth (HTML dinámico con pestañas)
│   ├── grancasino.py        # API Altenar (JSON)
│   ├── kirolbet.py          # Playwright (DOM con marketGroup)
│   ├── twentytwobet.py      # API LineFeed (JSON; subgames por partido)
│   └── run_all.py           # Entry point del cron (lanza los 4)
├── data/                    # CSV/JSON locales (gitignored)
├── schema.sql               # DDL de la tabla odds_history en Supabase
├── schema_migration_market_family.sql  # Migración si ya tenías la tabla sin market_family
├── requirements.txt
├── Dockerfile               # Imagen Playwright oficial
├── railway.json             # Build Dockerfile + cron cada 3h
├── .env.example             # Plantilla (copia a .env)
└── .gitignore
```

## 1. Setup local

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env             # rellena SUPABASE_DB_URL
```

Ejecutar uno solo:

```bash
python scripts/retabet.py
python scripts/grancasino.py
python scripts/kirolbet.py
python scripts/twentytwobet.py
python scripts/twentytwobet.py --max-matches 3 --verbose   # smoke test
```

Ejecutar los 4 en el mismo ciclo (mismo `scrape_run_id`):

```bash
python scripts/run_all.py
```

Para ejecutar un subconjunto:

```bash
SCRAPERS=retabet,kirolbet python scripts/run_all.py
```

## 2. Supabase

### 2.1 Crear la tabla

Entra en el SQL Editor de Supabase y ejecuta el contenido de
[`schema.sql`](./schema.sql). Crea la tabla `odds_history` y los
índices para consultas típicas (evolución de una cuota, comparativa
entre casas, etc.).

Si ya tenías la tabla de una versión anterior (sin la columna
`market_family`), ejecuta en su lugar
[`schema_migration_market_family.sql`](./schema_migration_market_family.sql).

### 2.2 Conexión

En `Project Settings → Database → Connection string → URI` hay varias
opciones. Usa el **Transaction pooler** (puerto 6543):

```
postgresql://postgres.xxxx:TU_PASSWORD@aws-0-eu-central-1.pooler.supabase.com:6543/postgres
```

Por qué el pooler: los jobs de Railway son efímeros (abren conexión,
hacen insert, cierran). El pooler evita que te quedes sin slots en el
Postgres directo.

Pégala en `SUPABASE_DB_URL` (local en `.env`, en Railway como variable
de entorno del servicio).

### 2.3 Consultas útiles

```sql
-- Faltas: comparativa de cuotas Over/Under entre todas las casas
select bookmaker, market_name, selection_name, odds_decimal, scraped_at
from odds_history
where match_key = 'barcelona_vs_real-madrid'
  and market_family in ('fouls_ou','fouls_team_ou')
order by scraped_at desc, bookmaker;

-- Saques de banda del Barça-Madrid
select bookmaker, market_name, selection_name, odds_decimal, scraped_at
from odds_history
where match_key = 'barcelona_vs_real-madrid'
  and market_family in ('throw_ins','throw_ins_ou')
order by scraped_at desc, bookmaker;
```

## 3. Despliegue en Railway

### 3.1 Crear el proyecto

1. `New Project → Deploy from GitHub repo` (o `railway up` con la CLI).
2. Railway detectará `railway.json` y construirá con el `Dockerfile`.

### 3.2 Variables de entorno

En el servicio, `Variables`:

| Variable              | Valor                                          | Notas                                         |
|-----------------------|------------------------------------------------|-----------------------------------------------|
| `SUPABASE_DB_URL`     | `postgresql://...pooler.supabase.com:6543/...` | Obligatoria                                   |
| `DISABLE_FILE_OUTPUT` | `1`                                            | Ya viene por defecto en el Dockerfile         |
| `HEADLESS`            | `1`                                            | Ya viene por defecto en el Dockerfile         |
| `MONGO_URI`           | *(vacío)*                                      | Si lo dejas vacío, no se intenta conectar     |
| `SCRAPERS`            | `retabet,grancasino,kirolbet,twentytwobet`     | Opcional (default: los 4)                     |
| `TZ`                  | `Europe/Madrid`                                | Sólo cosmético (logs); el cron es siempre UTC |

### 3.3 Cron

En `Settings → Cron Schedule` (ya viene definido en `railway.json`):

```
0 */3 * * *
```

Se ejecuta cada 3 horas en UTC.

Railway **no ejecuta** el siguiente run si el anterior sigue corriendo,
así que ajústalo según lo que tarde. El scraper de 22bet es el más
lento porque enumera todos los subgames por partido (≈ 1 HTTP por
subgame); el rate-limit por defecto (1.0–2.0s) protege contra 429.

## 4. MongoDB

MongoDB está **desactivado por defecto**. Se activa sólo cuando defines
`MONGO_URI`. Las filas se insertan (append-only) en
`sports_odds.odds_history` con el mismo esquema canónico que Supabase.

### ¿Puedo ejecutar en Railway y que lo guarde en mi MongoDB local?

**No directamente.** Un contenedor en Railway no puede alcanzar un
`mongodb://localhost:27017` de tu ordenador. Tres opciones en orden de
recomendación:

1. **MongoDB Atlas (gratis, M0)** — creas un cluster con una URI pública
   tipo `mongodb+srv://…mongodb.net/…`. La pones en `MONGO_URI` tanto en
   Railway como en tu `.env` local. Ambos escriben en el mismo sitio.
2. **Plugin de MongoDB en Railway** — añades el add-on y habilitas
   *Public Networking* para conectarte desde local con el mismo URI.
3. **Dump periódico** — job adicional que exporte Supabase → JSON y
   lo suba a un bucket.

### Migración futura

El esquema es idéntico entre Supabase y Mongo, así que si quisieras
migrar todo a Mongo más adelante la migración es trivial.

## 5. Probar la build Docker en local

```bash
docker build -t scrapers-bookers .
docker run --rm \
  -e SUPABASE_DB_URL=postgresql://... \
  -e HEADLESS=1 \
  -e DISABLE_FILE_OUTPUT=1 \
  scrapers-bookers
```
