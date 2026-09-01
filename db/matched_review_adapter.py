from __future__ import annotations

import json
from pathlib import Path

import db_manager as dbm  # noqa: E402

APP_ROOT = Path(__file__).resolve().parents[1]
TEMP_DIR = APP_ROOT / "temp"
CACHE_PATH = TEMP_DIR / "matched_review.json"
DICTIONARY_PATH = APP_ROOT / "dictionary.json"

# Columns kept for the accountant-facing view. Flag is a UI control, not a
# stored field. service_id is rewritten to the service category name after
# rows for the same patient + physician + category are merged.
REVIEW_COLUMNS = [
    "match_id",
    "physician_id",
    "physician_name",
    "patient_name",
    "service_id",
    "total_amount",
    "net_amount",
    "match_type",
    "confidence",
    "user_flagged_mismatch",
    "user_flag_reason",
]

# Categories omitted from the accountant review / Report table.
HIDDEN_REVIEW_CATEGORIES = frozenset({"Nursing & Procedures"})


def _load_dictionary() -> dict[str, str]:
    if not DICTIONARY_PATH.exists():
        return {}
    try:
        data = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _category_for(service_type: str | None, db_category: str | None, dictionary: dict[str, str]) -> str:
    if service_type and service_type in dictionary:
        cat = dictionary[service_type]
        return cat if cat in dbm.VALID_CATEGORIES else "Other"
    if db_category in dbm.VALID_CATEGORIES:
        return db_category
    return db_category or "Other"


def adapter(matched_rows: list[dict]) -> list[dict]:
    """Keep only the accountant-review columns (plus fields the merger needs)."""
    keep = set(REVIEW_COLUMNS) | {"payment_date", "service_type", "db_category"}
    projected = []
    for row in matched_rows:
        projected.append({col: row.get(col) for col in keep if col in row or col in REVIEW_COLUMNS})
    return projected


def _earliest_payment_iso(row: dict) -> str:
    dates = []
    for value in row.get("payment_dates") or []:
        iso = dbm.normalize_date_to_iso(value)
        if iso:
            dates.append(iso)
    return min(dates) if dates else "9999-99-99"


def id_to_name_merger(
    rows: list[dict],
    dictionary: dict[str, str] | None = None,
    *,
    group_by_date: bool = False,
) -> list[dict]:
    """Merge rows that share patient, physician, and service category.

    service_id is replaced with the category name. Amounts are summed;
    a merged row is flagged if any source row was flagged.

    When group_by_date is True (user date-range filter), rows on different
    days stay separate and the table is ordered by ascending payment date.
    """
    dictionary = dictionary if dictionary is not None else _load_dictionary()
    groups: dict[tuple, dict] = {}

    for row in rows:
        category = _category_for(row.get("service_type"), row.get("db_category"), dictionary)
        if category in HIDDEN_REVIEW_CATEGORIES:
            continue
        physician = (row.get("physician_name") or "").strip()
        patient = (row.get("patient_name") or "").strip()
        payment_date = row.get("payment_date")
        date_key = dbm.normalize_date_to_iso(payment_date) or ""
        if group_by_date:
            key = (date_key, physician.lower(), patient.lower(), category)
        else:
            key = (physician.lower(), patient.lower(), category)

        match_id = row.get("match_id")
        flagged = 1 if row.get("user_flagged_mismatch") in (1, True, "1") else 0
        reason = (row.get("user_flag_reason") or "").strip()
        match_type = row.get("match_type") or "exact"
        confidence = row.get("confidence")
        try:
            total = float(row.get("total_amount") or 0)
        except (TypeError, ValueError):
            total = 0.0
        try:
            net = float(row.get("net_amount") or 0)
        except (TypeError, ValueError):
            net = 0.0

        g = groups.get(key)
        if g is None:
            groups[key] = {
                "match_id": match_id,
                "physician_id": row.get("physician_id"),
                "physician_name": physician,
                "patient_name": patient,
                "service_id": category,
                "total_amount": total,
                "net_amount": net,
                "match_type": match_type,
                "confidence": confidence,
                "user_flagged_mismatch": flagged,
                "user_flag_reason": reason or None,
                "payment_dates": [payment_date] if payment_date else [],
                "source_match_ids": [match_id] if match_id is not None else [],
                "_match_types": {match_type},
                "_confidences": [confidence] if confidence is not None else [],
                "_reasons": [reason] if reason else [],
            }
            continue

        g["total_amount"] += total
        g["net_amount"] += net
        if match_id is not None:
            g["source_match_ids"].append(match_id)
            if g["match_id"] is None or match_id < g["match_id"]:
                g["match_id"] = match_id
        if payment_date:
            g["payment_dates"].append(payment_date)
        if flagged:
            g["user_flagged_mismatch"] = 1
        if reason and reason not in g["_reasons"]:
            g["_reasons"].append(reason)
        g["_match_types"].add(match_type)
        if confidence is not None:
            g["_confidences"].append(confidence)

    condensed = []
    for g in groups.values():
        types = g.pop("_match_types")
        confs = g.pop("_confidences")
        reasons = g.pop("_reasons")
        g["match_type"] = next(iter(types)) if len(types) == 1 else "merged"
        g["confidence"] = round(min(confs), 4) if confs else None
        g["user_flag_reason"] = "; ".join(reasons) if reasons else None
        g["total_amount"] = round(g["total_amount"], 2)
        g["net_amount"] = round(g["net_amount"], 2)
        condensed.append(g)

    if group_by_date:
        condensed.sort(key=lambda r: (
            _earliest_payment_iso(r),
            (r.get("physician_name") or "").lower(),
            (r.get("patient_name") or "").lower(),
            (r.get("service_id") or "").lower(),
            r.get("match_id") or 0,
        ))
    else:
        condensed.sort(key=lambda r: (
            (r.get("physician_name") or "").lower(),
            (r.get("patient_name") or "").lower(),
            (r.get("service_id") or "").lower(),
            r.get("match_id") or 0,
        ))
    return condensed


def _load_matched_rows(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    where, params = dbm._build_where(
        "matched_records", None, "payment_date", start_date, end_date,
    )
    query = f"""
        SELECT m.match_id, m.physician_id, m.physician_name, m.patient_name,
               m.service_id, m.total_amount, m.net_amount, m.match_type,
               m.confidence, m.user_flagged_mismatch, m.user_flag_reason,
               m.payment_date, sp.service_type, sp.category AS db_category
        FROM matched_records m
        LEFT JOIN service_prices sp ON sp.service_id = m.service_id
        {where}
    """
    with dbm.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def build_review_table(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    raw = _load_matched_rows(start_date, end_date)
    return id_to_name_merger(
        adapter(raw),
        _load_dictionary(),
        group_by_date=bool(start_date or end_date),
    )


def invalidate_review_cache() -> None:
    try:
        CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _cache_is_fresh() -> bool:
    if not CACHE_PATH.exists():
        return False
    try:
        db_mtime = dbm.DB_PATH.stat().st_mtime if dbm.DB_PATH.exists() else 0
        return CACHE_PATH.stat().st_mtime >= db_mtime
    except OSError:
        return False


def _write_cache(rows: list[dict]) -> None:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"rows": rows}
    CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_cache() -> list[dict] | None:
    if not _cache_is_fresh():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = data.get("rows") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else None


def get_review_rows(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """Return condensed review rows. Unfiltered results are cached in temp/."""
    if start_date or end_date:
        rows = build_review_table(start_date, end_date)
    else:
        cached = _read_cache()
        if cached is not None:
            rows = cached
        else:
            rows = build_review_table()
            _write_cache(rows)
    return [
        r for r in rows
        if (r.get("service_id") or "").strip() not in HIDDEN_REVIEW_CATEGORIES
    ]


def _like_match(value, needle: str) -> bool:
    hay = "" if value is None else str(value)
    return needle.lower() in hay.lower()


def _row_in_filters(row: dict, filters: dict | None) -> bool:
    if not filters:
        return True
    for col, val in filters.items():
        if val in (None, "", "All"):
            continue
        if not _like_match(row.get(col), str(val)):
            return False
    return True


def _public_row(row: dict, include_payment_date: bool = False) -> dict:
    public = {col: row.get(col) for col in REVIEW_COLUMNS}
    if include_payment_date:
        dates = []
        for value in row.get("payment_dates") or []:
            iso = dbm.normalize_date_to_iso(value)
            dates.append(iso or str(value))
        public["payment_date"] = min(dates) if dates else None
    return public


def fetch_review(filters: dict | None = None, limit: int = 1000, offset: int = 0,
                 start_date: str | None = None, end_date: str | None = None,
                 include_payment_date: bool = False) -> list[dict]:
    rows = [r for r in get_review_rows(start_date, end_date) if _row_in_filters(r, filters)]
    start = max(int(offset), 0)
    end = start + max(int(limit), 0)
    return [_public_row(r, include_payment_date=include_payment_date) for r in rows[start:end]]


def count_review(filters: dict | None = None, start_date: str | None = None,
                 end_date: str | None = None) -> int:
    return sum(1 for r in get_review_rows(start_date, end_date) if _row_in_filters(r, filters))


def distinct_review(column: str, limit: int = 1000) -> list:
    if column not in REVIEW_COLUMNS:
        raise ValueError(f"Unknown column: matched_records.{column}")
    seen = []
    found = set()
    for row in get_review_rows():
        value = row.get(column)
        if value in (None, ""):
            continue
        key = str(value)
        if key in found:
            continue
        found.add(key)
        seen.append(value)
        if len(seen) >= limit:
            break
    return seen


def source_match_ids_for(match_id: int) -> list[int]:
    """All original match_ids folded into a condensed review row."""
    for row in get_review_rows():
        if row.get("match_id") == match_id:
            ids = [i for i in (row.get("source_match_ids") or []) if i is not None]
            return ids or [match_id]
    return [match_id]
