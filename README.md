# cars_scrap

Repo scraping listing mobil untuk dua source:

- `mudah.my`
- `carlist.my`

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

Kedua source (`mudah.my` dan `carlist.my`) punya pola yang sama:

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
- `uv run serve-mudahmy`
- `uv run serve-carlist`

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

Project ini menyimpan data ke 6 tabel:

- `cars_scrap_mudahmy`
- `cars_scrap_mudahmy_details`
- `price_history_scrap_mudahmy`
- `cars_scrap_carlistmy`
- `cars_scrap_carlistmy_details`
- `price_history_scrap_carlistmy`

File SQL yang tersedia:

- `sql/cars_scrap_mudahmy.sql`
- `sql/cars_scrap_mudahmy_details.sql`
- `sql/price_history_scrap_mudahmy.sql`
- `sql/cars_scrap_carlist.sql`
- `sql/cars_scrap_carlistmy_details.sql`
- `sql/price_history_scrap_carlist.sql`

Contoh bootstrap schema:

```bash
export PGPASSWORD="$DB_PASSWORD"

psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_mudahmy.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_mudahmy_details.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/price_history_scrap_mudahmy.sql

psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_carlist.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/cars_scrap_carlistmy_details.sql
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f sql/price_history_scrap_carlist.sql
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

### Logging dan Flask

- `LOG_LEVEL`
- `HOST` untuk Flask bind address
- `PORT` untuk Flask port
- `FLASK_DEBUG`

Default port:

- `serve-mudahmy` -> `5201`
- `serve-carlist` -> `5202`

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

### Cloudflare fallback khusus carlist

- `CARLIST_AUTO_ATTEMPT_CLOUDFLARE`
- `CARLIST_CLOUDFLARE_MAX_ATTEMPTS`
- `CARLIST_CLOUDFLARE_POST_SOLVE_WAIT_SECONDS`

## Menjalankan scraper via CLI

### A. Ambil listing links saja

Mode ini hanya mengembalikan daftar `listing_url`, belum menyimpan detail ke Postgres.

```bash
uv run scrape-mudahmy --max-pages 1
uv run scrape-carlist --max-pages 1
```

Override halaman awal:

```bash
uv run scrape-mudahmy --start-url "https://www.mudah.my/malaysia/cars-for-sale"
uv run scrape-carlist --start-url "https://www.carlist.my/cars-for-sale/malaysia"
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

Contoh dasar:

```bash
uv run scrape-mudahmy --track-older-than 1w
uv run scrape-carlist --track-older-than 1w
```

Contoh dengan filter `last_scraped_at` supaya listing yang baru diproses tidak terkena lagi:

```bash
uv run scrape-mudahmy --track-older-than 3m --track-last-scraped-older-than 90d --track-limit 200
uv run scrape-carlist --track-older-than 3m --track-last-scraped-older-than 90d --track-limit 200
```

Mode hitung saja:

```bash
uv run scrape-mudahmy --track-older-than 1w --track-count
uv run scrape-carlist --track-older-than 1w --track-count
```

Mode preview tanpa scrape:

```bash
uv run scrape-mudahmy --track-older-than 1w --track-dry-run
uv run scrape-carlist --track-older-than 1w --track-dry-run
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

- `MUDAHMY_CARS_LISTING_URL` atau `CARLIST_CARS_LISTING_URL`
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
uv run serve-mudahmy
uv run serve-carlist
```
