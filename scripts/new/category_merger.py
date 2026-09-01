from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
import db_manager as dbm  # noqa: E402

DEFAULT_DICTIONARY_PATH = Path(__file__).resolve().parents[2] / "dictionary.json"

CATEGORY_COLUMN_MAP = dbm.CATEGORY_COLUMN_MAP  # Category label -> db column name


class Condensor:
    def __init__(self, dictionary_path: Path = DEFAULT_DICTIONARY_PATH, log=print):
        self.dictionary_path = Path(dictionary_path)
        self.log = log
        self.dictionary: dict[str, str] = {}
        self.data: list[dict] = []

    # ── step 1 ──────────────────────────────────────────────────
    def read_dictionary(self) -> dict:
        if self.dictionary_path.exists():
            self.dictionary = json.loads(self.dictionary_path.read_text(encoding="utf-8"))
        else:
            self.dictionary = {}
        self.log(f"Loaded {len(self.dictionary)} service->category rules from {self.dictionary_path.name}")
        return self.dictionary

    # ── step 2 ──────────────────────────────────────────────────
    def load_data(self, batch_id: str | None = None) -> list[dict]:
        query = """
            SELECT m.match_id, m.patient_name, m.net_amount, m.payment_date,
                   p.physician_name, p.physician_id,
                   sp.service_type, sp.category AS db_category
            FROM matched_records m
            JOIN physicians p ON p.physician_id = m.physician_id
            LEFT JOIN service_prices sp ON sp.service_id = m.service_id
        """
        params = []
        if batch_id:
            query += " WHERE m.batch_id = ?"
            params.append(batch_id)
        with dbm.get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        self.data = [dict(r) for r in rows]
        self.log(f"Loaded {len(self.data)} matched rows for condensing.")
        return self.data

    def _category_for(self, service_type: str, db_category: str | None) -> str:
        # Prefer live dictionary.json rule, fall back to the category
        # already stored on service_prices, then 'Other'.
        return self.dictionary.get(service_type) or db_category or "Other"

    # ── step 3 ──────────────────────────────────────────────────
    def list_condensor(self, batch_id: str | None = None) -> list[dict]:
        groups: dict[tuple, dict] = {}
        for row in self.data:
            date_key = dbm.normalize_date_to_iso(row["payment_date"]) or (row["payment_date"] or "")[:10]
            key = (row["physician_id"], row["patient_name"], date_key)
            category = self._category_for(row["service_type"], row["db_category"])
            column = CATEGORY_COLUMN_MAP.get(category, "other")

            g = groups.setdefault(key, {
                "physician_id": row["physician_id"],
                "physician_name": row["physician_name"],
                "patient_name": row["patient_name"],
                "payment_date": date_key,
                "ultrasound": 0.0, "laboratory": 0.0, "x-ray": 0.0,
                "nursing_and_procedures": 0.0, "consultation": 0.0, "other": 0.0,
            })
            g[column] += float(row["net_amount"] or 0)

        condensed = []
        for g in groups.values():
            total = (g["ultrasound"] + g["laboratory"] + g["x-ray"]
                      + g["nursing_and_procedures"] + g["consultation"] + g["other"])
            g["total"] = total
            # Admin-configured commission rate (Admin -> Commission
            # Rates page) is applied here so the exported/displayed
            # commission amount always reflects the current rate.
            rate = dbm.get_commission_rate(g["physician_id"])
            g["commision_percent"] = rate
            g["commision_amount"] = round(total * rate / 100.0, 2)
            condensed.append(g)

        # Sort by physician name (primary) then payment date (secondary) before insertion
        def sort_key(g):
            physician = (g.get("physician_name") or "").lower()
            date = dbm.normalize_date_to_iso(g.get("payment_date")) or "9999-99-99"
            patient = (g.get("patient_name") or "").lower()
            return (physician, date, patient)

        condensed_sorted = sorted(condensed, key=sort_key)

        with dbm.get_conn() as conn:
            if batch_id:
                conn.execute("DELETE FROM commission_per_physicians WHERE batch_id = ?", (batch_id,))
            for g in condensed_sorted:
                row_key = {
                    "physician_id": g["physician_id"],
                    "physician_name": g["physician_name"],
                    "patient_name": g["patient_name"],
                    "payment_date": g["payment_date"],
                }
                if dbm.row_exists(conn, "commission_per_physicians", row_key):
                    continue
                conn.execute(
                    """INSERT INTO commission_per_physicians
                       (physician_id, physician_name, patient_name, payment_date, ultrasound, laboratory,
                        "x-ray", nursing_and_procedures, consultation, other, total,
                        commision_percent, commision_amount, batch_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (g["physician_id"], g["physician_name"], g["patient_name"], g["payment_date"], g["ultrasound"],
                     g["laboratory"], g["x-ray"], g["nursing_and_procedures"], g["consultation"],
                     g["other"], g["total"], g["commision_percent"], g["commision_amount"], batch_id),
                )
        self.log(f"Condensed into {len(condensed)} patient/date rows -> commission_per_physicians.")
        return condensed_sorted


def run(batch_id: str | None, dictionary_path: Path = DEFAULT_DICTIONARY_PATH, log=print):
    c = Condensor(dictionary_path, log)
    c.read_dictionary()
    c.load_data(batch_id)
    return c.list_condensor(batch_id)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default=None)
    parser.add_argument("--dictionary", default=str(DEFAULT_DICTIONARY_PATH))
    args = parser.parse_args()
    result = run(args.batch, Path(args.dictionary))
    print(f"{len(result)} condensed rows written.")
