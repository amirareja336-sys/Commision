-- ============================================================
-- commissions.db schema (mended)
-- Adds surrogate PKs, FK relations between physicians/services
-- and the mirror / matched / unmatched / commission tables.
-- ============================================================
PRAGMA foreign_keys = ON;

-- ── Reference tables ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS physicians (
    physician_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    physician_name  TEXT NOT NULL UNIQUE
);

-- One row per distinct service name. category is populated from
-- dictionary.json (Laboratory, X-ray, Ultrasound, ECG,
-- Echocardiography, Nursing & Procedures, Supplies, Consultation, Other)
CREATE TABLE IF NOT EXISTS service_prices (
    service_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    service_type    TEXT NOT NULL UNIQUE,
    category        TEXT NOT NULL DEFAULT 'Other',
    cost            NUMERIC NOT NULL DEFAULT 0
);

-- ── Raw scraped Abronal export rows (physician name comes from the
--    source .xlsx filename at parse time) ───────────────────────
CREATE TABLE IF NOT EXISTS abronal_mirror (
    row_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    row_number          INTEGER,
    card_number         TEXT,
    patient_full_name   TEXT NOT NULL,
    patient_type        TEXT,
    service_id          INTEGER REFERENCES service_prices(service_id),
    service_raw         TEXT,
    total                NUMERIC,
    net                  NUMERIC,
    commission_percent   REAL,
    commision_amount     REAL,
    payment_date         TEXT,
    visit_date           TEXT,
    status               TEXT,
    physician_id         INTEGER REFERENCES physicians(physician_id),
    source_file          TEXT,
    batch_id             TEXT
);

-- ── Raw SoT (source of truth) rows loaded from uploaded Excel ───
CREATE TABLE IF NOT EXISTS sot_mirror (
    row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer        TEXT NOT NULL,
    tin_number      TEXT,
    description     TEXT,
    item_id         TEXT,
    base_sku        TEXT,
    quantity        INTEGER,
    unit_price      NUMERIC,
    sub_total       NUMERIC,
    tax_amount      REAL,
    withholding     TEXT,
    fs_number       INTEGER,
    transaction_date TEXT,
    reference       TEXT,
    MRC             TEXT,
    service_id      INTEGER REFERENCES service_prices(service_id),
    source_file     TEXT,
    batch_id        TEXT
);

-- ── Reconciliation results ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS matched_records (
    match_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_name    TEXT NOT NULL,
    service_id      INTEGER REFERENCES service_prices(service_id),
    total_amount    REAL NOT NULL,
    net_amount      REAL NOT NULL,
    payment_date    TEXT NOT NULL,
    physician_id    INTEGER REFERENCES physicians(physician_id),
    physician_name  TEXT NOT NULL DEFAULT '',
    match_type      TEXT NOT NULL DEFAULT 'exact',   -- exact | fuzzy_name
    confidence      REAL,
    user_flagged_mismatch INTEGER NOT NULL DEFAULT 0,
    user_flag_reason TEXT DEFAULT NULL,
    abronal_row_id  INTEGER REFERENCES abronal_mirror(row_id),
    sot_row_id      INTEGER REFERENCES sot_mirror(row_id),
    batch_id        TEXT
);

CREATE TABLE IF NOT EXISTS unmatched_records (
    unmatched_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    abronal_patient_name    TEXT NOT NULL,
    abronal_service_type    TEXT NOT NULL,
    abronal_net_amount      NUMERIC NOT NULL,
    abronal_payment_date    TEXT NOT NULL,
    physician_id            INTEGER REFERENCES physicians(physician_id),
    physician_name          TEXT NOT NULL DEFAULT '',
    sot_patient_name        TEXT,
    sot_service_type        TEXT,
    sot_amount              NUMERIC,
    sot_payment_date        TEXT,
    reason_for_mismatch     TEXT NOT NULL,
    abronal_row_id          INTEGER REFERENCES abronal_mirror(row_id),
    sot_row_id               INTEGER REFERENCES sot_mirror(row_id),
    batch_id                TEXT
);

-- ── Category merger output: one row per patient/physician/date,
--    one column-total per service category ─────────────────────
CREATE TABLE IF NOT EXISTS commission_per_physicians (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    physician_id            INTEGER NOT NULL REFERENCES physicians(physician_id),
    physician_name          TEXT NOT NULL DEFAULT '',
    patient_name            TEXT NOT NULL,
    payment_date            TEXT,
    ultrasound              NUMERIC NOT NULL DEFAULT 0,
    laboratory              NUMERIC NOT NULL DEFAULT 0,
    "x-ray"                 NUMERIC NOT NULL DEFAULT 0,
    nursing_and_procedures  NUMERIC NOT NULL DEFAULT 0,
    consultation            NUMERIC NOT NULL DEFAULT 0,
    other                   NUMERIC NOT NULL DEFAULT 0,
    total                   NUMERIC NOT NULL DEFAULT 0,
    commision_percent       REAL NOT NULL DEFAULT 0,
    commision_amount        REAL NOT NULL DEFAULT 0,
    batch_id                TEXT
);

-- (pipeline_runs table removed — live run progress/log is handled
-- in-memory by the FastAPI routers via websocket, not persisted here)

-- ── Auth & access control ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL UNIQUE,
    pass_hash    TEXT NOT NULL,          -- "salt_hex$hash_hex" (PBKDF2-HMAC-SHA256)
    role         TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user')),
    created_at   TEXT NOT NULL
);

-- Per-user allow-list of Evaluation-page tables. Only consulted for
-- role='user' — admins always see every table. A 'user' with no rows
-- here falls back to the default restricted set (the mirror, matched,
-- and unmatched tables) applied at the application layer.
CREATE TABLE IF NOT EXISTS user_table_access (
    user_id      INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    table_name   TEXT NOT NULL,
    PRIMARY KEY (user_id, table_name)
);

-- Admin-editable commission rate per physician × service category.
CREATE TABLE IF NOT EXISTS physician_category_commision_rates (
    physician_id       INTEGER NOT NULL REFERENCES physicians(physician_id),
    category           TEXT NOT NULL,
    commission_percent REAL NOT NULL DEFAULT 0,
    updated_at         TEXT,
    PRIMARY KEY (physician_id, category)
);

CREATE INDEX IF NOT EXISTS idx_abronal_physician ON abronal_mirror(physician_id);
CREATE INDEX IF NOT EXISTS idx_abronal_service ON abronal_mirror(service_id);
CREATE INDEX IF NOT EXISTS idx_sot_service ON sot_mirror(service_id);
CREATE INDEX IF NOT EXISTS idx_matched_physician ON matched_records(physician_id);
CREATE INDEX IF NOT EXISTS idx_unmatched_physician ON unmatched_records(physician_id);
CREATE INDEX IF NOT EXISTS idx_commission_physician ON commission_per_physicians(physician_id);

-- ── Accountant report snapshots (submitted from the user Report page) ─
CREATE TABLE IF NOT EXISTS reports (
    report_row_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id           TEXT NOT NULL,
    submitted_by            INTEGER REFERENCES users(user_id),
    submitted_by_name       TEXT NOT NULL DEFAULT '',
    submitted_at            TEXT NOT NULL,
    match_id                INTEGER,
    physician_id            INTEGER,
    physician_name          TEXT NOT NULL DEFAULT '',
    patient_name            TEXT NOT NULL DEFAULT '',
    service_id              TEXT,
    total_amount            REAL NOT NULL DEFAULT 0,
    net_amount              REAL NOT NULL DEFAULT 0,
    payment_date            TEXT,
    match_type              TEXT,
    confidence              REAL,
    user_flagged_mismatch   INTEGER NOT NULL DEFAULT 0,
    user_flag_reason        TEXT,
    filter_physician        TEXT,
    filter_start_date       TEXT,
    filter_end_date         TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_physician ON reports(physician_name);
CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(payment_date);
CREATE INDEX IF NOT EXISTS idx_reports_submission ON reports(submission_id);
