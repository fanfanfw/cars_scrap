# cars_scrap

Internal vehicle-data scraping services for Malaysia, Indonesia, and Japan. Repository provides listing discovery, detail extraction, PostgreSQL persistence, price tracking, catalog synchronization, model-based workers, and simple HTTP wrappers.

## Supported sources

| Source | Listing CLI | Detail storage | Tracking | HTTP service | Notes |
|---|---:|---:|---:|---:|---|
| Mudah.my | Yes | Yes | Yes | `5201` | Model-based worker available |
| Carlist.my | Yes | Yes | Yes | `5202` | Cloudflare retries and circuit breaker |
| Mobil123 | Yes | Yes | Yes | `5203` | Cloudflare retries and circuit breaker |
| Carmudi | Yes | Yes | Yes | `5204` | Cloudflare retries and circuit breaker |
| OLX Indonesia | Yes | Yes | No | `5205` | Browser-based detail extraction |
| Carsome Indonesia | Yes | Yes | No | `5206` | All-pages mode and detail API |
| Goonet Exchange | Yes | Yes | Yes | `5207` | Maker/model/grade master data |
| Goonet Catalog | Catalog sync | Specification storage | Checkpoint/resume | No | `requests` and BeautifulSoup |

## How it works

```text
CLI, HTTP request, or scheduled job
                |
                v
        source-specific service
        - discover listing URLs
        - extract listing/detail data
        - retry anti-bot/network failures
                |
                v
             PostgreSQL
        - source listing tables
        - source detail tables
        - price-history tables
        - master/catalog/checkpoint tables
```

Normal listing mode returns discovered URLs. `--details` opens each detail page and upserts normalized data. Tracking mode loads stale active records from PostgreSQL and scrapes their details again. Database triggers record price changes.

Goonet Catalog is separate from Goonet used-car listings. It synchronizes catalog makes, models, specification links, and specification details using direct HTTP requests. Mudah Model Worker atomically claims make/model work through a PostgreSQL lease, then reuses the Mudah detail scraper.

## Requirements

- Python `>=3.10`
- [`uv`](https://docs.astral.sh/uv/)
- PostgreSQL server
- PostgreSQL `psql` client on `PATH`
- Browser/runtime dependencies required by Botasaurus
- X11 display or Xvfb for browser scrapers
- Network access to target websites
- Optional proxy access

Optional operational tools: `cron`, `flock`, `lsof`, and `systemd`.

## Installation

```bash
git clone <internal-repository-url>
cd cars_scrap
cp .env.example .env
uv sync
```

Optional virtual environment activation:

```bash
source .venv/bin/activate
```

Configure `.env` before running migrations or scrapers. Most entrypoints load `.env` with override enabled, so repository `.env` values can replace exported process variables.

## Database setup

Create PostgreSQL database and user through internal infrastructure, then configure at least:

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=cars_scrap
DB_USER=
DB_PASSWORD=
DB_BATCH_SIZE=100
```

Inspect migration order without changing database:

```bash
uv run migrate-sql --dry-run
```

Apply every SQL file under `sql/`:

```bash
uv run migrate-sql
```

Migration notes:

- `psql` must exist on `PATH`.
- Migration requires `DB_NAME` and `DB_USER`; source-specific database names do not replace `DB_NAME` here.
- SQL files run sequentially as separate `psql` processes. Failure does not globally roll back files already applied.
- New `sql/*.sql` files are discovered automatically.
- `scripts/migrate_sql.sh` uses system `python3`; prefer `uv run migrate-sql` unless system Python has project dependencies.

Database currently includes seven main listing tables, seven detail tables, seven price-history tables, Goonet maker/model/grade tables, Goonet catalog/run tables, and Mudah make/model worker tables.

## Configuration

### Listing URLs

```env
MUDAHMY_CARS_LISTING_URL=
CARLIST_CARS_LISTING_URL=
MOBIL123_CARS_LISTING_URL=
CARMUDI_CARS_LISTING_URL=
CARSOMEID_CARS_LISTING_URL=
OLX_CARS_LISTING_URL=
GOONET_CARS_LISTING_URL=
```

URLs can usually be overridden with `--start-url` or HTTP `start_url`. Mudah and Carlist require either configured URL or explicit `--start-url`.

### Source-specific database names

Scrapers support these fallback names where configured:

```env
DB_NAME_MUDAH_CARS=
DB_NAME_CARLIST_CARS=
DB_NAME_MOBIL123_CARS=
DB_NAME_CARMUDI_CARS=
DB_NAME_CARSOMEID_CARS=
DB_NAME_OLX_CARS=
DB_NAME_GOONET_CARS=
```

### Botasaurus

Important variables:

```env
BOTASAURUS_PROFILE=
BOTASAURUS_TINY_PROFILE=true
BOTASAURUS_BYPASS_CLOUDFLARE=true
BOTASAURUS_HUMAN_MODE=true
BOTASAURUS_CLOSE_ON_CRASH=true
BOTASAURUS_PROMPT_ON_CLOUDFLARE=false
BOTASAURUS_SAVE_HTML_ON_EMPTY=false
BOTASAURUS_ENABLE_XVFB_VIRTUAL_DISPLAY=false
BOTASAURUS_LISTING_WAIT_SECONDS=8
BOTASAURUS_DETAIL_WAIT_SECONDS=12
BOTASAURUS_POLL_INTERVAL_SECONDS=0.25
```

Browser jobs use `headless=False`. On displayless Linux servers, enable:

```env
BOTASAURUS_ENABLE_XVFB_VIRTUAL_DISPLAY=true
```

Browser profiles and proxy rotation state are stored under `profiles/`. Concurrent jobs must use different profile names.

### Proxy

Supported variables:

```env
PROXY_ENABLED=false
PROXY_SERVER=
PROXY_SERVERS=
PROXY_SERVER_ID=
PROXY_SERVERS_ID=
PROXY_SERVER_MY=
PROXY_SERVERS_MY=
PROXY_SERVER_JP=
PROXY_SERVERS_JP=
PROXY_SCHEME=http
PROXY_SCHEME_ID=http
PROXY_SCHEME_MY=http
PROXY_SCHEME_JP=http
PROXY_POOL_MUDAHMY=MY
PROXY_POOL_CARLIST=MY
PROXY_POOL_MOBIL123=ID
PROXY_POOL_CARMUDI=ID
PROXY_POOL_CARSOMEID=ID
PROXY_POOL_OLX=ID
PROXY_POOL_GOONET=JP
PROXY_CHECK_IP=false
PROXY_CHECK_URL=
```

Accepted proxy formats:

```text
scheme://user:password@host:port
host:port
host:port:user:password
```

Comma-separated pools rotate round-robin. Default pools are MY for Mudah and Carlist, JP for Goonet, and ID for other services. Pool-specific settings fall back to global proxy settings.

### HTTP wrappers

```env
HOST=0.0.0.0
PORT=
FLASK_DEBUG=false
LOG_LEVEL=INFO
```

Each service has its own default port. Setting global `PORT` overrides it for launched process.

Retry, Cloudflare, timing, and circuit-breaker controls are source-prefixed, including `CARLIST_*`, `MOBIL123_*`, `CARMUDI_*`, `MUDAHMY_*`, `CARSOMEID_*`, `OLX_*`, `GOONET_*`, and `GOONET_CATALOG_*`. See `.env.example` and source defaults before tuning production jobs.

## CLI entrypoints

```text
scrape-mudahmy
scrape-mudahmy-models
scrape-carlist
scrape-mobil123
scrape-carmudi
scrape-carsomeid
scrape-olx
scrape-goonet
scrape-goonet-catalog
migrate-sql
serve-mudahmy
serve-carlist
serve-mobil123
serve-carmudi
serve-carsomeid
serve-olx
serve-goonet
```

Use `uv run <command> --help` for current command-specific options.

## Listing and detail scraping

Discover listing links only:

```bash
uv run scrape-mudahmy --max-pages 1
uv run scrape-carlist --max-pages 1
uv run scrape-mobil123 --max-pages 1
uv run scrape-carmudi --max-pages 1
uv run scrape-carsomeid --max-pages 1
uv run scrape-olx --max-pages 1
uv run scrape-goonet --max-pages 1
```

Fetch details and write to PostgreSQL:

```bash
uv run scrape-mudahmy --max-pages 1 --details
uv run scrape-carlist --max-pages 1 --details
uv run scrape-mobil123 --max-pages 1 --details
uv run scrape-carmudi --max-pages 1 --details
uv run scrape-carsomeid --max-pages 1 --details
uv run scrape-olx --max-pages 1 --details
uv run scrape-goonet --max-pages 1 --details
```

Override listing URL and isolate browser profile:

```bash
uv run scrape-carlist \
  --start-url "https://www.carlist.my/cars-for-sale/malaysia" \
  --profile carlist_manual \
  --max-pages 1 \
  --details
```

Carsome can traverse all pages up to its configured safety cap:

```bash
uv run scrape-carsomeid --all-pages --details
```

Debug one detail URL:

```bash
uv run scrape-mudahmy --debug-detail-url "<detail-url>"
uv run scrape-carlist --debug-detail-url "<detail-url>"
uv run scrape-mobil123 --debug-detail-url "<detail-url>"
uv run scrape-carmudi --debug-detail-url "<detail-url>"
uv run scrape-carsomeid --debug-detail-url "<detail-url>"
uv run scrape-olx --debug-detail-url "<detail-url>"
uv run scrape-goonet --debug-detail-url "<detail-url>"
```

## Tracking stale listings

Tracking is available for Mudah, Carlist, Mobil123, Carmudi, and Goonet. It selects active records, prioritizing oldest `last_scraped_at`, then oldest advertisement date.

```bash
uv run scrape-mudahmy --track-older-than 1w
uv run scrape-carlist --track-older-than 1w
uv run scrape-mobil123 --track-older-than 1w
uv run scrape-carmudi --track-older-than 1w
uv run scrape-goonet --track-older-than 1w
```

Preview or count without scraping:

```bash
uv run scrape-carlist --track-older-than 3m --track-dry-run
uv run scrape-carlist --track-older-than 3m --track-count
```

Limit work and exclude recently scraped records:

```bash
uv run scrape-carlist \
  --track-older-than 3m \
  --track-last-scraped-older-than 1d \
  --track-limit 15
```

Duration units: `d` days, `w` weeks, `m` months, `y` years. Tracking cannot be combined with listing/detail/debug modes.

## Mudah model worker

Initial setup:

```bash
uv run migrate-sql
uv run scrape-mudahmy-models --sync-master-data
uv run scrape-mudahmy-models --bootstrap-existing
```

Inspect queue status:

```bash
uv run scrape-mudahmy-models --status
uv run scrape-mudahmy-models --status --status-filter pending --status-limit 100 --json
```

Claim and process one model:

```bash
BOTASAURUS_PROFILE=mudahmy_models_worker_1 \
uv run scrape-mudahmy-models \
  --claim-model \
  --worker-id worker-1 \
  --lease-minutes 360 \
  --max-pages 0 \
  --page-retries 3 \
  --details
```

`--max-pages 0` processes all available pages. Multiple workers require unique `--worker-id` and `BOTASAURUS_PROFILE` values. PostgreSQL lease claims prevent workers from processing same model concurrently.

## Goonet listing and master data

```bash
uv run scrape-goonet --sync-master-data
uv run scrape-goonet --sync-missing-master-grades
uv run scrape-goonet --listing --maker TOYOTA --model ALLION --max-pages 1
uv run scrape-goonet --listing-batch --batch-limit 5 --listing-older-than 60d
uv run scrape-goonet --listing-batch --batch-limit 5 --details
```

## Goonet catalog

Parser dry-runs do not write to database:

```bash
uv run scrape-goonet-catalog --sync-makes --dry-run --max-makes 5
uv run scrape-goonet-catalog --make-url "<catalog-make-url>" --dry-run --max-models 5
uv run scrape-goonet-catalog --model-url "<catalog-model-url>" --dry-run --max-specs 5
uv run scrape-goonet-catalog --detail-url "<catalog-detail-url>" --dry-run
```

Synchronize catalog:

```bash
uv run scrape-goonet-catalog --sync-all
uv run scrape-goonet-catalog --resume
uv run scrape-goonet-catalog --resume-run-id <run-id>
uv run scrape-goonet-catalog --sync-spec-details
```

Current limitation: standalone `--sync-models`, standalone `--sync-spec-links`, and non-dry-run URL modes return `not_implemented`.

## HTTP services

Start one service:

```bash
uv run serve-mudahmy
uv run serve-carlist
uv run serve-mobil123
uv run serve-carmudi
uv run serve-olx
uv run serve-carsomeid
uv run serve-goonet
```

| Service | Port | Source path segment |
|---|---:|---|
| Mudah | 5201 | `mudahmy` |
| Carlist | 5202 | `carlist` |
| Mobil123 | 5203 | `mobil123` |
| Carmudi | 5204 | `carmudi` |
| OLX | 5205 | `olx` |
| Carsome ID | 5206 | `carsomeid` |
| Goonet | 5207 | `goonet` |

Common endpoints:

```text
GET  /health
POST /scrape/<source>-cars
POST /scrape/<source>-cars-details
```

Example:

```bash
curl http://localhost:5202/health

curl -X POST http://localhost:5202/scrape/carlist-cars \
  -H "Content-Type: application/json" \
  -d '{"max_pages": 1}'

curl -X POST http://localhost:5202/scrape/carlist-cars-details \
  -H "Content-Type: application/json" \
  -d '{"max_pages": 1, "links": ["<detail-url>"]}'
```

Carsome requests additionally accept `"all_pages": true`.

These wrappers use Flask's built-in development server and execute scraping synchronously. They provide no authentication, job queue, rate limiting, production WSGI server, or request timeout. Keep them on trusted internal networks; do not expose them directly to public internet.

## Output behavior

Listing-only CLI output normally contains full discovered links:

```json
{
  "count": 1,
  "links": ["<detail-url>"]
}
```

Detail output varies slightly by source and can include `scraped`, `upserted`, `skipped`, `errors`, `sold_marked`, `skip_reasons`, and `stored_to_db` fields.

HTTP listing endpoints return capped `sample` data rather than every URL. Detail endpoints also return capped samples. Treat per-record errors and database counts as authoritative; `stored_to_db` is not a transaction guarantee across every source.

## Operations

Use one browser profile and one lock per concurrent job:

```cron
*/15 * * * * flock -n /tmp/cars_scrap_carlist.lock bash -c 'cd /path/to/cars_scrap && uv run scrape-carlist --details --profile carlist_scrape' >> /path/to/cars_scrap/logs/cron_carlist.log 2>&1
*/5 * * * * flock -n /tmp/cars_scrap_carlist_track.lock bash -c 'cd /path/to/cars_scrap && DB_BATCH_SIZE=1 uv run scrape-carlist --track-older-than 3m --track-last-scraped-older-than 1d --track-limit 15 --profile carlist_track' >> /path/to/cars_scrap/logs/cron_carlist_track.log 2>&1
```

Inspect profile cleanup first:

```bash
./scripts/cleanup_carlist_profiles.sh --prefix carlist_scrape --dry-run
```

Delete matching inactive profiles:

```bash
./scripts/cleanup_carlist_profiles.sh --prefix carlist_scrape
```

Cleanup recursively deletes matching profile directories. Active-profile detection requires `lsof`; install it before relying on automatic skip behavior.

Monitor `logs/`, `error_logs/`, generated debug artifacts, database growth, skipped/error counts, sold detections, and proxy health. Respect target-site policies, traffic limits, and internal legal/compliance requirements.

## Checks

Current repository-owned parser check:

```bash
uv run python scripts/check_goonet_catalog_parser.py
```

Expected output:

```text
goonet catalog parser checks passed
```

No repository-configured lint command, typecheck command, pytest suite, coverage command, or CI workflow currently exists.

## Troubleshooting

### Browser fails on server

Set `BOTASAURUS_ENABLE_XVFB_VIRTUAL_DISPLAY=true`, verify Xvfb/browser runtime dependencies, and use unique profile names.

### Empty listing or detail result

Check listing URL, source HTML changes, proxy health, Botasaurus wait settings, and files under `logs/`, `error_logs/`, or generated debug directories.

### Cloudflare or anti-bot challenge

Use headful/default browser mode, a stable profile, conservative request frequency, source retry controls, and approved proxy pool.

### Migration fails

Verify `psql`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, and database privileges. Run `uv run migrate-sql --dry-run` to inspect file order.

### `uv run` fails

Verify Python `>=3.10`, rerun `uv sync`, and confirm project virtual environment is valid.

## Repository layout

```text
cars_scrap_common/                     Shared browser, proxy, logging, duration, and migration utilities
scrap_mudahmy_cars_botasaurus/        Mudah listing/detail scraper and Flask service
scrap_mudahmy_model_worker/            Lease-based Mudah make/model worker
scrap_carlist_cars_botasaurus/        Carlist scraper and Flask service
scrap_mobil123_cars_botasaurus/       Mobil123 scraper and Flask service
scrap_carmudi_cars_botasaurus/        Carmudi scraper and Flask service
scrap_carsomeid_cars_botasaurus/      Carsome ID scraper and Flask service
scrap_olx_cars_botasaurus/            OLX scraper and Flask service
scrap_goonet_cars_botasaurus/         Goonet listing scraper, master data, and Flask service
scrap_goonet_catalog_botasaurus/      Goonet catalog/specification synchronization
sql/                                   PostgreSQL schemas, indexes, and price-history triggers
scripts/                               Migration wrapper, parser check, and profile cleanup
profiles/                              Runtime browser profiles and proxy state
logs/                                  Runtime logs
error_logs/                            Error artifacts
output/                                Temporary/runtime output
```
