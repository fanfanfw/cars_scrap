# cars_scrap

Repo scraping listing mobil untuk empat source:

- `mudah.my`
- `carlist.my`
- `mobil123.com`
- `carmudi.co.id`

Project ini memakai:

- Python `>=3.10`
- `uv` untuk environment dan dependency management
- Botasaurus untuk browser automation
- PostgreSQL untuk penyimpanan hasil scrape dan histori harga
- Flask untuk HTTP wrapper sederhana di atas scraper

README ini merangkum cara setup, struktur repo, mode scraping, schema database, sampai endpoint service yang tersedia.

## Ringkasan cara kerja

Secara umum ada dua mode utama:

1. Scrape halaman listing untuk mengumpulkan `listing_url`.
2. Scrape halaman detail untuk mengambil data lengkap lalu `upsert` ke PostgreSQL.

Semua source (`mudah.my`, `carlist.my`, `mobil123.com`, dan `carmudi.co.id`) punya pola yang sama:

- service class untuk logic scraping
- CLI entrypoint untuk eksekusi manual
- Flask app untuk expose HTTP endpoint
- tabel utama + tabel detail + tabel histori harga di Postgres

Tracking/re-scrape juga tersedia. Mode ini membaca data `active` dari database, lalu scrape ulang detail listing yang sudah cukup lama berdasarkan `information_ads_date` dan opsional `last_scraped_at`.

## Struktur repo

```text
.
├── cars_scrap_common/                    # shared utilities (env, logging, proxy, duration)
├── scrap_mudahmy_cars_botasaurus/       # scraper mudah.my
│   ├── app.py                           # HTTP service (Flask)
│   ├── database.py                      # koneksi Postgres
│   ├── mudahmy_service.py               # logic scraping utama
│   └── run_scraper.py                   # CLI entrypoint
├── scrap_carlist_cars_botasaurus/       # scraper carlist.my
│   ├── app.py
│   ├── database.py
│   ├── carlist_service.py
│   └── run_scraper.py
├── scrap_mobil123_cars_botasaurus/      # scraper mobil123.com
│   ├── app.py
│   ├── database.py
│   ├── mobil123_service.py
│   └── run_scraper.py
├── scrap_carmudi_cars_botasaurus/       # scraper carmudi.co.id
│   ├── app.py
│   ├── database.py
│   ├── carmudi_service.py
│   └── run_scraper.py
├── sql/                                 # schema tabel dan price history trigger
├── scripts/                             # script migrasi dari DB lama
├── profiles/                            # profile Botasaurus + state proxy rotation
├── logs/                                # log runtime
├── error_logs/                          # error artifacts
└── output/                              # output runtime / hasil scrape sementara
```

## Entry point yang tersedia

Script berikut didefinisikan di `pyproject.toml`:

- `uv run scrape-mudahmy`
- `uv run scrape-carlist`
- `uv run scrape-mobil123`
- `uv run scrape-carmudi`
- `uv run serve-mudahmy`
- `uv run serve-carlist`
- `uv run serve-mobil123`
- `uv run serve-carmudi`

## Setup lokal

### 1. Clone dan siapkan env

```bash
cp .env.example .env
```

Lalu isi minimal:

- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- opsional `MUDAHMY_CARS_LISTING_URL`
- opsional `CARLIST_CARS_LISTING_URL`
- opsional `MOBIL123_CARS_LISTING_URL`
- opsional `CARMUDI_CARS_LISTING_URL`

### 2. Install dependency

```bash
uv venv
uv sync
```

Kalau ingin menjalankan command lewat virtualenv aktif:

```bash
source .venv/bin/activate
```

## Setup PostgreSQL

Project ini menyimpan data ke 12 tabel:

- `cars_scrap_mudahmy`
- `cars_scrap_mudahmy_details`
- `price_history_scrap_mudahmy`
- `cars_scrap_carlistmy`
- `cars_scrap_carlistmy_details`
- `price_history_scrap_carlistmy`
- `cars_scrap_mobil123`
- `cars_scrap_mobil123_details`
- `price_history_scrap_mobil123`
- `cars_scrap_carmudi`
- `cars_scrap_carmudi_details`
- `price_history_scrap_carmudi`

File SQL yang tersedia:

- `sql/cars_scrap_mudahmy.sql`
- `sql/cars_scrap_mudahmy_details.sql`
- `sql/price_history_scrap_mudahmy.sql`
- `sql/cars_scrap_carlist.sql`
- `sql/cars_scrap_carlistmy_details.sql`
- `sql/price_history_scrap_carlist.sql`
- `sql/cars_scrap_mobil123.sql`
- `sql/cars_scrap_mobil123_details.sql`
- `sql/price_history_scrap_mobil123.sql`
- `sql/cars_scrap_carmudi.sql`
- `sql/cars_scrap_carmudi_details.sql`
- `sql/price_history_scrap_carmudi.sql`

Contoh bootstrap schema:

```bash
export PGPASSWORD="$DB_PASSWORD"

psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_mudahmy.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_mudahmy_details.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/price_history_scrap_mudahmy.sql

psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_carlist.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_carlistmy_details.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/price_history_scrap_carlist.sql

psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_mobil123.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_mobil123_details.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/price_history_scrap_mobil123.sql

psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_carmudi.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_carmudi_details.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/price_history_scrap_carmudi.sql
```

Catatan:

- file SQL bersifat idempotent karena memakai `CREATE TABLE IF NOT EXISTS` dan `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
- tabel `price_history_*` memakai trigger untuk mencatat perubahan `price`
- tabel `*_details` menyimpan field ekstra dari halaman detail

## Konfigurasi environment

Berikut variabel penting dari `.env.example`.

### Listing URL

- `MUDAHMY_CARS_LISTING_URL`: default URL listing mudah.my, bisa dioverride lewat `--start-url` atau body HTTP request
- `CARLIST_CARS_LISTING_URL`: default URL listing carlist.my, bisa dioverride lewat `--start-url` atau body HTTP request
- `MOBIL123_CARS_LISTING_URL`: default URL listing mobil123.com, bisa dioverride lewat `--start-url` atau body HTTP request
- `CARMUDI_CARS_LISTING_URL`: default URL listing carmudi.co.id, bisa dioverride lewat `--start-url` atau body HTTP request

### Database

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_BATCH_SIZE`: ukuran batch upsert ke Postgres

Catatan kompatibilitas:

- scraper mudah.my juga menerima fallback `DB_NAME_MUDAH_CARS`
- scraper carlist juga menerima fallback `DB_NAME_CARLIST_CARS`
- scraper mobil123.com juga menerima fallback `DB_NAME_MOBIL123_CARS`
- scraper carmudi.co.id juga menerima fallback `DB_NAME_CARMUDI_CARS`

### Logging dan Flask

- `LOG_LEVEL`
- `HOST` untuk Flask bind address
- `PORT` untuk Flask port
- `FLASK_DEBUG`

Default port:

- `serve-mudahmy` -> `5201`
- `serve-carlist` -> `5202`
- `serve-mobil123` -> `5203`
- `serve-carmudi` -> `5204`

### Botasaurus runtime

- `BOTASAURUS_PROFILE`
- `BOTASAURUS_TINY_PROFILE`
- `BOTASAURUS_BYPASS_CLOUDFLARE`
- `BOTASAURUS_HUMAN_MODE`
- `BOTASAURUS_CLOSE_ON_CRASH`
- `BOTASAURUS_PROMPT_ON_CLOUDFLARE`
- `BOTASAURUS_SAVE_HTML_ON_EMPTY`
- `BOTASAURUS_ENABLE_XVFB_VIRTUAL_DISPLAY`
- `BOTASAURUS_LISTING_WAIT_SECONDS`
- `BOTASAURUS_DETAIL_WAIT_SECONDS`
- `BOTASAURUS_POLL_INTERVAL_SECONDS`

Catatan operasional:

- default scraper berjalan `headful`, ini biasanya lebih stabil untuk halaman yang punya challenge/anti-bot
- untuk server tanpa display, set `BOTASAURUS_ENABLE_XVFB_VIRTUAL_DISPLAY=true`
- profile browser disimpan di bawah `profiles/`

### Proxy

- `PROXY_ENABLED`
- `PROXY_SERVER`
- `PROXY_SERVERS`
- `PROXY_SCHEME`
- `PROXY_USERNAME`
- `PROXY_PASSWORD`
- `PROXY_STATE_FILE`
- `PROXY_CHECK_IP`
- `PROXY_CHECK_URL`

Catatan:

- `PROXY_SERVERS` bisa berisi daftar proxy dipisah koma
- format yang didukung: URL penuh, `HOST:PORT`, atau `HOST:PORT:USER:PASS`
- jika lebih dari satu proxy diberikan, repo ini melakukan rotasi round-robin dan menyimpan state di `profiles/proxy_state.json`

### Cloudflare fallback khusus carlist, mobil123, dan carmudi

- `CARLIST_AUTO_ATTEMPT_CLOUDFLARE`
- `CARLIST_CLOUDFLARE_MAX_ATTEMPTS`
- `CARLIST_CLOUDFLARE_POST_SOLVE_WAIT_SECONDS`
- `MOBIL123_AUTO_ATTEMPT_CLOUDFLARE`
- `MOBIL123_CLOUDFLARE_MAX_ATTEMPTS`
- `MOBIL123_CLOUDFLARE_POST_SOLVE_WAIT_SECONDS`
- `CARMUDI_AUTO_ATTEMPT_CLOUDFLARE`
- `CARMUDI_CLOUDFLARE_MAX_ATTEMPTS`
- `CARMUDI_CLOUDFLARE_POST_SOLVE_WAIT_SECONDS`

### Retry dan proteksi tambahan khusus carlist, mobil123, dan carmudi

- `CARLIST_STICKY_BROWSER_RETRIES`: retry di browser session yang sama sebelum ganti proxy/profile
- `CARLIST_STICKY_BROWSER_RETRY_WAIT_SECONDS`
- `CARLIST_HTTP_400_RETRY_ENABLED`
- `CARLIST_HTTP_400_MAX_RETRIES`: retry dengan session/profile baru setelah sticky retry habis
- `CARLIST_RUN_MAX_ATTEMPTS`: retry satu batch detail dengan profile baru jika banyak URL gagal
- `CARLIST_RUN_RETRY_WAIT_SECONDS`
- `CARLIST_DETAIL_CIRCUIT_BREAKER_ENABLED`
- `CARLIST_DETAIL_CIRCUIT_BREAKER_MIN_SAMPLES`
- `CARLIST_DETAIL_CIRCUIT_BREAKER_ERROR_RATE`
- `CARLIST_DETAIL_CIRCUIT_BREAKER_CONSECUTIVE_ERRORS`
- `MOBIL123_STICKY_BROWSER_RETRIES`
- `MOBIL123_STICKY_BROWSER_RETRY_WAIT_SECONDS`
- `MOBIL123_HTTP_400_RETRY_ENABLED`
- `MOBIL123_HTTP_400_MAX_RETRIES`
- `MOBIL123_RUN_MAX_ATTEMPTS`
- `MOBIL123_RUN_RETRY_WAIT_SECONDS`
- `MOBIL123_DETAIL_CIRCUIT_BREAKER_ENABLED`
- `MOBIL123_DETAIL_CIRCUIT_BREAKER_MIN_SAMPLES`
- `MOBIL123_DETAIL_CIRCUIT_BREAKER_ERROR_RATE`
- `MOBIL123_DETAIL_CIRCUIT_BREAKER_CONSECUTIVE_ERRORS`
- `CARMUDI_STICKY_BROWSER_RETRIES`
- `CARMUDI_STICKY_BROWSER_RETRY_WAIT_SECONDS`
- `CARMUDI_HTTP_400_RETRY_ENABLED`
- `CARMUDI_HTTP_400_MAX_RETRIES`
- `CARMUDI_RUN_MAX_ATTEMPTS`
- `CARMUDI_RUN_RETRY_WAIT_SECONDS`
- `CARMUDI_DETAIL_CIRCUIT_BREAKER_ENABLED`
- `CARMUDI_DETAIL_CIRCUIT_BREAKER_MIN_SAMPLES`
- `CARMUDI_DETAIL_CIRCUIT_BREAKER_ERROR_RATE`
- `CARMUDI_DETAIL_CIRCUIT_BREAKER_CONSECUTIVE_ERRORS`

Catatan:

- default `CARLIST_RUN_MAX_ATTEMPTS`, `MOBIL123_RUN_MAX_ATTEMPTS`, dan `CARMUDI_RUN_MAX_ATTEMPTS` sekarang `2`
- saat halaman listing carlist, mobil123, atau carmudi kosong/terlihat seperti soft-block, scraper akan menyimpan artefak debug ke `botasaurus_test/output/*_debug/`

### Cleanup profile Botasaurus per job

Jika profile base seperti `carlist_scrape`, `carlist_track`, `mudahmy_scrape`, atau `mudahmy_track` terlalu lama dipakai, cookie bisa menumpuk dan memicu error seperti `400 Request Header Or Cookie Too Large`.

Script cleanup yang tersedia:

```bash
./scripts/cleanup_carlist_profiles.sh --prefix carlist_scrape --dry-run
./scripts/cleanup_carlist_profiles.sh --prefix carlist_scrape
```

Catatan:

- walau nama file script masih `cleanup_carlist_profiles.sh`, sekarang script ini bisa dipakai untuk prefix profile apa pun
- script akan menghapus semua direktori profile dengan prefix yang sama, misalnya `carlist_scrape*` atau `mudahmy_track*`
- jika profile sedang dipakai proses aktif, script akan skip kecuali dipanggil dengan `--force`
- fallback prefix tetap bisa diambil dari `PROFILE_CLEANUP_PREFIX`, `CARLIST_PROFILE_CLEANUP_PREFIX`, atau `BOTASAURUS_PROFILE`

Rekomendasi profile per job:

- `mudahmy_scrape`
- `mudahmy_track`
- `carlist_scrape`
- `carlist_track`
- `mobil123_scrape`
- `mobil123_track`
- `carmudi_scrape`
- `carmudi_track`

Contoh cron dengan profile terpisah per job:

```cron
*/15 * * * * flock -n /tmp/cars_scrap_mudahmy_scrape.lock bash -c 'cd /home/ifcon/ff/cars_scrap && uv run scrape-mudahmy --details --profile mudahmy_scrape' >> /home/ifcon/ff/cars_scrap/logs/cron_mudahmy.log 2>&1
*/5 * * * * flock -n /tmp/cars_scrap_mudahmy_track.lock bash -c 'cd /home/ifcon/ff/cars_scrap && DB_BATCH_SIZE=1 uv run scrape-mudahmy --track-older-than 2m --track-last-scraped-older-than 1d --track-limit 15 --profile mudahmy_track' >> /home/ifcon/ff/cars_scrap/logs/cron_mudahmy_track.log 2>&1
*/15 * * * * flock -n /tmp/cars_scrap_carlist_scrape.lock bash -c 'cd /home/ifcon/ff/cars_scrap && uv run scrape-carlist --details --profile carlist_scrape' >> /home/ifcon/ff/cars_scrap/logs/cron_carlist.log 2>&1
*/5 * * * * flock -n /tmp/cars_scrap_carlist_track.lock bash -c 'cd /home/ifcon/ff/cars_scrap && DB_BATCH_SIZE=1 uv run scrape-carlist --track-older-than 3m --track-last-scraped-older-than 1d --track-limit 15 --profile carlist_track' >> /home/ifcon/ff/cars_scrap/logs/cron_carlist_track.log 2>&1
```

Contoh cron cleanup per job tiap 1 menit:

```cron
* * * * * flock -n /tmp/cars_scrap_mudahmy_scrape.lock bash -c 'cd /home/ifcon/ff/cars_scrap && ./scripts/cleanup_carlist_profiles.sh --prefix mudahmy_scrape' >> /home/ifcon/ff/cars_scrap/logs/cron_mudahmy_scrape_cleanup.log 2>&1
* * * * * flock -n /tmp/cars_scrap_mudahmy_track.lock bash -c 'cd /home/ifcon/ff/cars_scrap && ./scripts/cleanup_carlist_profiles.sh --prefix mudahmy_track' >> /home/ifcon/ff/cars_scrap/logs/cron_mudahmy_track_cleanup.log 2>&1
* * * * * flock -n /tmp/cars_scrap_carlist_scrape.lock bash -c 'cd /home/ifcon/ff/cars_scrap && ./scripts/cleanup_carlist_profiles.sh --prefix carlist_scrape' >> /home/ifcon/ff/cars_scrap/logs/cron_carlist_scrape_cleanup.log 2>&1
* * * * * flock -n /tmp/cars_scrap_carlist_track.lock bash -c 'cd /home/ifcon/ff/cars_scrap && ./scripts/cleanup_carlist_profiles.sh --prefix carlist_track' >> /home/ifcon/ff/cars_scrap/logs/cron_carlist_track_cleanup.log 2>&1
```

## Menjalankan scraper via CLI

### A. Ambil listing links saja

Mode ini hanya mengembalikan daftar `listing_url`, belum menyimpan detail ke Postgres.

```bash
uv run scrape-mudahmy --max-pages 1
uv run scrape-carlist --max-pages 1
uv run scrape-mobil123 --max-pages 1
uv run scrape-carmudi --max-pages 1
```

Override halaman awal:

```bash
uv run scrape-mudahmy --start-url "https://www.mudah.my/malaysia/cars-for-sale"
uv run scrape-carlist --start-url "https://www.carlist.my/cars-for-sale/malaysia"
uv run scrape-mobil123 --start-url "https://www.mobil123.com/mobil-bekas-dijual/indonesia?sort=modification_date_search.desc"
uv run scrape-carmudi --start-url "https://www.carmudi.co.id/mobil-bekas-dijual/indonesia?sort=modification_date_search.desc"
```

### B. Ambil detail dan simpan ke Postgres

Mode ini:

- scrape halaman listing
- buka halaman detail per listing
- `upsert` ke tabel utama
- `upsert` data ekstra ke tabel detail
- menandai listing `sold` bila terdeteksi

```bash
uv run scrape-mudahmy --max-pages 1 --details
uv run scrape-carlist --max-pages 1 --details
uv run scrape-mobil123 --max-pages 1 --details
uv run scrape-carmudi --max-pages 1 --details
```

Output CLI berupa JSON ringkasan seperti:

- `count`
- `scraped`
- `upserted`
- `skipped`
- `errors`
- `sold_detected`
- `skip_reasons`
- `stored_to_db`

Catatan:

- pada mode detail, scraper memakai validasi tambahan agar data yang masuk lebih konsisten
- untuk `mudah.my`, opsi `--print-links` tersedia bila ingin menampilkan daftar link yang dikumpulkan

### C. Re-scrape data lama dari DB (tracking mode)

Tracking mode membaca listing `active` dari DB, lalu scrape ulang detail listing berdasarkan cutoff umur iklan.
Urutan proses memprioritaskan `last_scraped_at` yang paling lama, lalu `information_ads_date` yang paling lama sebagai tie-breaker.

Contoh dasar:

```bash
uv run scrape-mudahmy --track-older-than 1w
uv run scrape-carlist --track-older-than 1w
uv run scrape-mobil123 --track-older-than 1w
uv run scrape-carmudi --track-older-than 1w
```

Contoh dengan filter `last_scraped_at` supaya listing yang baru diproses tidak terkena lagi:

```bash
uv run scrape-mudahmy --track-older-than 3m --track-last-scraped-older-than 90d --track-limit 200
uv run scrape-carlist --track-older-than 3m --track-last-scraped-older-than 90d --track-limit 200
uv run scrape-mobil123 --track-older-than 3m --track-last-scraped-older-than 90d --track-limit 200
uv run scrape-carmudi --track-older-than 3m --track-last-scraped-older-than 90d --track-limit 200
```

Mode hitung saja:

```bash
uv run scrape-mudahmy --track-older-than 1w --track-count
uv run scrape-carlist --track-older-than 1w --track-count
uv run scrape-mobil123 --track-older-than 1w --track-count
uv run scrape-carmudi --track-older-than 1w --track-count
```

Mode preview tanpa scrape:

```bash
uv run scrape-mudahmy --track-older-than 1w --track-dry-run
uv run scrape-carlist --track-older-than 1w --track-dry-run
uv run scrape-mobil123 --track-older-than 1w --track-dry-run
uv run scrape-carmudi --track-older-than 1w --track-dry-run
```

Format durasi yang didukung:

- `d` = hari, contoh `7d`
- `w` = minggu, contoh `1w`
- `m` = bulan, contoh `3m`
- `y` = tahun, contoh `1y`

Batasan tracking mode:

- tidak bisa digabung dengan `--details`
- tidak bisa digabung dengan `--start-url`
- tidak bisa digabung dengan `--debug-detail-url`
- tracking selalu bekerja dari data yang sudah ada di DB

### D. Debug satu detail URL

Berguna untuk validasi parser satu halaman detail. Command ini:

- scrape satu URL
- coba simpan ke DB
- cetak hasil scrape dan row DB sebagai JSON

```bash
uv run scrape-mudahmy --debug-detail-url "https://www.mudah.my/view?ad_id=..."
uv run scrape-carlist --debug-detail-url "https://www.carlist.my/used-cars/..."
uv run scrape-mobil123 --debug-detail-url "https://www.mobil123.com/..."
uv run scrape-carmudi --debug-detail-url "https://www.carmudi.co.id/..."
```

## Menjalankan HTTP service

Service Flask tersedia untuk masing-masing source.

### Mudah.my service

Jalankan:

```bash
uv run serve-mudahmy
```

Default:

- host `0.0.0.0`
- port `5201`

Endpoint:

- `GET /health`
- `POST /scrape/mudahmy-cars`
- `POST /scrape/mudahmy-cars-details`

Contoh request:

```bash
curl http://localhost:5201/health

curl -X POST http://localhost:5201/scrape/mudahmy-cars \
  -H "Content-Type: application/json" \
  -d '{"max_pages": 1}'

curl -X POST http://localhost:5201/scrape/mudahmy-cars-details \
  -H "Content-Type: application/json" \
  -d '{"max_pages": 1}'
```

Field body yang didukung:

- `start_url`
- `max_pages`
- `links` untuk endpoint detail jika ingin bypass scrape listing

### Carlist service

Jalankan:

```bash
uv run serve-carlist
```

Default:

- host `0.0.0.0`
- port `5202`

Endpoint:

- `GET /health`
- `POST /scrape/carlist-cars`
- `POST /scrape/carlist-cars-details`

Contoh request:

```bash
curl http://localhost:5202/health

curl -X POST http://localhost:5202/scrape/carlist-cars \
  -H "Content-Type: application/json" \
  -d '{"max_pages": 1}'

curl -X POST http://localhost:5202/scrape/carlist-cars-details \
  -H "Content-Type: application/json" \
  -d '{"max_pages": 1}'
```

### Mobil123 service

Jalankan:

```bash
uv run serve-mobil123
```

Default:

- host `0.0.0.0`
- port `5203`

Endpoint:

- `GET /health`
- `POST /scrape/mobil123-cars`
- `POST /scrape/mobil123-cars-details`

Contoh request:

```bash
curl http://localhost:5203/health

curl -X POST http://localhost:5203/scrape/mobil123-cars \
  -H "Content-Type: application/json" \
  -d '{"max_pages": 1}'

curl -X POST http://localhost:5203/scrape/mobil123-cars-details \
  -H "Content-Type: application/json" \
  -d '{"max_pages": 1}'
```

### Carmudi service

Jalankan:

```bash
uv run serve-carmudi
```

Default:

- host `0.0.0.0`
- port `5204`

Endpoint:

- `GET /health`
- `POST /scrape/carmudi-cars`
- `POST /scrape/carmudi-cars-details`

Contoh request:

```bash
curl http://localhost:5204/health

curl -X POST http://localhost:5204/scrape/carmudi-cars \
  -H "Content-Type: application/json" \
  -d '{"max_pages": 1}'

curl -X POST http://localhost:5204/scrape/carmudi-cars-details \
  -H "Content-Type: application/json" \
  -d '{"max_pages": 1}'
```

## Migrasi data dari DB lama

Repo ini menyediakan dua script migrasi:

- `scripts/migrate_mudahmy_db_scrap_new_to_db_cars_scrap.sh`
- `scripts/migrate_carlistmy_db_scrap_new_to_db_cars_scrap.sh`

Tujuan script:

- migrasi dari DB lama (default `db_scrap_new`)
- merge data tabel utama + `_archive` ke tabel utama di DB baru
- migrasi data `price_history`
- abort bila tabel target sudah berisi data, agar tidak duplikat

Contoh penggunaan:

```bash
./scripts/migrate_mudahmy_db_scrap_new_to_db_cars_scrap.sh
./scripts/migrate_carlistmy_db_scrap_new_to_db_cars_scrap.sh
```

Variabel yang relevan:

- `OLD_DB`
- `NEW_DB`
- `OUT_DIR`
- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`

Catatan:

- `.env` wajib ada karena script melakukan `source .env`
- target table harus kosong sebelum migrasi dijalankan

## Catatan perilaku data

- kolom `listing_id` bersifat unik dan dipakai untuk `upsert`
- status listing utama umumnya `active` atau `sold`
- `last_scraped_at` dipakai untuk mendukung tracking/re-scrape
- perubahan harga dicatat ke tabel `price_history_*` lewat trigger database
- detail page disimpan terpisah agar struktur field fleksibel

## Masalah umum

### Challenge / anti-bot

Kalau scraping macet karena challenge browser:

- jalankan mode non-headless/default dulu
- pakai `BOTASAURUS_ENABLE_XVFB_VIRTUAL_DISPLAY=true` bila server tidak punya display
- pertimbangkan pakai proxy

### Hasil scrape kosong

Cek:

- `MUDAHMY_CARS_LISTING_URL`, `CARLIST_CARS_LISTING_URL`, atau `MOBIL123_CARS_LISTING_URL`
- timeout Botasaurus (`BOTASAURUS_LISTING_WAIT_SECONDS`, `BOTASAURUS_DETAIL_WAIT_SECONDS`)
- `logs/` dan `error_logs/`

### `uv run` bermasalah

Pastikan:

- `uv sync` sudah sukses
- Python version sesuai (`>=3.10`)
- virtual environment project valid

## Pengembangan cepat

Command yang paling sering dipakai:

```bash
uv sync
uv run scrape-mudahmy --max-pages 1 --details
uv run scrape-carlist --max-pages 1 --details
uv run scrape-mobil123 --max-pages 1 --details
uv run serve-mudahmy
uv run serve-carlist
uv run serve-mobil123
```
