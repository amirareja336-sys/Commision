from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


class ColumnAdapterError(Exception):
    pass


# ── Report-footer boilerplate to strip ────────────────────────────
# SoT (and some Abronal) exports end with a trailing summary/branding
# line rather than real data, e.g.:
#   "2026-07-10 00:00:00   For Managment Purposes Only   Powered by MarakiErp(Beta) - 2012"
# Any row whose cells contain one of these phrases (case-insensitive,
# substring match — deliberately including the source's own "Managment"
# typo) is dropped before it reaches the rest of the pipeline.
FOOTER_MARKERS = [
    "for managment purposes only",
    "for management purposes only",
    "powered by marakierp",
]


def _is_footer_row(values) -> bool:
    joined = " ".join(str(v) for v in values if v is not None and str(v).lower() != "nan").lower()
    return any(marker in joined for marker in FOOTER_MARKERS)


# ── Canonical field -> accepted header spellings ─────────────────
# (all matching is case/punctuation/underscore-insensitive, see
#  _normalize_header below, so "Tin_no", "TIN No.", "tin number"
#  all resolve to the same alias key)

ABRONAL_SCHEMA: dict[str, list[str]] = {
    "row_number":          ["#", "no", "row no", "sequence", "sl no"],
    "card_number":         ["card #", "card no", "card number", "card"],
    "patient_full_name":   ["patient full name", "patient name", "full name", "customer", "name"],
    "patient_type":        ["patient type", "type"],
    "service_raw":         ["service", "service type", "procedure"],
    "total":               ["total", "amount", "gross"],
    "net":                 ["net", "net amount"],
    "commission_percent":  ["com (%)", "com %", "com", "commission", "commission %",
                             "commission percent", "commission rate"],
    "commision_amount":    ["com amt", "com amount", "commission amt", "commission amount"],
    "payment_date":        ["payment date", "paid date"],
    "visit_date":          ["visit date", "sot date", "service date"],
    "status":              ["status"],
}

SOT_SCHEMA: dict[str, list[str]] = {
    "customer":            ["customer", "patient", "patient name", "name"],
    "tin_number":          ["tin_no", "tin no", "tin number", "tin"],
    "description":         ["description", "service", "item description"],
    "item_id":             ["item id", "item"],
    "quantity":            ["quantity", "qty"],
    "base_sku":            ["base_sku", "base sku", "sku"],
    "unit_price":          ["unit price", "price"],
    "sub_total":           ["subtotal", "sub total", "sub_total", "amount"],
    "tax_amount":          ["tax_amt", "tax amt", "tax amount", "tax"],
    "withholding":         ["withholding", "wht"],
    "fs_number":           ["fs_no", "fs no", "fs number"],
    "transaction_date":    ["transaction date", "date", "trans date"],
    "reference":           ["reference", "ref"],
    "mrc":                 ["mrc"],
}


def _normalize_header(value) -> str:
    if value is None:
        return ""
    s = str(value)
    s = s.replace("_", " ")
    s = s.replace("#", " number ")           # '#' carries meaning ("row number") — don't just strip it
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s)    # strip remaining punctuation: '.', '(', ')', '%'...
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _build_lookup(schema: dict[str, list[str]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, aliases in schema.items():
        for alias in aliases + [canonical]:
            key = _normalize_header(alias)
            if key:                          # never let a blank/empty header match anything
                lookup.setdefault(key, canonical)
    return lookup


ABRONAL_LOOKUP = _build_lookup(ABRONAL_SCHEMA)
SOT_LOOKUP = _build_lookup(SOT_SCHEMA)


def _find_header_row(raw: pd.DataFrame, lookup: dict[str, str],
                      min_hits: int = 4, max_scan: int = 20) -> int | None:
    best_row, best_hits = None, 0
    for i in range(min(max_scan, len(raw))):
        row_vals = [_normalize_header(v) for v in raw.iloc[i].tolist()]
        hits = sum(1 for v in row_vals if v and v in lookup)
        if hits > best_hits:
            best_row, best_hits = i, hits
    if best_hits >= min_hits:
        return best_row
    return None


def _read_raw_sheet(path: str | Path, sheet_name=0) -> pd.DataFrame:
    suffix = Path(path).suffix.lower()
    if suffix in (".csv", ".tsv", ".txt"):
        try:
            return pd.read_csv(path, header=None, sep=None, engine="python")
        except UnicodeDecodeError:
            return pd.read_csv(path, header=None, sep=None, engine="python", encoding="latin-1")
    return pd.read_excel(path, header=None, sheet_name=sheet_name)


def adapt_sheet(path: str | Path, schema: dict[str, list[str]], *,
                 sheet_name=0, min_hits: int = 4, log=None) -> pd.DataFrame:
    lookup = _build_lookup(schema)
    raw = _read_raw_sheet(path, sheet_name=sheet_name)

    # Strip boilerplate footer rows up front so they can never be
    # mistaken for a header and never survive into the parsed data.
    footer_mask = raw.apply(lambda r: _is_footer_row(r.tolist()), axis=1)
    dropped_footers = int(footer_mask.sum())
    if dropped_footers:
        raw = raw.loc[~footer_mask].reset_index(drop=True)

    header_row = _find_header_row(raw, lookup, min_hits=min_hits)
    if header_row is None:
        raise ColumnAdapterError(
            f"Could not find a recognizable header row in {Path(path).name} "
            f"(scanned first {min(20, len(raw))} rows, needed >= {min_hits} known columns)."
        )

    headers = raw.iloc[header_row].tolist()
    data = raw.iloc[header_row + 1:].reset_index(drop=True)
    data.columns = headers

    rename_map, unmapped, seen = {}, [], set()
    for col in data.columns:
        canonical = lookup.get(_normalize_header(col))
        if canonical and canonical not in seen:
            rename_map[col] = canonical
            seen.add(canonical)
        else:
            unmapped.append(col)
    data = data.rename(columns=rename_map)
    data = data.dropna(how="all")

    if log:
        missing = [c for c in schema if c not in seen]
        log(f"  Column adapter: header row {header_row} in {Path(path).name} — "
            f"{len(seen)}/{len(schema)} canonical columns matched.")
        if dropped_footers:
            log(f"    Filtered out {dropped_footers} boilerplate footer row(s).")
        if missing:
            log(f"    Not present in this file (will default to blank/0): {missing}")
        real_unmapped = [u for u in unmapped if str(u).strip() and not str(u).lower().startswith("unnamed")]
        if real_unmapped:
            log(f"    Unrecognized extra column(s) kept as-is: {real_unmapped}")

    return data

# ── Typed getters: pull one field off a row Series, tolerating a
#    canonical column that's absent from this particular file ──────

def get_str(row: pd.Series, field: str, default: str = "") -> str:
    if field not in row.index:
        return default
    val = row[field]
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    s = str(val).strip()
    return default if s.lower() in ("nan", "none", "") else s


def get_float(row: pd.Series, field: str, default: float = 0.0) -> float:
    if field not in row.index:
        return default
    val = row[field]
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        if isinstance(val, str):
            val = val.replace(",", "").replace("$", "").replace("%", "").strip()
            if val == "":
                return default
        return float(val)
    except (TypeError, ValueError):
        return default


def get_int(row: pd.Series, field: str, default: int = 0) -> int:
    return int(get_float(row, field, default))
