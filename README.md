# Reconciliation Console

A FastAPI web app that replaces the Tk desktop tools (`service_analyzer.py`,
`reconciliation_app_v5.py`, `export_physician_performance.py`) with a
browser UI backed by `commissions.db` (SQLite), managed exclusively through
Python scripts (never edited by hand).

## Layout

```
app/
  db/
    schema.sql        mended schema: physicians / service_prices tables,
                       FKs from abronal_mirror, sot_mirror, matched_records,
                       unmatched_records, commission_per_physicians; plus
                       users / user_table_access / physician_commission_rates
                       for accounts, per-user table access, and commission
                       rates (see "Accounts & roles" below)
    db_manager.py      the ONLY module that opens sqlite3 connections —
                       also owns password hashing, user/role/table-access
                       helpers, and commission-rate/service-category helpers
    column_adapter.py   maps real Abronal/SoT header spellings to the
                         canonical field names the rest of the pipeline
                         uses, and auto-detects the real header row under
                         any title/criteria rows a source file has above it,
                         and filters out report-footer boilerplate rows
  security/
    crypto.py           Fernet-based encrypt/decrypt for the Abronal
                         credentials stored in .env (see "Encrypting .env
                         credentials" below)
  scripts/new/          the "neo-scripts" pipeline
    abronal_scraper.py           STEP 0 — logs into Abronal (Playwright)
                                  and downloads each physician's export
                                  straight into data/uploads/abronal/
    primary_reconciliation.py    parse Abronal (physician name = filename)
                                  + SoT, exact-match, split matched/unmatched
    secondary_name_matcher.py    load_mismatched_data / name_comparator
                                  (>=70% similarity + amount + ±1 day) / grafter
    category_merger.py           Condensor: read_dictionary / load_data /
                                  list_condensor -> commission_per_physicians
  backend/
    main.py             FastAPI app; serves the frontend, mounts routers,
                         and redirects unauthenticated requests to /login
                         for every page except /login itself
    auth.py              session cookies, password verification, and the
                          require_user / require_admin dependencies every
                          other router uses to gate access
    routers/
      auth_router.py       login / logout / current-user endpoints
      admin.py               user management, per-user table access,
                              commission rates, service categories
                              (all endpoints require role='admin')
      scraper.py          trigger an Abronal fetch, websocket log/progress
      pipeline.py          file upload, run pipeline, websocket log/progress
      tables.py             browse/filter any table the caller has access to
      export.py               export one table (or every table the caller
                               has access to) to .xlsx
  frontend/
    login.html            log-in page
    index.html           Intake & Run page (fetch, upload, run, progress, log)
    evaluation.html       Evaluation page (table browser, filters, export)
    admin.html             Admin hub (links to the three tools below)
    admin_users.html         create/edit/delete users, set roles, manage
                              each user's table access
    admin_commissions.html   set each physician's commission rate
    admin_categories.html    edit each service's category
    style.css
  dictionary.json         service -> category rules (seeds service_prices)
  examples/                real Abronal/SoT export templates (headers only)
                            used to validate the column adapter — handy for
                            testing header-matching changes without needing
                            a live Abronal login
  config.json               scraper settings: headless mode, patient type,
                             physician skip-list (same shape as the old
                             config.json's non-secret fields)
  .env.example               template for Abronal login credentials
  Dockerfile, docker-compose.yml, .dockerignore   container packaging
                             (see "Running with Docker" below)
  exports/                 generated .xlsx exports land here
  data/uploads/{sot,abronal}/   uploaded / scraped source files land here
```

## Setup

```bash
cd app
pip install -r requirements.txt
playwright install chromium        # one-time browser download for the scraper

cp .env.example .env               # then fill in real values:
#   BASE_URL, USERNAME, PASSWORD, ROLE

python db/db_manager.py --init
python db/db_manager.py --seed-dictionary dictionary.json

uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` — you'll be redirected to `/login`.
On this clinic PC the console is meant to live at **http://192.168.1.2:8000**.

## Quick Start Scripts

**Linux / macOS:**
```bash
./start.sh
```

**Windows:**
```cmd
start.bat
```

Opens a permanent titled **Reconciliation Console** window with live uvicorn
logs (window stays open after the process stops so you can read errors).
Use `start.bat --inline` to run in the current terminal instead.

The server listens on all interfaces at port 8000. Other PCs on the LAN
should use `http://192.168.1.2:8000`.

**Run at Windows startup:**
Right-click `install-autostart.bat` and choose **Run as administrator**.
That script leaves Ethernet at `192.168.1.2`, opens firewall port 8000, and
registers a scheduled task that launches the console 30 seconds after boot.
A permanent log console window opens at boot with server output.
A user must be logged in (or use Windows auto-logon) for the task to start
the server.

**First run creates a default admin account automatically** and prints its
random password once to the server console (search the startup log for
"Created a default admin account"); log in with it and change the password
immediately from Admin -> Users. From there: admins use `/` (Intake) and
`/evaluation`; users are sent to `/report`.

`.env` holds real login credentials — never commit it. If `.env` is missing
or incomplete, the "Fetch from Abronal" panel will say so and any fetch
attempt fails cleanly with a message like `BASE_URL is not set`, without a
stack trace. You can skip it entirely and just drag Abronal `.xlsx` files
into the upload zone instead, as before. See "Encrypting .env credentials"
below to avoid storing that password in plaintext.

## Accounts &amp; roles

Every page and API endpoint requires being logged in (`backend/auth.py`,
simple httponly-cookie sessions — no external auth service). Two roles:

- **admin** — full access: Intake & Run, Evaluation, every admin page, and
  the only role that can upload files, run reconciliation, or trigger an
  Abronal fetch.
- **user** — Report page only (`/report`). No Intake & Run and no Evaluation.
  After login they land on the accountant report over condensed matched
  records; they can filter and **Send Report** to admin. Pipeline, scrape,
  and upload APIs are admin-only.

Passwords are hashed with PBKDF2-HMAC-SHA256 (200,000 iterations, random
per-user salt) via stdlib `hashlib` — nothing sent or stored in plaintext.
Sessions live in memory and expire after 12 hours or on server restart.

## Admin pages

Reachable from the "Admin" tab (admin role only; hidden from `user`
accounts, and the pages 403/redirect if visited directly without it):

- **Users &amp; Access** (`/admin/users`) — create accounts, change a
  role or reset a password, delete a user, and open "Manage access" to
  pick exactly which tables a `user` account can see.
- **Commission Rates** (`/admin/commissions`) — one row per physician with
  an editable commission percentage. Applied by `category_merger.py` the
  next time reconciliation runs for that physician (`commision_percent` /
  `commision_amount` on `commission_per_physicians`); changing a rate does
  not rewrite past batches.
- **Service Categories** (`/admin/categories`) — searchable, grouped list
  of every service in `service_prices` (grouped by category, categories
  sorted alphabetically, services sorted alphabetically within each
  category) with an editable category dropdown per service, for fixing a
  miscategorized service without touching `dictionary.json`.

## Column adapter

Real Abronal and SoT exports don't start with a clean header row, and
header text isn't perfectly consistent between exports. `db/column_adapter.py`
handles both problems so `primary_reconciliation.py` never has to guess:

- **Header row detection.** Abronal exports carry one title row above the
  real header (`"Physician Performance - Abronal eHealth"`); SoT exports
  carry three (a clinic title, a criteria line, and a blank row). The
  adapter scans the first ~20 rows of each sheet and picks the one whose
  cells best match a known column name — it doesn't assume row 0.
- **Header spelling.** Each canonical DB field (`patient_full_name`,
  `sub_total`, `commission_percent`, ...) lists the header text variants it
  should recognize (`ABRONAL_SCHEMA` / `SOT_SCHEMA` in that file), matched
  case/punctuation/underscore-insensitively — `"Tin_no"`, `"TIN No."`, and
  `"tin number"` all resolve to `tin_number`. Matched columns are renamed to
  their canonical name; anything unrecognized is kept as-is (never silently
  dropped) and logged so it's visible in the run log.
- **To support a new header spelling or a whole new source format**, add
  the alias to the relevant schema dict — nothing else in the pipeline
  needs to change.
- **Physician-name extraction** (`primary_reconciliation.py`,
  `physician_from_filename()`) is naming-order agnostic: it locates the
  `Dr`/`Dr.` token and reads name words from there, stopping at the first
  date-like token (month name, day, day-range, year, or slash/dash date),
  then keeps only the first and last of those words — no title, no middle
  name. This handles both the real-world convention (`"dr bart jacobs july
  1-9.xlsx"` -> `Bart Jacobs`) and the older date-first convention the
  original export tool used (`"July 20 to July 22 Dr. Ahmed Reja.xlsx"` ->
  `Ahmed Reja`). The scraper (`abronal_scraper.py`) now names its own
  downloads physician-first to match the real-world convention.

## Pipeline order

0. **abronal_scraper.py** *(optional, triggered by the "Fetch from Abronal"
   panel on the Intake page)* — logs into Abronal with Playwright, opens the
   Physician Performance report, and downloads one `.xlsx` per physician for
   the date range you pick, naming each file physician-first, matching how
   Abronal names these exports in practice: `"Dr. Name <date label>.xlsx"`.
   Files land directly in `data/uploads/abronal/`, so step 1 below picks
   them up with no manual download/upload step. You can restrict it to
   specific physicians (comma-separated names) or leave it blank to export
   everyone not on the `skip_physicians` list in `config.json`.
1. **primary_reconciliation.py** — parses every Abronal `.xlsx` (columns
   resolved through `column_adapter.py`; physician name taken from the
   filename via `physician_from_filename()`, e.g. `"dr bart jacobs july
   1-9.xlsx"` -> `Bart Jacobs`) and every SoT `.xlsx`, mirrors both into
   `abronal_mirror` / `sot_mirror`, exact-matches on name+amount, and writes
   `matched_records` / `unmatched_records`.
2. **secondary_name_matcher.py** — for the leftovers, tries to fix spelling
   mismatches: candidate pairs need >=70% character similarity, matching
   amount, and visit dates within 1 day. Matches are renamed to the Abronal
   name, buffered, then grafted into `matched_records` (removed from
   `unmatched_records`).
3. **category_merger.py** — `Condensor` reads `dictionary.json`, loads all
   matched rows for the batch, and condenses them into one row per
   physician/patient/date in `commission_per_physicians`, with one summed
   total per category (Laboratory, X-ray, Ultrasound, Nursing & Procedures,
   Consultation, ECG/Echocardiography/Supplies folded into an `other` column
   for now — extend `CATEGORY_COLUMN_MAP` in `db_manager.py` if you want
   dedicated columns for those too). Each row also carries the physician's
   name directly (`physician_name`, alongside the `physician_id` foreign
   key) so the Evaluation page — and anyone exporting this table — doesn't
   need to join back to `physicians` just to see whose commission it is.

## Evaluation page (admin)

Every filterable column (physician name, patient name, service, category,
status, etc.) uses one combined control instead of a plain text box or a
plain `<select>`: click it and every distinct value in that table/column is
listed (fetched live from the database via `GET
/api/tables/{table}/distinct/{column}`); start typing and the list narrows
to matches, the same way a search box would. Selecting a value — or typing
a partial term and applying — filters the table with a `LIKE` match
server-side, so a typed fragment works just as well as a picked option.

Admins see the raw per-service `matched_records` table (and any other
tables they open). Table access for accounts is still managed under
Admin → Users → "Manage access".

## Report page (user)

`/report` is the only dashboard for `user` accounts. It uses the condensed
matched-records adapter (`temp/matched_review.json`): extra source columns
are dropped, and rows that share the same patient, physician, and service
category are merged with `service_id` rewritten to the category name and
amounts summed. Filter by physician and date range, then **Send Report** to
insert the current view into the `reports` table. Admins open those
snapshots from Admin → View Reports, with the same physician and date-range
filters.

## Upgrading an existing database

Schema changes (like the `physician_name` column added to
`commission_per_physicians`) are applied automatically: `db_manager.py`
runs a small migration step on every app startup (and whenever `--init` is
run) that adds any columns `schema.sql` has gained since your database was
first created, and backfills them from existing data where possible. No
manual `ALTER TABLE` or data loss involved — just restart the app or
re-run `python db/db_manager.py --init`.

All four scripts can also be run standalone from the CLI (see each file's
`__main__` block) for debugging outside the web app, e.g.:

```bash
python scripts/new/abronal_scraper.py --from-date 2026-08-10 --to-date 2026-08-11
python scripts/new/primary_reconciliation.py --abr data/uploads/abronal --sot data/uploads/sot
python scripts/new/secondary_name_matcher.py --batch <batch_id>
python scripts/new/category_merger.py --batch <batch_id>
```

## Visual identity

The UI (`frontend/style.css`) uses a soft, minimal, post-modern-corporate
register: a warm off-white ground, white cards with a soft shadow and a
single recurring accent — a thin colored tab on each card and a quiet
quarter-circle in the header, in a muted steel-blue/sage palette. Typography
is deliberately system-font-only (no external CDN/`@import`) — a
geometric-leaning system sans for headings, a humanist system sans for
body/UI text, and the system monospace stack for tabular data and the log
windows — so styling never depends on outbound internet access, which
matters for an on-prem/Docker deployment that may not have any. The
stylesheet is served with a cache-busting `?v=` query string
(`/static/style.css?v=7`, bumped whenever the CSS changes) so a browser
never serves a stale cached copy after an update — if buttons or a page
ever look unstyled after pulling a new version, a hard refresh
(Ctrl/Cmd+Shift+R) clears any leftover cache from before that was added.

## Encrypting .env credentials

`.env` holds a plaintext Abronal login by default, same as before — this
step is optional but recommended. `security/crypto.py` encrypts
`USERNAME`/`PASSWORD` in place with Fernet (AES-128-CBC + HMAC):

```bash
python security/crypto.py --generate-key   # one-time; writes .encryption_key
python security/crypto.py --encrypt-env    # rewrites USERNAME/PASSWORD in .env
```

This replaces those two lines in `.env` with an `enc:`-prefixed encrypted
value; `abronal_scraper.py` decrypts them transparently at runtime. Keep
`.encryption_key` out of version control, same as `.env` itself. To use a
key from a secrets manager instead of a file on disk, set the
`ENCRYPTION_KEY` environment variable (checked before `.encryption_key`).
Losing the key means an encrypted `.env` can no longer be read — keep a
backup of `.encryption_key` somewhere safe, or re-run
`--generate-key --force` and re-enter the credentials.

## Running with Docker

```bash
cd app
cp .env.example .env      # fill in real values, or leave as-is / touch an
                           # empty .env if you don't need Abronal scraping
docker compose up --build
```

Then open `http://localhost:8000` as usual. `docker-compose.yml` mounts
`./db`, `./data`, and `./exports` as volumes so the database, uploaded
files, and generated exports all persist across container rebuilds/restarts.
If you've encrypted `.env` (see above), either set `ENCRYPTION_KEY` in the
shell before `docker compose up` or uncomment the `.encryption_key` volume
line in `docker-compose.yml` — it's commented out by default so a
first-time setup without encryption doesn't accidentally mount an empty
directory where the key file should be. The image bakes in Playwright's
Chromium (with its system library dependencies) at build time, so the
scraper works out of the box inside the container.

See `CHANGELOG.md` for what changed since the previous version.
