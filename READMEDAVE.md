READMEDAVE - Developer Test Mode Operations
=========================================

Overview
--------
This project includes a "test mode" to make reconciliation testing easier. When you run the app against a separate database (not the repository default `db/commissions.db`), an interactive Test Mode UI is available at `/testmode`.

Quick start (Windows)
---------------------
1. From the project root:

```bat
start.bat --inline
```

If `data\test_1_1\test.db` is missing, the script asks whether to create a development test DB and prints a `dev` password.

2. Log in as `dev`, then open http://localhost:8000/testmode

Or set a scenario first:

```bat
set SCENARIO=1.2
start.bat --inline
```

Quick start (Linux / macOS)
---------------------------
```bash
./start.sh
# or
SCENARIO=1.2 ./start.sh
```

Manual generator
----------------
```bash
python "tests/test file generator/generate_test_data.py" --scenario 1.1
# optional: sample real rows from db/commissions.db
python "tests/test file generator/generate_test_data.py" --scenario 1.1 --from-prod --start-date 2026-08-01 --end-date 2026-08-15
```

Then start against that file:

```bash
set COMMISSIONS_DB=./data/test_1_1/test.db
# or
COMMISSIONS_DB=./data/test_1_1/test.db uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

`COMMISSIONS_DB` may be a file path or a directory containing `test.db`.

What the Test UI provides
-------------------------
- Sidebar: load `data/test_*` SQLite packs, import CSV packs, generate scenarios (synthetic or from production), reset schema.
- Reconcile: runs the same primary → secondary → category-merger pipeline against current mirror tables (no Excel upload required).
- Metrics + per-table clear.
- Upload SQLite/zip datasets and per-table CSVs.

Safety
------
- Test Mode APIs and UI only activate when `COMMISSIONS_DB` is **not** `db/commissions.db`.
- Loading a dataset overwrites the active DB file. Never point `COMMISSIONS_DB` at production when using Test Mode.

CSV datasets
------------
Store packs under `data/csv_datasets/<name>/` with files named after tables (`abronal_mirror.csv`, `sot_mirror.csv`, …). Column headers must match the table columns.
