"""Flow: ask dates -> login -> select role -> open report -> for each physician:
      set filters -> search -> Excel"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import os
import tkinter as tk
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk
from dotenv import load_dotenv

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from secret_helper import get_secret

# Directory containing this script. (Fixed from the original
# `Path(__file__).resolve().parent[1]`, which is not valid — `.parent` is a
# single Path, not subscriptable, and would raise TypeError on import. See
# CONVERSION_MANUAL.md, "Bugs fixed during conversion".)
load_dotenv()

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = ROOT_DIR/ "configs" / "config.json"


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class ExportError(Exception):
    """Base class for all expected errors raised by this program."""


class ConfigError(ExportError):
    pass


class DateRangeError(ExportError):
    pass


class PhysicianNotFoundError(ExportError):
    pass


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

class LoggingConfigurator:
    """Sets up file + console logging. Call `configure()` once at startup."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir

    def configure(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.log_dir / "export.log"
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[
                logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler(sys.stdout),
            ],
        )


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

class AppConfig:
    """Wraps config.json. Read-only properties for known keys, with the
    original defaults preserved. Use `variant()` to get a modified copy
    written to a temp file (used by --headed)."""

    DEFAULT_ANALYZER_SCRIPT = r"C:\Users\senay\Desktop\dr master analyzer\reconciliation_app_v5.py"

    def __init__(self, path: Path, data: dict | None = None):
        self.path = path
        self._data = data if data is not None else self._load(path)

    @staticmethod
    def _load(path: Path) -> dict:
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError as e:
            raise ConfigError(f"Config file not found: {path}") from e
        except json.JSONDecodeError as e:
            raise ConfigError(f"Config file is not valid JSON: {path} ({e})") from e

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        return cls(path)

    # -- required fields (raise KeyError -> surfaced as-is, matches original) --
    @property
    def base_url(self) -> str:
        return os.getenv("BASE_URL")

    @property
    def username(self) -> str:
        return os.getenv("USERNAME")

    @property
    def password(self) -> str:
        return os.getenv("PASSWORD")

    @property
    def role(self) -> str:
        return os.getenv("ROLE")

    # -- optional fields with defaults --
    @property
    def headless(self) -> bool:
        return bool(self._data.get("headless", True))

    @property
    def skip_physicians(self) -> list[str]:
        return list(self._data.get("skip_physicians", []))

    @property
    def physicians_cfg(self):
        return self._data.get("physicians", "all")

    @property
    def patient_type(self) -> str:
        return self._data.get("patient_type", "OPD")

    @property
    def lookback_days(self) -> int:
        return int(self._data.get("lookback_days", 3))

    @property
    def exclude_today(self) -> bool:
        return bool(self._data.get("exclude_today", True))

    @property
    def launch_analyzer(self) -> bool:
        return bool(self._data.get("launch_analyzer", True))

    @property
    def analyzer_script(self) -> Path:
        return Path(self._data.get("analyzer_script", self.DEFAULT_ANALYZER_SCRIPT))

    @property
    def analyzer_auto_run(self) -> bool:
        return bool(self._data.get("analyzer_auto_run", True))

    def variant(self, **overrides) -> "AppConfig":
        """Return a new in-memory AppConfig with some keys overridden
        (does not touch disk)."""
        data = dict(self._data)
        data.update(overrides)
        return AppConfig(self.path, data=data)

    def write_temp(self, tmp_path: Path, **overrides) -> Path:
        """Write a modified copy of this config to `tmp_path` (used for
        the --headed flag) and return the path."""
        data = dict(self._data)
        data.update(overrides)
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return tmp_path


# --------------------------------------------------------------------------
# Domain value objects
# --------------------------------------------------------------------------

class DateRange:
    """A validated (from_date, to_date) pair with the label/formatting
    logic that used to be scattered across free functions."""

    def __init__(self, from_date: date, to_date: date):
        if from_date > to_date:
            raise DateRangeError("From date must be on or before To date.")
        self.from_date = from_date
        self.to_date = to_date

    @staticmethod
    def parse_date(text: str) -> date:
        text = text.strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        raise DateRangeError(f"Unrecognized date: {text!r} (use YYYY-MM-DD)")

    @staticmethod
    def _format_day(d: date) -> str:
        """e.g. July 20"""
        return f"{d.strftime('%B')} {d.day}"

    @classmethod
    def default(cls, lookback_days: int, exclude_today: bool) -> "DateRange":
        end = date.today() - timedelta(days=1 if exclude_today else 0)
        start = end - timedelta(days=lookback_days - 1)
        return cls(start, end)

    def label(self) -> str:
        """e.g. July 20 to July 22"""
        return f"{self._format_day(self.from_date)} to {self._format_day(self.to_date)}"

    def __repr__(self) -> str:
        return f"DateRange({self.from_date.isoformat()}..{self.to_date.isoformat()})"


class Physician:
    """A physician option pulled from the dropdown: a raw <option> value
    plus its display label, with filename-sanitizing logic attached."""

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

    def __repr__(self) -> str:
        return f"Physician(value={self.value!r}, label={self.label!r})"


class OutputPaths:
    """Creates and holds the Desktop/<range>/{abronal,sot,analysis} folder
    structure for a given date range, and knows how to name export files."""

    def __init__(self, date_range: DateRange, desktop: Path | None = None):
        self.date_range = date_range
        self.desktop = desktop or (Path.home() / "Desktop")
        self.label = date_range.label()
        self.root = self.desktop / self.label
        self.abronal = self.root / f"{self.label} abronal"
        self.sot = self.root / f"{self.label} sot"
        self.analysis = self.root / f"{self.label} analysis"

    def create_all(self) -> "OutputPaths":
        for folder in (self.root, self.abronal, self.sot, self.analysis):
            folder.mkdir(parents=True, exist_ok=True)
        logging.info("Created folder structure under: %s", self.root)
        return self

    def export_filename(self, physician: Physician) -> str:
        """July 20 to July 22 Dr. Ahmed Reja.xlsx"""
        return f"{self.label} {physician.display_name}.xlsx"

    @staticmethod
    def count_xlsx(folder: Path) -> int:
        if not folder.exists():
            return 0
        return sum(1 for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".xlsx")


@dataclass
class ExportResult:
    """Accumulates outcomes across a run so the summary/exit-code logic has
    one place to read from."""

    saved: list[Path] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def any_saved(self) -> bool:
        return bool(self.saved)

    @property
    def any_failed(self) -> bool:
        return bool(self.failed)


# --------------------------------------------------------------------------
# Date picking (Strategy pattern: GUI first, console fallback)
# --------------------------------------------------------------------------

class DatePicker(ABC):
    """A strategy for asking the user for a date range."""

    @abstractmethod
    def prompt(self, default_from: date, default_to: date) -> DateRange | None:
        """Return a DateRange, or None if the user cancelled."""


class ConsoleDatePicker(DatePicker):
    def prompt(self, default_from: date, default_to: date) -> DateRange | None:
        print("Enter report date range (YYYY-MM-DD). Press Enter to keep the default.")
        raw_from = input(f"From date [{default_from.isoformat()}]: ").strip()
        raw_to = input(f"To date   [{default_to.isoformat()}]: ").strip()
        try:
            f = DateRange.parse_date(raw_from) if raw_from else default_from
            t = DateRange.parse_date(raw_to) if raw_to else default_to
            return DateRange(f, t)
        except DateRangeError as e:
            print(e)
            return None


class GuiDatePicker(DatePicker):
    """Small Tk date-picker window. Uses tkcalendar's DateEntry if
    installed, otherwise falls back to plain text entries."""

    def prompt(self, default_from: date, default_to: date) -> DateRange | None:
        result: dict[str, date | None] = {"from": None, "to": None}

        root = tk.Tk()
        root.title("Physician Performance Export — Date Range")
        root.resizable(False, False)
        root.attributes("-topmost", True)

        frame = ttk.Frame(root, padding=16)
        frame.grid(row=0, column=0)

        ttk.Label(frame, text="Select the report date range", font=("", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )

        from_var = tk.StringVar(value=default_from.isoformat())
        to_var = tk.StringVar(value=default_to.isoformat())

        try:
            from tkcalendar import DateEntry  # type: ignore

            ttk.Label(frame, text="From date").grid(row=1, column=0, sticky="w", padx=(0, 8))
            from_picker = DateEntry(
                frame, width=14, date_pattern="yyyy-mm-dd",
                year=default_from.year, month=default_from.month, day=default_from.day,
            )
            from_picker.grid(row=1, column=1, sticky="w", pady=4)

            ttk.Label(frame, text="To date").grid(row=2, column=0, sticky="w", padx=(0, 8))
            to_picker = DateEntry(
                frame, width=14, date_pattern="yyyy-mm-dd",
                year=default_to.year, month=default_to.month, day=default_to.day,
            )
            to_picker.grid(row=2, column=1, sticky="w", pady=4)
            use_calendar = True
        except Exception:
            use_calendar = False
            ttk.Label(frame, text="From date (YYYY-MM-DD)").grid(row=1, column=0, sticky="w", padx=(0, 8))
            ttk.Entry(frame, textvariable=from_var, width=16).grid(row=1, column=1, sticky="w", pady=4)
            ttk.Label(frame, text="To date (YYYY-MM-DD)").grid(row=2, column=0, sticky="w", padx=(0, 8))
            ttk.Entry(frame, textvariable=to_var, width=16).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(
            frame,
            text="Example filename: July 20 to July 22 Dr. Ahmed Reja.xlsx",
            foreground="#555",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 4))

        def on_ok() -> None:
            try:
                if use_calendar:
                    f = from_picker.get_date()
                    t = to_picker.get_date()
                    if not isinstance(f, date):
                        f = f.date()
                    if not isinstance(t, date):
                        t = t.date()
                else:
                    f = DateRange.parse_date(from_var.get())
                    t = DateRange.parse_date(to_var.get())
                if f > t:
                    messagebox.showerror("Invalid range", "From date must be on or before To date.")
                    return
                result["from"] = f
                result["to"] = t
                root.destroy()
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("Invalid date", str(e))

        def on_cancel() -> None:
            root.destroy()

        btns = ttk.Frame(frame)
        btns.grid(row=4, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=on_cancel).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="Start Export", command=on_ok).grid(row=0, column=1, padx=4)

        root.bind("<Return>", lambda _e: on_ok())
        root.bind("<Escape>", lambda _e: on_cancel())

        root.update_idletasks()
        w, h = root.winfo_width(), root.winfo_height()
        x = (root.winfo_screenwidth() - w) // 2
        y = (root.winfo_screenheight() - h) // 3
        root.geometry(f"+{x}+{y}")

        root.mainloop()

        if result["from"] is None or result["to"] is None:
            return None
        return DateRange(result["from"], result["to"])


class DateRangeResolver:
    """Decides which DateRange to use: CLI args > prompt (GUI, falling
    back to console) > config defaults."""

    def __init__(self, config: AppConfig, picker: DatePicker | None = None):
        self.config = config
        self.picker = picker or GuiDatePicker()

    def resolve(
        self,
        *,
        prompt: bool,
        cli_from: str | None,
        cli_to: str | None,
    ) -> DateRange | None:
        default = DateRange.default(self.config.lookback_days, self.config.exclude_today)

        if cli_from or cli_to:
            f = DateRange.parse_date(cli_from) if cli_from else default.from_date
            t = DateRange.parse_date(cli_to) if cli_to else default.to_date
            return DateRange(f, t)

        if not prompt:
            return default

        try:
            return self.picker.prompt(default.from_date, default.to_date)
        except Exception as e:  # noqa: BLE001
            logging.warning("GUI date picker unavailable (%s); using console prompt.", e)
            return ConsoleDatePicker().prompt(default.from_date, default.to_date)


# --------------------------------------------------------------------------
# Browser automation (Page Object pattern)
# --------------------------------------------------------------------------

class AbronalSession:
    """Wraps a Playwright page for the Abronal eHealth site: login, opening
    the report, listing physicians, and exporting one physician's Excel."""

    def __init__(self, page: Page, base_url: str):
        self.page = page
        self.base_url = base_url.rstrip("/")

    def login(self, username: str, password: str, role: str) -> None:
        logging.info("Logging in as %s", username)
        page = self.page
        page.goto(self.base_url, wait_until="domcontentloaded")
        page.fill("#username", username)
        page.fill("#password", password)
        page.click("button[type='submit']")
        page.wait_for_url("**/Account/LoginAs**", timeout=30_000)
        page.wait_for_selector("#selRole")

        page.select_option("#selRole", label=role)
        page.evaluate(
            """() => {
                if (window.jQuery) {
                    window.jQuery('#selRole').trigger('change');
                }
            }"""
        )
        logging.info("Selected role: %s", role)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)

    def open_report(self) -> None:
        report_url = f"{self.base_url}/Reports/PhysicianPerformance"
        logging.info("Opening report %s", report_url)
        self.page.goto(report_url, wait_until="domcontentloaded")
        self.page.wait_for_selector("#fromDate")
        self.page.wait_for_selector("#cardiologist")
        self.page.wait_for_timeout(1000)

    def list_physicians(self) -> list[Physician]:
        """Read every option from the physician dropdown (#cardiologist)."""
        options = self.page.eval_on_selector_all(
            "#cardiologist option",
            """els => els.map(e => ({
                value: String(e.value || '').trim(),
                text: (e.textContent || '').trim()
            }))""",
        )
        return [
            Physician(o["value"], o["text"])
            for o in options
            if o["value"] and o["text"]
        ]

    def _select2_set(self, select_id: str, value: str) -> None:
        self.page.evaluate(
            """({ selectId, value }) => {
                const $el = window.jQuery('#' + selectId);
                $el.val(value).trigger('change');
            }""",
            {"selectId": select_id, "value": value},
        )

    def export_one(
        self,
        *,
        physician: Physician,
        patient_type: str,
        date_range: DateRange,
        out_dir: Path,
        paths: OutputPaths,
    ) -> Path:
        page = self.page
        page.fill("#fromDate", date_range.from_date.isoformat())
        page.fill("#toDate", date_range.to_date.isoformat())
        page.fill("#fromTime", "00:00")
        page.fill("#toTime", "23:59")

        ptype_value = "opd" if patient_type.strip().upper() == "OPD" else "ipd"
        self._select2_set("pType", ptype_value)
        self._select2_set("cardiologist", physician.value)

        logging.info(
            "Filters: %s to %s | patientType=%s | physician=%s (id=%s)",
            date_range.from_date, date_range.to_date, patient_type,
            physician.label, physician.value,
        )

        with page.expect_response(
            lambda r: "/Reports/GetPhysicianPerformance" in r.url and r.ok,
            timeout=60_000,
        ):
            page.click("#show")

        page.wait_for_timeout(800)

        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / paths.export_filename(physician)

        with page.expect_download(timeout=60_000) as dl_info:
            page.click("button.buttons-excel")
        download = dl_info.value
        download.save_as(target)
        logging.info("Saved %s", target)
        return target


class PhysicianResolver:
    """Turns the dropdown options + config/CLI physician filters into the
    concrete list of Physicians to export."""

    def __init__(self, options: list[Physician], skip_names: list[str]):
        self.options = options
        self.skip_names = skip_names

    def should_skip(self, name: str) -> bool:
        name_l = name.lower().strip()
        for skip in self.skip_names:
            s = skip.lower().strip()
            if not s:
                continue
            if name_l == s or s in name_l:
                return True
        return False

    def resolve_one(self, needle: str) -> Physician:
        """Match physician option by name substring; prefer closest match."""
        needle_l = needle.lower().strip()
        exact = [
            o for o in self.options
            if o.label.lower().rstrip(". ").strip() == needle_l.rstrip(". ").strip()
            or o.label.lower().strip() == needle_l
        ]
        if exact:
            return exact[0]

        starts = [o for o in self.options if o.label.lower().startswith(needle_l)]
        if len(starts) == 1:
            return starts[0]

        contains = [o for o in self.options if needle_l in o.label.lower()]
        if contains:
            contains.sort(key=lambda o: len(o.label))
            return contains[0]

        raise PhysicianNotFoundError(f"Physician not found in dropdown: {needle!r}")

    def resolve_export_list(
        self,
        physicians_cfg,
        cli_physicians: list[str] | None,
    ) -> list[Physician]:
        if cli_physicians:
            return [self.resolve_one(n) for n in cli_physicians]

        if physicians_cfg == "all" or physicians_cfg is None or physicians_cfg == []:
            selected = []
            for o in self.options:
                if self.should_skip(o.label):
                    logging.info("Skipping physician: %s", o.label)
                    continue
                selected.append(o)
            return selected

        if isinstance(physicians_cfg, list):
            return [self.resolve_one(n) for n in physicians_cfg]

        raise ConfigError(
            'config "physicians" must be "all" or a list of names; '
            f"got {type(physicians_cfg).__name__}"
        )


# --------------------------------------------------------------------------
# SoT hand-off + reconciliation launch
# --------------------------------------------------------------------------

class SotFileWaiter:
    """Opens Explorer on the SoT folder and blocks (via a small dialog
    loop) until the user has copied files in and confirmed, or cancels."""

    def __init__(self, sot_dir: Path):
        self.sot_dir = sot_dir

    def _open_explorer(self) -> None:
        try:
            subprocess.Popen(["explorer", str(self.sot_dir)])
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not open Explorer: %s", e)

    def wait(self) -> bool:
        """Returns False if the user cancels."""
        self._open_explorer()

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        while True:
            n = OutputPaths.count_xlsx(self.sot_dir)
            msg = (
                "Abronal export is complete.\n\n"
                f"Copy your SoT Excel file(s) into:\n{self.sot_dir}\n\n"
                f"Currently found: {n} .xlsx file(s)\n\n"
                "Click Retry to re-check, Continue when ready, or Cancel to skip analysis."
            )
            result = messagebox.askyesnocancel(
                "SoT files needed",
                msg + "\n\nYes = Continue  |  No = Re-check  |  Cancel = Skip analyzer",
                parent=root,
            )
            if result is None:
                root.destroy()
                return False
            if result is False:
                continue
            n = OutputPaths.count_xlsx(self.sot_dir)
            if n == 0:
                messagebox.showwarning(
                    "No SoT files",
                    "No .xlsx files were found in the SoT folder yet.\n"
                    "Copy the files in, then click Yes again.",
                    parent=root,
                )
                continue
            root.destroy()
            return True


class ReconciliationLauncher:
    """Launches the external reconciliation analyzer script as a subprocess."""

    def __init__(self, analyzer_script: Path):
        self.analyzer_script = analyzer_script

    def launch(
        self,
        abronal_dir: Path,
        sot_dir: Path,
        analysis_dir: Path,
        *,
        date_label: str = "",
        auto_run: bool = True,
    ) -> None:
        if not self.analyzer_script.exists():
            raise FileNotFoundError(f"Analyzer not found: {self.analyzer_script}")

        cmd = [
            sys.executable, str(self.analyzer_script),
            "--abr", str(abronal_dir),
            "--sot", str(sot_dir),
            "--out", str(analysis_dir),
        ]
        if date_label:
            cmd.extend(["--date-label", date_label])
        if auto_run:
            cmd.append("--auto-run")

        logging.info("Launching reconciliation app: %s", self.analyzer_script)
        subprocess.Popen(cmd, cwd=str(self.analyzer_script.parent))


# --------------------------------------------------------------------------
# Terminal I/O helper
# --------------------------------------------------------------------------

class TerminalReporter:
    """Small helper for the pause-at-end / summary printing behavior, kept
    separate from the orchestration logic so it's easy to silence in tests."""

    @staticmethod
    def pause(message: str = "Press Enter to close...") -> None:
        try:
            input(message)
        except EOFError:
            pass


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

class PhysicianPerformanceExporter:
    """Top-level application: ties config, date resolution, browser
    automation, and the SoT/reconciliation hand-off together. Equivalent
    to the original module-level `run()` function."""

    def __init__(
        self,
        config: AppConfig,
        *,
        date_resolver: DateRangeResolver | None = None,
        pause_at_end: bool = True,
        skip_analyzer: bool = False,
    ):
        self.config = config
        self.date_resolver = date_resolver or DateRangeResolver(config)
        self.pause_at_end = pause_at_end
        self.skip_analyzer = skip_analyzer

    def run(
        self,
        physicians: list[str] | None = None,
        *,
        prompt_dates: bool = True,
        cli_from: str | None = None,
        cli_to: str | None = None,
    ) -> int:
        LoggingConfigurator(SCRIPT_DIR / "logs").configure()

        exit_code = 0
        result = ExportResult()
        paths: OutputPaths | None = None

        try:
            date_range = self.date_resolver.resolve(
                prompt=prompt_dates, cli_from=cli_from, cli_to=cli_to,
            )
        except DateRangeError as e:
            logging.error("%s", e)
            self._maybe_pause()
            return 1

        if date_range is None:
            logging.info("Cancelled — no dates selected.")
            self._maybe_pause()
            return 0

        logging.info("Using date range: %s", date_range.label())
        paths = OutputPaths(date_range).create_all()

        exit_code = self._run_browser_phase(date_range, paths, physicians, result)

        self._print_summary(result, paths, exit_code)
        exit_code = self._maybe_reconcile(result, paths, date_range, exit_code)

        self._maybe_pause()
        return exit_code

    # -- phases --------------------------------------------------------

    def _run_browser_phase(
        self,
        date_range: DateRange,
        paths: OutputPaths,
        physicians: list[str] | None,
        result: ExportResult,
    ) -> int:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.config.headless)
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()
                page.set_default_timeout(45_000)

                try:
                    session = AbronalSession(page, self.config.base_url)
                    session.login(
                        self.config.username, self.config.password, self.config.role
                    )
                    session.open_report()

                    options = session.list_physicians()
                    logging.info("Dropdown has %d physician(s)", len(options))

                    resolver = PhysicianResolver(options, self.config.skip_physicians)
                    targets = resolver.resolve_export_list(
                        self.config.physicians_cfg, physicians
                    )

                    if not targets:
                        logging.error("No physicians selected for export")
                        return 1

                    logging.info("Will export %d physician(s)", len(targets))
                    patient_type = self.config.patient_type

                    for i, physician in enumerate(targets, start=1):
                        logging.info("[%d/%d] Exporting %s", i, len(targets), physician.label)
                        try:
                            path = session.export_one(
                                physician=physician,
                                patient_type=patient_type,
                                date_range=date_range,
                                out_dir=paths.abronal,
                                paths=paths,
                            )
                            result.saved.append(path)
                        except Exception:
                            logging.exception("Failed for physician: %s", physician.label)
                            result.failed.append(physician.label)
                            try:
                                session.open_report()
                            except Exception:
                                logging.exception("Could not recover report page")
                                break
                    return 0
                except PlaywrightTimeout as e:
                    logging.exception("Timed out: %s", e)
                    return 2
                except Exception:
                    logging.exception("Export failed")
                    return 3
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception:
            logging.exception("Could not start browser")
            return 3

    def _print_summary(self, result: ExportResult, paths: OutputPaths, exit_code: int) -> None:
        print()
        if result.any_saved and not result.any_failed and exit_code == 0:
            print(f"{len(result.saved)} files exported successfully")
            print(f"Saved to: {paths.abronal}")
            logging.info("%d files exported successfully to %s", len(result.saved), paths.abronal)
        elif result.any_saved:
            print(f"{len(result.saved)} files exported successfully ({len(result.failed)} failed)")
            print(f"Saved to: {paths.abronal}")
            if result.any_failed:
                logging.error("Failed physicians: %s", ", ".join(result.failed))
        else:
            print("0 files exported successfully")

        print(f"Parent folder: {paths.root}")
        print()

    def _maybe_reconcile(
        self,
        result: ExportResult,
        paths: OutputPaths,
        date_range: DateRange,
        exit_code: int,
    ) -> int:
        # Preserve the original exit-code fallback semantics: only bump to 4
        # here (no files saved) if nothing worse already happened.
        if not result.any_saved:
            return exit_code if exit_code != 0 else 4
        if result.any_failed and exit_code == 0:
            exit_code = 4

        if self.skip_analyzer or not self.config.launch_analyzer:
            return exit_code

        print("Next step: copy SoT Excel files into the SoT folder, then continue.")
        print(f"SoT folder: {paths.sot}")
        print(f"Analysis output will go to: {paths.analysis}")
        print()

        if SotFileWaiter(paths.sot).wait():
            try:
                ReconciliationLauncher(self.config.analyzer_script).launch(
                    paths.abronal, paths.sot, paths.analysis,
                    date_label=date_range.label(),
                    auto_run=self.config.analyzer_auto_run,
                )
                print("Reconciliation app launched.")
                print("Confirm service categories there, then outputs will be generated.")
            except Exception:
                logging.exception("Failed to launch reconciliation app")
                exit_code = 5 if exit_code == 0 else exit_code
        else:
            print("Skipped reconciliation analyzer.")
        print()
        return exit_code

    def _maybe_pause(self) -> None:
        if self.pause_at_end:
            TerminalReporter.pause()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class ExportCLI:
    """Parses arguments and drives a PhysicianPerformanceExporter."""

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(description="Export Physician Performance Excel reports")
        parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config.json")
        parser.add_argument(
            "--physician", action="append", dest="physicians",
            help="Export only these physician name(s); can be repeated",
        )
        parser.add_argument("--from-date", dest="from_date", help="From date YYYY-MM-DD (skips picker)")
        parser.add_argument("--to-date", dest="to_date", help="To date YYYY-MM-DD (skips picker)")
        parser.add_argument(
            "--no-prompt", action="store_true",
            help="Skip date picker; use lookback defaults from config",
        )
        parser.add_argument(
            "--no-pause", action="store_true",
            help="Do not wait for Enter at the end (for scheduled tasks)",
        )
        parser.add_argument(
            "--skip-analyzer", action="store_true",
            help="Do not launch the reconciliation analyzer after export",
        )
        parser.add_argument("--headed", action="store_true", help="Show browser window")
        return parser

    def main(self, argv: list[str] | None = None) -> int:
        args = self.build_parser().parse_args(argv)
        prompt = not args.no_prompt and not (args.from_date and args.to_date)

        try:
            config = AppConfig.load(args.config)
        except ConfigError as e:
            print(f"Config error: {e}", file=sys.stderr)
            return 1

        tmp_path: Path | None = None
        if args.headed:
            config = config.variant(headless=False)
            tmp_path = SCRIPT_DIR / ".config.headed.json"
            config.write_temp(tmp_path)
            config = AppConfig(tmp_path)

        try:
            exporter = PhysicianPerformanceExporter(
                config,
                pause_at_end=not args.no_pause,
                skip_analyzer=args.skip_analyzer,
            )
            return exporter.run(
                physicians=args.physicians,
                prompt_dates=prompt,
                cli_from=args.from_date,
                cli_to=args.to_date,
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)


def main() -> int:
    return ExportCLI().main()


if __name__ == "__main__":
    raise SystemExit(main())
