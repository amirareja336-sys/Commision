"""Discover service names in uploaded Abronal/SoT files that are missing
from dictionary.json (would otherwise be created as category Other)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
import db_manager as dbm  # noqa: E402
import primary_reconciliation as primary  # noqa: E402


def discover_new_services(abr_dir: str | Path, sot_dir: str | Path, log=print) -> dict:
    """Parse upload dirs and return services not yet in dictionary.json."""
    batch = "discover"
    silent = log if log is not print else (lambda _m: None)
    abr_rows = primary.parse_abronal_dir(str(abr_dir), batch, silent)
    sot_rows = primary.parse_sot_dir(str(sot_dir), batch, silent)

    found: set[str] = set()
    sources: dict[str, set[str]] = {}
    for r in abr_rows:
        name = (r.get("service_raw") or "").strip()
        if name:
            found.add(name)
            sources.setdefault(name, set()).add("abronal")
    for r in sot_rows:
        name = (r.get("description") or "").strip()
        if name:
            found.add(name)
            sources.setdefault(name, set()).add("sot")

    dictionary = dbm.load_dictionary()
    new_services = sorted(
        (s for s in found if s not in dictionary),
        key=lambda s: s.lower(),
    )
    log(f"Service discovery: {len(found)} distinct in uploads, {len(new_services)} new.")
    return {
        "new_services": [
            {"service": s, "sources": sorted(sources.get(s, []))}
            for s in new_services
        ],
        "known_in_uploads": len(found) - len(new_services),
        "total_in_uploads": len(found),
        "categories": list(dbm.VALID_CATEGORIES),
    }
