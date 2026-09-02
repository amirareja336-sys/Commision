from __future__ import annotations

import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
import db_manager as dbm  # noqa: E402

NAME_SIMILARITY_THRESHOLD = 0.70
DATE_TOLERANCE_DAYS = 1
AMOUNT_TOLERANCE = 0.01


def _parse_date(value):
    iso = dbm.normalize_date_to_iso(value)
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return None


def load_mismatched_data(batch_id: str | None, log=print):
    with dbm.get_conn() as conn:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM unmatched_records WHERE batch_id = ?", (batch_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM unmatched_records").fetchall()
    rows = [dict(r) for r in rows]

    abr_side = [r for r in rows if r["abronal_row_id"] and not r["sot_row_id"]]
    sot_side = [r for r in rows if r["sot_row_id"] and not r["abronal_row_id"]]
    log(f"Loaded {len(abr_side)} unmatched Abronal rows and {len(sot_side)} unmatched SoT rows.")
    return abr_side, sot_side


def name_comparator(abr_side, sot_side, log=print):
    buffer = []
    consumed_sot = set()
    still_abr = []

    for a in abr_side:
        best, best_score = None, 0.0
        a_date = _parse_date(a.get("abronal_payment_date"))
        for s in sot_side:
            if s["unmatched_id"] in consumed_sot:
                continue
            if abs(float(a["abronal_net_amount"] or 0) - float(s["sot_amount"] or 0)) >= AMOUNT_TOLERANCE:
                continue
            s_date = _parse_date(s.get("sot_payment_date"))
            if a_date is not None and s_date is not None:
                if abs((a_date - s_date).days) > DATE_TOLERANCE_DAYS:
                    continue
            score = SequenceMatcher(None, str(a["abronal_patient_name"]).upper(),
                                     str(s["sot_patient_name"]).upper()).ratio()
            if score > best_score:
                best_score, best = score, s

        if best is not None and best_score >= NAME_SIMILARITY_THRESHOLD:
            consumed_sot.add(best["unmatched_id"])
            buffer.append({
                "abronal_row": a,
                "sot_row": best,
                "renamed_to": a["abronal_patient_name"],   # rename based on Abronal name
                "confidence": round(best_score, 4),
            })
        else:
            still_abr.append(a)

    still_sot = [s for s in sot_side if s["unmatched_id"] not in consumed_sot]
    log(f"Name comparator: {len(buffer)} candidate pairs satisfy the "
        f"{int(NAME_SIMILARITY_THRESHOLD*100)}% similarity + amount + ±{DATE_TOLERANCE_DAYS}d rules.")
    return buffer, still_abr, still_sot


def grafter(buffer, batch_id, log=print):
    grafted = 0
    # Sort by payment date for chronological insertion order
    buffer_sorted = sorted(buffer, key=lambda p: dbm.normalize_date_to_iso(
        p["abronal_row"].get("abronal_payment_date")) or "9999-99-99")

    with dbm.get_conn() as conn:
        for pair in buffer_sorted:
            a = pair["abronal_row"]
            s = pair["sot_row"]

            service_id = None
            svc_row = conn.execute(
                "SELECT service_id FROM service_prices WHERE service_type = ?",
                (a["abronal_service_type"],),
            ).fetchone()
            if svc_row:
                service_id = svc_row["service_id"]
            else:
                service_id = dbm.get_or_create_service(conn, a["abronal_service_type"])

            # Lookup physician name from physician_id
            physician_name = ""
            if a["physician_id"]:
                phys_row = conn.execute(
                    "SELECT physician_name FROM physicians WHERE physician_id = ?",
                    (a["physician_id"],),
                ).fetchone()
                if phys_row:
                    physician_name = phys_row["physician_name"]

            match_key = {
                "patient_name": pair["renamed_to"],
                "service_id": service_id,
                "net_amount": a["abronal_net_amount"],
                "payment_date": a["abronal_payment_date"],
                "physician_id": a["physician_id"],
                "abronal_row_id": a["abronal_row_id"],
                "sot_row_id": s["sot_row_id"],
            }
            if dbm.row_exists(conn, "matched_records", match_key):
                conn.execute("DELETE FROM unmatched_records WHERE unmatched_id = ?", (a["unmatched_id"],))
                conn.execute("DELETE FROM unmatched_records WHERE unmatched_id = ?", (s["unmatched_id"],))
                continue

            conn.execute(
                """INSERT INTO matched_records
                   (patient_name, service_id, total_amount, net_amount, payment_date,
                    physician_id, physician_name, match_type, confidence, user_flagged_mismatch,
                    user_flag_reason, abronal_row_id, sot_row_id, batch_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pair["renamed_to"], service_id, a["abronal_net_amount"], a["abronal_net_amount"],
                 a["abronal_payment_date"], a["physician_id"], physician_name, "fuzzy_name",
                 pair["confidence"], 0, None, a["abronal_row_id"], s["sot_row_id"], batch_id),
            )
            conn.execute("DELETE FROM unmatched_records WHERE unmatched_id = ?", (a["unmatched_id"],))
            conn.execute("DELETE FROM unmatched_records WHERE unmatched_id = ?", (s["unmatched_id"],))
            grafted += 1
    log(f"Grafted {grafted} fuzzy-name matches into matched_records.")
    return grafted


def run(batch_id: str | None, log=print):
    log("── Secondary Name Matcher ──")
    abr_side, sot_side = load_mismatched_data(batch_id, log)
    buffer, still_abr, still_sot = name_comparator(abr_side, sot_side, log)
    grafted = grafter(buffer, batch_id or "manual", log)
    log(f"Remaining genuinely unreconciled: {len(still_abr)} Abronal, {len(still_sot)} SoT.")
    return {"grafted": grafted, "still_unreconciled_abr": len(still_abr), "still_unreconciled_sot": len(still_sot)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default=None, help="Batch id to restrict to (default: all rows)")
    args = parser.parse_args()
    result = run(args.batch)
    print(result)
