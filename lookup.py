"""
partslink24 VIN -> paint code lookup automation.

Routes each VIN to the correct partslink24 brand catalogue based on
data supplied by VDG (or any equivalent UK vehicle data source). The
script never tries to guess the brand from the VIN itself; it trusts
the make + category fields from the input file.

Routing rules:
  - make + category=M1 (or empty)   -> normal brand catalogue
  - make=Volkswagen + category=N1   -> Volkswagen Commercial Vehicles
  - make=Ford + category=N1         -> Ford Commercial
  - make=Mercedes-Benz + N1         -> Mercedes-Benz Vans
  - make=Mercedes-Benz + N2/N3      -> Mercedes-Benz Trucks
  - any other category=N* on a make with no commercial sibling: keep
    the base brand and hope partslink24 routes it correctly.

Catalogue first; if the catalogue rejects the VIN we fall back to the
dashboard's universal SEARCH VIN box.

lookups.txt format (whitespace tolerated, # for comments):
    vin,make,category,year
    WDD2120022A341787,Mercedes-Benz,M1,2010
    WV1ZZZ2EZ76030517,Volkswagen,N1,2007
    WF0YXXTTGYFT38981,Ford,N1,2015

Only `vin` is strictly required. Missing make causes the row to be
recorded as an error (we don't guess). Missing category defaults to
passenger. Year is captured for the output CSV but doesn't affect
routing yet.

Usage:
    python lookup.py                    # process all rows in lookups.txt
    python lookup.py --headed           # show browser window
    python lookup.py --vin WVW... --make Volkswagen
    python lookup.py --vin WV1... --make Volkswagen --category N1
    python lookup.py --debug            # headed + dump HTML on failure
    python lookup.py --fresh            # ignore saved session, log in fresh
    python lookup.py --skip-brand-check # skip the partslink24 brand-list
                                        # verification at startup
    python lookup.py --no-fallback      # disable dashboard SEARCH VIN fallback
"""

import argparse
import csv
import os
import re
import shutil
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv  # used as fallback if env.py is missing
from playwright.sync_api import (
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


# ---------- paths / constants ------------------------------------------------

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "storage_state.json"
LOOKUPS_FILE = ROOT / "lookups.txt"
RESULTS_FILE = ROOT / "results.csv"
DEBUG_DIR = ROOT / "_debug"

LOGIN_URL = "https://www.partslink24.com/partslink24/user/login.do"
HOME_URL = "https://www.partslink24.com/"
CATALOG_URL_TEMPLATE = (
    "https://www.partslink24.com/partslink24/launchCatalog.do?service={}"
)

# Per-VIN retries are only useful for transient errors (network/timeout).
# Logical failures (no catalog for brand, VIN not in DB) are not retried.
MAX_RETRIES = 1


# ---------- VDG make -> partslink24 brand -----------------------------------

# Keys are lowercased. Values use partslink24's exact brand naming.
# This matches what VDG returns in ModelDetails.ModelIdentification.Make.
MAKE_TO_BRAND: dict[str, str] = {
    "abarth":         "Abarth",
    "alfa romeo":     "Alfa Romeo",
    "alpine":         "Alpine",
    "audi":           "Audi",
    "bentley":        "Bentley",
    "bmw":            "BMW",
    "citroen":        "Citroën",
    "citroën":        "Citroën",
    "cupra":          "Cupra",
    "dacia":          "Dacia",
    "ds":             "Citroën DS",
    "ds automobiles": "Citroën DS",
    "fiat":           "Fiat",
    "ford":           "Ford",
    "hyundai":        "Hyundai",
    "infiniti":       "Infiniti",
    "iveco":          "Iveco",
    "jaguar":         "Jaguar",
    "jeep":           "Jeep",
    "kia":            "Kia",
    "lancia":         "Lancia",
    "land rover":     "Land Rover",
    "lexus":          "Lexus",
    "man":            "MAN",
    "mercedes-benz":  "Mercedes-Benz",
    "mercedes benz":  "Mercedes-Benz",
    "mercedes":       "Mercedes-Benz",
    "mini":           "MINI",
    "mitsubishi":     "Mitsubishi",
    "nissan":         "Nissan",
    "opel":           "Opel",
    "peugeot":        "Peugeot",
    "polestar":       "Polestar",
    "porsche":        "Porsche",
    "renault":        "Renault",
    "seat":           "SEAT",
    "skoda":          "Škoda",
    "škoda":          "Škoda",
    "smart":          "smart",
    "suzuki":         "Suzuki",
    "toyota":         "Toyota",
    "vauxhall":       "Vauxhall",
    "volkswagen":     "Volkswagen",
    "vw":             "Volkswagen",
    "volvo":          "Volvo",
}

# Brands that have a commercial sibling on partslink24. When category
# indicates commercial (N1/N2/N3), we re-route from base -> sibling.
# Mercedes-Benz has two siblings depending on weight class: N1 -> Vans,
# N2/N3 -> Trucks.
COMMERCIAL_REROUTING: dict[str, dict[str, str]] = {
    "Volkswagen": {
        "N1": "Volkswagen Commercial Vehicles",
        "N2": "Volkswagen Commercial Vehicles",
        "N3": "Volkswagen Commercial Vehicles",
    },
    "Ford": {
        "N1": "Ford Commercial",
        "N2": "Ford Commercial",
        "N3": "Ford Commercial",
    },
    "Mercedes-Benz": {
        "N1": "Mercedes-Benz Vans",
        "N2": "Mercedes-Benz Trucks",
        "N3": "Mercedes-Benz Trucks",
    },
}

# Brand -> partslink24 catalog service id (from launchCatalog.do?service=…).
# Verified against partslink24's brand-selection grid on the home page.
BRAND_CATALOG_SERVICE: dict[str, str] = {
    "Abarth": "abarth_parts",
    "Alfa Romeo": "alfa_parts",
    "Alpine": "alpine_parts",
    "Audi": "audi_parts",
    "Bentley": "bentley_parts",
    "BMW": "bmw_parts",
    "BMW Classic": "bmwclassic_parts",
    "BMW Motorrad": "bmwmotorrad_parts",
    "BMW Motorrad Classic": "bmwmotorradclassic_parts",
    "Citroën": "citroen_parts",
    "Citroën DS": "citroenDs_parts",
    "Cupra": "cupra_parts",
    "Dacia": "dacia_parts",
    "Fiat": "fiatp_parts",
    "Fiat Professional": "fiatt_parts",
    "Ford": "fordp_parts",
    "Ford Commercial": "fordt_parts",
    "Hyundai": "hyundai_parts",
    "Infiniti": "infiniti_parts",
    "Iveco": "iveco_parts",
    "Jaguar": "jaguar_parts",
    "Jeep": "jeep_parts",
    "Kia": "kia_parts",
    "Lancia": "lancia_parts",
    "Land Rover": "landrover_parts",
    "Lexus": "lexus_parts",
    "MAN": "man_parts",
    "Mercedes-Benz": "mercedes_parts",
    "Mercedes-Benz Classic": "mercedesclassic_parts",
    "Mercedes-Benz Trucks": "mercedestrucks_parts",
    "Mercedes-Benz Unimog": "mercedesunimog_parts",
    "Mercedes-Benz Vans": "mercedesvans_parts",
    "MINI": "mini_parts",
    "MINI Classic": "miniclassic_parts",
    "Mitsubishi": "mmc_parts",
    "Nissan": "nissan_parts",
    "Opel": "opel_parts",
    "Peugeot": "peugeot_parts",
    "Polestar": "polestar_parts",
    "Porsche": "porsche_parts",
    "Porsche Classic": "porscheclassic_parts",
    "Renault": "renault_parts",
    "SEAT": "seat_parts",
    "Škoda": "skoda_parts",
    "smart": "smart_parts",
    "Suzuki": "suzuki_parts",
    "Toyota": "toyota_parts",
    "Vauxhall": "vauxhall_parts",
    "Volkswagen": "vw_parts",
    "Volkswagen Classic": "vwclassic_parts",
    "Volkswagen Commercial Vehicles": "vn_parts",
    "Volvo": "volvo_parts",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def normalise_make(make: str | None) -> str | None:
    if not make:
        return None
    return make.strip().lower() or None


def normalise_category(category: str | None) -> str | None:
    """Lowercase and strip; empty -> None. We accept M1/N1/N2/N3 etc.
    in any case, plus simple words like 'passenger' / 'commercial'."""
    if not category:
        return None
    c = category.strip().lower()
    return c or None


def is_commercial_category(category: str | None) -> str | None:
    """If category indicates a commercial vehicle, return its uppercase
    EU type-approval code (N1/N2/N3). Returns None for passenger or
    unknown."""
    if not category:
        return None
    c = category.strip().lower()
    if not c:
        return None
    # Direct EU type-approval codes
    for code in ("n1", "n2", "n3"):
        if c == code or c.startswith(code + " "):
            return code.upper()
    # Friendly synonyms — assume light commercial (N1) which is the
    # vast majority of UK vans (Sprinter / Transit / Crafter etc.).
    if c in ("commercial", "lcv", "van", "panel van"):
        return "N1"
    if c in ("truck", "hgv"):
        return "N2"
    return None


def resolve_brand(make: str | None, category: str | None) -> tuple[str | None, str]:
    """Map (make, category) -> (partslink24 brand, explanation).
    Returns (None, explanation) if we can't resolve."""
    norm_make = normalise_make(make)
    if not norm_make:
        return None, "no make supplied"

    base = MAKE_TO_BRAND.get(norm_make)
    if not base:
        return None, f"unknown make {make!r}"

    commercial = is_commercial_category(category)
    if commercial and base in COMMERCIAL_REROUTING:
        rerouted = COMMERCIAL_REROUTING[base].get(commercial)
        if rerouted:
            return rerouted, (f"make={make!r} category={category!r} "
                              f"-> {rerouted} (commercial sibling)")

    return base, f"make={make!r} -> {base}"


def catalog_url_for_brand(brand: str) -> str | None:
    svc = BRAND_CATALOG_SERVICE.get(brand)
    return CATALOG_URL_TEMPLATE.format(svc) if svc else None


# ---------- input parsing ---------------------------------------------------

@dataclass
class LookupRow:
    """One line from lookups.txt. Only `vin` is required; the rest are
    fields we got from VDG (or supplied via CLI for one-off lookups)."""
    vin: str
    make: str | None = None
    category: str | None = None
    year: str | None = None


def read_lookups(path: Path) -> list[LookupRow]:
    """Parse lookups.txt. Each non-empty, non-comment line is a CSV row:
        vin[,make[,category[,year]]]
    Whitespace around fields is trimmed."""
    if not path.exists():
        sys.exit(f"input file not found: {path}")
    rows: list[LookupRow] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        # Skip a possible header row.
        if parts and parts[0].lower() == "vin":
            continue
        # Pad to 4 columns so unpacking always works.
        while len(parts) < 4:
            parts.append("")
        vin_part, make_part, cat_part, year_part = parts[:4]
        vin = vin_part.replace(" ", "").upper()
        if len(vin) != 17:
            log(f"skipping malformed VIN (not 17 chars): {raw!r}")
            continue
        if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
            log(f"skipping malformed VIN (invalid chars): {raw!r}")
            continue
        rows.append(LookupRow(
            vin=vin,
            make=make_part or None,
            category=cat_part or None,
            year=year_part or None,
        ))
    return rows


# ---------- partslink24 brand-list verification ------------------------------

# The login/home page exposes every available catalog as
#   <a id="<service>_lc" ... title="<Brand>" href=".../launchCatalog.do?service=<service>">
# We scrape this once per run to detect drift between our hardcoded
# BRAND_CATALOG_SERVICE map and what partslink24 actually offers.
BRAND_LINK_PATTERN = re.compile(
    r'id="([a-zA-Z0-9_]+)_lc"[^>]*?title="([^"]+)"',
    re.I,
)


def fetch_partslink24_brand_list(page: Page) -> dict[str, str] | None:
    """Return {brand_title: service_id} as advertised by partslink24's home
    page, or None if the page can't be parsed."""
    try:
        tab = page.context.new_page()
        try:
            tab.goto(HOME_URL, wait_until="domcontentloaded", timeout=20_000)
            html = tab.content()
        finally:
            try:
                tab.close()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        log(f"brand-list scrape failed ({type(e).__name__}: {e})")
        return None

    found: dict[str, str] = {}
    for m in BRAND_LINK_PATTERN.finditer(html):
        service, title = m.group(1), m.group(2).strip()
        title = title.replace("&amp;", "&")
        found[title] = service
    return found or None


def verify_brand_list(page: Page) -> None:
    """Warn (not fail) if partslink24's brand list differs from ours."""
    advertised = fetch_partslink24_brand_list(page)
    if advertised is None:
        log("brand-list verify: skipped (could not scrape home page)")
        return

    ours = BRAND_CATALOG_SERVICE
    missing = {b: s for b, s in advertised.items() if b not in ours}
    stale = {b: s for b, s in ours.items() if b not in advertised}
    mismatched = {
        b: (ours[b], advertised[b])
        for b in ours
        if b in advertised and ours[b] != advertised[b]
    }

    if not (missing or stale or mismatched):
        log(f"brand-list verify: OK ({len(advertised)} brands match)")
        return

    log(f"brand-list verify: drift detected ({len(advertised)} brands "
        f"on partslink24)")
    for b, s in sorted(missing.items()):
        log(f"  + partslink24 has new brand: {b!r} -> service={s!r}")
    for b, s in sorted(stale.items()):
        log(f"  - we list {b!r} ({s!r}) but partslink24 doesn't")
    for b, (ours_s, theirs_s) in sorted(mismatched.items()):
        log(f"  ! {b!r} service id changed: ours={ours_s!r} "
            f"theirs={theirs_s!r}")


# ---------- popup / dialog handlers ------------------------------------------

def handle_cookie_consent(page: Page) -> bool:
    """Click through Usercentrics cookie banner if present."""
    candidates = [
        'button:has-text("Accept All")',
        'button:has-text("Accept all")',
        'button:has-text("Accept only essential services")',
        'button[data-testid="uc-accept-all-button"]',
        '#uc-btn-accept-banner',
    ]
    for sel in candidates:
        btn = page.locator(sel).first
        try:
            btn.wait_for(state="visible", timeout=1_000)
        except PlaywrightTimeoutError:
            continue
        log("dismissing cookie banner")
        btn.click()
        return True
    return False


def handle_session_squeeze_out(page: Page) -> bool:
    """If a previous session is still open, click Confirm to end it."""
    prompt = page.locator('#sessionSqueezeOutPrompt').first
    if not prompt.count() or not prompt.is_visible():
        return False
    log("session squeeze-out -> Confirm")
    page.locator('#squeezeout-login-btn').first.click()
    page.wait_for_timeout(1_000)
    return True


# ---------- login ------------------------------------------------------------

def is_logged_in(page: Page) -> bool:
    """Return True if the page is the logged-in dashboard."""
    if page.locator('input[type="password"]:visible').count():
        return False
    if page.locator('a:has-text("Log out"), a:has-text("Logout")').count():
        return True
    if page.locator('input[placeholder*="SEARCH VIN" i]').count():
        return True
    return False


def login(page: Page) -> None:
    """Run the full login flow. Saves session state on success."""
    p_id = os.environ["PARTSLINK24_COMPANY_ID"]
    user = os.environ["PARTSLINK24_USERNAME"]
    pw = os.environ["PARTSLINK24_PASSWORD"]

    page.on("dialog", lambda d: d.accept())

    log("logging in")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    handle_cookie_consent(page)
    handle_session_squeeze_out(page)

    pw_field = page.locator('#inputPassword').first
    pw_field.wait_for(state="visible", timeout=15_000)

    page.locator('#login-id').first.fill(p_id)
    page.locator('#login-name').first.fill(user)
    pw_field.fill(pw)
    page.locator('#login-btn').first.click()

    try:
        page.locator('input[placeholder*="SEARCH VIN" i]').first.wait_for(
            state="visible", timeout=20_000
        )
    except PlaywrightTimeoutError:
        pass

    if not is_logged_in(page):
        _dump_login_failure(page)
        err = page.locator('#loginErrorDiv, .error, .alert').first
        msg = err.inner_text().strip() if err.count() else "(no error text)"
        raise RuntimeError(f"login failed: {msg}")

    log("logged in, saved session")
    page.context.storage_state(path=str(STATE_FILE))


def _dump_login_failure(page: Page) -> None:
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / "login_failed.png"), full_page=True)
        (DEBUG_DIR / "login_failed.html").write_text(
            page.content(), encoding="utf-8"
        )
        log(f"saved login_failed.* under {DEBUG_DIR.name}/")
    except Exception:
        pass


# ---------- catalog navigation ----------------------------------------------

def open_catalog(page: Page, brand: str) -> "Page | None":
    """Open the brand's catalog directly in a new tab. Returns the new
    page, or None if the session has expired."""
    url = catalog_url_for_brand(brand)
    if not url:
        return None

    catalog = page.context.new_page()
    log(f"opening {brand} catalog")
    try:
        catalog.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        pass

    if catalog.locator('input[type="password"]:visible').count():
        log("session expired (catalog tab redirected to login)")
        try:
            catalog.close()
        except Exception:
            pass
        return None
    return catalog


def submit_vin_in_catalog(catalog: Page, vin: str) -> bool:
    """Wait for the catalog's VIN input and submit the VIN."""
    box = catalog.locator(
        'input[placeholder*="Direct entry" i], '
        'input[placeholder*="Direkteingabe" i], '
        'input[placeholder*="VIN" i], '
        'input[placeholder*="FIN" i], '
        'input[name*="vin" i], '
        'input[name*="fin" i]'
    ).first
    try:
        box.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeoutError:
        return False
    box.fill(vin)
    box.press("Enter")
    return True


# ---------- dashboard SEARCH VIN fallback -----------------------------------

def submit_vin_on_dashboard(page: Page, vin: str) -> bool:
    """Find the dashboard's SEARCH VIN box and submit the VIN."""
    box = page.locator(
        'input[placeholder*="SEARCH VIN" i], '
        'input[placeholder*="VIN" i], '
        'input[name*="vin" i], '
        'input[name*="fin" i]'
    ).first
    try:
        box.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError:
        return False
    box.fill(vin)
    box.press("Enter")
    return True


# ---------- vehicle data extraction -----------------------------------------

PAINT_CODE_PATTERNS = [
    re.compile(
        r"Exterior\s*colou?r\s*/\s*Paint\s*Code\s*[:\n]?\s*"
        r"[A-Z0-9]+\s*/\s*([A-Z0-9]{2,8})",
        re.I,
    ),
    re.compile(r"Paint\s*Code\s*[:\n]\s*([A-Z0-9]{2,8})", re.I),
    re.compile(r"Colou?r\s*Code\s*[:\n]\s*([A-Z0-9]{2,8})", re.I),
    re.compile(r"Farbcode\s*[:\n]\s*([A-Z0-9]{2,8})", re.I),
    re.compile(r"Lackcode\s*[:\n]\s*([A-Z0-9]{2,8})", re.I),
]

EXTRA_FIELD_PATTERNS = {
    "model":           re.compile(r"^Model\s*\n\s*(.+?)\s*$", re.I | re.M),
    "production_date": re.compile(r"Date of production\s*\n\s*(\d{2}\.\d{2}\.\d{4})", re.I),
    "engine_code":     re.compile(r"Engine Code\s*\n\s*([A-Z0-9]+)", re.I),
}

VEHICLE_DATA_NEEDLE = re.compile(
    r"Paint\s*Code|Lackcode|Farbcode|Vehicle Identification", re.I
)

VIN_NOT_FOUND_PHRASES = (
    "no vehicle found",
    "vehicle not found",
    "no data found",
    "could not be assigned to a distinct model",
    "kein fahrzeug",
    "nicht gefunden",
    "vin invalid",
    "invalid vin",
)


def collect_all_text(page: Page) -> str:
    parts = []
    for fr in page.frames:
        try:
            parts.append(fr.locator("body").inner_text(timeout=2_000))
        except Exception:
            continue
    return "\n".join(parts)


def wait_for_vehicle_data(page: Page, timeout_ms: int = 30_000) -> str | None:
    waited = 0
    interval = 750
    while waited < timeout_ms:
        text = collect_all_text(page)
        lower = text.lower()
        if any(p in lower for p in VIN_NOT_FOUND_PHRASES):
            return text
        if VEHICLE_DATA_NEEDLE.search(text):
            return text
        page.wait_for_timeout(interval)
        waited += interval
    return None


def extract_paint_code(text: str) -> str:
    for pat in PAINT_CODE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).upper()
    return ""


def extract_extras(text: str) -> dict[str, str]:
    out = {}
    for key, pat in EXTRA_FIELD_PATTERNS.items():
        m = pat.search(text)
        if m:
            out[key] = m.group(1).strip()
    return out


def vin_error_in_text(text: str) -> str | None:
    lower = text.lower()
    for phrase in VIN_NOT_FOUND_PHRASES:
        if phrase in lower:
            return phrase
    return None


# ---------- VIN lookup -------------------------------------------------------

@dataclass
class LookupResult:
    timestamp: str
    vin: str
    make: str = ""
    category: str = ""
    year: str = ""
    brand: str = ""
    paint_code: str = ""
    model: str = ""
    production_date: str = ""
    engine_code: str = ""
    via: str = ""       # "catalog", "dashboard", or "" on failure
    error: str = ""


def _populate_from_text(result: LookupResult, text: str) -> None:
    result.paint_code = extract_paint_code(text)
    for k, v in extract_extras(text).items():
        setattr(result, k, v)


def _try_catalog(page: Page, vin: str, brand: str, result: LookupResult,
                 debug: bool) -> tuple[bool, str | None]:
    """Open the brand catalog and submit the VIN. Returns
    (paint_code_found, error_message)."""
    catalog = open_catalog(page, brand)
    if catalog is None:
        login(page)
        catalog = open_catalog(page, brand)
    if catalog is None:
        return False, "could not open catalog after re-login"

    try:
        if not submit_vin_in_catalog(catalog, vin):
            if debug:
                dump_debug(catalog, vin)
            return False, f"VIN box not found in {brand} catalog"

        text = wait_for_vehicle_data(catalog, timeout_ms=30_000)
        if text is None:
            if debug:
                dump_debug(catalog, vin)
            return False, "vehicle data did not load (timeout)"

        err = vin_error_in_text(text)
        if err:
            if debug:
                dump_debug(catalog, vin)
            return False, err

        _populate_from_text(result, text)
        if not result.paint_code:
            if debug:
                dump_debug(catalog, vin)
            return False, "paint code not found on result page"
        return True, None
    finally:
        try:
            catalog.close()
        except Exception:
            pass


def _try_dashboard(page: Page, vin: str, result: LookupResult,
                   debug: bool) -> tuple[bool, str | None]:
    """Submit the VIN into the dashboard's universal SEARCH VIN box."""
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20_000)
    except PlaywrightTimeoutError:
        return False, "dashboard fallback: home page load timeout"

    if page.locator('input[type="password"]:visible').count():
        log("dashboard fallback: session expired, re-logging in")
        login(page)

    if not submit_vin_on_dashboard(page, vin):
        if debug:
            dump_debug(page, vin + "_dashboard")
        return False, "dashboard fallback: SEARCH VIN box not found"

    text = wait_for_vehicle_data(page, timeout_ms=30_000)
    if text is None:
        if debug:
            dump_debug(page, vin + "_dashboard")
        return False, "dashboard fallback: vehicle data did not load"

    err = vin_error_in_text(text)
    if err:
        if debug:
            dump_debug(page, vin + "_dashboard")
        return False, f"dashboard fallback: {err}"

    _populate_from_text(result, text)
    if not result.paint_code:
        if debug:
            dump_debug(page, vin + "_dashboard")
        return False, "dashboard fallback: paint code not found"
    return True, None


def lookup_vin(page: Page, row: LookupRow, debug: bool = False,
               allow_dashboard_fallback: bool = True) -> LookupResult:
    """Catalog-first VIN lookup with optional dashboard fallback."""
    result = LookupResult(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        vin=row.vin,
        make=row.make or "",
        category=row.category or "",
        year=row.year or "",
    )

    brand, explanation = resolve_brand(row.make, row.category)
    catalog_error: str | None = None

    if brand and brand in BRAND_CATALOG_SERVICE:
        result.brand = brand
        log(f"looking up {row.vin}  {explanation}")
        ok, err = _try_catalog(page, row.vin, brand, result, debug)
        if ok:
            result.via = "catalog"
            return result
        catalog_error = err
        log(f"catalog attempt failed: {err}")
    else:
        # No brand resolvable, or brand has no catalog. Fall through to
        # the dashboard if allowed.
        if not brand:
            catalog_error = explanation  # "no make supplied" / "unknown make ..."
        else:
            result.brand = brand
            catalog_error = f"no catalog URL configured for {brand}"
        log(catalog_error)

    if not allow_dashboard_fallback:
        result.error = catalog_error or "lookup failed"
        return result

    log(f"trying dashboard fallback for {row.vin}")
    # Wipe any partial extracts from the catalog attempt.
    result.paint_code = ""
    result.model = ""
    result.production_date = ""
    result.engine_code = ""

    ok, err = _try_dashboard(page, row.vin, result, debug)
    if ok:
        result.via = "dashboard"
        return result

    if catalog_error and err:
        result.error = f"{catalog_error}; {err}"
    else:
        result.error = err or catalog_error or "lookup failed"
    return result


def lookup_vin_with_retry(page: Page, row: LookupRow, debug: bool,
                          allow_dashboard_fallback: bool = True,
                          ) -> LookupResult:
    """Retry only on transient errors. Logical errors return on first try."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = lookup_vin(page, row, debug=debug,
                           allow_dashboard_fallback=allow_dashboard_fallback)
        except PlaywrightTimeoutError as e:
            r = LookupResult(
                timestamp=datetime.now().isoformat(timespec="seconds"),
                vin=row.vin, make=row.make or "", category=row.category or "",
                year=row.year or "", error=f"timeout: {e}",
            )
        except Exception as e:  # noqa: BLE001
            r = LookupResult(
                timestamp=datetime.now().isoformat(timespec="seconds"),
                vin=row.vin, make=row.make or "", category=row.category or "",
                year=row.year or "", error=f"{type(e).__name__}: {e}",
            )

        if r.paint_code:
            return r
        retryable = (
            "timeout" in r.error.lower()
            or "did not load" in r.error.lower()
        )
        if not retryable:
            return r

        if attempt < MAX_RETRIES:
            log(f"retrying {row.vin} "
                f"(attempt {attempt + 2}/{MAX_RETRIES + 1})")
            page.wait_for_timeout(1500)
    return r


def dump_debug(page: Page, vin: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    base = DEBUG_DIR / vin
    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass
    try:
        base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    for i, fr in enumerate(page.frames):
        try:
            (DEBUG_DIR / f"{vin}_frame_{i}.html").write_text(
                fr.content(), encoding="utf-8"
            )
        except Exception:
            pass
    log(f"debug artifacts: {DEBUG_DIR.name}/{vin}.*")


# ---------- runner / main ---------------------------------------------------

def write_results(results: list[LookupResult]) -> None:
    """Append to results.csv. If the existing CSV has a different header,
    archive it and start fresh."""
    fields = list(LookupResult.__annotations__.keys())

    if RESULTS_FILE.exists():
        with RESULTS_FILE.open("r", newline="") as f:
            existing_header = next(csv.reader(f), [])
        if existing_header and existing_header != fields:
            archive = RESULTS_FILE.with_suffix(
                f".old-{datetime.now():%Y%m%d-%H%M%S}.csv"
            )
            shutil.move(str(RESULTS_FILE), str(archive))
            log(f"results.csv header changed; archived old file -> "
                f"{archive.name}")

    new_file = not RESULTS_FILE.exists()
    with RESULTS_FILE.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        for r in results:
            w.writerow(asdict(r))


def run(pw: Playwright, rows: list[LookupRow], headed: bool, debug: bool,
        fresh: bool, skip_brand_check: bool,
        allow_dashboard_fallback: bool) -> list[LookupResult]:
    if fresh and STATE_FILE.exists():
        STATE_FILE.unlink()

    browser = pw.chromium.launch(
        headless=not headed,
        args=["--disable-features=Translate"],
    )
    ctx_kwargs = {
        "viewport": {"width": 1400, "height": 1200},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "locale": "en-GB",
    }
    if STATE_FILE.exists():
        ctx_kwargs["storage_state"] = str(STATE_FILE)
    context = browser.new_context(**ctx_kwargs)
    page = context.new_page()

    if not STATE_FILE.exists():
        login(page)

    if not skip_brand_check:
        verify_brand_list(page)

    results = []
    for i, row in enumerate(rows, 1):
        log(f"--- {i}/{len(rows)} ---")
        results.append(lookup_vin_with_retry(
            page, row, debug=debug,
            allow_dashboard_fallback=allow_dashboard_fallback,
        ))

    if debug:
        log("debug: leaving browser open for 30s")
        page.wait_for_timeout(30_000)

    context.close()
    browser.close()
    return results


def main() -> None:
    # Credentials: prefer env.py (your coloureg pattern), fall back to .env.
    if (ROOT / "env.py").exists():
        sys.path.insert(0, str(ROOT))
        import env  # noqa: F401
    else:
        load_dotenv(ROOT / ".env")

    for var in ("PARTSLINK24_COMPANY_ID", "PARTSLINK24_USERNAME",
                "PARTSLINK24_PASSWORD"):
        if not os.environ.get(var):
            sys.exit(f"missing env var: {var} (check env.py or .env)")

    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--vin", help="look up a single VIN")
    ap.add_argument("--make",
                    help="VDG-style make (e.g. 'Volkswagen', 'Ford'). "
                         "Required for one-off lookups.")
    ap.add_argument("--category",
                    help="EU type-approval category (M1/N1/N2/N3) or "
                         "'commercial'/'passenger'. Used to route N* "
                         "vehicles to commercial catalogues.")
    ap.add_argument("--year", help="model year (recorded in CSV)")
    ap.add_argument("--debug", action="store_true",
                    help="show browser + dump HTML on failure")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore saved session and log in fresh")
    ap.add_argument("--skip-brand-check", action="store_true",
                    help="skip the once-per-run partslink24 brand-list check")
    ap.add_argument("--no-fallback", action="store_true",
                    help="disable dashboard SEARCH VIN fallback")
    args = ap.parse_args()

    if args.vin:
        if not args.make:
            sys.exit("--vin requires --make (we don't guess the make)")
        rows = [LookupRow(vin=args.vin.upper(), make=args.make,
                          category=args.category, year=args.year)]
    else:
        for unused in ("make", "category", "year"):
            if getattr(args, unused):
                log(f"warning: --{unused} ignored (only used with --vin)")
        rows = read_lookups(LOOKUPS_FILE)
    if not rows:
        sys.exit("no rows to process")

    with sync_playwright() as pw:
        results = run(pw, rows,
                      headed=args.headed or args.debug,
                      debug=args.debug,
                      fresh=args.fresh,
                      skip_brand_check=args.skip_brand_check,
                      allow_dashboard_fallback=not args.no_fallback)

    write_results(results)
    print()
    print(f"{'VIN':<19} {'BRAND':<32} {'PAINT':<8} {'YEAR':<6} {'VIA':<10} ERROR")
    print("-" * 105)
    for r in results:
        print(f"{r.vin:<19} {r.brand[:32]:<32} {r.paint_code:<8} "
              f"{r.year:<6} {r.via:<10} {r.error}")
    print(f"\nappended to {RESULTS_FILE.name}")


if __name__ == "__main__":
    main()