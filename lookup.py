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
passenger. Year is captured but currently unused — reserved for future
Classic-catalogue routing once partslink24 confirms cutoff dates.

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
import unicodedata
from dataclasses import dataclass
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

# When True, dump_debug artifacts are written for EVERY result page, not
# just failures — including successful lookups. Set once from the
# --dump-always CLI flag in run(). Useful for inspecting a page that
# succeeded but produced a surprising/empty field (e.g. a success with no
# description), where the normal failure-only --debug dump writes nothing.
# Module-level rather than threaded through every lookup function because
# it's a whole-run CLI switch, not a per-VIN setting.
DUMP_ALWAYS = False

LOGIN_URL = "https://www.partslink24.com/partslink24/user/login.do"
HOME_URL = "https://www.partslink24.com/"
CATALOG_URL_TEMPLATE = (
    "https://www.partslink24.com/partslink24/launchCatalog.do?service={}"
)

# Per-VIN extra attempts (on top of the first one) for transient errors
# like network timeouts. Logical failures (no catalog for brand, VIN not
# in DB) are not retried. EXTRA_RETRIES=1 means we make at most 2 total
# attempts. Named "EXTRA_" rather than "MAX_" so it can't be misread as
# "maximum total attempts".
EXTRA_RETRIES = 1


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
    "bmw motorrad":   "BMW Motorrad",
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
    "Fiat": {
        "N1": "Fiat Professional",
        "N2": "Fiat Professional",
        "N3": "Fiat Professional",
    },
    "Mercedes-Benz": {
        "N1": "Mercedes-Benz Vans",
        "N2": "Mercedes-Benz Trucks",
        "N3": "Mercedes-Benz Trucks",
    },
}

# Mercedes is the only brand where the EU category picks between two
# different commercial catalogues — N1 -> Vans, N2/N3 -> Trucks. But the
# N1/N2/N3 boundary is genuinely fuzzy: GVW can't be reliably derived
# from a VIN, and a 3.5t van vs a 3.5t-plus light truck sit right on the
# line, so VDG's category occasionally puts a Mercedes on the wrong side.
# When the routed commercial catalogue fails, try the sibling commercial
# catalogue before falling through to Classic/dashboard. This only fires
# for Mercedes commercials and only on failure, so correctly-routed
# lookups pay nothing.
#
# VW and Ford don't need this (single commercial catalogue each). MAN
# and IVECO don't need it either — they're standalone heavy-truck
# catalogues with no category-dependent routing.
COMMERCIAL_FALLBACK: dict[str, str] = {
    "Mercedes-Benz Vans":   "Mercedes-Benz Trucks",
    "Mercedes-Benz Trucks": "Mercedes-Benz Vans",
    # Mirror the Mercedes-style cross-fallback for Fiat: passenger vs
    # commercial Fiats live in separate catalogues, and the M1/N1
    # boundary for a Doblò or Panda Van is just as fuzzy as Mercedes's
    # van/truck line. If the routed catalogue doesn't recognise the VIN,
    # try the sibling before falling through to dashboard.
    "Fiat":                 "Fiat Professional",
    "Fiat Professional":    "Fiat",
}

# Per Matt at LexCom (partslink24 UK support, May 2026): there is no
# year-based rule for whether a VIN belongs in a brand's modern vs
# Classic catalogue — it's whether the model is still in production.
# Since we can't tell that upfront from VIN/make/year alone, we treat
# Classic as a fallback: if the main (or commercial) catalogue fails to
# find the vehicle, retry against the Classic sibling before giving up
# to the dashboard.
#
# The map's keys cover BOTH the base brand and its commercial siblings,
# so that an N1 Sprinter routed to Mercedes-Benz Vans, when not found,
# still falls back to Mercedes-Benz Classic. Whether Classic actually
# carries old commercial vehicles is something we'll learn from real
# lookups — if it doesn't, the request fails fast and we move on.
CLASSIC_SIBLING: dict[str, str] = {
    "BMW":                              "BMW Classic",
    "BMW Motorrad":                     "BMW Motorrad Classic",
    "Mercedes-Benz":                    "Mercedes-Benz Classic",
    "Mercedes-Benz Vans":               "Mercedes-Benz Classic",
    "Mercedes-Benz Trucks":             "Mercedes-Benz Classic",
    "Mercedes-Benz Unimog":             "Mercedes-Benz Classic",
    "MINI":                             "MINI Classic",
    "Porsche":                          "Porsche Classic",
    "Volkswagen":                       "Volkswagen Classic",
    "Volkswagen Commercial Vehicles":   "Volkswagen Classic",
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
    """Normalise a make string for MAKE_TO_BRAND lookup.

    Lowercase, strip, and apply Unicode NFC normalisation so that
    accented characters compare equal regardless of whether they were
    stored as precomposed code points (e.g. 'ë' = U+00EB) or as a base
    letter plus combining mark (e.g. 'e' + U+0308). Without NFC,
    'Citroën' from one source could fail to match the 'citroën' key
    in MAKE_TO_BRAND even though they look identical.
    """
    if not make:
        return None
    return unicodedata.normalize("NFC", make.strip().lower()) or None


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
    Whitespace around fields is trimmed.

    File is read as UTF-8 (with BOM tolerance) regardless of the host
    platform's default encoding — important on Windows where the default
    can be cp1252 and accented makes like 'Citroën' or 'Škoda' would
    silently decode to mojibake and fail MAKE_TO_BRAND lookup.

    Unicode is normalised to NFC so that 'ë' encoded as a single code
    point (U+00EB) and 'ë' encoded as 'e' + combining diaeresis (U+0065
    + U+0308) both compare equal to the keys in MAKE_TO_BRAND.
    """
    if not path.exists():
        sys.exit(f"input file not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    text = unicodedata.normalize("NFC", text)
    rows: list[LookupRow] = []
    for raw in text.splitlines():
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
            # If we hit the bookmark-warning interstitial, click through.
            handle_attention_page(tab)
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


def _wait_for_squeeze_or_form(page: Page, timeout_ms: int = 15_000) -> str:
    """Wait until either the squeeze-out prompt or the login form's password
    field becomes visible. Returns 'squeeze', 'form', or 'timeout'.

    partslink24's startup runs an async session check after DOMContentLoaded,
    so we have to wait for the JS to decide which UI to show — checking
    once on page-ready isn't enough."""
    waited = 0
    interval = 250
    while waited < timeout_ms:
        try:
            squeeze = page.locator('#sessionSqueezeOutPrompt').first
            if squeeze.count() and squeeze.is_visible():
                return "squeeze"
        except Exception:
            pass
        try:
            pw = page.locator('#inputPassword').first
            if pw.count() and pw.is_visible():
                return "form"
        except Exception:
            pass
        page.wait_for_timeout(interval)
        waited += interval
    return "timeout"


class AttentionPageLoopError(RuntimeError):
    """Raised when the partslink24 attention/bookmark-warning interstitial
    keeps reappearing after we click Reload. Indicates the bypass has
    stopped working — possibly because partslink24 changed the page format
    or our click target. Caller should abort rather than spin forever."""


def handle_attention_page(page: Page) -> bool:
    """Detect and dismiss the partslink24 'Attention - Please read carefully'
    bookmark-warning page, which intercepts direct navigation to login.do.

    The page has no login form, just a heading and a Reload link that
    redirects to the proper login flow. We detect by heading text and
    click Reload; if we somehow land back on it, we raise
    AttentionPageLoopError rather than silently letting the caller spin.

    Returns True if the page was detected and successfully dismissed,
    False if no attention page was visible. Raises if a loop is detected
    or if the page format changed (heading present but no Reload link).
    """
    # Cheap check first — only do the click work if the heading is present.
    heading = page.locator('h1, h2').filter(has_text="Attention").first
    try:
        if not heading.count() or not heading.is_visible():
            return False
    except Exception:
        return False

    log("attention/bookmark-warning page detected -> clicking Reload")
    reload_link = page.locator('a').filter(has_text=re.compile(r"^\s*Reload\s*$",
                                                                 re.I)).first
    if not reload_link.count():
        # Heading was there but no Reload link — page format changed.
        # We have no way to dismiss it; fail loudly so the user can see
        # what happened instead of looping into a re-login wall.
        raise AttentionPageLoopError(
            "attention page detected but no Reload link found "
            "(partslink24 may have changed the page format)"
        )

    try:
        with page.expect_navigation(wait_until="domcontentloaded",
                                    timeout=15_000):
            reload_link.click()
    except PlaywrightTimeoutError:
        log("attention page: navigation after Reload click timed out")

    # Make sure we didn't loop back to the same warning.
    looped = page.locator('h1, h2').filter(has_text="Attention").first
    try:
        if looped.count() and looped.is_visible():
            raise AttentionPageLoopError(
                "attention page still showing after Reload — "
                "would loop indefinitely if we returned to the caller"
            )
    except AttentionPageLoopError:
        raise
    except Exception:
        pass
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
    """Run the full login flow. Saves session state on success.

    Note: the dialog handler (auto-accepting JS prompts like the
    squeeze-out confirmation) is registered once on the browser context
    in run(), not here — registering it per-login leaked a fresh handler
    on every call."""
    p_id = os.environ["PARTSLINK24_COMPANY_ID"]
    user = os.environ["PARTSLINK24_USERNAME"]
    pw = os.environ["PARTSLINK24_PASSWORD"]

    log("logging in")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    handle_attention_page(page)  # bookmark-warning interstitial
    handle_cookie_consent(page)

    # partslink24 runs an async session check on page load. The result is
    # either: (a) the squeeze-out prompt — a previous session is still
    # active and we must Confirm to end it before the login form appears;
    # or (b) the login form itself, ready to fill. Wait for whichever the
    # server decides on.
    state = _wait_for_squeeze_or_form(page, timeout_ms=15_000)
    if state == "squeeze":
        log("session squeeze-out -> Confirm (pre-form)")
        page.locator('#squeezeout-login-btn').first.click()
        # After Confirm, the form should appear; wait for it.
        state = _wait_for_squeeze_or_form(page, timeout_ms=15_000)
    if state != "form":
        _dump_login_failure(page)
        raise RuntimeError(
            f"login failed: form never became visible "
            f"(state={state!r}); see {DEBUG_DIR.name}/login_failed.*"
        )

    page.locator('#login-id').first.fill(p_id)
    page.locator('#login-name').first.fill(user)
    page.locator('#inputPassword').first.fill(pw)
    page.locator('#login-btn').first.click()

    # After clicking Login, partslink24 may also throw up a squeeze-out
    # (if a session reappeared between our load and our submit). Give it
    # a moment, then handle if needed.
    page.wait_for_timeout(1_500)
    handle_session_squeeze_out(page)

    try:
        page.locator('input[placeholder*="SEARCH VIN" i]').first.wait_for(
            state="visible", timeout=20_000
        )
    except PlaywrightTimeoutError:
        pass

    if not is_logged_in(page):
        _dump_login_failure(page)
        msg = _extract_login_error(page)
        raise RuntimeError(f"login failed: {msg}")

    log("logged in, saved session")
    page.context.storage_state(path=str(STATE_FILE))


def _extract_login_error(page: Page) -> str:
    """Pull the actual error message off the login page.

    partslink24's HTML puts a literal '►' bullet character in its own
    <span class='error'> sibling next to the real message, so just
    grabbing the first .error gives us a useless arrow. We try harder:

      1. #loginErrorDiv has the dedicated text, when populated
      2. all visible .error spans, concatenated, with the bullet stripped
      3. fall back to the page URL + title so the user has *something*
    """
    # 1. Dedicated error div.
    err_div = page.locator('#loginErrorDiv')
    try:
        if err_div.count():
            text = err_div.inner_text().strip()
            if text:
                return text
    except Exception:
        pass

    # 2. All .error / .alert spans, joined; drop the bullet glyph and
    # any whitespace-only bits.
    parts: list[str] = []
    try:
        spans = page.locator('.error, .alert')
        for i in range(spans.count()):
            try:
                t = spans.nth(i).inner_text().strip()
            except Exception:
                continue
            t = t.replace("►", "").strip()
            if t and t not in parts:
                parts.append(t)
    except Exception:
        pass
    if parts:
        return " | ".join(parts)

    # 3. Last resort — just say where we are.
    try:
        url = page.url
        title = page.title()
    except Exception:
        url, title = "(unknown)", "(unknown)"
    return f"(no error text on page; url={url} title={title!r})"


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

    # Session expired -> partslink24 redirects to either the login page
    # (password field visible) or its bookmark-warning interstitial
    # (Attention heading). In either case we treat it as expired and let
    # the caller re-login.
    if catalog.locator('input[type="password"]:visible').count():
        log("session expired (catalog tab redirected to login)")
        try:
            catalog.close()
        except Exception:
            pass
        return None
    if catalog.locator('h1, h2').filter(has_text="Attention").first.count():
        log("session expired (catalog tab redirected to attention page)")
        try:
            catalog.close()
        except Exception:
            pass
        return None
    return catalog


def _wait_for_editable(box, timeout_ms: int) -> bool:
    """Poll Locator.is_editable() until True or timeout. Playwright's
    wait_for(state=...) doesn't accept 'editable' (only attached/detached/
    visible/hidden), so we poll explicitly. is_editable returns True only
    when the element is enabled AND not readonly."""
    waited = 0
    interval = 250
    while waited < timeout_ms:
        try:
            if box.is_editable():
                return True
        except Exception:
            pass
        try:
            box.page.wait_for_timeout(interval)
        except Exception:
            return False
        waited += interval
    return False


def submit_vin(page: Page, vin: str, *, source: str) -> tuple[bool, str | None]:
    """Find the VIN input on the page and submit `vin`.

    Used for both the brand catalog tabs and the dashboard. Their VIN
    boxes differ only in placeholder text — catalog uses 'Direct entry' /
    'Direkteingabe' / 'VIN' / 'FIN', dashboard uses 'SEARCH VIN' / 'VIN' —
    so the selector below accepts any of them; in practice only one box is
    present on each page.

    `source` is "catalog" or "dashboard" and only changes the error
    wording so log lines stay readable.

    Failure modes:
      - '<box> not visible' — input never rendered in 10s.
      - '<box> never became editable' — input rendered but stayed
        disabled. Failing fast here beats letting Playwright's fill()
        hang for its 30s default.

    Returns (True, None) on success, or (False, reason) on failure."""
    box_name = "SEARCH VIN box" if source == "dashboard" else "VIN box"
    editable_suffix = ("never became editable" if source == "dashboard"
                       else "visible but never became editable")
    box = page.locator(
        'input[placeholder*="Direct entry" i], '
        'input[placeholder*="Direkteingabe" i], '
        'input[placeholder*="SEARCH VIN" i], '
        'input[placeholder*="VIN" i], '
        'input[placeholder*="FIN" i], '
        'input[name*="vin" i], '
        'input[name*="fin" i]'
    ).first
    try:
        box.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError:
        return False, f"{box_name} not visible"
    if not _wait_for_editable(box, timeout_ms=10_000):
        return False, f"{box_name} {editable_suffix}"
    try:
        box.fill(vin, timeout=5_000)
        box.press("Enter")
    except PlaywrightTimeoutError:
        return False, f"{box_name} fill timed out"
    return True, None


# ---------- vehicle data extraction -----------------------------------------

PAINT_CODE_PATTERNS = [
    # VW/Audi: "Exterior color / Paint Code\n8E / A7W" — code after the slash.
    re.compile(
        r"Exterior\s*colou?r\s*/\s*Paint\s*Code\s*[:\n]?\s*"
        r"[A-Z0-9]+\s*/\s*([A-Z0-9]{2,8})",
        re.I,
    ),
    # Nissan: "Exterior color\tZ11" — code follows the label directly,
    # no slash and no parentheses. Negative lookahead on "/" prevents
    # this from firing on VW/Audi pages where "Exterior color" is
    # followed by " / Paint Code". Anchored on "Exterior" so we don't
    # pick up "Interior color".
    re.compile(
        r"Exterior\s*colou?r(?!\s*/)\s*[:\n\t ]+\s*([A-Z0-9]{2,8})\b",
        re.I,
    ),
    # Vauxhall (Opel): "Color Option\tGAZ" or "Color Option\tGAZ (40R)".
    # The code is the bare token after the label; we don't capture the
    # parenthesised sub-code that sometimes follows.
    re.compile(
        r"Color\s*Option\s*[:\n\t ]+\s*([A-Z0-9]{2,8})\b",
        re.I,
    ),
    # Fiat / Alfa Romeo / Abarth / Jeep (Stellantis Italian side):
    # "COLEST\n679\nCOLORE ESTERNO (Siva metalik) (679)"
    # The code is on the line immediately after the COLEST label.
    # COLINT (interior) has the same shape but a distinct label.
    re.compile(
        r"COLEST\s*\n\s*([A-Z0-9]{2,8})\b",
        re.I,
    ),
    # Land Rover (newer models): "Paint Exterior Body Colour\nEiger grey-JBC2409".
    # Code is the suffix after the final dash. Anchored to [A-Z]{2,}\d{2,}
    # to require both letters and digits — keeps us from matching things
    # like "Java Black" (older LR, no code) or "Exterior Paint - Caesium
    # Blue" (Jaguar uses the same label but with a leading dash sub-
    # format that doesn't include a real code).
    re.compile(
        r"Paint\s*Exterior\s*Body\s*Colou?r\s*\n\s*"
        r".+?-([A-Z]{2,}\d{2,})\b",
        re.I,
    ),
    # BMW/MINI: "Color\nSTERLINGGRAU (472)" — code in parens after the
    # colour name. Label is just "Color" / "Colour" / "Farbe" with no
    # trailing "Code". The colour-name run may include letters, digits,
    # spaces, hyphens and slashes (e.g. "BLACK SAPPHIRE METALLIC").
    re.compile(
        r"(?:Exterior\s*)?(?:Colou?r|Farbe)\s*[:\n]\s*"
        r"[A-Z0-9][A-Z0-9 \-/]*?"
        r"\s*\(\s*([A-Z0-9]{2,8})\s*\)",
        re.I,
    ),
    # PSA (Peugeot/Citroën/DS): "BODY COLOUR\nEVL - PLATINUM GREY PAINT".
    # The code precedes the name, separated by " - ", and the value
    # always ends with " PAINT". Anchor on both the specific "BODY
    # COLOUR" label (so we don't match "UPHOLSTERY COLOUR" etc.) and the
    # trailing "PAINT" word so we don't run into the next row.
    #
    # Separator between label and value can be \n, \t or run of spaces —
    # depends on how Playwright's text extractor renders the PSA popup's
    # table cells. Two-tone paint (e.g. DS) shows as "EZR/EXY - ..." so
    # we capture only the first code and ignore an optional /SECOND.
    re.compile(
        r"BODY\s*COLOU?R\s*[:\n\t ]+\s*"
        r"([A-Z0-9]{2,6})"            # primary code
        r"(?:/[A-Z0-9]{2,6})?"        # optional second code (two-tone)
        r"\s*-\s*"
        r".+?"                        # the name (consumed but not captured here)
        r"\s+PAINT\b",
        re.I,
    ),
    re.compile(
        r"(?:Paint\s*Code|Colou?r\s*Code|Farbcode|Lackcode)"
        r"\s*[:\n]\s*([A-Z0-9]{2,8})",
        re.I,
    ),
]

# Captures the human-readable colour name where the page provides one.
# Known formats:
#  - BMW/MINI:    "Color\nSTERLINGGRAU (472)" -> "STERLINGGRAU"
#  - PSA:         "BODY COLOUR\nEVL - PLATINUM GREY PAINT" -> "PLATINUM GREY"
#  - Fiat family: "COLEST\n679\nCOLORE ESTERNO (Siva metalik) (679)" -> "Siva metalik"
#  - Land Rover:  "Paint Exterior Body Colour\nEiger grey-JBC2409" -> "Eiger grey"
# VW/Audi and Nissan/Vauxhall don't include a colour name in their paint
# row, so this returns "" for those.
PAINT_DESCRIPTION_PATTERNS = [
    # BMW/MINI format
    re.compile(
        r"(?:Exterior\s*)?(?:Colou?r|Farbe)\s*[:\n]\s*"
        r"([A-Z0-9][A-Z0-9 \-/]*?)"            # the colour name
        r"\s*\(\s*[A-Z0-9]{2,8}\s*\)",          # immediately followed by (code)
        re.I,
    ),
    # PSA (Peugeot/Citroën/DS) BODY COLOUR is NOT handled here — its field
    # is too irregular for a single capture group (code-prefixed, no-code,
    # leading-PAINT two-tone). It's normalised by _extract_psa_body_colour,
    # which extract_paint_description calls before this pattern list.
    # Fiat family: "COLEST\nCODE\nCOLORE ESTERNO (name) (code)"
    # Jeep uses "EXTERNAL COLOR (code)" with no inner-parens name, so the
    # inner group is optional. When Jeep matches, group(1) is None — the
    # extraction function below returns "" for that.
    re.compile(
        r"COLEST\s*\n\s*[A-Z0-9]{2,8}\s*\n\s*"
        r"(?:COLORE\s*ESTERNO|EXTERNAL\s*COLOR)\s*"
        r"(?:\((.+?)\)\s*)?"
        r"\(\s*[A-Z0-9]{2,8}\s*\)",
        re.I,
    ),
    # Land Rover format
    re.compile(
        r"Paint\s*Exterior\s*Body\s*Colou?r\s*\n\s*"
        r"(.+?)-[A-Z]{2,}\d{2,}\b",
        re.I,
    ),
    # Jaguar and older Land Rover (Defender etc.): the "Paint Exterior
    # Body Colour" label is present but the value is just a colour name
    # with no extractable code — Jaguar uses "Exterior Paint - Caesium
    # Blue", older LR just "Java Black". Capturing the name into the
    # description column means coloureg can at least show users the
    # colour even when partslink24 has no code. paint_code stays empty.
    # Negative lookahead excludes the newer-LR "<name>-<CODE>" form so
    # this only fires when there isn't a real code.
    re.compile(
        r"Paint\s*Exterior\s*Body\s*Colou?r\s*\n\s*"
        r"(?!.+?-[A-Z]{2,}\d{2,}\b)"
        r"(?:Exterior\s*Paint\s*-\s*)?"
        r"([^\n]+?)\s*$",
        re.I | re.M,
    ),
    # Volvo / Polestar: paint info rendered as two rows in the popup —
    # left cell has the 5-digit catalogue code (e.g. "49800"), right
    # cell has the 3-digit commercial code plus the colour name (e.g.
    # "498 Caspian Blue"). We pick up the description from the right cell.
    #
    # The code and name MUST be on the same line (separated by space/tab,
    # not a newline). The earlier version used "\d{3}\s+<name>" where \s
    # spans newlines, which false-matched Lexus/Toyota's "Exterior color\n
    # 085\nInterior color": it took the 3-digit code, crossed the newline,
    # and grabbed the NEXT field's label "Interior color" as the colour.
    # Requiring [ \t]+ between code and name (and $ line-anchoring under
    # re.M) keeps the match inside the Volvo right-cell line. Lexus, whose
    # code stands alone with a newline before the next field, no longer
    # matches.
    re.compile(
        r"Exterior\s*colou?r[:\t ]*[\t\n][ \t]*"
        r"\d{3}[ \t]+"                    # 3-digit commercial code, SAME line
        r"([A-Za-z][A-Za-z0-9 \-/]+?)\s*$",  # the colour name, to line end
        re.I | re.M,
    ),
    # Volvo / Polestar SEPARATED layout (seen on EX30, XC60): the Vehicle-
    # data tab is two columns of label/value pairs, so the colour code and
    # name sit in DIFFERENT pairs, both labelled "Exterior color":
    #   Exterior color \n 62600 \n Exterior color \n CLOUD BLUE
    # i.e. <label> <5-digit code> <label-again> <name>. Distinct from the
    # joined "<3-digit> <name>" form above (Osmium Grey / Luminous Sand).
    # The doubled "Exterior color" label is the anchor — Lexus/Nissan/
    # Toyota have only ONE such label, so they can't match this and don't
    # get their next-field label mis-captured (the Lexus "Interior Color"
    # bug). The code here is the 5-digit catalogue form; extract_paint_code
    # still picks it up via the Nissan/Volvo code pattern + _normalise_code.
    re.compile(
        r"Exterior\s*colou?r[ \t]*[\t\n][ \t]*"
        r"\d{3,5}[ \t]*[\t\n][ \t]*"               # first pair: the code
        r"Exterior\s*colou?r[ \t]*[\t\n][ \t]*"    # second "Exterior color" label
        r"([A-Za-z][A-Za-z0-9 \-/]+?)\s*$",          # the colour name
        re.I | re.M,
    ),
    # Ford (passenger): the VIN-dialog "Vehicle data" table renders a row
    # "Exterior Paint\t<colour>" — the label is exactly "Exterior Paint"
    # in one cell and the colour NAME in the next. partslink24 carries no
    # paint CODE for Ford passenger cars, so this fills the description
    # column only (paint_code stays empty -> outcome name_only, but
    # coloureg can at least show the colour name).
    #
    # The value cell may be a bare name ("Flame") or carry a finish in
    # parens ("Panther Black (Metallic)"); we keep the finish because it
    # matters for paint matching. Hence the capture class allows "()" and
    # "&" in addition to letters/digits/space/hyphen/slash.
    #
    # False-match hazards on the same Ford page, both in the Equipment
    # tab, both excluded:
    #   "Exterior Paint Pack\tExterior Paint - Solid"
    # We require a cell boundary (tab or newline) IMMEDIATELY after
    # "Paint", which "Paint Pack" (space + word) does not have, so the
    # label side can't match; and the captured value rejects a leading
    # "-", so the value side "Exterior Paint - Solid" can't match either.
    #
    # Placed last so the brand-specific patterns above (notably the
    # Jaguar/older-LR "Exterior Paint - <name>" name-only fallback) win
    # first; this only fires when nothing else has.
    re.compile(
        r"Exterior\s*Paint[ \t]*[\t\n]\s*"      # label + cell boundary
        r"(?!-)"                                 # not the "- Solid" value cell
        r"(?!Interior\b)"                        # not the NEXT field's label:
                                                 # when the Exterior Paint cell
                                                 # is empty (seen on US Fords,
                                                 # e.g. 1FA6...), the rendered
                                                 # text collapses to "Exterior
                                                 # Paint\nInterior Fabric" and
                                                 # the capture would otherwise
                                                 # grab "Interior Fabric" as a
                                                 # bogus colour. Interior * is
                                                 # the consistent follow-on row
                                                 # across all observed Fords.
        r"([A-Za-z][A-Za-z0-9 ()&\-/]*?)\s*$",   # the colour name
        re.I | re.M,
    ),
]

VEHICLE_DATA_NEEDLE = re.compile(
    # We use this regex to decide "has the result page finished loading
    # the data we care about". It must match ONLY when a paint-code-
    # bearing field is actually on the page — NOT just a page heading
    # like "Vehicle Identification", because partslink24's older popups
    # (Nissan/Vauxhall/Stellantis/Ford/etc.) render the heading first
    # and the body content a second or two later, and exiting the poll
    # loop on the heading caused intermittent false negatives ("paint
    # code not found").
    #
    # Patterns below mirror PAINT_CODE_PATTERNS — when any matches, we
    # can confidently extract from the page.
    r"Paint\s*Code|Lackcode|Farbcode|Colou?r\s*Code|"
    r"BODY\s*COLOU?R|"                     # PSA
    r"COLEST|"                             # Fiat / Stellantis Italian side
    r"Color\s*Option|"                     # Vauxhall
    r"Paint\s*Exterior\s*Body\s*Colou?r|"  # Land Rover / Jaguar
    r"Exterior\s*colou?r\s*[/:\n\t ]|"     # VW / Nissan / Toyota / Lexus
    # Ford passenger: label "Exterior Paint" + a cell boundary (tab or
    # newline) immediately after. The boundary is what keeps this from
    # tripping on the Equipment-tab "Exterior Paint Pack" row (space +
    # word, no boundary) before the Vehicle-data tab has rendered.
    r"Exterior\s*Paint[ \t]*[\t\n]|"
    r"(?:Colou?r|Farbe)\s*\n\s*[A-Z0-9][A-Z0-9 \-/]*\(\s*[A-Z0-9]{2,8}\s*\)",
    re.I,
)

VIN_NOT_FOUND_PHRASES = (
    "no vehicle found",
    "vehicle not found",
    "no data found",
    # partslink24's "Tip" popup wording when a VIN isn't in their data:
    # "No data was found for the searched vehicle identification number
    # (VIN). Either this VIN does not exist or the data of the associated
    # vehicle has not been entered yet." Note the "was" — distinct from
    # the "no data found" variant above, so both are needed. Catching this
    # in the poll loop makes an unresolvable VIN fail within ~300ms instead
    # of eating the full 10s VEHICLE_DATA_NEEDLE timeout.
    "no data was found",
    "could not be assigned to a distinct model",
    "kein fahrzeug",
    "nicht gefunden",
    "vin invalid",
    "invalid vin",
)

# partslink24 sometimes switches OFF VIN identification for an entire
# brand, showing: "We regret to inform you that the identification of
# VINs for this brand will not be available for an indefinite period of
# time. We are currently working with the manufacturer to find a
# solution." (seen on Dacia). This is a brand-wide, temporary,
# partslink24-side disablement — NOT a missing VIN and NOT an unsupported
# brand. We detect it to (a) fail fast in the poll instead of waiting the
# full 10s, and (b) categorise it distinctly as brand_unavailable so these
# VINs read as "retry later" rather than dead.
#
# A REGEX with \s+ (not a literal substring) because the rendered text
# carries an embedded newline mid-phrase ("will not\nbe available"), which
# a plain substring check misses. The fragment is the most stable,
# distinctive part of the message; if partslink24 reword it, detection
# silently reverts to the slower page_load_timeout path (the lookup is
# still attempted and still works the moment the brand returns).
BRAND_UNAVAILABLE_RE = re.compile(
    r"will\s+not\s+be\s+available\s+for\s+an\s+indefinite\s+period", re.I
)


# When wait_for_vehicle_data times out we'd normally treat that as
# "page didn't load". But some result pages genuinely don't have any
# paint info at all (e.g. older PSA vehicles where the catalogue knows
# the model but only has "PAINT TYPE", not "BODY COLOUR"). In those
# cases the page IS loaded, just without anything our paint patterns
# can match — so we should report "paint code not found" rather than
# the misleading "vehicle data did not load (timeout)" error. This
# regex detects the page-loaded state via field labels that appear on
# the result page regardless of whether paint info is present.
PAGE_LOADED_NEEDLE = re.compile(
    r"Vehicle\s*Identification\s*No\.?|"   # the VIN field label (not the heading)
    r"PAINT\s*TYPE|"                       # PSA pages without BODY COLOUR
    r"COMMERCIAL\s*MARQUE",                # PSA generic field
    re.I,
)


def collect_all_text(page: Page) -> str:
    parts = []
    for fr in page.frames:
        try:
            parts.append(fr.locator("body").inner_text(timeout=2_000))
        except Exception:
            continue
    return "\n".join(parts)


def wait_for_vehicle_data(page: Page, timeout_ms: int = 10_000) -> str | None:
    waited = 0
    # Tight polling (300ms) so we detect the data within ~300ms of when
    # the page is actually ready. The check inside the loop is cheap
    # (one frame-text pull + a couple of regex matches); the wall-clock
    # win on fast pages is worth the small extra CPU.
    interval = 300
    text = ""
    while waited < timeout_ms:
        text = collect_all_text(page)
        lower = text.lower()
        if BRAND_UNAVAILABLE_RE.search(text):
            return text
        if any(p in lower for p in VIN_NOT_FOUND_PHRASES):
            return text
        if VEHICLE_DATA_NEEDLE.search(text):
            return text
        page.wait_for_timeout(interval)
        waited += interval
    # Timed out waiting for paint info. If the page nonetheless looks
    # like a fully-loaded vehicle-data page (some PSA vehicles have no
    # BODY COLOUR row at all), return what we have so we surface the
    # honest "paint code not found" error rather than "timeout".
    if text and PAGE_LOADED_NEEDLE.search(text):
        return text
    return None


def _extract_hyundai_kia_colour(text: str) -> tuple[str, str]:
    """(code, description) from a Hyundai/Kia "Exterior color" row, or
    ("","") if the field isn't a Hyundai/Kia-style name.

    Hyundai and Kia put the colour NAME where Nissan/Toyota put a code,
    in the same "Exterior color" field — which is why the Nissan code
    pattern used to grab name-words (ELECTRIC, PHANTOM, SLEEK, MACHINE)
    that _is_valid_code then rejected, leaving both code and description
    blank. Observed real values (6 Hyundai + 4 Kia):

        CREAMY WHITE [TCW]              name + bracket code  (code rare)
        CHAMPION BLUE / SHADOW GRAY     name only
        PHANTOM BLACK PEARL             name only
        JD HP / TRICOAT WHITE PEARL     name with messy spec prefix
        CD NEW BROWN EXT LEATHER BEIGE  messy Kia internal string

    We capture the NAME into description (verbatim — we deliberately do
    NOT try to surgically strip the "JD HP /" / "EXT LEATHER" noise on the
    messy Kia values, because guessing risks emitting a WRONG colour, which
    is worse than a slightly noisy-but-correct one), and a trailing
    "[CODE]" into the paint code when present.

    Disambiguation from Nissan: this fires only when the value is a NAME
    (contains a space, or is 4+ all-alpha chars). Nissan's bare "KAD" /
    "Z11" code has no space and is <=3 chars, so it does NOT qualify and is
    left to the Nissan code pattern. Without this guard the Nissan pattern
    would also wrongly grab the 2-char "JD"/"CD" prefixes off the messy Kia
    values as if they were paint codes.
    """
    m = re.search(r"Exterior\s*colou?r[ \t]*[\t\n][ \t]*([^\n\t]+)",
                  text, re.I)
    if not m:
        return "", ""
    val = m.group(1).strip()
    code = ""
    bm = re.search(r"\[([A-Z0-9]{1,4})\]\s*$", val)
    if bm:
        code = bm.group(1)
        val = val[:bm.start()].strip()
    # Don't claim a value that STARTS with a 3-digit code + space — that's
    # the Volvo/Polestar right-cell shape ("498 Caspian Blue"), handled by
    # its own pattern. Hyundai/Kia names never lead with "<3 digits> ".
    if re.match(r"\d{3}\s", val):
        return "", ""
    is_name = (" " in val) or (len(val) >= 4 and val.replace(" ", "").isalpha())
    if not is_name:
        return "", ""
    return code, val


def extract_paint_code(text: str) -> str:
    """Try each pattern in order; return the first match that survives
    validation. Patterns are ordered most-specific-first, so the natural
    case is that the first match is correct. The validation step exists
    to handle pages where a less-specific pattern (notably the Nissan-
    style 'Exterior color\\tCODE') matches a colour-name word like
    'ELECTRIC' or 'PHANTOM' as if it were a code. When that happens we
    skip and try the next pattern, rather than returning the bad token.
    """
    # smart first: its two-part "Paint Code" field lists the tridion frame
    # code before the body code, so the generic patterns would grab the
    # frame. The helper picks the body-panel code (and is a no-op on every
    # non-smart page, anchored on the word "tridion").
    smart_code, _ = _extract_smart_colour(text)
    if smart_code and _is_valid_code(smart_code):
        return _normalise_code(smart_code.upper())
    # Hyundai/Kia first: their name-style "Exterior color" field may carry
    # a bracket code (rare). Claiming the field here also stops the Nissan
    # pattern below from grabbing a messy Kia prefix ("JD"/"CD") as a false
    # code. If they had a name but NO code, we must still not fall through
    # to the Nissan pattern (it would grab the name-word), so we return
    # early for that case too.
    hk_code, hk_desc = _extract_hyundai_kia_colour(text)
    if hk_code and _is_valid_code(hk_code):
        return _normalise_code(hk_code.upper())
    if hk_desc:                       # Hyundai/Kia name present, no usable code
        return ""
    for pat in PAINT_CODE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        candidate = _normalise_code(m.group(1).upper())
        if _is_valid_code(candidate):
            return candidate
    return ""


def _is_valid_code(code: str) -> bool:
    """Reject English-word false positives that the Nissan/Vauxhall
    patterns can grab when a page shows the colour *name* in the same
    cell where other brands show a code (Hyundai/Kia behaviour).

    Empirical rule based on every real code we've observed across 40+
    brands: a genuine manufacturer paint code either (a) contains a
    digit (e.g. Z11, 1H9, A7W, 49000, JBC2409, 955) or (b) is 3
    characters or fewer (e.g. JD, CD, KAD, NAJ). Mis-captured colour
    words like ELECTRIC, PHANTOM, SLEEK, MACHINE, METALLIC, CHAMPION
    are 4+ letters all-alphabetic and get rejected.

    Revisit if a real 4+-letter all-alpha code ever turns up.
    """
    if not code:
        return False
    if len(code) <= 3:
        return True
    return any(c.isdigit() for c in code)


def _normalise_code(code: str) -> str:
    """Post-process a raw extracted paint code.

    Volvo/Polestar pages show paint codes as 5-digit values padded with
    "00" (e.g. "49000", "71200", "71900"). The actual commercial code
    paint suppliers use is the 3-digit prefix ("490", "712", "719"). We
    trim only when the value is exactly 5 digits and ends in "00" — a
    pattern that as far as we've observed is unique to Volvo's catalogue
    format. Any other shape (alphanumeric like "A7W", 3-digit like
    "955", longer like "JBC2409") is returned unchanged.
    """
    if len(code) == 5 and code.isdigit() and code.endswith("00"):
        return code[:3]
    return code


# A whole token that is a valid Roman numeral II..XXXIX (length >= 2 so a
# stray single "I"/"V"/"X" word isn't caught). Used to undo the one place
# str.title() gets colour names wrong: it lowercases Roman numerals, e.g.
# "MIDNIGHT BLACK II" -> "Midnight Black Ii". Deliberately strict — it
# matches only complete, valid numerals, so real words built from the same
# letters (Ivy, Mix, Ill, Civic, Ivory) are left untouched.
_ROMAN_NUMERAL = re.compile(
    r"^(?:I{1,3}|IV|VI{0,3}|IX|X{1,3}(?:I{1,3}|IV|VI{0,3}|IX)?)$"
)


def _titlecase_colour(name: str) -> str:
    """Title Case a colour name, then re-uppercase any standalone Roman
    numeral token (str.title() would lower it to 'Ii'/'Iv'/etc.)."""
    out = []
    for word in name.title().split(" "):
        if len(word) >= 2 and _ROMAN_NUMERAL.match(word.upper()):
            out.append(word.upper())
        else:
            out.append(word)
    return " ".join(out)


def _smart_balanced_paren_groups(s: str):
    """Yield (code, inner_text) for each '<CODE> (...)' in s, matching
    parentheses with balanced nesting so a code's full description is
    captured even when it contains inner parens like '(Inv (Body...))'."""
    for m in re.finditer(r"\b([A-Z]{1,2}[A-Z0-9]{1,3})\s*\(", s):
        code = m.group(1)
        depth = 0
        i = m.end() - 1          # position of the opening "("
        start = i
        while i < len(s):
            if s[i] == "(":
                depth += 1
            elif s[i] == ")":
                depth -= 1
                if depth == 0:
                    yield code, s[start + 1:i]
                    break
            i += 1


def _extract_smart_colour(text: str) -> tuple[str, str]:
    """(code, description) for smart's two-part "Paint Code" field.

    smart bodies have TWO colour components — the tridion safety cell
    (frame) and the body panels — so the field lists two codes, e.g.:
        Paint Code
        EB2 (Invalid (tridion safety cell, silver))  EAZ (Body panels in white)
    The body-panel colour (EAZ / white) is the one worth matching, not the
    tridion frame (EB2 / silver), but the frame code comes FIRST so the
    generic code patterns would grab it. This helper SKIPS the tridion
    component and returns the other one.

    Notes from four real pages:
      - The frame component always says "tridion safety cell"; that is the
        reliable marker. We skip it and take the other code.
      - The body component is NOT reliably labelled "Body": three pages say
        "Body panels in <colour>" / "Body in <colour>", but a fourth just
        says the colour, "EAA (Light blue metallic)". So we must NOT key off
        the word "Body" — keying off the frame's "tridion" and taking the
        remainder is what generalises.
      - "Inv"/"Invalid" is on the frame always and the body sometimes, so
        it is not a discriminator; we just skip a bare "Inv" code token.
      - Descriptions: prefer a colour word after "in"/comma; otherwise fall
        back to the whole parenthetical ("Light blue metallic").

    Anchored on the smart-only word "tridion" so it can't fire on any
    other brand's page.
    """
    if "tridion" not in text.lower():
        return "", ""
    m = re.search(
        r"Paint\s*Code\s*[:\n\t ]+(.*?)(?:\n(?:Interior|Engine|Transmission)\b)",
        text, re.I | re.S,
    )
    if not m:
        return "", ""
    for code, inner in _smart_balanced_paren_groups(m.group(1)):
        if code.lower() == "inv":
            continue
        if re.search(r"tridion", inner, re.I):   # skip the frame component
            continue
        cm = (re.search(r"\bin\s+([a-z]+)", inner, re.I)
              or re.search(r",\s*([a-z]+)\s*$", inner, re.I))
        desc = cm.group(1) if cm else inner.strip()
        return code, desc
    return "", ""


def _extract_psa_body_colour(text: str) -> str:
    """Generalised PSA (Peugeot/Citroën/DS) BODY COLOUR name extractor.

    PSA's BODY COLOUR field is irregular — observed across real DS/Citroën
    pages in at least these shapes, all on the value line right after a
    line-leading "BODY COLOUR" label:

        EVL - PLATINUM GREY PAINT          (code, name, trailing PAINT)
        CHRYSTAL PEARL PAINT               (no code, name, trailing PAINT)
        ERU/EXY - PAINT WHISPER+BLACK ONYX (two-tone code, LEADING PAINT,
                                            name parts joined by "+")

    Rather than chase each shape with its own regex, we capture the whole
    value (to end of line — every real value is single-line, so this can't
    bleed into the next field) and normalise:
      1. strip a leading "<code> - " / "<code>/<code> - " prefix
      2. drop the noise word PAINT wherever it sits (lead/mid/trail)
      3. turn the two-tone "+" joiner into " + "
      4. collapse whitespace and Title Case

    BODY COLOUR is anchored to line start so it does NOT match the tail of
    Jaguar/Land Rover's "Paint Exterior Body Colour" label (handled by
    their own patterns).

    Returns "" if there's no BODY COLOUR field or it cleans to nothing.
    """
    m = re.search(r"(?:^|\n)\s*BODY\s*COLOU?R\s*[:\n\t ]+\s*([^\n]+)",
                  text, re.I)
    if not m:
        return ""
    v = m.group(1).strip()
    # Volvo's equipment tab has a "BODY COLOR   626 POWDER BLUE" line whose
    # value LEADS with a 3-digit code. PSA values never do, so reject that
    # shape here — it belongs to Volvo's own (separated) pattern, and
    # matching it would mis-fill PSA's slot with a Volvo colour.
    if re.match(r"\d{3}\s", v):
        return ""
    v = re.sub(r"^[A-Z0-9]{2,6}(?:/[A-Z0-9]{2,6})?\s*-\s*", "", v, flags=re.I)
    v = re.sub(r"\bPAINT\b", " ", v, flags=re.I)
    v = v.replace("+", " + ")
    v = re.sub(r"\s+", " ", v).strip()
    return _titlecase_colour(v)


def extract_paint_description(text: str) -> str:
    """Extract the human-readable colour name (e.g. "STERLINGGRAU") and
    return it Title Cased ("Sterlinggrau"). Returns "" if no description
    is on the page (e.g. VW/Audi don't include one in the paint row, and
    Jeep's COLEST row has the code but no inner-parens name)."""
    # smart two-part "Paint Code" — pick the body-panel colour word.
    _, smart_desc = _extract_smart_colour(text)
    if smart_desc:
        return _titlecase_colour(smart_desc)
    # Hyundai/Kia name-style "Exterior color" — checked before the pattern
    # list (and before PSA) since their colour name lives in a field other
    # brands use for a code.
    _, hk_desc = _extract_hyundai_kia_colour(text)
    if hk_desc:
        return _titlecase_colour(hk_desc)
    # PSA BODY COLOUR is handled by a dedicated normaliser (its field is
    # too irregular for a single capture group); checked first so its
    # cleaning wins over any generic pattern that might partially match.
    psa = _extract_psa_body_colour(text)
    if psa:
        return psa
    for pat in PAINT_DESCRIPTION_PATTERNS:
        m = pat.search(text)
        if m:
            return _titlecase_colour(m.group(1).strip()) if m.group(1) else ""
    return ""


def vin_error_in_text(text: str) -> str | None:
    if BRAND_UNAVAILABLE_RE.search(text):
        # Canonical, whitespace-normalised string so downstream categorise()
        # and the human-readable error column don't carry the embedded
        # newline from the rendered page.
        return "brand VIN identification unavailable (indefinite)"
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
    paint_code: str = ""
    paint_description: str = ""
    via: str = ""       # "catalog", "dashboard", or "" on failure
    error: str = ""
    outcome: str = ""   # categorised classification, set by categorise()


# Fixed vocabulary of outcome categories. Used for triage and analysis —
# the human-readable `error` column stays as-is for debugging, this is
# the machine-parseable companion for filtering results.csv. Adding a
# new category requires updating both this set and `categorise()` below.
OUTCOMES = frozenset({
    "success",            # paint code was extracted
    "name_only",          # description captured but no code (Jaguar, old LR)
    "not_found_as_routed", # the lookup attempts we made all said "not here"
                           # — could be VIN genuinely absent from partslink24,
                           # or VIN present but we routed to the wrong brand
                           # (e.g. forgot category=N1 for a Sprinter). The
                           # label asserts something about the *attempt*, not
                           # a definitive claim about the database.
    "unsupported_brand",  # make not in MAKE_TO_BRAND (Honda, Maserati, etc.)
    "brand_unavailable",  # partslink24 has VIN identification switched OFF for
                          # this whole brand "for an indefinite period" (seen
                          # on Dacia). Distinct from unsupported_brand (never
                          # carried) and not_found_as_routed (this VIN absent):
                          # it's a TEMPORARY, retryable state — the brand works
                          # again the moment partslink24 restores it, with no
                          # code change. Labelled distinctly so these VINs read
                          # as "retry later", not permanently dead.
    "page_load_timeout",  # catalog/dashboard never loaded the vehicle data
    "paint_data_missing", # page loaded but no paint info (old PSA, etc.)
    "catalog_ui_error",   # VIN box never visible/editable
    "auth_error",         # login/session failure
    "missing_input",      # no make supplied in lookups.txt
    "unknown",            # anything not yet categorised
})


def categorise(result: "LookupResult") -> str:
    """Classify a finished lookup into one of OUTCOMES.

    Pure function of the final-state fields. The ordering of checks
    matters: more specific/authoritative signals are checked first.
    Notably, the dashboard's 'could not be assigned' verdict is treated
    as authoritative even if an upstream catalog timed out — that's the
    final word on whether partslink24 knows the VIN.
    """
    if result.paint_code:
        return "success"

    err = (result.error or "").lower()

    # Most specific signals first
    if "unknown make" in err:
        return "unsupported_brand"
    if "no make supplied" in err:
        return "missing_input"
    if "login failed" in err or "could not open catalog after re-login" in err:
        return "auth_error"

    # partslink24 has VIN identification switched off brand-wide (Dacia).
    # Authoritative and checked early: it wins over a downstream dashboard
    # timeout in the combined error string, because the brand notice is the
    # real reason the lookup can't succeed. Keys off the canonical string
    # vin_error_in_text emits (not the raw page text).
    if "brand vin identification unavailable" in err:
        return "brand_unavailable"

    # Paint-code-not-found path: differentiate "vehicle has a colour name
    # but no code" (Jaguar/old LR) from "page loaded with no paint info at
    # all" (old PSA)
    if "paint code not found" in err:
        if result.paint_description:
            return "name_only"
        return "paint_data_missing"

    # Authoritative "not in database" markers — checked before timeouts
    # because dashboard's verdict overrides an upstream catalog timeout.
    # Note: this is "not found by the routing we attempted", not a
    # definitive claim about partslink24's database — a Sprinter VIN
    # routed to passenger Mercedes will land here too.
    #
    # Reuses VIN_NOT_FOUND_PHRASES (the same set wait_for_vehicle_data
    # detects mid-poll) rather than a separate inline list, so the two
    # can't drift apart. "could not be assigned" is matched as a substring
    # of the full "could not be assigned to a distinct model" phrase.
    if any(p in err for p in VIN_NOT_FOUND_PHRASES):
        return "not_found_as_routed"

    if "did not load" in err or "timeout" in err:
        return "page_load_timeout"

    if "vin box" in err or "search vin" in err:
        return "catalog_ui_error"

    return "unknown"


def _populate_from_text(result: LookupResult, text: str) -> None:
    result.paint_code = extract_paint_code(text)
    result.paint_description = extract_paint_description(text)


def _process_result_page(page: Page, vin: str, result: LookupResult,
                        debug: bool, *,
                        debug_suffix: str = "",
                        error_prefix: str = "",
                        timeout_msg: str = "vehicle data did not load (timeout)",
                        no_paint_msg: str = "paint code not found on result page",
                        ) -> tuple[bool, str | None]:
    """Wait for the post-submit result page, then extract the paint code.

    Shared by _try_catalog and _try_dashboard: both submit a VIN, wait
    for the same kind of vehicle-data page, run the same extractors, and
    have the same three failure modes (timeout, vin-not-found, no paint
    code). Differences are confined to the debug-filename suffix and the
    exact error wording, passed via kwargs so each caller keeps the same
    strings it produced before (historical results.csv rows depend on
    that wording).

    Returns (True, None) on success or (False, reason) on failure."""
    def dump():
        # Failure-path dumps: --debug OR --dump-always both write here.
        if debug or DUMP_ALWAYS:
            dump_debug(page, vin + debug_suffix)

    text = wait_for_vehicle_data(page, timeout_ms=10_000)
    if text is None:
        dump()
        return False, f"{error_prefix}{timeout_msg}"

    err = vin_error_in_text(text)
    if err:
        dump()
        return False, f"{error_prefix}{err}"

    _populate_from_text(result, text)
    if not result.paint_code:
        dump()
        return False, f"{error_prefix}{no_paint_msg}"
    # Success path: dump ONLY under --dump-always. Plain --debug must stay
    # failure-only (its long-standing contract), so we deliberately do not
    # call dump() here — we check DUMP_ALWAYS directly.
    if DUMP_ALWAYS:
        dump_debug(page, vin + debug_suffix)
    return True, None


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
        ok, err = submit_vin(catalog, vin, source="catalog")
        if not ok:
            if debug:
                dump_debug(catalog, vin)
            return False, f"{err} ({brand} catalog)"

        log("VIN submitted, waiting up to 10s for vehicle data")
        return _process_result_page(catalog, vin, result, debug)
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
    elif page.locator('h1, h2').filter(has_text="Attention").first.count():
        log("dashboard fallback: attention page shown, re-logging in")
        login(page)

    ok, err = submit_vin(page, vin, source="dashboard")
    if not ok:
        if debug:
            dump_debug(page, vin + "_dashboard")
        return False, f"dashboard fallback: {err}"

    log("dashboard VIN submitted, waiting up to 10s for vehicle data")
    return _process_result_page(
        page, vin, result, debug,
        debug_suffix="_dashboard",
        error_prefix="dashboard fallback: ",
        timeout_msg="vehicle data did not load",
        no_paint_msg="paint code not found",
    )


def lookup_vin(page: Page, row: LookupRow, debug: bool = False,
               allow_dashboard_fallback: bool = True) -> LookupResult:
    """Catalog-first VIN lookup with optional dashboard fallback."""
    result = LookupResult(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        vin=row.vin,
    )

    brand, explanation = resolve_brand(row.make, row.category)

    # Two separate trackers — deliberately split:
    #   catalog_error    accumulates ALL legs for the human-readable
    #                    `error` column. Compound string, OK to be messy.
    #   last_leg_error   holds ONLY the most recent leg's error string.
    #                    Used to decide whether to skip the dashboard,
    #                    which must reflect the latest authoritative
    #                    outcome — not a substring scan of the compound.
    catalog_error: str | None = None
    last_leg_error: str | None = None

    if brand and brand in BRAND_CATALOG_SERVICE:
        log(f"looking up {row.vin}  {explanation}")
        ok, err = _try_catalog(page, row.vin, brand, result, debug)
        if ok:
            result.via = "catalog"
            return result
        catalog_error = err
        last_leg_error = err
        log(f"catalog attempt failed: {err}")

        # Commercial-sibling retry: for a Mercedes commercial vehicle the
        # routed catalogue (Vans or Trucks) may be wrong because the
        # N1/N2/N3 category can't be reliably derived from the VIN. Try
        # the sibling commercial catalogue before anything else. Skipped
        # for "paint code not found" (the page DID load — wrong category
        # would have failed with "vehicle not found" instead, so a
        # paint-not-found here means the right catalogue but no code).
        sibling = COMMERCIAL_FALLBACK.get(brand)
        if (sibling and sibling in BRAND_CATALOG_SERVICE
                and "paint code not found" not in (last_leg_error or "").lower()
                and "brand vin identification unavailable"
                    not in (last_leg_error or "").lower()):
            log(f"trying commercial sibling: {sibling}")
            result.paint_code = ""
            result.paint_description = ""
            ok, err2 = _try_catalog(page, row.vin, sibling, result, debug)
            if ok:
                result.via = "catalog:commercial"
                return result
            log(f"commercial sibling failed: {err2}")
            catalog_error = f"{catalog_error}; {sibling}: {err2}"
            last_leg_error = err2
            # If the sibling resolved the brand family, prefer Classic
            # fallback for the sibling too (covered by CLASSIC_SIBLING
            # which already maps both Vans and Trucks to MB Classic).
            brand = sibling

        # Classic-sibling retry: many partslink24 brands have a separate
        # Classic catalogue for out-of-production models. We can't tell
        # upfront which side a VIN belongs to (see comment on
        # CLASSIC_SIBLING), so we try the modern catalogue first and
        # fall back to Classic only if it failed.
        #
        # But skip Classic when the modern catalog returned "paint code
        # not found": that means the vehicle WAS positively identified in
        # the modern catalogue (page loaded, data present, just no code) —
        # so it's definitively a modern model, not a Classic one, and the
        # Classic catalogue can't supply a code partslink24 doesn't have.
        # Trying it just burns a ~10s timeout (observed on a modern MINI
        # whose F-series VIN isn't in MINI Classic). We still try Classic
        # on a timeout or not-found, where the modern catalogue genuinely
        # didn't identify the vehicle and Classic might.
        classic = CLASSIC_SIBLING.get(brand)
        if (classic and classic in BRAND_CATALOG_SERVICE
                and "paint code not found" not in (last_leg_error or "").lower()):
            log(f"trying Classic sibling: {classic}")
            # Reset any partial extraction from the failed attempt.
            result.paint_code = ""
            result.paint_description = ""
            ok, err = _try_catalog(page, row.vin, classic, result, debug)
            if ok:
                result.via = "catalog:classic"
                return result
            log(f"Classic sibling failed: {err}")
            catalog_error = f"{catalog_error}; {classic}: {err}"
            last_leg_error = err
    else:
        # No brand resolvable, or brand has no catalog. Fall through to
        # the dashboard if allowed.
        if not brand:
            catalog_error = explanation  # "no make supplied" / "unknown make ..."
        else:
            catalog_error = f"no catalog URL configured for {brand}"
        last_leg_error = catalog_error
        log(catalog_error)

    if not allow_dashboard_fallback:
        result.error = catalog_error or "lookup failed"
        return result

    # Skip the dashboard fallback when partslink24 returned the vehicle
    # data but had no paint code on it (Jaguar, Ford, Kia, Hyundai, MAN,
    # IVECO — partslink24 just doesn't carry their codes). The dashboard
    # would return the same page from the same database and the same
    # extractors would fail again. ~10s saved per affected lookup.
    #
    # Also skip when the catalog reported the brand as unavailable (Dacia's
    # "VIN identification ... unavailable for an indefinite period"). The
    # dashboard's universal VIN search resolves the brand and routes INTO
    # that same brand catalogue — i.e. straight into the catalogue that
    # just told us it's switched off. There is no other database for it to
    # try, so the dashboard can only hit the identical wall (and time out
    # for 10s doing so). This is a stronger skip case than "no paint code":
    # there the page at least loaded; here the whole brand catalogue is off.
    #
    # Decision keys off `last_leg_error` specifically, not the accumulated
    # `catalog_error` — earlier legs in a multi-step fallback chain may
    # have produced "paint code not found" while a later leg returned a
    # genuine not-found, and only the final leg's verdict should drive
    # whether the dashboard is worth trying.
    #
    # We still try the dashboard for genuine "VIN not found" errors:
    # those CAN succeed via the dashboard's universal search if VDG gave
    # us the wrong make (e.g. a re-badged or imported vehicle filed
    # under a different brand on partslink24). Brand-unavailable is the
    # opposite of that case — the brand is known, it's just disabled — so
    # it skips rather than retries.
    if last_leg_error and (
        "paint code not found" in last_leg_error.lower()
        or "brand vin identification unavailable" in last_leg_error.lower()
    ):
        reason = ("brand unavailable"
                  if "unavailable" in last_leg_error.lower()
                  else "catalog returned data but no paint code")
        log(f"skipping dashboard fallback ({reason})")
        result.error = catalog_error
        return result

    log(f"trying dashboard fallback for {row.vin}")
    # Wipe any partial extracts from the catalog attempt.
    result.paint_code = ""
    result.paint_description = ""

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
    """Retry only on genuine browser-side exceptions (Playwright timeout,
    network errors, etc.). 'No data' / 'paint code not found' / 'VIN not
    in DB' are logical outcomes returned cleanly from lookup_vin, and
    retrying them just wastes time — they'll always produce the same
    result."""
    for attempt in range(EXTRA_RETRIES + 1):
        was_exception = False
        try:
            r = lookup_vin(page, row, debug=debug,
                           allow_dashboard_fallback=allow_dashboard_fallback)
        except PlaywrightTimeoutError as e:
            r = LookupResult(
                timestamp=datetime.now().isoformat(timespec="seconds"),
                vin=row.vin, error=f"timeout: {e}",
            )
            was_exception = True
        except Exception as e:  # noqa: BLE001
            r = LookupResult(
                timestamp=datetime.now().isoformat(timespec="seconds"),
                vin=row.vin, error=f"{type(e).__name__}: {e}",
            )
            was_exception = True

        if r.paint_code or not was_exception:
            r.outcome = categorise(r)
            return r

        if attempt < EXTRA_RETRIES:
            log(f"retrying {row.vin} "
                f"(attempt {attempt + 2}/{EXTRA_RETRIES + 1})")
            page.wait_for_timeout(1500)
    r.outcome = categorise(r)
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
        # Skip the usercentrics cookie-consent cross-domain bridge — same
        # boilerplate JS in every dump, useless for diagnosing partslink24
        # issues, just adds clutter to _debug/.
        if "usercentrics.eu" in (fr.url or ""):
            continue
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
    # Explicit (field, header) pairs — keeps Python field names idiomatic
    # while giving the CSV a friendly Title Case header row.
    columns = [
        ("timestamp",         "Timestamp"),
        ("vin",               "Vin"),
        ("paint_code",        "Paint code"),
        ("paint_description", "Paint description"),
        ("via",               "Via"),
        ("outcome",           "Outcome"),
        ("error",             "Error"),
    ]
    headers = [h for _, h in columns]

    if RESULTS_FILE.exists():
        with RESULTS_FILE.open("r", newline="") as f:
            existing_header = next(csv.reader(f), [])
        if existing_header and existing_header != headers:
            archive = RESULTS_FILE.with_suffix(
                f".old-{datetime.now():%Y%m%d-%H%M%S}.csv"
            )
            shutil.move(str(RESULTS_FILE), str(archive))
            log(f"results.csv header changed; archived old file -> "
                f"{archive.name}")

    new_file = not RESULTS_FILE.exists()
    with RESULTS_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(headers)
        for r in results:
            row = [getattr(r, field, "") for field, _ in columns]
            w.writerow(row)


def run(pw: Playwright, rows: list[LookupRow], headed: bool, debug: bool,
        fresh: bool, skip_brand_check: bool,
        allow_dashboard_fallback: bool,
        dump_always: bool = False) -> list[LookupResult]:
    global DUMP_ALWAYS
    DUMP_ALWAYS = dump_always

    if fresh and STATE_FILE.exists():
        STATE_FILE.unlink()

    if (debug or dump_always) and DEBUG_DIR.exists():
        # Clear stale dumps from previous runs so this run's _debug/
        # only contains what just happened. We only delete files we
        # would have created — html/png — to avoid clobbering anything
        # the user happens to have stashed in there.
        cleared = 0
        for f in DEBUG_DIR.iterdir():
            if f.is_file() and f.suffix.lower() in (".html", ".png"):
                try:
                    f.unlink()
                    cleared += 1
                except OSError:
                    pass
        if cleared:
            log(f"cleared {cleared} stale file(s) from {DEBUG_DIR.name}/")

    # Launch flags. The two AutomationControlled-related switches are the
    # ones that matter for not looking automated: by default Playwright
    # launches Chromium with --enable-automation (which sets a CDP marker
    # and the "controlled by automated software" infobar) and with the
    # AutomationControlled blink feature enabled (which is what sets
    # navigator.webdriver = true). We turn both off here; the webdriver
    # flag is then additionally masked via add_init_script below as a
    # belt-and-braces measure for any code path that re-adds it.
    browser = pw.chromium.launch(
        headless=not headed,
        args=[
            "--disable-features=Translate,AutomationControlled",
            "--disable-blink-features=AutomationControlled",
        ],
        ignore_default_args=["--enable-automation"],
    )

    # Build the UA from the REAL bundled Chromium version rather than a
    # hardcoded "Chrome/131". A hardcoded major version drifts out of sync
    # with the engine that's actually running, and the Sec-CH-UA client-
    # hint headers Chromium sends are generated from the real version — so
    # a stale UA major vs. live client-hint major is a detectable mismatch.
    # Deriving it keeps UA and client hints internally consistent across
    # any Playwright version this runs on.
    chrome_version = browser.version            # e.g. "131.0.6778.33"
    chrome_major = chrome_version.split(".")[0]
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_version} Safari/537.36"
    )

    ctx_kwargs = {
        "viewport": {"width": 1400, "height": 1200},
        "user_agent": user_agent,
        "locale": "en-GB",
        # Timezone coherence: the UA claims Windows/GB and locale is en-GB,
        # so the JS timezone should be a UK zone too. A US/other timezone
        # under an en-GB locale is a soft inconsistency fingerprinters flag.
        "timezone_id": "Europe/London",
    }
    if STATE_FILE.exists():
        ctx_kwargs["storage_state"] = str(STATE_FILE)
    context = browser.new_context(**ctx_kwargs)

    # Sec-CH-UA client-hint coherence (headless only).
    #
    # Headless Chromium sends a Sec-CH-UA REQUEST header whose brand list
    # is "HeadlessChrome";v="N" — which directly contradicts our UA string
    # (it claims plain Chrome). A server comparing the two sees an obvious
    # automation tell. Headed Chromium instead sends "Chromium";v="N",
    # which is an innocuous discrepancy (plenty of real users run Chromium)
    # and needs no fixing — so we only override in the headless case (plain
    # runs, and the future Railway worker).
    #
    # We replace it with a real-Google-Chrome-shaped 3-brand list, the
    # version DERIVED from the live engine (chrome_major) so it always
    # matches the UA and never drifts when Playwright/Chromium updates.
    # Only sec-ch-ua needs fixing: by default Chrome also sends
    # sec-ch-ua-mobile (?0) and sec-ch-ua-platform ("Windows"), both of
    # which already match our UA, and no high-entropy hints are sent unless
    # the server requests them via Accept-CH.
    if not headed:
        context.set_extra_http_headers({
            "sec-ch-ua": (
                f'"Google Chrome";v="{chrome_major}", '
                f'"Chromium";v="{chrome_major}", '
                f'"Not?A_Brand";v="99"'
            )
        })

    # Mask the remaining JS-visible automation tells before any page
    # script runs. add_init_script runs on every new document in the
    # context (main page, catalog tabs, re-login navigations) ahead of
    # the site's own scripts.
    #   - navigator.webdriver: should read false/undefined, not true.
    #   - navigator.platform: must say "Win32" to match the Windows UA
    #     (Playwright on a Linux/other host would otherwise leak the real
    #     host platform here, contradicting the UA).
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
        """
    )
    # Auto-dismiss any JS dialog (squeeze-out confirmation, leave-page
    # prompts, etc.) on any tab in this context. Registered once on the
    # context rather than per-page so it covers the main tab, all catalog
    # tabs we open, and any re-login flows without re-registering each
    # time. Previously this lived inside login() and leaked a fresh
    # handler per call.
    context.on("dialog", lambda d: d.accept())
    page = context.new_page()

    if not STATE_FILE.exists():
        login(page)
    else:
        # We reused saved cookies — but partslink24 sessions expire and
        # nothing in storage_state.json tells us whether they're still
        # valid. Visit the home page and check; if the session has
        # expired, partslink24 will show the login form and we re-log
        # in cleanly. Without this, every lookup would silently run
        # against a logged-out browser (catalogs render in demo mode,
        # VIN inputs stay disabled, all VINs fail with the same error).
        log("verifying saved session is still valid")
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeoutError:
            log("could not load home page to verify session; re-logging in")
            login(page)
        else:
            handle_attention_page(page)
            if not is_logged_in(page):
                log("saved session expired; re-logging in")
                login(page)
            else:
                log("saved session OK")

    if not skip_brand_check:
        verify_brand_list(page)

    results = []
    for i, row in enumerate(rows, 1):
        log(f"--- {i}/{len(rows)} ---")
        results.append(lookup_vin_with_retry(
            page, row, debug=debug,
            allow_dashboard_fallback=allow_dashboard_fallback,
        ))

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
    ap.add_argument("--year", help="model year (currently unused; "
                                    "reserved for future Classic-catalogue "
                                    "routing)")
    ap.add_argument("--debug", action="store_true",
                    help="show browser + dump HTML on failure")
    ap.add_argument("--dump-always", action="store_true",
                    help="dump HTML for every result page, including "
                         "successes (implies headed browser)")
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
                      headed=args.headed or args.debug or args.dump_always,
                      debug=args.debug,
                      fresh=args.fresh,
                      skip_brand_check=args.skip_brand_check,
                      allow_dashboard_fallback=not args.no_fallback,
                      dump_always=args.dump_always)

    write_results(results)
    print()
    print(f"{'VIN':<19} {'PAINT':<8} {'DESCRIPTION':<28} "
          f"{'VIA':<18} {'OUTCOME':<19} ERROR")
    print("-" * 132)
    for r in results:
        print(f"{r.vin:<19} {r.paint_code:<8} "
              f"{r.paint_description[:28]:<28} {r.via:<18} "
              f"{r.outcome:<19} {r.error}")
    print(f"\nappended to {RESULTS_FILE.name}")


if __name__ == "__main__":
    main()