# Changelog

## v0.15 — Runtime state across page switches

**Added**
- **Volatile `temp/runtime_state.json`** keeps Abronal scrape and reconciliation progress/logs while you navigate away from Intake, plus Intake/Evaluation/Report UI filters. Returning to a page restores progress and reconnects live log streams if a job is still running. Starting a job while one is already running reattaches to the existing batch instead of starting a second.

## v0.14 — User role: Report only

**Changed**
- **`user` accounts no longer see Intake & Run or Evaluation.** After login they land on `/report` only. Hitting `/` or `/evaluation` redirects them there. Pipeline upload/run and Abronal scrape APIs are admin-only.
- Admin nav/pages unchanged (Intake, Evaluation, Admin).

## v0.13 — Permanent Windows log console

**Added**
- **`start.bat` opens a permanent titled log console** (`Reconciliation Console`) so uvicorn output stays visible after launch, crash, or stop. Autostart uses the same path. Pass `start.bat --inline` to keep logs in the current terminal instead.

## v0.12 — Date-range filter fix (mm/dd/yyyy + Abronal time suffixes)

**Fixed**
- **Evaluation / Report date filters were matching almost nothing.** Abronal payment dates are stored as US month-first values with a quirky time suffix (`08/19/2026 2:28:PM`). The old parser assumed day-first dates and did not strip `:PM`, so `norm_date()` returned `NULL` for nearly every Abronal/matched row and the range filter looked broken (scraping still worked because it uses ISO `<input type=date>` values).
- New `parse_date()` / `filter_date()` / `resolve_date_column()` in `db_manager.py`: strip Abronal-style times, interpret ambiguous slash dates as **mm/dd/yyyy**, fall back to day-first only when month-first is impossible, keep ISO for SQL comparison, and prefer `payment_date` when a table has multiple date columns.

## v0.11 — User Report page and admin View Reports

**Added**
- **Report page for `user` accounts** (`/report`). Uses the condensed matched-records adapter as its data source, with physician-name and date-range as the main filters. **Send Report** writes the currently filtered view into a `reports` table (created automatically), rows inserted in payment-date order.
- **View Reports admin module** (`/admin/reports`). Admins can load submitted snapshots, pick a submission, and filter by the same physician name and date range.
- `reports` table stores each send as a `submission_id` batch (who sent it, when, the condensed columns, and the filters used). Not listed on the Evaluation page.

**Changed**
- User nav shows a Report tab; admin hub gains a View Reports tile. Admins hitting `/report` are redirected to `/admin/reports`.

## v0.10 — Accountant review view for matched records

**Added**
- **Condensed `matched_records` view for `user` accounts.** When a non-admin opens `matched_records`, the table is passed through a review adapter (`db/matched_review_adapter.py`) that keeps only the accountant-facing columns (`match_id`, `physician_id`, `physician_name`, `patient_name`, `service_id`, `total_amount`, `net_amount`, `match_type`, `confidence`, `user_flagged_mismatch`, `user_flag_reason`) and then merges rows that share the same patient, physician, and service category. `service_id` is rewritten to the category name (Laboratory, X-ray, …) and amounts are summed. Admins still see the raw per-service matched table.
- **Volatile JSON cache in `temp/`.** The condensed table is not written to SQLite. It is rebuilt from `matched_records` + `dictionary.json` / `service_prices` and stored as `temp/matched_review.json`, invalidated after a pipeline run or a flag change (and whenever `commissions.db` is newer than the cache).
- Flagging a condensed row flags every underlying `match_id` that was merged into it. Excel export for `user` accounts uses the same condensed view, with flagged rows still highlighted red.

**Changed**
- `backend/routers/tables.py` / `export.py` — `user`-role reads of `matched_records` go through the review adapter; date filters still apply to each source row's payment date before merging, so a date range returns that period's condensed totals including gaps.
- `backend/routers/pipeline.py` — clears the review cache when a run finishes.
- `frontend/evaluation.html` — Evaluation defaults to `matched_records` when that table is in the caller's allow-list (typical for `user` accounts).

## v0.9 — User mismatch flagging, physician name on all tables, Windows support

**Added**
- **User-flaggable matched records.** Users can now flag any matched record they believe is incorrect directly from the Evaluation page. A flag button appears on each row of the `matched_records` table; clicking it prompts for an optional reason, and the row is visually highlighted. Flags are persisted in `user_flagged_mismatch` and `user_flag_reason` columns. New API endpoint: `POST /api/tables/matched_records/{match_id}/flag`.
- **Physician name on matched and unmatched records.** Both `matched_records` and `unmatched_records` tables now include a `physician_name` column (not just `physician_id`), so the physician is immediately visible without joining. Auto-migration backfills existing rows from the `physicians` table.
- **Windows startup script.** New `start.bat` for Windows users, equivalent to `start.sh` for Linux/macOS. Activates the venv if present, displays local and LAN URLs, and launches uvicorn.

**Fixed**
- **Date filtering now correctly handles all date formats.** The `normalize_date_to_iso()` function strips trailing time components (hh:mm, hh:mm:ss, AM/PM) before parsing, and supports ISO dates (yyyy-mm-dd), day-first dates (dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy), and various separators. Date range filters work correctly even when source data contains mixed formats or gaps between dates.
- **Records with missing/invalid dates sort to the end.** Instead of failing or being excluded, rows with unparseable dates are sorted after valid dates (using a `9999-99-99` sentinel) so they remain visible and filterable.
- **Commission table entries no longer appear split.** `commission_per_physicians` rows are now inserted sorted by physician name first, then by payment date, so all entries for a physician appear grouped together. The query ordering (`ORDER BY physician_name, payment_date`) ensures this grouping is maintained on display.

**Changed**
- `db/schema.sql` — added `physician_name` column to `matched_records` and `unmatched_records`; fixed `unmatched_records.physician_id` type from TEXT to INTEGER.
- `db/db_manager.py` — added migrations for new columns with backfill; enhanced `table_default_order()` for matched/unmatched tables to include physician_name in ordering.
- `scripts/new/primary_reconciliation.py` — now includes `physician_name` in matched/unmatched inserts; sorts records by date before insertion.
- `scripts/new/secondary_name_matcher.py` — looks up and includes `physician_name` when grafting fuzzy matches; sorts by date before insertion.
- `scripts/new/category_merger.py` — sorts condensed rows by physician name then date before insertion.
- `backend/routers/tables.py` — added `POST /matched_records/{match_id}/flag` endpoint.
- `frontend/evaluation.html` — added flag button column for `matched_records` table with visual feedback.
- `frontend/style.css` — added styles for flag buttons and flagged row highlighting.

**Verified**
- Date filtering tested with ISO dates, dd/mm/yyyy, dd/mm/yyyy hh:mm, and mixed formats — all correctly filtered by the specified range.
- Flagging workflow confirmed: flag button toggles state, reason is stored, row is highlighted, state persists across page reloads.
- Commission table grouping confirmed: entries for each physician appear together, sorted by date within each group.

## v0.8 — Date-ordering and duplicate protection for imported data

**Fixed**
- **Imported lists are now stored in chronological order and duplicates are skipped.** The database layer now checks for an existing matching record before inserting mirrored and matched rows, so repeated imports do not create duplicate entries.
- **Table ordering is now date-first by default.** Mirror and result tables fetch in chronological order, and `commission_per_physicians` is grouped by physician name before date so a physician's split entries stay together in the expected sequence.
- **Date filtering remains consistent across the app.** Date comparison uses the normalized payment date and strips trailing time values before comparison, so the range filter works for real source values such as `dd/mm/yyyy hh:mm` and date-only strings.

## v0.7 — Grouped service categories, CSS reliability fix

**Fixed**
- **CSS could fail to render on any/every page.** `frontend/style.css`
  loaded its fonts via `@import url('https://fonts.googleapis.com/...')`.
  In a sandboxed test of this exact deployment the request came back
  `403` — and the same failure is plausible in a real on-prem/Docker
  deployment with restricted or no outbound internet access, which this
  app now explicitly supports (see v0.6's Docker packaging). Removed the
  external font dependency entirely; `--font-display`/`--font-body`/
  `--font-mono` now use system font stacks (`-apple-system`/"Segoe UI"/
  Roboto/Consolas/etc.) that approximate the same geometric-sans /
  humanist-sans / mono character without any network request. Also added
  a cache-busting `?v=7` query string to every page's `<link
  rel="stylesheet">` (all 7 HTML pages), since `style.css` has been
  edited in place across several previous turns without one — a browser
  that had cached an earlier version would be missing rules added later
  (e.g. `.user-chip`, `.admin-tile`), which independently could produce
  exactly the "some buttons/the whole admin page look unstyled" symptom
  reported. Verified with a fresh browser context: zero failed network
  requests on load (previously 3x 403s per page), and all pages render
  fully styled.

**Changed**
- **Service Categories admin page now groups by category.** Was a flat,
  service-name-ordered list; now grouped under a category header (e.g.
  "Laboratory (54)"), categories sorted alphabetically, services sorted
  alphabetically within each category. `db_manager.list_services()`
  changed its `ORDER BY` from `service_type` alone to `category ASC,
  service_type ASC`, and `frontend/admin_categories.html` renders
  category-header rows between groups instead of a flat `<tr>` list.
  Saving a service's new category now reloads and re-groups the whole
  list (since the row may need to move to a different group) instead of
  just flipping the Save button to a checkmark in place.

**Verified**
- Fresh screenshots of the Intake & Run page, Admin hub, and the
  Service Categories page confirm: no unstyled elements, correct grouping
  ("Consultation (13)" first alphabetically, then "Laboratory" with
  services like Cholesterol/Creatinine/ESR in alphabetical order within
  it, confirmed by scrolling), and zero failed network requests.

## v0.6 — Accounts, roles, admin pages, .env encryption, Docker

**Added**
- **User accounts and login.** New `users` table (`user_id`, `username`,
  `pass_hash`, `role`) plus `backend/auth.py` (httponly-cookie sessions,
  PBKDF2-HMAC-SHA256 password hashing, `require_user`/`require_admin`
  dependencies) and `backend/routers/auth_router.py`
  (`/api/auth/login|logout|me`). Every page redirects to a new
  `frontend/login.html` if not logged in; first run auto-creates a default
  admin account with a random password printed once to the server console.
- **Two roles: admin, user.** Enforced on every table-serving endpoint
  (`tables.py`, `export.py`): `admin` sees every table; `user` is
  restricted by default to `abronal_mirror`, `sot_mirror`,
  `matched_records`, `unmatched_records` — configurable per individual
  user (not just per role) via a new `user_table_access` table and the
  Admin -> Users "Manage access" dialog. Triggering an Abronal fetch now
  requires `admin` (it uses stored login credentials, so it's treated as
  an operational action rather than a plain read).
- **Three new admin pages** (`/admin`, and `/admin/users`,
  `/admin/commissions`, `/admin/categories`, all admin-only —
  403/redirected otherwise):
  - **Users & Access** — create/edit-role/reset-password/delete users,
    plus the per-user table-access picker described above.
  - **Commission Rates** — a new `physician_commission_rates` table and
    admin UI to set each physician's commission percentage, now actually
    applied by `category_merger.py` when writing
    `commission_per_physicians` (previously hardcoded to 0/0 — a 12.5%
    rate on a $300 total now correctly produces `commision_amount` $37.50).
  - **Service Categories** — searchable inline editor for every service's
    category in `service_prices`, an alternative to hand-editing
    `dictionary.json`.
- **`.env` credential encryption.** New `security/crypto.py`: Fernet
  (AES-128-CBC + HMAC) encrypt/decrypt for `USERNAME`/`PASSWORD` in `.env`,
  with a `--generate-key` / `--encrypt-env` CLI. Encrypted values are
  stored `enc:`-prefixed; `abronal_scraper.py`'s `ScraperConfig` decrypts
  them transparently at runtime and falls back to plaintext untouched for
  anything not prefixed, so existing unencrypted `.env` files keep working
  with zero changes. The key itself comes from `.encryption_key` (kept out
  of version control, same as `.env`) or an `ENCRYPTION_KEY` environment
  variable, e.g. from a secrets manager in Docker/production.
- **Dockerized.** New `Dockerfile` (Python 3.12-slim, Playwright + Chromium
  and its system libraries baked in at build time) and `docker-compose.yml`
  (persistent volumes for `db/`, `data/`, `exports/`; optional `.env`;
  optional `.encryption_key` mount, commented out by default so a
  first-time no-encryption setup doesn't accidentally mount an empty
  directory) and a `.dockerignore` that keeps secrets and runtime data out
  of the built image.

**Changed**
- `db/schema.sql` — added `users`, `user_table_access`,
  `physician_commission_rates`. All covered by the existing
  auto-migration path (`db_manager._run_migrations()` now re-runs the full
  `CREATE TABLE IF NOT EXISTS` schema script before its column-level
  `ALTER`s, so new tables appear on upgrade with no manual steps), plus a
  new "seed a default admin if no users exist yet" step on every startup.
- `db/db_manager.py` — added password hashing (`hash_password`/
  `verify_password`), user CRUD, table-access helpers
  (`get_user_allowed_tables`/`set_user_table_access`,
  `DEFAULT_USER_TABLES`), and commission-rate/service-category helpers
  (`list_commission_rates`/`set_commission_rate`,
  `list_services`/`update_service_category`). Replaced `datetime.utcnow()`
  (deprecated) with `datetime.now(timezone.utc)`.
- `scripts/new/category_merger.py` — `Condensor.list_condensor()` now
  looks up each physician's commission rate via
  `dbm.get_commission_rate()` and writes real `commision_percent`/
  `commision_amount` instead of hardcoded zeros.
- `scripts/new/abronal_scraper.py` — `ScraperConfig` now resolves
  `BASE_URL`/`USERNAME`/`PASSWORD`/`ROLE` through
  `security/crypto.py:resolve_env_value()`, transparently decrypting
  `enc:`-prefixed values.
- `backend/main.py` — mounts the new `auth_router` and `admin` routers;
  every page route now checks the session cookie server-side and
  redirects to `/login` (or back to `/` for a non-admin hitting an admin
  page) instead of serving the page unconditionally.
- `backend/routers/tables.py` / `export.py` — every endpoint now requires
  a logged-in user and checks the requesting user's allowed-tables list
  before touching a table; `export_all` exports only the tables the
  current user can see rather than unconditionally every table.
- `backend/routers/pipeline.py` / `scraper.py` — HTTP endpoints (upload,
  run, uploads-list, log) now require a logged-in user;
  `scraper.py`'s `/run` additionally requires `admin`. The `/ws/{batch_id}`
  log-streaming endpoints are unchanged (a random batch id functions as a
  capability token, same as before).
- `requirements.txt` — added `cryptography`.
- `frontend/style.css` — added `.user-chip` (username/role badge/logout),
  `.admin-grid`/`.admin-tile` (admin hub cards), and `.inline-edit-row`
  styles; fixed a layering bug where the header's decorative accent shapes
  painted over the user-chip text (added explicit `z-index` stacking).
- `frontend/index.html` / `evaluation.html` — header now includes an
  Admin nav tab (hidden for non-admins) and a user chip (username, role
  badge, Log Out); both redirect to `/login` if the session has expired.
  The "Fetch from Abronal" panel on the Intake page is hidden for
  non-admin users.

**Verified**
- Full curl-driven auth flow: unauthenticated page request redirects
  (307) to `/login`, unauthenticated API call 401s, login issues a working
  session cookie, `/api/auth/me` reflects it correctly.
- RBAC: a freshly-created `user`-role account's `/api/tables/list` returns
  exactly the 4 default tables; direct requests for
  `commission_per_physicians` and any `/api/admin/*` endpoint both 403 for
  that account.
- Commission rate end-to-end: set a physician's rate via the admin API,
  re-ran the full pipeline (primary reconciliation -> secondary name
  matcher -> category merger), confirmed `commision_percent`/
  `commision_amount` on the resulting `commission_per_physicians` row
  matched the configured rate.
- `.env` encryption round-trip: generated a key, encrypted a test
  `USERNAME`/`PASSWORD`, confirmed `ScraperConfig` reads back the original
  plaintext; confirmed a missing/wrong key fails with a clean `CryptoError`
  message rather than a stack trace.
- All five new/changed pages (login, index, admin hub, admin/users,
  admin/commissions, admin/categories) screenshotted end-to-end
  (Playwright/Chromium) after logging in as the auto-created admin
  account — including catching and fixing the header layering bug above.
- Full pipeline re-run after all of the above changes together (auth,
  RBAC, commission rates) still correctly parses real-header test files,
  filters the SoT footer row, derives "Bart Jacobs" as the physician name,
  and produces a correct `commission_per_physicians` row — confirming
  nothing from v0.1–v0.5 regressed.

## v0.5 — Footer-row filtering, Row ID filters, chunked pagination, whole-DB search

**Fixed**
- **SoT footer boilerplate no longer gets mirrored.** SoT exports (and
  occasionally Abronal ones) end with a trailing summary/branding line —
  e.g. `"2026-07-10 00:00:00   For Managment Purposes Only   Powered by
  MarakiErp(Beta) - 2012"` — rather than real data. `column_adapter.py`
  now scans every sheet for rows containing that boilerplate (matched
  case-insensitively, including the source's own "Managment" typo) and
  drops them before header detection or parsing ever sees them, so they
  can no longer be mistaken for a header row or mirrored as a data row.
  Logged as "Filtered out N boilerplate footer row(s)" when it happens.
- **Physician name is now first name + surname only.**
  `physician_from_filename()` in `primary_reconciliation.py` no longer
  keeps the "Dr." title or any middle name — it isolates the name portion
  of the filename exactly as before, then keeps only the first and last
  word. `"dr bart jacobs july 1-9.xlsx"` -> `"Bart Jacobs"` (previously
  `"Dr. Bart Jacobs"`); `"dr ahmed ali reja july 1-9.xlsx"` -> `"Ahmed
  Reja"` (middle name dropped). Applies to every physician name written
  to the database from here on — existing rows are not rewritten.

**Added**
- **Row ID filter on every table.** The Evaluation page's filter row now
  always includes a "Row ID" control bound to each table's own
  identifying column (`row_id` for `abronal_mirror`/`sot_mirror`,
  `match_id`, `unmatched_id`, `id` for `commission_per_physicians`,
  `physician_id`, `service_id` — whichever is that table's first/primary
  column), using the same combined dropdown-and-search widget as the
  other filters.
- **1000-row chunked table loading.** `db_manager.fetch_table()` and the
  new `count_table()` take `limit`/`offset`; `GET
  /api/tables/{table}` returns `{rows, total, offset, limit}` for one
  1000-row chunk plus the total matching count. The Evaluation page loads
  one chunk at a time (default 1000 rows) with "← Previous 1000" / "Next
  1000 →" buttons that replace the displayed rows rather than
  accumulating them, and a "Showing X–Y of Z" label; buttons disable
  themselves at either end of the result set.
- **Every filter — including the date range — now searches the whole
  database, not the currently-loaded chunk.** Date-range filtering moved
  from client-side JS (which only ever saw whatever chunk was already in
  the browser) to the server: the Evaluation page now sends
  `date_column`/`start_date`/`end_date` to `GET /api/tables/{table}`,
  which applies them as a SQL `date(...)` comparison against the entire
  table before paginating. Changing any filter or the date range resets
  pagination back to the first chunk of the new (filtered) result set —
  "point to that, then load its respective chunk," per the request.

**Removed**
- **`pipeline_runs` table.** Dropped from `schema.sql` and
  `db_manager.TABLES`, along with `db_manager.start_run()` /
  `update_run()` / `finish_run()` and the `GET
  /api/pipeline/status/{batch_id}` and `GET
  /api/scraper/status/{batch_id}` endpoints that read from it (neither
  was called by the frontend). Live run progress and the log window are
  unaffected — they were always tracked in-memory in the routers and
  streamed over the websocket, never persisted to this table, so nothing
  about the actual Intake & Run experience changes.

**Changed**
- `db/schema.sql` — `pipeline_runs` table removed.
- `db/db_manager.py` — `start_run`/`update_run`/`finish_run` removed;
  `TABLES` no longer lists `pipeline_runs`; `fetch_table`/new
  `count_table` gained `limit`, `offset`, `date_column`, `start_date`,
  `end_date`; shared WHERE-clause building factored into `_build_where()`.
- `db/column_adapter.py` — added `FOOTER_MARKERS` / `_is_footer_row()`
  and a filtering pass in `adapt_sheet()` before header-row detection.
- `scripts/new/primary_reconciliation.py` — `physician_from_filename()`
  rewritten to return first-name-plus-surname only; removed its
  now-defunct `dbm.start_run`/`finish_run` calls in the CLI entry point;
  docstring updated to no longer reference `pipeline_runs`.
- `backend/routers/pipeline.py` / `backend/routers/scraper.py` — removed
  all `dbm.start_run`/`update_run`/`finish_run` calls and the `/status`
  endpoints; in-memory `RUN_LOGS`/websocket streaming is unchanged.
- `backend/routers/tables.py` — `GET /{table}` and `POST /{table}/filter`
  now accept `offset` (default 0) and cap `limit` at 1000; `GET
  /{table}` also accepts `date_column`/`start_date`/`end_date` and
  returns `total` alongside `rows`.
- `frontend/evaluation.html` — filter row always includes a "Row ID"
  combo; date range now sent to the server instead of filtered in JS;
  added Previous/Next 1000 pagination controls and a "Showing X–Y of Z"
  label; table/filter/clear actions reset pagination to the first chunk.

**Verified**
- Footer-row test file (using the exact line from the report) confirmed
  filtered out — SoT parse reported 2 real rows instead of 3, with the
  drop logged.
- `physician_from_filename()` checked against physician-first,
  date-first, and a three-word-name input — all correctly reduced to
  "First Surname" with no title.
- Fresh `db_manager.py --init` confirmed `pipeline_runs` is absent from
  `sqlite_master`; full pipeline (primary reconciliation → secondary name
  matcher → category merger) still runs end-to-end with no errors.
- `GET /api/tables/{table}` confirmed: a Row ID filter (`?id=1`) returns
  only that row; a `date_column`/`start_date`/`end_date` query correctly
  isolates matching rows regardless of chunk boundaries; `total` in the
  response matches the filtered count independent of `limit`/`offset`.
- Evaluation page screenshotted end-to-end: Row ID filter present
  alongside the other combo filters, and the Previous/Next 1000 controls
  render with a correct "Showing 1–2 of 2" label and correctly disabled
  buttons at both ends of a 2-row result set.

## v0.4 — Physician name on commissions, combined filter widget, softer UI

**Added**
- `commission_per_physicians.physician_name` — a real column (not just the
  `physician_id` foreign key) so the Evaluation page and any Excel export
  of this table show whose commission each row is without a join.
  Populated by `category_merger.py`'s `Condensor.list_condensor()`, which
  already had the physician's name on hand from its join with
  `physicians`.
- `db_manager._run_migrations()` — a small, idempotent migration step that
  adds any column `schema.sql` has gained since a database was first
  created (currently: `commission_per_physicians.physician_name`) and
  backfills it from existing data (joins back to `physicians` by id for
  rows written before the column existed). Runs automatically on every
  app startup and on `python db/db_manager.py --init`, so upgrading an
  existing installation needs no manual `ALTER TABLE` and loses no data.
  Verified against a hand-built "old schema" database: the column gets
  added and every pre-existing row is correctly backfilled.
- `GET /api/tables/{table}/distinct/{column}` — new endpoint returning
  every distinct non-null value for a column, powering the new filter
  widget below.
- **Combined dropdown-and-search filter** — every filterable column on the
  Evaluation page (physician name, patient name, service, category,
  status, etc.) is now one control instead of a plain text box: click it
  and every distinct value in that column is listed; start typing and the
  list narrows the same way a search box would. `commission_per_physicians`
  filters now include physician name (and no longer offer the now-redundant
  `physician_id` as a filter, since the name column covers that need
  directly).
- `db_manager.fetch_table()` filters switched from exact match to a `LIKE`
  match, so both a value picked from the new dropdown and a partial term
  typed by hand narrow the table correctly.

**Changed**
- `frontend/style.css` — full restyle to a softer, minimalist, post-modern
  corporate register: warm off-white ground, white cards on a soft shadow,
  a single recurring signature accent (a thin colored tab on each card's
  top edge, echoed as a quiet quarter-circle in the header) in a muted
  steel-blue/sage palette. Space Grotesk for headings, Inter for body/UI
  text, IBM Plex Mono reserved for tabular data and the log windows —
  replacing the previous dark-ink "ledger/paper" theme.
- `frontend/evaluation.html` — filter row rebuilt around the new combo
  widget; masthead title simplified to "Reconciliation Console."
- `frontend/index.html` — masthead title updated to match.
- `db/schema.sql` — `commission_per_physicians` gains `physician_name TEXT
  NOT NULL DEFAULT ''`.
- `scripts/new/category_merger.py` — carries `physician_name` through the
  group key/dict into the `INSERT` for `commission_per_physicians`.
- `backend/main.py` — startup now calls `dbm._run_migrations()` for an
  already-existing database (not just `dbm.init_db()` on first run).

**Verified**
- Migration tested against a simulated pre-v0.4 database: column added,
  existing rows backfilled correctly from `physicians`.
- Full pipeline run (primary reconciliation → secondary name matcher →
  category merger) against realistic multi-physician test data confirms
  `physician_name` lands correctly in `commission_per_physicians`.
- `GET /api/tables/.../distinct/...` and the `LIKE`-based filter endpoint
  both confirmed via direct API calls.
- Both pages screenshotted end-to-end (Playwright/Chromium): new visual
  identity renders as intended, the physician-name column and filter are
  present, and the combo widget's "click shows everything" and "type
  narrows it" behaviors both work as designed.

## v0.3 — Column adapter for real-world Abronal/SoT formats

**Added**
- `db/column_adapter.py` — new module sitting between raw uploaded sheets
  and the fixed columns `commissions.db` expects.
  - `ABRONAL_SCHEMA` / `SOT_SCHEMA`: each canonical DB field (e.g.
    `patient_full_name`, `sub_total`, `commission_percent`) lists the
    header spellings it should recognize.
  - `adapt_sheet(path, schema, log=...)`: reads a sheet with no assumed
    header row, scans the first ~20 rows to find the one that actually
    looks like a header (scoring how many cells match a known alias),
    and returns a DataFrame renamed to canonical column names. Built
    specifically around the real files: Abronal exports carry one title
    row (`"Physician Performance - Abronal eHealth"`) above the header;
    SoT exports carry three (clinic title, criteria line, blank row).
    Header matching is case/punctuation/underscore-insensitive, so
    `"Tin_no"`, `"TIN No."`, and `"tin number"` all resolve the same way.
    Unrecognized columns are kept (not dropped) and logged, so nothing
    silently disappears.
  - `get_str` / `get_float` / `get_int`: typed row getters that tolerate a
    canonical column being entirely absent from a given file, defaulting
    instead of raising.
  - Raises `ColumnAdapterError` (not a generic exception) when no header
    row can be found with confidence, so a malformed upload fails with a
    clear log line naming the file instead of silently importing 0 rows
    or garbage.

**Changed**
- `scripts/new/primary_reconciliation.py`
  - `parse_abronal_dir()` / `parse_sot_dir()` rewritten to call
    `column_adapter.adapt_sheet()` instead of the old manual "guess a
    column name" lookup helper — this is the fix for headers/rows not
    lining up with what the previous version assumed.
  - `physician_from_filename()` rewritten to be naming-order agnostic. It
    now locates the `Dr`/`Dr.` token and reads name words outward from
    there, stopping at the first date-like token (month name, day, day
    range like `"1-9"`, year, or slash/dash date). Verified against both
    the real-world convention (`"dr bart jacobs july 1-9.xlsx"` -> `Dr.
    Bart Jacobs`) and the older date-first convention the original
    `export_physician_performance.py` used (`"July 20 to July 22 Dr.
    Ahmed Reja.xlsx"` -> `Dr. Ahmed Reja`), so old and new files can sit in
    the same upload folder without special-casing.
- `scripts/new/abronal_scraper.py` — `export_one()` now names its
  downloads physician-first (`"Dr. Name <date label>.xlsx"`) to match how
  Abronal names these exports in practice, instead of the old tool's
  date-first convention.
- `README.md` — new "Column adapter" section explaining header-row
  detection, alias matching, and how to add support for a new header
  spelling or source format; pipeline-order section's example filenames
  updated to match the real naming convention.

**Verified**
- Ran `column_adapter.adapt_sheet()` directly against the real
  `abronal-example.xlsx` (12/12 canonical columns matched, header
  correctly found at row 1 under the title row) and `sot-example.xlsx`
  (14/14 matched, header correctly found at row 3 under three leading
  rows).
- Built populated versions of both templates with realistic data and a
  physician-first filename (`"dr bart jacobs july 1-9.xlsx"`), then ran
  the full pipeline (primary reconciliation -> secondary name matcher ->
  category merger) both standalone and through the live FastAPI
  endpoints. Physician was correctly recorded as `Dr. Bart Jacobs`
  (extracted from the filename), 2 exact matches and 1 fuzzy-name match
  were found, and `commission_per_physicians` came out with 3 correctly
  condensed rows.
- Spot-checked `physician_from_filename()` against six filename variants
  (physician-first, date-first, different date formats, different casing)
  — all five filesystem-realistic cases resolved to the correct name.

## v0.2 — Abronal scraper integrated into the web app

**Added**
- `scripts/new/abronal_scraper.py` — new neo-script (step 0 of the
  pipeline). Ports the Playwright login/report/export flow out of the old
  standalone `export_physician_performance.py` (its `AbronalSession`,
  `DateRange`, and `Physician` classes) and adapts it to run headlessly
  from the web app: no Tk date picker, no subprocess hand-off to a
  reconciliation app, no SoT-folder wait dialog — it just logs in, exports
  each requested physician's report for a given date range, and drops the
  files straight into `data/uploads/abronal/` using the same
  `"<date label> Dr. Name.xlsx"` naming `primary_reconciliation.py` already
  parses. Raises a clean `ScraperError` (not a stack trace) for missing
  config, bad dates, or an unknown physician name.
- `backend/routers/scraper.py` — new FastAPI router: `POST
  /api/scraper/run` (kicks off a scrape in a background thread), `GET
  /api/scraper/status/{batch_id}`, `GET /api/scraper/log/{batch_id}`, `WS
  /api/scraper/ws/{batch_id}` for live progress, and `GET
  /api/scraper/config-check` so the UI can tell the person whether `.env`
  credentials are present without ever exposing them. Mirrors the existing
  `pipeline.py` router's log-buffer/websocket pattern for consistency.
- **Intake page** — new "01 · Fetch from Abronal" panel above the upload
  zones: From/To date pickers, an optional comma-separated physician-name
  filter, a "Fetch from Abronal" button, its own progress bar, status
  banner, and log window (reusing the same websocket-driven UI pattern as
  the reconciliation run). On success it auto-refreshes the Abronal
  upload-file list so the newly scraped files are visible immediately.
  Section numbers on the rest of the page were bumped (SoT upload is now
  "02", Abronal upload "03", Run Pipeline "04", System Log "05").
- `config.json` (new, at the project root) — non-secret scraper settings:
  `headless`, `patient_type`, `skip_physicians` (same shape as the old
  desktop tool's config, credentials removed).
- `.env.example` — template for `BASE_URL` / `USERNAME` / `PASSWORD` /
  `ROLE`, loaded via `python-dotenv`. Real `.env` is git-ignored/never
  bundled; credentials are read server-side only and never sent to the
  browser.

**Changed**
- `requirements.txt` — added `playwright` and `python-dotenv`.
- `backend/main.py` — registers the new `scraper` router at
  `/api/scraper`.
- `README.md` — documents the scraper as pipeline step 0, adds the
  one-time `playwright install chromium` setup step and `.env` setup
  instructions, updated project layout tree and CLI examples.

**Not changed**
- The reconciliation pipeline itself (`primary_reconciliation.py`,
  `secondary_name_matcher.py`, `category_merger.py`), the database schema,
  and the Evaluation page are untouched. Manually uploading Abronal
  `.xlsx` files still works exactly as before — the scraper is an optional
  shortcut that populates the same upload folder, not a replacement for
  the upload endpoint.

## v0.1 — Initial release

- Mended SQLite schema (`physicians`, `service_prices` reference tables
  with foreign keys into `abronal_mirror`, `sot_mirror`, `matched_records`,
  `unmatched_records`, `commission_per_physicians`) plus `db_manager.py` as
  the sole database access point.
- Neo-scripts: `primary_reconciliation.py`, `secondary_name_matcher.py`,
  `category_merger.py` (`Condensor`).
- FastAPI backend (`pipeline`, `tables`, `export` routers) and a two-page
  frontend: Intake & Run (upload, run, progress bar, log window) and
  Evaluation (table browser with per-column/date filters, export current
  table or full database history to Excel).
