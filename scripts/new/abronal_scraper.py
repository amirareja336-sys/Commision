from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

APP_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = APP_ROOT / ".env"
CONFIG_PATH = APP_ROOT / "config.json"
UPLOAD_ABR_DIR = APP_ROOT / "data" / "uploads" / "abronal"
tempfile =  APP_ROOT / "data" / "temp"

load_dotenv(ENV_PATH)


class ScraperError(Exception):
    pass


# ── Config ───────────────────────────────────────────────────────


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


class ScraperConfig:
    def __init__(self):
        self._data = _load_config()

    @staticmethod
    def _resolve(env_var: str) -> str | None:
        raw = os.getenv(env_var)
        if raw is None:
            return None
        try:
            sys.path.insert(0, str(APP_ROOT / "security"))
            import crypto  # noqa: E402
            return crypto.resolve_env_value(raw)
        except ImportError:
            return raw  # cryptography package not installed — plaintext only

    @property
    def base_url(self) -> str:
        v = self._resolve("BASE_URL")
        if not v:
            raise ScraperError("BASE_URL is not set (add it to .env)")
        return v

    @property
    def username(self) -> str:
        v = self._resolve("USERNAME")
        if not v:
            raise ScraperError("USERNAME is not set (add it to .env)")
        return v

    @property
    def password(self) -> str:
        v = self._resolve("PASSWORD")
        if not v:
            raise ScraperError("PASSWORD is not set (add it to .env)")
        return v

    @property
    def role(self) -> str:
        v = self._resolve("ROLE")
        if not v:
            raise ScraperError("ROLE is not set (add it to .env)")
        return v

    @property
    def headless(self) -> bool:
        return bool(self._data.get("headless", True))

    @property
    def patient_type(self) -> str:
        return self._data.get("patient_type", "OPD")

    @property
    def skip_physicians(self) -> list[str]:
        return list(self._data.get("skip_physicians", []))

    @staticmethod
    def has_credentials() -> bool:
        return all(os.getenv(k) for k in ("BASE_URL", "USERNAME", "PASSWORD", "ROLE"))


# ── Value objects (trimmed from export_physician_performance.py) ─

class DateRange:
    def __init__(self, from_date: date, to_date: date):
        if from_date > to_date:
            raise ScraperError("From date must be on or before To date.")
        self.from_date = from_date
        self.to_date = to_date

    @staticmethod
    def _format_day(d: date) -> str:
        return f"{d.strftime('%B')} {d.day}"

    def label(self) -> str:
        return f"{self._format_day(self.from_date)} to {self._format_day(self.to_date)}"

    @classmethod
    def from_iso(cls, from_iso: str, to_iso: str) -> "DateRange":
        try:
            f = datetime.strptime(from_iso, "%Y-%m-%d").date()
            t = datetime.strptime(to_iso, "%Y-%m-%d").date()
        except ValueError as e:
            raise ScraperError(f"Dates must be YYYY-MM-DD: {e}") from e
        return cls(f, t)


class Physician:
    _ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*]')
    _DR_PREFIX = re.compile(r"(?i)^dr\.?\s")

    def __init__(self, value: str, label: str):
        self.value = value
        self.label = label

    @property
    def display_name(self) -> str:
        name = re.sub(r"\s+", " ", self.label.strip().rstrip(".").strip())
        if not self._DR_PREFIX.match(name):
            name = f"Dr. {name}"
        return self._ILLEGAL_CHARS.sub("-", name)


@dataclass
class ScrapeResult:
    saved: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


# ── Browser automation ────────────────────────────────────────────

class AbronalSession:
    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url.rstrip("/")

    def login(self, username: str, password: str, role: str) -> None:
        page = self.page
        page.goto(self.base_url, wait_until="domcontentloaded")
        page.fill("#username", username)
        page.fill("#password", password)
        page.click("button[type='submit']")

        # Wait for Abronal's post-login redirect to the role-selection page.
        # Using wait_for_url (same as the working standalone script) is more
        # reliable than networkidle: networkidle on a busy server can time out
        # even when the page loaded correctly, and swallowing that timeout means
        # we silently proceed and then hit a confusing "#selRole not found" error
        # instead of a clean "bad credentials" message
        try:
            page.wait_for_url("**/Account/LoginAs**", timeout=30_000)
        except PlaywrightTimeout as exc:
            # Take a screenshot so the debug folder captures exactly what the
            # browser sees when login fails (mirrors the working script's behaviour).
            _debug_dir = APP_ROOT / "data" / "scraper_debug"
            _debug_dir.mkdir(parents=True, exist_ok=True)
            _ts = __import__("datetime").datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            page.screenshot(path=str(_debug_dir / f"login_failure_{_ts}.png"))
            current = page.url
            raise PlaywrightTimeout(
                f"Role-selection page did not appear after login "
                f"(current URL: {current}). "
                f"Check BASE_URL, USERNAME, and PASSWORD in .env."
            ) from exc

        page.wait_for_selector("#selRole")
        page.select_option("#selRole", label=role)
        page.evaluate(
            """() => { if (window.jQuery) window.jQuery('#selRole').trigger('change'); }"""
        )
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)

    def open_report(self) -> None:
        self.page.goto(f"{self.base_url}/Reports/PhysicianPerformance", wait_until="domcontentloaded")
        self.page.wait_for_selector("#fromDate")
        self.page.wait_for_selector("#cardiologist")
        self.page.wait_for_timeout(1000)

    def list_physicians(self) -> list[Physician]:
        options = self.page.eval_on_selector_all(
            "#cardiologist option",
            """els => els.map(e => ({ value: String(e.value || '').trim(),
                                       text: (e.textContent || '').trim() }))""",
        )
        return [Physician(o["value"], o["text"]) for o in options if o["value"] and o["text"]]

    def _select2_set(self, select_id: str, value: str) -> None:
        self.page.evaluate(
            """({ selectId, value }) => { window.jQuery('#' + selectId).val(value).trigger('change'); }""",
            {"selectId": select_id, "value": value},
        )

    def export_one(self, *, physician: Physician, patient_type: str,
                    date_range: DateRange, out_dir: Path) -> Path:
        page = self.page
        page.fill("#fromDate", date_range.from_date.isoformat())
        page.fill("#toDate", date_range.to_date.isoformat())
        page.fill("#fromTime", "00:00")
        page.fill("#toTime", "23:59")

        ptype_value = "opd" if patient_type.strip().upper() == "OPD" else "ipd"
        self._select2_set("pType", ptype_value)
        self._select2_set("cardiologist", physician.value)

        with page.expect_response(
            lambda r: "/Reports/GetPhysicianPerformance" in r.url and r.ok, timeout=60_000
        ):
            page.click("#show")
        page.wait_for_timeout(800)

        out_dir.mkdir(parents=True, exist_ok=True)
        # Physician-first naming, matching how Abronal itself names
        # these exports in practice (e.g. "Dr. Bart Jacobs July 1-9.xlsx"),
        # which is also what primary_reconciliation.py's
        # physician_from_filename() is written to expect first.
        target = out_dir / f"{physician.display_name} {date_range.label()}.xlsx"

        with page.expect_download(timeout=60_000) as dl_info:
            page.click("button.buttons-excel")
        dl_info.value.save_as(target)
        return target


def _should_skip(name: str, skip_names: list[str]) -> bool:
    name_l = name.lower().strip()
    for skip in skip_names:
        s = skip.lower().strip()
        if s and (name_l == s or s in name_l):
            return True
    return False


def _resolve_one(options: list[Physician], needle: str) -> Physician:
    """Match a physician option by name with 3-tier fallback (same logic as
    the working standalone export_physician_performance.py):
      1. exact match (stripped, case-insensitive, trailing dots ignored)
      2. starts-with match (only if unique)
      3. contains match (shortest label wins)
    Raises ScraperError if nothing matches."""
    needle_l = needle.lower().strip()

    exact = [
        o for o in options
        if o.label.lower().rstrip(". ").strip() == needle_l.rstrip(". ").strip()
        or o.label.lower().strip() == needle_l
    ]
    if exact:
        return exact[0]

    starts = [o for o in options if o.label.lower().startswith(needle_l)]
    if len(starts) == 1:
        return starts[0]

    contains = [o for o in options if needle_l in o.label.lower()]
    if contains:
        contains.sort(key=lambda o: len(o.label))
        return contains[0]

    raise ScraperError(f"Physician not found in dropdown: {needle!r}")


def _resolve_targets(options: list[Physician], skip_names: list[str],
                      physicians: list[str] | None) -> list[Physician]:
    if physicians:
        return [_resolve_one(options, n) for n in physicians]
    return [o for o in options if not _should_skip(o.label, skip_names)]


# ── Orchestration ──────────────────────────────────────────────

def run(from_date: str, to_date: str, physicians: list[str] | None = None, log=print) -> ScrapeResult:
    """Log in to Abronal, export every requested physician's report
    for [from_date, to_date] straight into data/uploads/abronal/."""
    cfg = ScraperConfig()
    date_range = DateRange.from_iso(from_date, to_date)
    result = ScrapeResult()

    log(f"Date range: {date_range.label()}")
    log("Launching browser…")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.headless)

        user_data_dir = os.path.join(tempfile, "abronal_profile")
    
    # 3. Launch with a persistent context and strict flags to block auto-login
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False, # Set to True if running headlessly
            args=[
                "--bwsi",                # Browse Without Sign-In (disables Google account sync)
                "--no-first-run",         # Skips welcome screens
                "--disable-extensions",   # Blocks any local extensions from injecting sessions
                "--incognito"             # Forces private browsing behavior
            ]
        )
    

        #context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(45_000)
        try:
            session = AbronalSession(page, cfg.base_url)
            log(f"Logging in as {cfg.username}…")
            session.login(cfg.username, cfg.password, cfg.role)
            log(f"Selected role: {cfg.role}")

            log("Opening Physician Performance report…")
            session.open_report()

            options = session.list_physicians()
            log(f"Dropdown has {len(options)} physician(s).")

            targets = _resolve_targets(options, cfg.skip_physicians, physicians)
            if not targets:
                raise ScraperError("No physicians selected for export.")
            log(f"Will export {len(targets)} physician(s): "
                f"{', '.join(t.label for t in targets)}")

            for i, physician in enumerate(targets, start=1):
                log(f"[{i}/{len(targets)}] Exporting {physician.label}…")
                try:
                    path = session.export_one(
                        physician=physician, patient_type=cfg.patient_type,
                        date_range=date_range, out_dir=UPLOAD_ABR_DIR,
                    )
                    result.saved.append(path.name)
                    log(f"  Saved {path.name}")
                except Exception as e:  # noqa: BLE001
                    log(f"  FAILED for {physician.label}: {e}")
                    result.failed.append(physician.label)
                    try:
                        session.open_report()
                    except Exception:
                        log("  Could not recover report page — stopping.")
                        break
        except PlaywrightTimeout as e:
            raise ScraperError(f"Timed out talking to Abronal: {e}") from e
        finally:
            try:
                browser.close()
            except Exception:
                pass

    log(f"Done. {len(result.saved)} exported, {len(result.failed)} failed.")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scrape Abronal physician performance exports")
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--physician", action="append", dest="physicians")
    args = parser.parse_args()
    r = run(args.from_date, args.to_date, args.physicians)
    print(f"saved={r.saved} failed={r.failed}")
