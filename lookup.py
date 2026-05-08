"""
partslink24 VIN -> paint code lookup automation.

Catalog-first: every VIN is opened directly in its brand catalog
(determined from the VIN's WMI prefix). Faster and more reliable than
going via the dashboard's SEARCH VIN box.

Usage:
    python lookup.py                    # process all VINs in vins.txt
    python lookup.py --headed           # show browser window
    python lookup.py --vin WVW...       # one-off lookup
    python lookup.py --debug            # headed + dump HTML on failure
    python lookup.py --fresh            # ignore saved session, log in fresh
"""

import argparse
import csv
import os
import re
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
VINS_FILE = ROOT / "vins.txt"
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


# ---------- VIN -> brand mapping ---------------------------------------------

# WMI (first 3 chars) -> brand name.
WMI_TO_BRAND: dict[str, str] = {
    "WVW": "Volkswagen", "1VW": "Volkswagen", "3VW": "Volkswagen",
    "WV1": "Volkswagen Commercial Vehicles",
    "WV2": "Volkswagen Commercial Vehicles",
    "WV3": "Volkswagen Commercial Vehicles",
    "WAU": "Audi", "TRU": "Audi", "WA1": "Audi",
    "TMB": "Škoda",
    "VSS": "SEAT",
    "WP0": "Porsche", "WP1": "Porsche",
    "WBA": "BMW", "WBS": "BMW", "WBY": "BMW", "WBX": "BMW",
    "5UX": "BMW", "5YM": "BMW",
    "WDB": "Mercedes-Benz", "WDD": "Mercedes-Benz",
    "WDC": "Mercedes-Benz", "W1K": "Mercedes-Benz",
    "WDF": "Mercedes-Benz Vans",
    "SAJ": "Jaguar",
    "SAL": "Land Rover",
    "ZFA": "Fiat", "ZFF": "Fiat",
    "VF1": "Renault", "VF3": "Peugeot", "VF7": "Citroën",
    "JTD": "Toyota", "VNK": "Toyota",
    "JN1": "Nissan",
    "KMH": "Hyundai", "KNA": "Kia",
}

# Brand -> partslink24 catalog service id (from launchCatalog.do?service=…).
BRAND_CATALOG_SERVICE: dict[str, str] = {
    "Volkswagen": "vw_parts",
    "Volkswagen Commercial Vehicles": "vn_parts",
    "Volkswagen Classic": "vwclassic_parts",
    "Audi": "audi_parts",
    "Škoda": "skoda_parts",
    "SEAT": "seat_parts",
    "Cupra": "cupra_parts",
    "Porsche": "porsche_parts",
    "Porsche Classic": "porscheclassic_parts",
    "BMW": "bmw_parts",
    "BMW Classic": "bmwclassic_parts",
    "Mercedes-Benz": "mercedes_parts",
    "Mercedes-Benz Vans": "mercedesvans_parts",
    "Mercedes-Benz Classic": "mercedesclassic_parts",
    "Mercedes-Benz Trucks": "mercedestrucks_parts",
    "Jaguar": "jaguar_parts",
    "Land Rover": "landrover_parts",
    "Fiat": "fiatp_parts",
    "Fiat Professional": "fiatt_parts",
    "Renault": "renault_parts",
    "Peugeot": "peugeot_parts",
    "Citroën": "citroen_parts",
    "Toyota": "toyota_parts",
    "Nissan": "nissan_parts",
    "Hyundai": "hyundai_parts",
    "Kia": "kia_parts",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def brand_for_vin(vin: str) -> str | None:
    return WMI_TO_BRAND.get(vin[:3].upper())


def catalog_url_for_brand(brand: str) -> str | None:
    svc = BRAND_CATALOG_SERVICE.get(brand)
    return CATALOG_URL_TEMPLATE.format(svc) if svc else None


def read_vins(path: Path) -> list[str]:
    if not path.exists():
        sys.exit(f"VIN file not found: {path}")
    vins = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        vin = line.replace(" ", "").upper()
        if len(vin) != 17:
            log(f"skipping malformed VIN (not 17 chars): {raw!r}")
            continue
        if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
            log(f"skipping malformed VIN (invalid chars): {raw!r}")
            continue
        vins.append(vin)
    return vins


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
    """If a previous session is still open, click Confirm to end it.
    The dialog has id=sessionSqueezeOutPrompt and a link id=squeezeout-login-btn
    that calls doLoginAjax(true)."""
    prompt = page.locator('#sessionSqueezeOutPrompt').first
    if not prompt.count() or not prompt.is_visible():
        return False
    log("session squeeze-out -> Confirm")
    page.locator('#squeezeout-login-btn').first.click()
    page.wait_for_timeout(1_000)  # let doLoginAjax round-trip
    return True


# ---------- login ------------------------------------------------------------

def is_logged_in(page: Page) -> bool:
    """Return True if the page is the logged-in dashboard (no password field
    and either Log out link or SEARCH VIN box visible)."""
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

    page.on("dialog", lambda d: d.accept())  # accept any native confirm()

    log("logging in")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    handle_cookie_consent(page)
    handle_session_squeeze_out(page)  # may briefly delay form appearance

    # Form field IDs from partslink24's HTML.
    pw_field = page.locator('#inputPassword').first
    pw_field.wait_for(state="visible", timeout=15_000)

    page.locator('#login-id').first.fill(p_id)
    page.locator('#login-name').first.fill(user)
    pw_field.fill(pw)
    page.locator('#login-btn').first.click()

    # Wait for the dashboard's SEARCH VIN box to confirm we're in.
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
    """Open the brand's catalog directly in a new tab. Returns the new page,
    or None if the session has expired (caller should re-login and retry)."""
    url = catalog_url_for_brand(brand)
    if not url:
        return None

    catalog = page.context.new_page()
    log(f"opening {brand} catalog")
    try:
        catalog.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        pass

    # Session expired -> partslink24 redirects to the login page in this tab.
    if catalog.locator('input[type="password"]:visible').count():
        log("session expired (catalog tab redirected to login)")
        try:
            catalog.close()
        except Exception:
            pass
        return None
    return catalog


def submit_vin_in_catalog(catalog: Page, vin: str) -> bool:
    """Wait for the catalog's VIN/Direct-entry box and submit the VIN."""
    # Different catalogs label this box differently; try all variants.
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


# ---------- vehicle data extraction -----------------------------------------

# Paint code patterns, in priority order.
PAINT_CODE_PATTERNS = [
    # VW/Audi: "Exterior color / Paint Code\nZ1 / A7N" — capture after slash.
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
    "year":            re.compile(r"^Year\s*\n\s*(\d{4})\s*$", re.I | re.M),
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
    """Concatenate visible text from the main frame and all subframes once.
    Single extraction beats running each regex against each frame separately."""
    parts = []
    for fr in page.frames:
        try:
            parts.append(fr.locator("body").inner_text(timeout=2_000))
        except Exception:
            continue
    return "\n".join(parts)


def wait_for_vehicle_data(page: Page, timeout_ms: int = 30_000) -> str | None:
    """Poll until vehicle data appears in any frame. Returns the combined text
    on success, None on timeout. Returns early if a 'VIN not found' phrase
    appears."""
    waited = 0
    interval = 750
    while waited < timeout_ms:
        text = collect_all_text(page)
        lower = text.lower()
        if any(p in lower for p in VIN_NOT_FOUND_PHRASES):
            return text  # caller will detect the error phrase
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
    brand: str = ""
    paint_code: str = ""
    model: str = ""
    year: str = ""
    production_date: str = ""
    engine_code: str = ""
    error: str = ""


def lookup_vin(page: Page, vin: str, debug: bool = False) -> LookupResult:
    """Open the brand catalog, submit the VIN, extract the paint code +
    extras. Catalog-first only; no dashboard fallback."""
    result = LookupResult(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        vin=vin,
    )

    brand = brand_for_vin(vin)
    if not brand:
        result.brand = "(unknown WMI)"
        result.error = f"no brand mapping for WMI {vin[:3]}"
        return result
    result.brand = brand

    if brand not in BRAND_CATALOG_SERVICE:
        result.error = f"no catalog URL configured for {brand}"
        return result

    log(f"looking up {vin}  brand={brand}")

    # Open the catalog. If session expired, re-login and retry once.
    catalog = open_catalog(page, brand)
    if catalog is None:
        login(page)
        catalog = open_catalog(page, brand)
    if catalog is None:
        result.error = "could not open catalog after re-login"
        return result

    try:
        if not submit_vin_in_catalog(catalog, vin):
            result.error = f"VIN box not found in {brand} catalog"
            if debug:
                dump_debug(catalog, vin)
            return result

        text = wait_for_vehicle_data(catalog, timeout_ms=30_000)
        if text is None:
            result.error = "vehicle data did not load (timeout)"
            if debug:
                dump_debug(catalog, vin)
            return result

        err = vin_error_in_text(text)
        if err:
            result.error = err
            if debug:
                dump_debug(catalog, vin)
            return result

        result.paint_code = extract_paint_code(text)
        for k, v in extract_extras(text).items():
            setattr(result, k, v)

        if not result.paint_code:
            result.error = "paint code not found on result page"
            if debug:
                dump_debug(catalog, vin)

        return result
    finally:
        try:
            catalog.close()
        except Exception:
            pass


def lookup_vin_with_retry(page: Page, vin: str, debug: bool) -> LookupResult:
    """Retry only on transient errors (timeouts, exceptions). Logical errors
    (no catalog, VIN not in DB, paint code missing) return on first try."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = lookup_vin(page, vin, debug=debug)
        except PlaywrightTimeoutError as e:
            r = LookupResult(
                timestamp=datetime.now().isoformat(timespec="seconds"),
                vin=vin, error=f"timeout: {e}",
            )
        except Exception as e:  # noqa: BLE001
            r = LookupResult(
                timestamp=datetime.now().isoformat(timespec="seconds"),
                vin=vin, error=f"{type(e).__name__}: {e}",
            )

        # Stop early if it succeeded or hit a non-retryable error.
        if r.paint_code:
            return r
        retryable = (
            "timeout" in r.error.lower()
            or "did not load" in r.error.lower()
        )
        if not retryable:
            return r

        if attempt < MAX_RETRIES:
            log(f"retrying {vin} (attempt {attempt + 2}/{MAX_RETRIES + 1})")
            page.wait_for_timeout(1500)
    return r  # last attempt's result


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
    new_file = not RESULTS_FILE.exists()
    fields = list(LookupResult.__annotations__.keys())
    with RESULTS_FILE.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        for r in results:
            w.writerow(asdict(r))


def run(pw: Playwright, vins: list[str], headed: bool, debug: bool,
        fresh: bool) -> list[LookupResult]:
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

    # We don't pre-probe login state. Each VIN's open_catalog() will detect
    # an expired session and trigger login() lazily. Saves a homepage load
    # on every run when the session is valid.
    if not STATE_FILE.exists():
        login(page)

    results = []
    for i, vin in enumerate(vins, 1):
        log(f"--- {i}/{len(vins)} ---")
        results.append(lookup_vin_with_retry(page, vin, debug=debug))

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
    ap.add_argument("--debug", action="store_true",
                    help="show browser + dump HTML on failure")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore saved session and log in fresh")
    args = ap.parse_args()

    vins = [args.vin.upper()] if args.vin else read_vins(VINS_FILE)
    if not vins:
        sys.exit("no VINs to process")

    with sync_playwright() as pw:
        results = run(pw, vins,
                      headed=args.headed or args.debug,
                      debug=args.debug,
                      fresh=args.fresh)

    write_results(results)
    print()
    print(f"{'VIN':<19} {'BRAND':<14} {'PAINT':<8} {'YEAR':<6} ERROR")
    print("-" * 80)
    for r in results:
        print(f"{r.vin:<19} {r.brand[:14]:<14} {r.paint_code:<8} "
              f"{r.year:<6} {r.error}")
    print(f"\nappended to {RESULTS_FILE.name}")


if __name__ == "__main__":
    main()