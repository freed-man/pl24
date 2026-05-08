"""
partslink24 VIN -> paint code lookup automation.

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

from dotenv import load_dotenv  # kept as fallback if env.py is missing
from playwright.sync_api import (
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "storage_state.json"
VINS_FILE = ROOT / "vins.txt"
RESULTS_FILE = ROOT / "results.csv"
DEBUG_DIR = ROOT / "_debug"

LOGIN_URL = "https://www.partslink24.com/partslink24/user/login.do"
HOME_URL = "https://www.partslink24.com/"

MAX_RETRIES = 2  # per-VIN retry on transient errors


# Maps the WMI (first 3 chars of VIN) to the brand name on the dashboard.
WMI_TO_BRAND: dict[str, str] = {
    "WVW": "Volkswagen", "1VW": "Volkswagen", "3VW": "Volkswagen",
    "WV1": "Volkswagen Commercial Vehicles",
    "WV2": "Volkswagen Commercial Vehicles",
    "WV3": "Volkswagen Commercial Vehicles",
    "WAU": "Audi", "TRU": "Audi", "WA1": "Audi",
    "TMB": "Škoda", "VSS": "SEAT",
    "WP0": "Porsche", "WP1": "Porsche",
    "WBA": "BMW", "WBS": "BMW", "WBY": "BMW", "WBX": "BMW",
    "5UX": "BMW", "5YM": "BMW",
    "WDB": "Mercedes-Benz", "WDD": "Mercedes-Benz",
    "WDC": "Mercedes-Benz", "W1K": "Mercedes-Benz",
    "WDF": "Mercedes-Benz Vans",
    "SAJ": "Jaguar", "SAL": "Land Rover",
    "ZFA": "Fiat", "ZFF": "Fiat",
    "VF1": "Renault", "VF3": "Peugeot", "VF7": "Citroën",
    "JTD": "Toyota", "VNK": "Toyota",
    "JN1": "Nissan", "KMH": "Hyundai", "KNA": "Kia",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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


def brand_for_vin(vin: str) -> str | None:
    return WMI_TO_BRAND.get(vin[:3].upper())


# ---------- popup / dialog handlers ------------------------------------------

def handle_cookie_consent(page: Page) -> bool:
    candidates = [
        'button:has-text("Accept All")',
        'button:has-text("Accept all")',
        'button:has-text("Accept only essential services")',
        'button[data-testid="uc-accept-all-button"]',
        'button[data-testid="uc-deny-all-button"]',
        '#uc-btn-accept-banner',
    ]
    for sel in candidates:
        btn = page.locator(sel).first
        try:
            btn.wait_for(state="visible", timeout=1_500)
        except PlaywrightTimeoutError:
            continue
        log("dismissing cookie banner")
        btn.click()
        page.wait_for_timeout(500)
        return True
    return False


def handle_bookmark_warning(page: Page) -> bool:
    if not page.locator('text=/Attention.*read carefully/i').count():
        return False
    reload_link = page.locator('a:has-text("Reload")').first
    if reload_link.count():
        log("clicking past bookmark warning")
        reload_link.click()
        page.wait_for_load_state("networkidle", timeout=30_000)
        return True
    return False


def handle_session_conflict(page: Page) -> bool:
    # The dialog has id="sessionSqueezeOutPrompt" with an <a> button
    # (id="squeezeout-login-btn") that calls doLoginAjax(true) to confirm.
    prompt = page.locator('#sessionSqueezeOutPrompt').first
    if not prompt.count():
        return False
    if not prompt.is_visible():
        return False
    log("session squeeze-out dialog -> clicking Confirm")
    confirm = page.locator('#squeezeout-login-btn').first
    if confirm.count():
        confirm.click()
        page.wait_for_timeout(1500)  # let doLoginAjax finish
        return True
    log("WARN: squeeze-out dialog visible but #squeezeout-login-btn missing")
    return False


def dismiss_all_popups(page: Page) -> None:
    handle_cookie_consent(page)
    handle_bookmark_warning(page)
    handle_session_conflict(page)


# ---------- login ------------------------------------------------------------

def is_logged_in(page: Page) -> bool:
    if page.locator('input[type="password"]:visible').count():
        return False
    if page.locator('a:has-text("Log out"), a:has-text("Logout")').count():
        return True
    if page.locator('input[placeholder*="SEARCH VIN" i]').count():
        return True
    return False


def login(page: Page) -> None:
    p_id = os.environ["PARTSLINK24_COMPANY_ID"]
    user = os.environ["PARTSLINK24_USERNAME"]
    pw = os.environ["PARTSLINK24_PASSWORD"]

    # Auto-accept any native browser confirm() dialogs.
    page.on("dialog", lambda d: (log(f"native dialog: {d.message[:60]}"), d.accept()))

    log("logging in")
    page.goto(HOME_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(500)
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    dismiss_all_popups(page)

    # If we already had a session, partslink24 shows a squeeze-out dialog
    # on page load (BEFORE the login form). Click Confirm to dismiss it and
    # reveal the real form. The form has id="loginDialog" and is hidden
    # (display:none) while the dialog is showing.
    page.wait_for_timeout(500)
    if handle_session_conflict(page):
        log("squeeze-out cleared, waiting for login form")
        # After clicking Confirm, doLoginAjax(true) ends the other session
        # and the page reloads the form. Wait for the form to become visible.
        try:
            page.locator('#inputPassword').first.wait_for(
                state="visible", timeout=15_000
            )
        except PlaywrightTimeoutError:
            # Sometimes after squeeze-out we end up on the dashboard directly.
            if is_logged_in(page):
                log("logged in via squeeze-out (no re-auth needed)")
                page.context.storage_state(path=str(STATE_FILE))
                return

    # Use the real form element IDs from partslink24's HTML.
    pw_field = page.locator('#inputPassword').first
    pw_field.wait_for(state="visible", timeout=20_000)

    id_field = page.locator('#login-id').first
    user_field = page.locator('#login-name').first

    id_field.click()
    page.keyboard.type(p_id, delay=20)
    user_field.click()
    page.keyboard.type(user, delay=20)
    pw_field.click()
    page.keyboard.type(pw, delay=20)
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)

    # The login button is <a id="login-btn"> with onclick=doLoginAjax().
    submit = page.locator('#login-btn').first
    if submit.count():
        submit.click()
    else:
        pw_field.press("Enter")

    # Wait for the dashboard to render (the SEARCH VIN box is the giveaway).
    try:
        page.locator('input[placeholder*="SEARCH VIN" i]').first.wait_for(
            state="visible", timeout=30_000
        )
    except PlaywrightTimeoutError:
        pass

    # After submit, partslink24 may still pop the squeeze-out dialog if
    # someone else logged in between our page load and our submit.
    for _ in range(3):
        if handle_session_conflict(page):
            page.wait_for_timeout(2000)
            continue
        break

    if not is_logged_in(page):
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            page.screenshot(path=str(DEBUG_DIR / "login_failed.png"), full_page=True)
            (DEBUG_DIR / "login_failed.html").write_text(
                page.content(), encoding="utf-8"
            )
            for i, fr in enumerate(page.frames):
                try:
                    (DEBUG_DIR / f"login_failed_frame_{i}.html").write_text(
                        fr.content(), encoding="utf-8"
                    )
                except Exception:
                    pass
            log(f"saved login_failed.png and *.html under {DEBUG_DIR.name}/")
        except Exception:
            pass
        err = page.locator('#loginErrorDiv, .error, .alert').first
        msg = err.inner_text().strip() if err.count() else "(no error text found)"
        raise RuntimeError(f"login failed: {msg}")

    log("logged in, saved session")
    page.context.storage_state(path=str(STATE_FILE))


def ensure_logged_in(page: Page) -> None:
    page.goto(HOME_URL, wait_until="domcontentloaded")
    dismiss_all_popups(page)
    if not is_logged_in(page):
        login(page)


# ---------- vehicle data extraction ------------------------------------------

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
    "model": re.compile(r"^Model\s*\n\s*(.+?)\s*$", re.I | re.M),
    "year": re.compile(r"^Year\s*\n\s*(\d{4})\s*$", re.I | re.M),
    "production_date": re.compile(
        r"Date of production\s*\n\s*(\d{2}\.\d{2}\.\d{4})", re.I
    ),
    "engine_code": re.compile(r"Engine Code\s*\n\s*([A-Z0-9]+)", re.I),
}


def search_all_frames(page: Page, regex: re.Pattern) -> str | None:
    for fr in page.frames:
        try:
            text = fr.locator("body").inner_text(timeout=3_000)
        except Exception:
            continue
        m = regex.search(text)
        if m:
            return m.group(1).strip()
    return None


def extract_paint_code(page: Page) -> str | None:
    for pat in PAINT_CODE_PATTERNS:
        v = search_all_frames(page, pat)
        if v:
            return v.upper()
    return None


def extract_vehicle_info(page: Page) -> dict:
    info = {}
    for key, pat in EXTRA_FIELD_PATTERNS.items():
        v = search_all_frames(page, pat)
        if v:
            info[key] = v
    return info


# ---------- VIN error / dialog detection -------------------------------------

VIN_NOT_FOUND_PHRASES = [
    "no vehicle found",
    "vehicle not found",
    "no data found",
    "kein fahrzeug",
    "nicht gefunden",
    "vin invalid",
    "invalid vin",
]


def vin_error_on_page(page: Page) -> str | None:
    try:
        text = page.locator("body").inner_text(timeout=3_000).lower()
    except Exception:
        return None
    for phrase in VIN_NOT_FOUND_PHRASES:
        if phrase in text:
            return phrase
    return None


# Direct catalog URLs from the partslink24 HTML — clicking a brand logo
# navigates to /partslink24/launchCatalog.do?service=<id>, so we just go
# there directly and skip the click entirely (more reliable than scrolling
# to a logo that may be below the fold).
BRAND_CATALOG_SERVICE = {
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
CATALOG_URL_TEMPLATE = "https://www.partslink24.com/partslink24/launchCatalog.do?service={}"


def open_catalog(page: Page, brand: str) -> "Page | None":
    """Open the brand catalog directly via its launchCatalog URL.
    Tries opening in a new tab first; falls back to in-place navigation."""
    service = BRAND_CATALOG_SERVICE.get(brand)
    if not service:
        log(f"no catalog URL known for brand: {brand}")
        return None
    url = CATALOG_URL_TEMPLATE.format(service)

    # Open in a new tab so the dashboard stays as our home base.
    catalog = page.context.new_page()
    log(f"opening catalog: {url}")
    try:
        catalog.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        pass
    return catalog


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


def wait_for_vehicle_data(page: Page, timeout_ms: int = 30_000) -> bool:
    waited = 0
    interval = 1000
    needle = re.compile(r"Paint\s*Code|Lackcode|Farbcode|Vehicle Identification",
                        re.I)
    while waited < timeout_ms:
        for fr in page.frames:
            try:
                txt = fr.locator("body").inner_text(timeout=2_000)
            except Exception:
                continue
            if needle.search(txt):
                return True
            for phrase in VIN_NOT_FOUND_PHRASES:
                if phrase in txt.lower():
                    return False
        page.wait_for_timeout(interval)
        waited += interval
    return False


def submit_vin_global(page: Page, vin: str) -> bool:
    vin_box = page.locator(
        'input[placeholder*="SEARCH VIN" i], '
        'input[placeholder*="VIN" i], '
        'input[name*="vin" i], input[name*="fin" i]'
    ).first
    try:
        vin_box.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeoutError:
        return False
    vin_box.fill(vin)
    go = page.locator(
        'button:has-text("GO"), input[value="GO"], '
        'button:has-text("Go"), input[value="Go"]'
    ).first
    if go.count():
        go.click()
    else:
        vin_box.press("Enter")
    return True


def submit_vin_in_catalog(page: Page, vin: str) -> bool:
    """Find the catalog's VIN/Direct-entry box and submit the VIN."""
    # The VW/Audi catalog labels the box "Direct entry"; other catalogs use
    # "VIN" or "FIN". Try all variants.
    selectors = [
        'input[placeholder*="Direct entry" i]',
        'input[placeholder*="Direkteingabe" i]',  # German
        'input[placeholder*="VIN" i]',
        'input[placeholder*="FIN" i]',
        'input[name*="vin" i]',
        'input[name*="fin" i]',
    ]
    vin_box = None
    for _ in range(30):  # up to ~30s for catalog to render
        for sel in selectors:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                vin_box = loc
                break
        if vin_box:
            break
        page.wait_for_timeout(1000)
    if not vin_box:
        return False
    vin_box.click()
    vin_box.fill(vin)
    vin_box.press("Enter")
    return True


def lookup_vin(page: Page, vin: str, debug: bool = False) -> LookupResult:
    ts = datetime.now().isoformat(timespec="seconds")
    result = LookupResult(timestamp=ts, vin=vin)

    brand = brand_for_vin(vin)
    result.brand = brand or "(unknown WMI)"
    log(f"looking up {vin}  brand={result.brand}")

    page.goto(HOME_URL, wait_until="domcontentloaded")
    dismiss_all_popups(page)

    if not is_logged_in(page):
        log("session lost — re-logging in")
        login(page)
        page.goto(HOME_URL, wait_until="domcontentloaded")
        dismiss_all_popups(page)

    catalog: Page | None = None  # the catalog page (new tab) if brand-click works

    if brand:
        catalog = open_catalog(page, brand)
        if catalog is not None:
            log(f"working in {brand} catalog")
            dismiss_all_popups(catalog)
            if not submit_vin_in_catalog(catalog, vin):
                log(f"VIN box not found in {brand} catalog")
                try:
                    catalog.close()
                except Exception:
                    pass
                catalog = None

    # If brand-click didn't work, fall back to global SEARCH VIN.
    # (Some older VINs work this way; newer EVs typically don't.)
    if catalog is None:
        page.goto(HOME_URL, wait_until="domcontentloaded")
        dismiss_all_popups(page)
        if not submit_vin_global(page, vin):
            result.error = "VIN input not found on dashboard"
            return result
        catalog = page  # treat the dashboard as the result page

    # Poll the catalog page (not the dashboard) for vehicle data.
    log("waiting for vehicle data")
    if not wait_for_vehicle_data(catalog, timeout_ms=45_000):
        err = vin_error_on_page(catalog)
        result.error = err or "vehicle data did not load (timeout or VIN not in catalog)"
        if debug:
            dump_debug(catalog, vin)
        # Close the catalog tab so we start fresh next VIN.
        if catalog is not page:
            catalog.close()
        return result

    result.paint_code = extract_paint_code(catalog) or ""
    info = extract_vehicle_info(catalog)
    result.model = info.get("model", "")
    result.year = info.get("year", "")
    result.production_date = info.get("production_date", "")
    result.engine_code = info.get("engine_code", "")

    if not result.paint_code:
        result.error = "paint code not found on result page"
        if debug:
            dump_debug(catalog, vin)

    # Close the catalog tab to keep things tidy and avoid stale state.
    if catalog is not page:
        try:
            catalog.close()
        except Exception:
            pass

    return result


def lookup_vin_with_retry(page: Page, vin: str, debug: bool) -> LookupResult:
    last: LookupResult | None = None
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

        # Success or non-retryable error -> stop.
        if r.paint_code:
            return r
        if r.error and any(p in r.error.lower() for p in VIN_NOT_FOUND_PHRASES):
            return r  # data really isn't there, no point retrying

        last = r
        if attempt < MAX_RETRIES:
            log(f"retrying {vin} (attempt {attempt + 2}/{MAX_RETRIES + 1})")
            page.wait_for_timeout(2000)
    return last or LookupResult(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        vin=vin, error="unknown failure",
    )


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
    log(f"debug artifacts saved under {DEBUG_DIR.name}/")


# ---------- main -------------------------------------------------------------

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
        args=["--disable-features=Translate"],  # kill Chrome's translate bar
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

    ensure_logged_in(page)

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
    # Load credentials. Prefers env.py (your coloureg pattern); falls back
    # to .env via python-dotenv if env.py is missing.
    if (ROOT / "env.py").exists():
        sys.path.insert(0, str(ROOT))
        import env  # noqa: F401  -- side effect: sets os.environ keys
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
                    help="ignore saved session, log in fresh")
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