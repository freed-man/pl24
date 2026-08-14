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
    python lookup.py --debug            # dump HTML on failure (headless)
    python lookup.py --dump             # dump HTML for every page (headless)
    python lookup.py --dump --headed    # ...and show the browser window
    python lookup.py --fresh            # ignore saved session, log in fresh
    python lookup.py --skip-brand-check # accepted but a no-op (the brand-list
                                        # check was removed 2026-08)
    python lookup.py --no-fallback      # disable dashboard SEARCH VIN fallback
    python lookup.py --delay 20-60      # wait 20-60s between VINs (multi-VIN
                                        # runs only; off by default)

--debug and --dump control HTML dumping only; add --headed to either to also
show the browser window. Neither implies the other.
"""

import argparse
import csv
import os
import random
import re
import shutil
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
import weakref
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin

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
# just failures — including successful lookups. Set once from the --dump
# CLI flag in run(). Useful for inspecting a page that succeeded but produced
# a surprising/empty field (e.g. a success with no description), where the
# normal failure-only --debug dump writes nothing. The internal name stays
# DUMP_ALWAYS for stability; only the user-facing flag is --dump.
# Module-level rather than threaded through every lookup function because
# it's a whole-run CLI switch, not a per-VIN setting.
DUMP_ALWAYS = False

LOGIN_URL = "https://www.partslink24.com/en/index.html"
HOME_URL = "https://www.partslink24.com/"
CATALOG_URL_TEMPLATE = (
    "https://www.partslink24.com/partslink24/launchCatalog.do?service={}"
)

# <pl24-login-ui> field selectors (component data-version 1.0.10).
# data-test-id and name= are the stable hooks; the element ids are React
# useId() output and change per instance/render. Always use these SCOPED
# to a single component instance — see _complete_login_from_current_page.
LOGIN_FIELD_COMPANY = '[data-test-id="pl24-login-ui-loginForm-input-companyId"]'
LOGIN_FIELD_USERNAME = '[data-test-id="pl24-login-ui-loginForm-input-username"]'
LOGIN_FIELD_PASSWORD = '[data-test-id="pl24-login-ui-loginForm-input-password"]'
LOGIN_BUTTON_SUBMIT = '[data-test-id="pl24-login-ui-loginForm-button-submitForm"]'
# Session cookie partslink24 itself tests for before redirecting to
# /portal-ui. Authoritative signal for "are we logged in".
SESSION_COOKIE = "PL24TOKEN"

# Per-VIN extra attempts (on top of the first one) for transient errors
# like network timeouts. Logical failures (no catalog for brand, VIN not
# in DB) are not retried. EXTRA_RETRIES=1 means we make at most 2 total
# attempts. Named "EXTRA_" rather than "MAX_" so it can't be misread as
# "maximum total attempts".
EXTRA_RETRIES = 1

# Proactive re-login threshold (seconds) for the long-lived service session.
# A partslink24 session survives idle for a while (empirically measured at
# 15+ minutes — it outlives the 600s access token, which refreshes silently
# underneath), and any successful lookup refreshes it. The service tracks the
# time since the session last did real work (Session.last_interaction); when a
# request arrives after a longer idle gap than this, the session is PROBABLY
# stale, so we re-login BEFORE attempting the lookup instead of letting the
# attempt fail and recovering via the catalog_ui_error self-heal (which costs
# a wasted ~5s failed attempt + the re-login). This is a SPEED optimisation,
# not a correctness mechanism: the self-heal in lookup() remains the backstop
# for sessions that die *within* the window (e.g. a squeeze-out by another
# login), so this threshold being slightly wrong only changes whether a stale
# lookup is fast (pre-empted here) or slow (healed there) — never whether it's
# correct. Default 900s (15 min) = the measured-safe lower bound; tune via the
# PL24_SESSION_IDLE_S env var (Railway dashboard) with no code change. A value
# of 0 disables the proactive check (self-heal still covers staleness).
SESSION_IDLE_RELOGIN_S = float(os.environ.get("PL24_SESSION_IDLE_S", "900"))

# Outcomes that PROVE the session was alive when the lookup ran (partslink24
# served a real catalogue/dashboard response). Reaching any of these refreshes
# the session, so Session.last_interaction is updated to "now" after them. The
# COMPLEMENT — catalog_ui_error / auth_error / page_load_timeout / unknown —
# means the session was dead or never engaged, so we do NOT treat those as a
# refresh (updating the clock on a failed-because-dead lookup would wrongly
# mark a dead session "fresh" and suppress the next proactive re-login).
_SESSION_PROVEN_ALIVE_OUTCOMES = frozenset({
    "success",
    "name_only",
    "not_found_as_routed",
    "unsupported_brand",
    "brand_unavailable",
    "paint_data_missing",
})


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
# When the routed catalogue fails to IDENTIFY the vehicle, try the
# sibling catalogue in the same family before falling through to
# Classic/dashboard. Fires only on "vehicle not found"-type failures
# (skipped on "paint code not found" — see the chain logic), so
# correctly-routed lookups pay nothing.
#
# Originally commercial<->commercial only (Mercedes Vans/Trucks: the
# N1/N2/N3 split can't be derived reliably upstream). The Caddy entry
# below proved the passenger<->commercial direction fails the same way:
# the M1/N1 category comes from the upstream provider and can simply be
# wrong. One hop only — the chain does not walk sibling-of-sibling.
#
# MAN and IVECO need no entry (standalone heavy-truck catalogues, no
# category-dependent routing). BMW <-> BMW Motorrad is deliberately NOT
# cross-linked: cars vs motorcycles isn't a realistic misclassification,
# and a dead Motorrad attempt would only delay the BMW Classic leg that
# actually rescues old BMWs. Modern<->Classic pairs are handled by
# CLASSIC_SIBLING, Opel/Vauxhall legacy by LEGACY_SIBLING.
COMMERCIAL_FALLBACK: dict[str, str] = {
    "Mercedes-Benz Vans":   "Mercedes-Benz Trucks",
    "Mercedes-Benz Trucks": "Mercedes-Benz Vans",
    # Base passenger Mercedes -> Vans: an M1-classified Vito/V-Class/
    # small Sprinter routes to the passenger catalogue and fails
    # "vehicle not found"; one hop into Vans recovers it. Trucks-as-M1
    # is left unwired — a misclassified HGV is implausible, and the
    # single-hop chain couldn't reach it from here anyway.
    "Mercedes-Benz":        "Mercedes-Benz Vans",
    # Mirror the Mercedes-style cross-fallback for Fiat: passenger vs
    # commercial Fiats live in separate catalogues, and the M1/N1
    # boundary for a Doblò or Panda Van is just as fuzzy as Mercedes's
    # van/truck line. If the routed catalogue doesn't recognise the VIN,
    # try the sibling before falling through to dashboard.
    "Fiat":                 "Fiat Professional",
    "Fiat Professional":    "Fiat",
    # Ford: same split as Fiat (fordp_parts / fordt_parts). No observed
    # failure yet, but UK traffic is Transit-heavy and an M1-classified
    # Transit whose paint VDG lacks would fail exactly like the Caddy
    # below. Structural add with the same guard/cost profile.
    "Ford":                 "Ford Commercial",
    "Ford Commercial":      "Ford",
    # Volkswagen: proven live by a 2026 Caddy Maxi (WV2ZZZSK7TX044364,
    # reg RK26LTJ). VDG classified it M1 -> routed to the passenger
    # catalogue ("VIN could not be assigned") -> the chain skipped
    # straight past the commercial catalogue that resolves it in ~2s
    # (M7P), because no VW entry existed here. With this entry the
    # misrouted lookup self-heals as via=catalog:commercial.
    "Volkswagen":                     "Volkswagen Commercial Vehicles",
    "Volkswagen Commercial Vehicles": "Volkswagen",
    # Citroën <-> DS: not an M1/N1 split, but the same failure shape —
    # one family, two catalogues (citroen_parts / citroenDs_parts), and
    # the upstream make string can't reliably tell us which side a
    # DS 3 / DS 7 lives on. Reuses the same single-retry slot; via
    # reads catalog:commercial for these (cosmetic only).
    "Citroën":              "Citroën DS",
    "Citroën DS":           "Citroën",
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
    "Opel": "psa_opel_parts",
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
    "Vauxhall": "psa_vauxhall_parts",
    "Volkswagen": "vw_parts",
    "Volkswagen Classic": "vwclassic_parts",
    "Volkswagen Commercial Vehicles": "vn_parts",
    "Volvo": "volvo_parts",
}

# partslink24 split Opel/Vauxhall: the LIVE catalogue moved under PSA
# (psa_opel_parts / psa_vauxhall_parts, where BRAND_CATALOG_SERVICE points for
# the base brands), and the old service ids (opel_parts / vauxhall_parts)
# became "<brand> legacy" catalogues for OLDER vehicles. partslink24 still
# advertises the legacy catalogues on its home grid under these names, but
# they are NOT added to BRAND_CATALOG_SERVICE on purpose: the legacy
# catalogues are reached only as a fallback (see LEGACY_SIBLING /
# LEGACY_CATALOG_SERVICE below), never as a routed base brand. This set
# records that the omission is deliberate rather than an oversight.
BRANDS_KNOWN_UNROUTED = {
    "Opel legacy",
    "Vauxhall legacy",
}

# Old-car sibling for GM's PSA split: when the live PSA catalogue fails to
# IDENTIFY an Opel/Vauxhall (e.g. a pre-PSA-era vehicle), retry against the
# legacy catalogue before falling through to the dashboard. Same mechanism and
# the same "only on a genuine not-found, not on paint-code-not-found" guard as
# CLASSIC_SIBLING. Confirmed need: a 2006 Vauxhall Astra (W0L0AHL...) returns
# "no results" on psa_vauxhall_parts but resolves to 4CU on Vauxhall legacy.
# Kept separate from BRAND_CATALOG_SERVICE (see note above) with the legacy
# service ids in their own map so the fallback can build the catalogue URL
# without the legacy names being treated as routed base brands.
LEGACY_SIBLING: dict[str, str] = {
    "Opel": "Opel legacy",
    "Vauxhall": "Vauxhall legacy",
}
LEGACY_CATALOG_SERVICE: dict[str, str] = {
    "Opel legacy": "opel_parts",
    "Vauxhall legacy": "vauxhall_parts",
}


# Control characters are stripped from every log line, newlines included.
# Reason: several messages interpolate caller-supplied values — the clearest
# being `make=`, which the service accepts as free text (bounded to 40 chars
# but not character-validated, deliberately, since legitimate makes carry
# spaces, hyphens and accents: 'Mercedes-Benz', 'Citroen', 'Skoda'). A make
# containing a percent-encoded newline decodes to a real one and would let a
# caller inject fabricated timestamped lines into the Railway log — the log
# these audits, the §5 triage table and any future incident all depend on
# being trustworthy. Sanitising HERE rather than at each call site means no
# interpolation added later can reintroduce it, and it costs one translate()
# on a line we were formatting anyway. Every message in this codebase is a
# single line by design, so nothing legitimate is lost.
_LOG_CTRL = {c: None for c in range(32)} | {127: None}


def log(msg: str) -> None:
    safe = str(msg).translate(_LOG_CTRL)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {safe}", flush=True)


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
    # Base brands live in BRAND_CATALOG_SERVICE (verified against the home
    # grid); the Opel/Vauxhall legacy catalogues are reached only as a
    # fallback and live in LEGACY_CATALOG_SERVICE (kept out of the verified
    # map on purpose — see the LEGACY_SIBLING note).
    svc = BRAND_CATALOG_SERVICE.get(brand) or LEGACY_CATALOG_SERVICE.get(brand)
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


_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def clean_vin(raw: str) -> str | None:
    """Normalise + validate a VIN. Returns the canonical 17-char VIN, or
    None if the input is not a VIN.

    THE ASCII CHECK RUNS BEFORE .upper(), AND THE ORDER IS THE POINT.
    str.upper() is not length-preserving outside ASCII: the ligature
    '\ufb00' uppercases to 'FF' and '\u00df' to 'SS', so a 16-character
    raw string can EXPAND into 17 valid-looking characters and sail
    through a "must be 17 chars" check that runs after case-folding —
    found by the fuzz battery 2026-08-03, live in all three entry points.
    The ISO 3779 alphabet is pure ASCII, so rejecting non-ASCII first
    kills the whole case-folding class at once, and provably changes
    nothing for any legitimate VIN: every string the old checks accepted,
    ligatures aside, was ASCII already.

    This is the ONLY VIN validator; read_lookups, --vin and the service
    endpoint all call it, so the three can never drift apart again."""
    s = raw.replace(" ", "").strip()
    if not s.isascii():
        return None
    s = s.upper()
    return s if _VIN_RE.fullmatch(s) else None


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
        vin = clean_vin(vin_part)
        if vin is None:
            log(f"skipping malformed VIN: {raw!r}")
            continue
        rows.append(LookupRow(
            vin=vin,
            make=make_part or None,
            category=cat_part or None,
            year=year_part or None,
        ))
    return rows


# ---------- popup / dialog handlers ------------------------------------------

def handle_cookie_consent(page: Page) -> bool:
    """Click through Usercentrics cookie banner if present."""
    # Essential-only first: it satisfies the banner without pulling in
    # Meta Pixel, Google Ads and Matomo (which is configured with session
    # recording). Fewer third-party scripts per session establishment
    # means faster, less flaky page loads. "Accept All" stays as fallback
    # for locales/layouts where the essential-only button is absent.
    candidates = [
        'button:has-text("Accept only essential services")',
        'button:has-text("Accept only essential")',
        'button:has-text("Accept All")',
        'button:has-text("Accept all")',
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


class SqueezeOutUnhandledError(RuntimeError):
    """Raised when a session squeeze-out prompt appears but we cannot
    confirm it. See handle_session_squeeze_out for why this is loud."""


def handle_session_squeeze_out(page: Page) -> bool:
    """Confirm the 'a previous session is still open' prompt, if shown.

    The 2026-07 rebuild moved this into the <pl24-login-ui> React
    component; the old ids (#sessionSqueezeOutPrompt, #squeezeout-login-btn)
    no longer exist. Markup captured from a live prompt on 2026-08-01:

        <div data-test-id="pl24-login-ui-sessionSqueezeOut-squeezeOut">
          "Would you like to end the current session now and log in again?"
          <button data-test-id="...-sessionSqueezeOut-button-cancel">Cancel
          <button data-test-id="...-sessionSqueezeOut-button-confirm">Confirm

    Cancel renders FIRST. That ordering is a trap: a selector matching
    merely "squeeze" + "button" resolves to both and .first takes Cancel,
    which aborts the login and gets the session force-logged-out ("For
    security reasons, you have been automatically logged out"). Match the
    confirm button explicitly and nothing else.

    Returns True if a prompt was found and confirmed, False if none was
    present."""
    prompt = page.locator(
        '[data-test-id="pl24-login-ui-sessionSqueezeOut-squeezeOut"]').first
    try:
        if not prompt.count() or not prompt.is_visible():
            return False
    except Exception:
        return False

    log("session squeeze-out prompt detected")
    # Capture the markup before touching anything: the prompt exists only
    # until a button is clicked.
    _dump_squeeze_prompt(page)

    # Exact confirm selector, captured from a live prompt 2026-08-01.
    # The prompt renders Cancel FIRST and Confirm second, both matching a
    # naive '*="squeeze"' + '*="button"' selector — which is precisely how
    # an earlier revision of this function clicked Cancel and got the
    # session force-logged-out ("For security reasons, you have been
    # automatically logged out"). Match the confirm button and nothing else.
    btn = page.locator(
        '[data-test-id="pl24-login-ui-sessionSqueezeOut-button-confirm"]'
    ).first
    try:
        if btn.count() and btn.is_visible():
            log("session squeeze-out -> Confirm")
            btn.click()
            page.wait_for_timeout(1_000)
            return True
    except Exception:
        pass

    raise SqueezeOutUnhandledError(
        "session squeeze-out prompt appeared but the Confirm button was "
        f"not clickable; see {DEBUG_DIR.name}/squeeze_prompt.*"
    )


def _dump_squeeze_prompt(page: Page) -> None:
    """Snapshot the squeeze-out prompt the instant it is seen.

    Separate from _dump_login_failure so the two never overwrite each
    other: the failure dump runs after the page has moved on, whereas
    this one has to fire while the prompt is still on screen."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / "squeeze_prompt.png"),
                        full_page=True)
        (DEBUG_DIR / "squeeze_prompt.html").write_text(
            _redact_page_html(page.content()), encoding="utf-8"
        )
        log(f"saved squeeze_prompt.* under {DEBUG_DIR.name}/")
    except Exception:
        pass


def is_logged_in(page: Page) -> bool:
    """Return True if we hold a live partslink24 session.

    Keyed on the PL24TOKEN cookie rather than page text. As of the
    2026-07 rebuild the DOM is no longer a reliable signal:

      - /portal-ui (the new dashboard) has NO "Log out" link at all, just
        an account-menu icon button, so the old text check returned False
        while fully logged in.
      - The old "SEARCH VIN" placeholder is gone; the new box reads
        "Chassis number".
      - The landing page redirects logged-in users to /portal-ui via an
        inline script that itself tests for PL24TOKEN, so the cookie is
        exactly what partslink24 considers authoritative.

    Old-style Struts catalog pages still carry a "Log out" link and remain
    covered, since they are served under the same cookie."""
    try:
        for c in page.context.cookies():
            if c.get("name") == SESSION_COOKIE and c.get("value"):
                return True
    except Exception:
        pass
    return False


class Pl24Credentials(NamedTuple):
    """One partslink24 login. Company id is per-account, not global: an
    additional user under the same company shares company_id and differs
    only in username/password, while a wholly separate account differs in
    all three. Both shapes work."""
    company_id: str
    username: str
    password: str


# Page -> credentials. A WeakKeyDictionary so entries disappear with the
# page; nothing to clean up on session teardown.
#
# WHY A REGISTRY rather than a `creds` parameter threaded through the call
# chain: re-login is triggered from deep inside the lookup flow —
# _try_catalog() and _try_dashboard() both call login(page) when they find
# an expired session, and neither has any notion of which Session it
# belongs to. With a multi-account pool, threading credentials would mean
# touching six signatures and getting every one right; miss a single call
# site and session 2 silently re-authenticates as account 1, squeezing out
# its own sibling. Binding to the page makes the correct account
# unavoidable: every login path already has the page in hand.
_PAGE_CREDENTIALS: "weakref.WeakKeyDictionary[Page, Pl24Credentials]" = (
    weakref.WeakKeyDictionary()
)


def bind_credentials(page: Page, creds: Pl24Credentials | None) -> None:
    """Associate a page with the account it should log in as. Pass None to
    leave the page on the process-wide environment credentials."""
    if creds is not None:
        _PAGE_CREDENTIALS[page] = creds


def credentials_for(page: Page) -> Pl24Credentials:
    """Credentials for this page, falling back to the environment.

    The fallback is what keeps the CLI and the single-account service
    working unchanged: nothing binds, so everything reads the same env vars
    it always did."""
    try:
        creds = _PAGE_CREDENTIALS.get(page)
    except TypeError:
        creds = None
    if creds is not None:
        return creds
    return Pl24Credentials(
        os.environ["PARTSLINK24_COMPANY_ID"],
        os.environ["PARTSLINK24_USERNAME"],
        os.environ["PARTSLINK24_PASSWORD"],
    )


# Page -> save_state preference, for exactly the reason _PAGE_CREDENTIALS
# exists (see that comment): the deep re-login sites in _try_catalog and
# _try_dashboard call login(page) with no notion of which Session they
# belong to, and login()'s save_state default is True — the CLI's
# behaviour. In the SERVICE that default is wrong, and not merely untidy:
# the Session is built save_state=False precisely so no session state
# touches disk in the container, yet one dead-session recovery inside a
# lookup would write storage_state.json — the live PL24TOKEN — to /app,
# where it persists between requests. That is the same exposure class as
# the component-JWT-in-debug-dumps leak fixed on 2026-08-01, minus the
# 10-minute expiry. It also meant a multi-account pool's slots would
# clobber one shared file with whichever account re-logged-in last, and a
# later crash-recovery start() would key _establish_session's reuse branch
# off that file's mere existence. Binding the preference to the page makes
# the deep call sites correct without threading a parameter through the
# lookup flow; unbound pages keep the True default, so the CLI is
# unchanged.
_PAGE_SAVE_STATE: "weakref.WeakKeyDictionary[Page, bool]" = (
    weakref.WeakKeyDictionary()
)


def bind_save_state(page: Page, save_state: bool) -> None:
    """Associate a page with whether logins on it should persist
    storage_state.json. Sessions bind False; the CLI binds nothing and
    keeps the historical save-on-login behaviour."""
    _PAGE_SAVE_STATE[page] = save_state


def save_state_for(page: Page) -> bool:
    """save_state preference for this page, defaulting to True (the CLI's
    long-standing behaviour) when nothing is bound."""
    try:
        pref = _PAGE_SAVE_STATE.get(page)
    except TypeError:
        pref = None
    return True if pref is None else pref


def login(page: Page, save_state: bool = True) -> None:
    """Navigate to the login page and run the full login flow.

    Used for the cold-start case (no saved session at all). When we
    already have a saved session and only need to re-login after expiry,
    run() instead lands on the landing page via HOME_URL and calls
    _complete_login_from_current_page() directly — the landing page hosts
    the login component inline, so no second navigation is needed.

    `save_state` is forwarded to the login tail: True (default, the CLI's
    behaviour) writes storage_state.json after a successful login; the
    long-lived service passes False since it keeps the session in memory
    and an ephemeral container has no use for the file.

    Note: the dialog handler (auto-accepting JS prompts like the
    squeeze-out confirmation) is registered once on the browser context
    in run(), not here — registering it per-login leaked a fresh handler
    on every call."""
    log("logging in")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    # No attention/bookmark interstitial any more: it only ever guarded
    # direct navigation to login.do, which 404s since the 2026-07 rebuild.
    # The consent banner DOES still appear, and Usercentrics scroll-locks
    # the body while it is up, so it must be cleared before the form is
    # actionable.
    handle_cookie_consent(page)
    _complete_login_from_current_page(page, save_state=save_state)


def _complete_login_from_current_page(page: Page,
                                      save_state: bool = True) -> None:
    """Fill and submit the login form that is ALREADY present on the
    current page, then confirm we're logged in and (optionally) save the
    session.

    Assumes the caller has already navigated to a partslink24 page that
    resolves to the login form (either login() after going to LOGIN_URL,
    or run() after the HOME_URL redirect on an expired session) and has
    cleared the cookie-consent banner. This is the shared tail of the
    login flow, factored out so the expired-session path doesn't have to
    navigate a second time.

    `save_state` controls the storage_state.json write at the end: True
    (default) for the CLI; False for the in-memory service session."""
    # Per-page credentials when the caller bound them (multi-account pool),
    # otherwise the process-wide environment (CLI, single-account service).
    p_id, user, pw = credentials_for(page)

    # The login form is a React/MUI web component (<pl24-login-ui>,
    # bundle /pl24-login-ui/v1/index.iife.js) rendered into the LIGHT dom,
    # so ordinary selectors reach it — no shadow piercing needed.
    #
    # IMPORTANT: the landing page mounts the component TWICE — once inside
    # the header dropdown (#login-wrapper-dialog, hidden) and once inline
    # in the page body (.pl24-components__login). Their data-test-ids are
    # identical, so an unscoped locator resolves to 2 elements and
    # Playwright raises a strict-mode violation. We scope to the in-page
    # instance, which is the visible one at our 1400px viewport.
    #
    # Do NOT key on the element ids (_r_1_, _r_3_, _r_5_): those are React
    # useId() output and differ between the two instances on the very same
    # page, so they will not survive a re-render or a bundle bump.
    form = page.locator(".pl24-components__login pl24-login-ui")
    try:
        form.locator(LOGIN_FIELD_COMPANY).wait_for(state="visible",
                                                   timeout=20_000)
    except PlaywrightTimeoutError as e:
        _dump_login_failure(page)
        raise RuntimeError(
            f"login failed: login component never rendered; "
            f"see {DEBUG_DIR.name}/login_failed.*"
        ) from e

    # fill() dispatches the input events React listens for, so controlled
    # component state stays in sync (a raw value assignment would not).
    form.locator(LOGIN_FIELD_COMPANY).fill(p_id)
    form.locator(LOGIN_FIELD_USERNAME).fill(user)
    form.locator(LOGIN_FIELD_PASSWORD).fill(pw)
    # Click the real submit button rather than pressing Enter, so any
    # onClick validation in the component runs.
    form.locator(LOGIN_BUTTON_SUBMIT).click()

    # After clicking Login, partslink24 may throw up a squeeze-out (if a
    # session reappeared between our load and our submit). Give the page a
    # moment to settle into either the dashboard or that popup, then handle
    # it if present.
    page.wait_for_timeout(1_500)
    handle_session_squeeze_out(page)

    # Poll for the PL24TOKEN cookie rather than waiting on a DOM needle:
    # where we land after submit now depends on the brand estate
    # (/portal-ui, or an old Struts catalog), but the cookie is set in
    # every case.
    waited = 0
    while waited < 20_000:
        if is_logged_in(page):
            break
        page.wait_for_timeout(500)
        waited += 500

    if not is_logged_in(page):
        _dump_login_failure(page)
        msg = _extract_login_error(page)
        raise RuntimeError(f"login failed: {msg}")

    _bump_login_generation()
    log("logged in, saved session" if save_state else "logged in")
    if save_state:
        page.context.storage_state(path=str(STATE_FILE))


# Monotonic count of successful logins in this process. Every successful
# login of every kind — cold start, layer 1 proactive, layer 2 in-place,
# layer 3 forced, and the inline heals inside _try_catalog/_try_dashboard —
# funnels through _complete_login_from_current_page, so bumping here and
# nowhere else cannot miss one. lookup_vin_with_retry snapshots it PER
# ATTEMPT, at the wrapper — deliberately NOT inside lookup_vin, whose eight
# return statements meant an on-entry/at-exit check there covered exactly
# one exit and silently missed the rest (found and corrected 2026-08-03,
# same day it shipped). A changed
# value at exit means the session was re-established MID-lookup, i.e. some
# prefix of the fallback chain ran against a dead session and its failures
# are void. Proven necessary 2026-08-03 (live): a squeezed-out session made
# the routed VW leg fail "VIN box not visible" (the /pl24-app SPA serves its
# shell to a dead session — no VIN box, no password field for the expiry
# probe to catch), the COMMERCIAL leg then healed inline, and legs 2-4
# honestly reported a passenger VW absent from catalogues it is genuinely
# absent from: final answer not_found_as_routed for a VIN that is A7N.
# Neither existing net fired — layer 3 needs the final outcome to be
# catalog_ui_error (the chain diluted it) and B2 needs the catalog leg to
# be a TIMEOUT (it was VIN-box-not-visible).
#
# Per-process, not per-slot: two slots interleaving logins can make an
# unrelated slot's lookup see a changed generation and take one spurious
# whole-VIN retry on its own live session — bounded by EXTRA_RETRIES,
# costing ~3s, and only when a heal coincided with a no-code result. Wrong
# answers are impossible from it; cheapness beats plumbing a per-slot
# signal through module-level functions.
LOGIN_GENERATION = 0
# `LOGIN_GENERATION += 1` is a read-modify-write, NOT atomic: two slots
# logging in at the same moment can both read N and both write N+1, losing
# an increment. A lost increment makes a concurrent lookup fail to notice
# its heal — a silent degrade to pre-C1 behaviour on the one path C1
# exists for. Harmless at POOL_SIZE=1 (single slot, no concurrency) but
# PL24_ACCOUNTS is designed to grow, and this is the kind of latent race
# that only shows up after the pool is expanded and is then very hard to
# attribute. Reads stay unlocked: loading a single int is atomic, and the
# comparison only ever needs to know whether the value CHANGED.
_LOGIN_GEN_LOCK = threading.Lock()


def _bump_login_generation() -> None:
    global LOGIN_GENERATION
    with _LOGIN_GEN_LOCK:
        LOGIN_GENERATION += 1


def _extract_login_error(page: Page) -> str:
    """Pull the actual error message off the login page.

    partslink24's HTML puts a literal '►' bullet character in its own
    <span class='error'> sibling next to the real message, so just
    grabbing the first .error gives us a useless arrow. We try harder:

      1. #loginErrorDiv has the dedicated text, when populated
      2. all visible .error spans, concatenated, with the bullet stripped
      3. fall back to the page URL + title so the user has *something*
    """
    # 0. New <pl24-login-ui> error paragraph (CSS module class
    # ._login__error_*), and the landing page's forced-logout banner
    # (#pl24-alert, "For security reasons, you have been automatically
    # logged out..."). Also surface the server-side login-disabled message
    # from window.pl24Settings when it is showing.
    for sel in ('[class*="_login__error"]',
                '#pl24-alert:not(.hidden) .alert__text'):
        try:
            node = page.locator(sel).first
            if node.count() and node.is_visible():
                text = node.inner_text().strip()
                if text:
                    return text
        except Exception:
            pass

    # 1. Dedicated error div (old Struts login page).
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


# The /portal-ui SPA passes a short-lived (10 min) authorization JWT into
# its web components as a plain DOM attribute:
#     <pl24-vinsearch-ui token="eyJ...">
# Its payload carries the session id, user id, account id and licence id.
# page.content() serialises that verbatim, so any dump taken on a
# logged-in portal page would write a live credential to disk (and into
# the Railway container, where _debug/ persists between requests).
# Strip it at the point of capture.
_TOKEN_ATTR_RE = re.compile(r'(\stoken=")[^"]{40,}(")', re.I)
# Belt for the braces above: the attribute pattern only catches the ONE
# serialisation we've observed (`<pl24-vinsearch-ui token="eyJ...">`), but
# /portal-ui builds that attribute from state it also holds elsewhere —
# inline script JSON, other components — and any of those would serialise
# into page.content() just as readily. Rather than enumerate placements,
# catch the token by its SHAPE: three dot-separated base64url segments
# with realistic lengths is a JWT and nothing else on these pages, so
# redact it wherever it sits. Segment minimums (10/20/20) keep this from
# ever touching version strings or dotted identifiers, which never carry
# 20+ base64url chars per segment.
_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{7,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"
)


def _redact_page_html(html: str) -> str:
    """Remove component bearer tokens from captured HTML."""
    html = _TOKEN_ATTR_RE.sub(r"\1<redacted>\2", html)
    return _JWT_RE.sub("<redacted-jwt>", html)


def _dump_login_failure(page: Page) -> None:
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / "login_failed.png"), full_page=True)
        (DEBUG_DIR / "login_failed.html").write_text(
            _redact_page_html(page.content()), encoding="utf-8"
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
    #
    # The Attention branch is RETAINED BUT DORMANT: handle_attention_page
    # was removed 2026-08-01 because the interstitial only ever guarded
    # direct navigation to login.do, which now 404s. This check is a
    # different thing — a redirect landing on that page mid-catalogue-open —
    # and it has not been observed since the rebuild. Kept because it costs
    # one locator round trip and failing to notice an expiry is expensive;
    # delete it only once a live run confirms the page is gone for good.
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

# PSA-built vehicles (Toyota Proace family) render attributes as `B0` +
# FAMILY LETTER + a 2-character payload:
#
#     B0CK0 -> PROACE VERSO (K0)          C = model,   payload K0
#     B0MP0 -> NON-METALLIC PAINT         M = finish,  payload P0
#     B0NVL -> PLATINUM GREY PAINT        N = colour,  payload VL
#     B0RFX -> BLACK "FX"                 R = trim,    payload FX
#
# THREE RULES FOR DERIVING A PAINT CODE FROM THIS BLOCK HAVE BEEN TRIED
# AND ALL THREE WERE WRONG (2026-08-07). No code is derived here now.
# The history is kept in full so nobody re-derives any of them:
#
#   1. "the payload IS the code" -> emitted NVL, NWP, NM0, NP0, none of
#      which exist under any marque. Survived its first sample only
#      because PSA files one colour under BOTH NEU and EEU, so the single
#      ambiguous case in the set was the case we happened to test.
#   2. "the paint row is whichever value ends in PAINT" -> on the G9 van
#      TWO rows qualify (B0MP0 is the FINISH type) and the wrong one won,
#      returning MP0.
#   3. "code = 'E' + payload" -> NEVER CONFIRMED, only unfalsified for a
#      few hours. Read the table below: the very first row is a
#      DEALER-CONFIRMED NEU, which rule 3 would emit as EEU. That
#      counter-example was in hand BEFORE the rule was called evidenced,
#      sitting under the prefix column, and was read past because the
#      search was for what killed the rule rather than for what the rule
#      could not explain. It survived only because PSA files that one
#      colour under both NEU and EEU, so the customer outcome happened
#      to be identical. The Proace City later killed it outright.
#
# The full dealer-confirmed table, which kills rules 1 and 3 together:
#
#     page key   value              TRUE CODE   payload=last2   prefix
#     B0NEU      SAND PAINT         NEU         yes             N
#     B0NVL      PLATINUM GREY      EVL         yes             E
#     B0NWP      BANQUISE WHITE     EWP         yes             E
#     B0NWP (G9) BANQUISE WHITE     EWP         yes             E
#     B0NF4      ARTENSE GREY       KCA         NO              K
#
# Prefixes run N, E, E, E, K — not constant and not present on the page.
# Rule 1 is right only on NEU; rule 3 only on EVL/EWP. Each was
# "confirmed" by precisely the samples that could not distinguish it,
# which is how all three reached production. The Proace City breaks the
# payload half as well: page payload F4, true code KCA, and the string
# "KCA" occurs ZERO times in that DOM.
#
# CONCLUSION: the manufacturer paint code is NOT PRESENT on PSA pages.
# The page carries a two-character build-spec payload plus a marque
# colour name. No regex can recover what is not there. This is a
# coverage fact about partslink24's PSA estate, not a missing feature.
#
# The common thread: the paint code's leading character is NOT PRESENT ON
# THE PAGE, and every attempt to reconstruct it has been an inference
# about PSA's coding scheme that a regression battery cannot check.
#
# NOR CAN COLOUREG'S UNRESOLVABLE-CODE LOG, and this must not be
# misread. That log flags a delivered code that resolves to nothing in
# the paint dataset. It caught NVL and NWP — but ONLY because PSA's
# namespace happens not to contain them. THE GATE'S CATCH RATE IS A
# PROPERTY OF THE FABRICATION, NOT OF THE EXTRACTOR: a scheme that
# fabricates into occupied namespace passes silently every time, and
# EEU is the standing proof — it resolved perfectly, right hex, right
# model tags, and was the wrong answer for that car. A quiet log
# therefore says nothing about extractor health. The only instrument
# for that class is periodic verification of live output against a
# dealer system. A
# battery can only verify that we return what the page says, never that
# the page says the truth. All three were caught by EXTERNAL ground truth
# (a dealer system, a dataset query) after reaching production.
#
# DO NOT re-enable. The prefix is unobservable and the payload is not
# even reliable. If PSA coverage is ever wanted, the only viable route is
# the colour NAME ("ARTENSE GREY", "BANQUISE WHITE") resolved against a
# dataset that holds PSA marque names — a coloureg-side capability, not
# an extraction rule, and one that must be validated the same way these
# were falsified: against a dealer system, before shipping.
#
# The regex below IS still used, but only by wait_for_vehicle_data, where
# recognising a rendered PSA page saves ~10s per lookup. That use cannot
# produce a wrong answer: it only decides when to stop polling.
PSA_BCODE_COLOUR_RE = re.compile(
    r"\bB[0-9]N([A-Z0-9]{2})\b[\s:]*[A-Z][A-Z0-9 /-]{2,40}?\s+PAINT\b")


# ---------------------------------------------------------------------
# VERIFICATION LEDGER — which estates have been checked against a DEALER
# system, and which have only ever been checked for STABILITY.
#
# This distinction is the single most important fact about this file and
# it is not otherwise visible: a green battery proves the extractor
# returns the same thing every time, never that the thing is right.
# Suzuki was stable and WRONG for months, certified by its own
# regression expectation (C05, the trim code). Keep this current.
#
#   DEALER-VERIFIED
#     Suzuki    2026-08-08  ZCF (Swift), 26U (Vitara). Body row, not the
#                           row partslink24 labels "Exterior color".
#     Nissan    2026-08-08  Qashqai/Micra/Juke, one shape, pattern via
#                           the "Exterior color" row. Confirmed correct.
#     VW/Audi   2026-08-08  Golf 5G, WVWZZZAUZFW002714. Dealer code
#                           LA7N (Limestone Grey Metallic); page row
#                           "Exterior color / Paint Code -> Z1 / A7N".
#                           POST-slash confirmed as the paint code —
#                           this had been the open Suzuki-shaped risk,
#                           two candidates in one row, choice never
#                           checked. Three caveats, all load-bearing:
#                           (1) NOTATION. The page carries only the
#                           L-STRIPPED form — LA7N occurs ZERO times in
#                           the DOM — so A7N is the maximal verbatim
#                           extraction. First ledger entry where the
#                           extracted string != the dealer string yet is
#                           its deterministic notation (VW "L" lacquer
#                           prefix, dropped by parts databases). NEVER
#                           prefix the L here: that is reconstruction,
#                           PSA rule 3's exact move. If the full form is
#                           ever needed it is coloureg-side mapping.
#                           Corroborated end-to-end 2026-08-08:
#                           coloureg's dataset resolves A7N directly,
#                           and to the same name the dealer gave for
#                           LA7N (Limestone Grey Metallic). Dealer,
#                           page and dataset agree — the two notations
#                           name one colour.
#                           (2) PRE-slash identified: the 2-char
#                           exterior ORDER code (Z1 on this car, where
#                           it also mirrors the "Roof color" row; 8E,
#                           0E, 2R elsewhere). Not a paint code. Bare
#                           2-char VW returns (0E, 2R) reach coloureg
#                           via other page shapes and are genuine
#                           orderable codes — see corroborate-or-log.
#                           (3) SCOPE. Checked on one VW; Audi shares
#                           the row shape (8E / A7W) and moves with the
#                           estate per the one-check convention. X5Q
#                           expectation unchanged — now readable as
#                           L-stripped LX5Q, not itself dealer-checked.
#     PSA/Toyota Proace
#               2026-08-07  Verified NOT extractable — dealer codes
#                           (EVL, EWP, NEU, KCA) are absent from the
#                           page. Correctly returns nothing.
#
#     Renault/  2026-08-14  Clio V VF1RJA00773682232 -> OV369 "ICE
#     Dacia                   WHITE BC"; Dacia Spring UU1DBG005RU197157
#                             -> OVDQH "GREEN LICHEN GREY". Both codes
#                             AND names confirmed verbatim in Renault
#                             Dialogys (the dealer system), Body type ->
#                             BODY COLOUR. No notation divergence, unlike
#                             VW: page string == dealer string exactly.
#                             The code is NOT in the Vehicle data panel —
#                             it is in the COLLAPSED "Equipment"
#                             accordion, unmounted until clicked, so this
#                             needed a page-interaction fix as well as a
#                             pattern (see _expand_equipment_panel).
#                             OVDQH is digit-free and 5 chars, i.e. the
#                             _is_valid_code false-negative class fired
#                             for real; handled by trusted-context
#                             exemption, not by relaxing the rule.
#                             CAVEAT: coloureg's dataset has NO row for
#                             OVDQH, and its single OV369 row is filed
#                             under dacia with the name "Blanc Glacier
#                             Verni" — a DIFFERENT string from the
#                             manufacturer's "ICE WHITE BC" for this VIN.
#                             Do not add a Renault<->Dacia cross-family
#                             dataset fallback on that row's strength;
#                             prefer the page name for these makes.
#                             Two cars, two makes, both hatchbacks; the
#                             row shape is unconfirmed on Renault vans.
#
#   STABILITY-ONLY (expectations never checked against a dealer)
#     Mercedes (9744, 441), Mini (851), Vauxhall legacy (4CU),
#     VW Commercial (M7P), Ford (name-only), Hyundai (name-only),
#     Volvo (490), Fiat family (679), Jaguar.
#
#   NOTE VW Commercial is NOT cleared by the VW/Audi entry above, for
#   the same reason Volvo is not cleared by Nissan: verification
#   attaches to estates, not to shapes or brand families. If its pages
#   carry the same compound row, one Transporter/Caddy check clears it.
#
#   NOTE Volvo shares the "Exterior colour" pattern with Nissan but is
#   NOT thereby verified: the pattern is confirmed correct for NISSAN's
#   page, and a different estate can label a different field the same
#   way. That is precisely how Suzuki went wrong.
# ---------------------------------------------------------------------
# EXTRACTION, NEVER INFERENCE — the standing rule for this list, written
# 2026-08-07 after three PSA rules shipped wrong in one day. A paint code
# returned from here must be a VERBATIM SUBSTRING OF THE PAGE, captured
# by a group. It must never be assembled, prefixed, or otherwise
# reconstructed from page fragments plus an assumption: all three failed
# rules were reconstructions (payload-as-code, row-selection-by-suffix,
# "E"+payload), every pattern that has ever held is an observation, and
# the difference is checkable at review time — if a candidate code is
# built by string concatenation anywhere outside re.Match.group, it is
# inference and it does not ship. Regression batteries cannot police
# this line: they verify that we return what the page says, never that
# the page says the truth. Only external ground truth (a dealer system,
# a dataset) can, which is why no new extraction shape ships without it.
PAINT_CODE_PATTERNS = [
    # SUZUKI TWO-ROW SHAPE — MUST PRECEDE THE "Exterior colour" PATTERN
    # BELOW, because partslink24 labels these two rows the opposite way
    # round to what the names suggest:
    #
    #     Color            26U     <- the BODY paint code
    #     Exterior color   C01     <- the COMBINATION / trim code
    #
    # Confirmed against the Suzuki dealer system 2026-08-08 on two cars:
    # TSMLYEA1S00702058 (Vitara) -> dealer paint code 26U, page
    # "Exterior color" C01; and TSMNZC72S00618058 (Swift), whose dealer
    # record reads BODY COLOR ZCF ("ZCF - RED") with TRIM COLOR /
    # Combination Color C05.
    #
    # This was WRONG IN PRODUCTION for as long as Suzuki has been
    # supported, and the regression battery certified it: the expected
    # answer for TSMNZC72S00618058 was recorded as C05 — the trim code —
    # so every green run reinforced a wrong answer. Nothing in the
    # extractor could catch that; only a dealer record could, and did.
    #
    # RESIDUAL RISK, recorded rather than guessed away (2026-08-08 audit).
    # This pattern is make-AGNOSTIC, like every other in this list — it
    # fires on any page with the two-row shape, not only Suzuki. It is
    # verified on Suzuki and unverified elsewhere. The failure it could
    # cause: a make whose page has a literal "Color" row holding a short
    # alphanumeric token, immediately followed by "Exterior colour"
    # holding the REAL paint code — there we would now take the wrong
    # row. No such page exists among the dumps held (checked: the shape
    # fires on all three Suzuki dumps and none of the eight others), but
    # absence of a counter-example is not evidence, as three PSA rules
    # demonstrated the day before this was written.
    #
    # Three cases are excluded BY CONSTRUCTION rather than by hope:
    # an "Interior color" first row cannot match (the line begins
    # "Interior", so ^Colou?r fails); a "Color" row holding a NAME cannot
    # match ([A-Z0-9]{2,8} admits no multi-word value); and single-row
    # pages cannot match (both rows are required adjacent), which is what
    # leaves Volvo alone.
    #
    # TRIPWIRE: if any NON-Suzuki make begins returning a code that fails
    # to resolve in coloureg's dataset, suspect this pattern first — the
    # unresolvable-code log is the signal, and this comment is the reason
    # to look here.
    #
    # STRUCTURAL, not a make guess: the pattern requires BOTH rows
    # ADJACENT and returns the first. A page carrying only one colour row
    # cannot match it, which is what keeps Volvo's single "Exterior
    # colour / 490" row on the pattern below, untouched. Verified against
    # both Suzuki legs (catalogue and dashboard dumps, identical shape)
    # and against the Volvo, Mercedes, Ford and colour-name negatives.
    # [ \t\r] not [ \t]: under CRLF line endings the bare \t/space class
    # could not cross the \r, the pattern failed, and extraction fell
    # through to the "Exterior colour" pattern below — returning the TRIM
    # code (C05) instead of the body code. A silent wrong answer, not a
    # miss. Found by metamorphic CRLF-perturbation testing 2026-08-08.
    re.compile(r"(?m)^Colou?r[ \t\r]*\n[ \t\r]*([A-Z0-9]{2,8})[ \t\r]*\n"
               r"[ \t\r]*Exterior\s*colou?r\b"),

    # VW/Audi: "Exterior color / Paint Code\n8E / A7W" — code after the slash.
    # POST-slash choice dealer-verified 2026-08-08 (Golf, LA7N vs page
    # A7N: L-stripped notation; pre-slash is the 2-char order code) —
    # detail in the VERIFICATION LEDGER above. Do not prefix the L.
    # The (?>...) atomic groups are load-bearing, not style. With plain
    # \s* / [A-Z0-9]+ this pattern backtracks QUADRATICALLY when the label
    # is present but the value is not: "Exterior color / Paint Code"
    # followed by a long whitespace run and a long alphanumeric run with no
    # "/" measured 1.4s on a 12k-char input (and ~900ms at 10k). Real pages
    # are ~8k and cost ~2ms for the whole extraction stack, so this was
    # never hit in practice — but a malformed or error page carrying the
    # label could add over a second to a single lookup for nothing.
    # Atomic groups forbid backtracking into the whitespace/alnum runs,
    # which drops the same input to 0.18ms while matching exactly the same
    # strings (verified against real VW pages plus colon, blank-line and
    # spaces-only variants, and the Interior-color negative case).
    # Requires Python 3.11+; the container is 3.12 (playwright noble image).
    re.compile(
        r"Exterior\s*colou?r\s*/\s*Paint\s*Code(?>\s*)[:\n]?(?>\s*)"
        r"(?>[A-Z0-9]+)\s*/\s*([A-Z0-9]{2,8})",
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
    #
    # Some MINIs break the finish into its OWN parenthesis BEFORE the code:
    # "MOONWALK GREY (METALLIC) (B71)". Without handling that, the non-greedy
    # name run stops at the first "(", capturing "METALLIC" (rejected by
    # _is_valid_code) and losing the real code "B71". The optional
    # "(<FINISH>)" group — letters/spaces only, so it can never swallow a
    # code paren (codes contain a digit or are <=3 chars) — lets the capture
    # reach the actual code paren. Single-paren "STERLINGGRAU (472)" is
    # unaffected (the optional group simply doesn't match).
    re.compile(
        r"(?:Exterior\s*)?(?:Colou?r|Farbe)\s*[:\n]\s*"
        r"[A-Z0-9][A-Z0-9 \-/]*?"
        r"(?:\s*\([A-Z ]+\))?"
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

    # BMW/MINI format. The name run may be followed by a separate
    # "(FINISH)" paren before the "(CODE)" paren — e.g. "MOONWALK GREY
    # (METALLIC) (B71)". We KEEP the finish in the description (-> "Moonwalk
    # Grey (Metallic)"), matching how Ford's name-only path keeps "(Metallic)"
    # — a consistent treatment of the finish across brands, and it matters
    # for paint matching. Single-paren "STERLINGGRAU (472)" is unchanged
    # (optional group doesn't match), and a name with the finish already
    # inline ("SNAPPER ROCKS BLUE METALLIC (C1G)") is likewise unaffected.
    re.compile(
        r"(?:Exterior\s*)?(?:Colou?r|Farbe)\s*[:\n]\s*"
        r"([A-Z0-9][A-Z0-9 \-/]*?(?:\s*\([A-Z ]+\))?)"  # name + optional (FINISH)
        r"\s*\(\s*[A-Z0-9]{2,8}\s*\)",                    # then the (code) paren
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
    # The newer React/SPA dashboard ("pl24-app") shows a red MUI snackbar
    # reading exactly "Error while loading vehicle" when the universal
    # search can't resolve a VIN. Observed on VINs partslink24 genuinely
    # doesn't carry (US-built Jeeps "1C4...", and a Fiat) — in every
    # observed case the catalog leg simultaneously showed the yellow
    # "No data was found ... built before 2006" Tip, and VINs that ARE
    # carried loaded a full vehicle-data page instead of this toast. So
    # this toast is a DEFINITIVE not-found, not a transient load error.
    # Adding it here lets the dashboard leg fast-fail in ~300ms instead of
    # eating the full 10s wait; the resulting outcome (not_found_as_routed)
    # is unchanged from what the slow path already produced — only faster.
    # NB the snackbar auto-dismisses after a few seconds, so detection is
    # best-effort within the poll window; missing it simply reverts to the
    # existing 10s timeout path (still correct, just slower).
    "error while loading vehicle",
    # The restructured PSA-platform catalogues (Opel/Vauxhall moved under
    # PSA -> psa_opel_parts / psa_vauxhall_parts, and other Stellantis
    # brands on the same React/SPA "pl24-app") show this autocomplete-style
    # message under the VIN box when the catalogue can't resolve the VIN:
    # "There are no results for the specified search criteria." Without it,
    # the catalog leg never recognises the not-found, polls the full 10s,
    # returns a silent timeout, the retry fires for ANOTHER 10s, and the
    # VIN finally fails as a timeout (~22s) — when the catalogue had in fact
    # answered "no match" almost instantly. Matching the distinctive core
    # substring lets the catalog leg fast-fail in ~300ms. Outcome is
    # unchanged (the VIN genuinely isn't in this catalogue), only far faster.
    "no results for the specified search",
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


def _handle_catalog_candidates(page: Page) -> bool:
    """Pick a catalogue when partslink24 offers several for one VIN.

    Distinct from _handle_model_picker: that one handles the React
    sales-type dropdown ("Please select:" + _item_yt7ex_27 rows, Mercedes).
    THIS one handles the old-Struts CATALOGUE-candidate table, which has no
    "please select" text at all and so was invisible to every existing
    check:

        <table id="nav-vinCatalogCandidates-table">
          <tr class="tc-row tc-data-row"
              url="vin-group.action?catalog=THMTPB917&...&vin=...">
            <td class="catalogName">I10 17</td>
            <td class="fromDate">Oct 3, 2016</td>
            <td class="toDate">Aug 20, 2019</td>

    Without this, the VIN resolves fine server-side but the vehicle page is
    never reached: wait_for_vehicle_data times out, the silent-timeout
    re-submit hits the same table, the dashboard leg hits it twice more,
    and the VIN is reported as page_load_timeout — ~47s of dead waiting
    ending in a false not-found. Observed on NLHA851ALKZ503536 (Hyundai
    i10) on both 2026-07-14 and 2026-08-01, identically.

    Picking the FIRST candidate is safe: verified 2026-08-01 by opening
    both candidates for that VIN by hand — THMTPB917 and TEURPB917 returned
    byte-identical vehicle data (same production date, market, engine and
    transmission numbers, and the same "Exterior color: TOMOTO RED"). The
    candidates are market/date-range variants of one catalogue, not
    different vehicles, so they cannot yield a different paint code. Same
    reasoning as the Mercedes sales-type picker above.

    Navigates via the row's own `url` attribute rather than clicking, so we
    don't depend on the Struts click handler being bound yet.

    Returns True if candidates were present and one was opened.
    """
    rows = page.locator(
        "#nav-vinCatalogCandidates-table tbody tr.tc-data-row")
    try:
        n = rows.count()
    except Exception:
        return False
    if not n:
        return False

    href = None
    try:
        href = rows.first.get_attribute("url")
    except Exception:
        pass

    label = ""
    try:
        label = rows.first.inner_text().replace("\n", " ").strip()
    except Exception:
        pass
    log(f"catalogue candidates ({n}) -> opening first: {label or '?'}")

    if href:
        try:
            page.goto(urljoin(page.url, href),
                      wait_until="domcontentloaded", timeout=20_000)
            return True
        except Exception:
            pass
    # Fallback: click the row and let the page's own handler navigate.
    try:
        rows.first.click()
        return True
    except Exception:
        return False


def _expand_equipment_panel(page: Page) -> bool:
    """Renault/Dacia hide the paint code behind a COLLAPSED "Equipment"
    accordion. MUI unmounts a collapsed accordion's children, so the rows
    are not merely hidden — they are absent from the DOM, and inner_text
    cannot see them at any timeout. No regex change can reach them; the
    panel has to be opened. Confirmed on two real dumps (2026-08-14):
    collapsed, the element is a bare <h3> with aria-expanded="false" and
    no MuiCollapse sibling at all.

    Gated deliberately:
      - only fires when the panel EXISTS (VW/Audi pages have no such
        accordion — their code is in the always-expanded "Vehicle data"
        panel), so every currently-verified estate is untouched;
      - only called after the normal extraction found no code, so a
        successful lookup never pays the click or the mount;
      - every failure is swallowed and reported as False. A page that
        will not expand must degrade to the existing no-code outcome,
        never to an exception.

    Returns True if the panel was clicked open, False otherwise."""
    try:
        panel = page.locator('[data-test-id="vinfoEquipment"]').first
        if panel.count() == 0:
            return False
        btn = panel.locator("button").first
        if btn.get_attribute("aria-expanded") == "true":
            return False          # already open; nothing to do
        btn.click(timeout=3_000)
        # The accordion animates (measured ~1.3-1.7s transition-duration on
        # the two real pages). Wait for a row to actually mount rather than
        # sleeping a guessed interval.
        panel.locator('[data-test-id="row"]').first.wait_for(
            state="attached", timeout=5_000)
        return True
    except Exception:
        return False


def _handle_model_picker(page: Page) -> bool:
    """Some VINs (notably older Mercedes) don't resolve to a single vehicle:
    partslink24 shows a "Please select:" dropdown of sales-type variants
    (different markets — 'Valid for: AU/JP/CA,US' or unmarked) and waits for
    a pick before loading the vehicle page with the paint code. Without
    handling this, the page never shows vehicle data, so wait_for_vehicle_data
    times out (then the silent-retry + every fallback leg + the B2 transient
    retry all hit the same picker — ~2 min of dead waiting ending in a false
    not-found).

    Empirically (WDB2010242F790734, a UK 190E): EVERY sales-type variant
    resolves to the SAME Paint Code (441) — they differ only in parts
    catalogue/market, not paint. So auto-picking the first option is SAFE: it
    cannot pick a "wrong colour", because the colour is identical across
    variants. (If a future VIN is found where variants carry different paint
    codes, this assumption would need revisiting — but the observed Mercedes
    behaviour is shared paint across sales-types.)

    Returns True if a picker was found and an option was clicked (caller should
    keep waiting for the vehicle data to load), False if no picker was present.
    """
    # The picker is a dropdown under a "Please select:" title; each variant is
    # an _item_yt7ex_27 row, the first of which is the title itself.
    title = page.locator('div._item_yt7ex_27._itemTitle_yt7ex_44',
                          has_text="Please select").first
    try:
        if not title.count() or not title.is_visible():
            return False
    except Exception:
        return False
    # Click the first SELECTABLE option (an _item_yt7ex_27 that is NOT the
    # title and NOT the camera-scan icon row from the other dropdown panel).
    # Picking any is safe (same paint code); first is simplest. Scoped by
    # has_text="Type code" so we only ever match real sales-type rows.
    options = page.locator(
        'div._item_yt7ex_27:not(._itemTitle_yt7ex_44):not(._iconItem_yt7ex_51)',
        has_text="Type code")
    try:
        n = options.count()
    except Exception:
        return False
    if not n:
        # The title matched but NO selectable option did. The realistic
        # cause is partslink24 redeploying the component with fresh CSS-
        # module hashes (the _yt7ex_ suffix in the class names above is a
        # build hash, not a stable id) — at which point this handler goes
        # blind and a multi-variant VIN regresses to the old ~2min false
        # not_found_as_routed. Say so loudly: the Railway log should name
        # the cause the day it happens, not present a bare timeout.
        log("model picker: 'Please select' text present but no sales-type "
            "options matched the known selectors — partslink24 may have "
            "rotated the component's hashed class names; picker handling "
            "is blind until the selectors in _handle_model_picker are "
            "updated from a live dump")
        return False
    log("model picker ('Please select') detected -> picking first sales-type "
        "(all variants share the same paint code)")
    try:
        options.first.click()
    except Exception as exc:
        log(f"model picker click failed: {exc!r}")
        return False
    return True


def wait_for_vehicle_data(page: Page, timeout_ms: int = 10_000) -> str | None:
    # WALL-CLOCK deadline, not a sleep counter. The loop body's
    # collect_all_text is not free: it does one inner_text round trip per
    # frame with a 2s per-frame ceiling, so a degraded/stuck frame makes an
    # iteration cost seconds, not milliseconds. The previous form
    # (`waited += interval` per iteration) counted only the 300ms sleeps and
    # therefore ran a FIXED ~34 iterations regardless — one consistently
    # slow frame turned this "10s" wait into ~78s of wall time, and that
    # multiplies across every catalogue leg, each leg's silent-timeout
    # re-submit, and the dashboard leg: minutes of a pinned pool slot for
    # one job, a certain client 504 at REQUEST_TIMEOUT_S, and every queued
    # job behind it abandoned. Same hang-not-fail shape POOL_START_TIMEOUT_S
    # exists to bound at startup; this bounds it in the hot path. Fewer
    # polls happen under pathological slowness, which risks nothing: data
    # either appears inside the wall budget or it doesn't, and the
    # silent-timeout re-submit in _process_result_page remains the second
    # chance either way.
    deadline = time.monotonic() + timeout_ms / 1000.0
    # Tight polling (300ms) so we detect the data within ~300ms of when
    # the page is actually ready. The check inside the loop is cheap
    # (one frame-text pull + a couple of regex matches); the wall-clock
    # win on fast pages is worth the small extra CPU.
    interval = 300
    text = ""
    picker_handled = False
    candidates_handled = False
    while time.monotonic() < deadline:
        text = collect_all_text(page)
        lower = text.lower()
        # Model-disambiguation picker: if present, click a variant once and
        # keep waiting for the vehicle page it loads. Guarded by a flag so we
        # only auto-pick once (a re-appearing picker would otherwise loop).
        if not picker_handled and "please select" in lower:
            if _handle_model_picker(page):
                picker_handled = True
                page.wait_for_timeout(interval)
                continue
        if BRAND_UNAVAILABLE_RE.search(text):
            return text
        if any(p in lower for p in VIN_NOT_FOUND_PHRASES):
            return text
        # PSA_BCODE_COLOUR_RE alongside the needle: the PSA-built estate
        # (Toyota Proace, K0/G9) labels its colour row `B0N?? -> <name>
        # PAINT` and carries NONE of the phrases the needle looks for, so
        # without this the loop cannot recognise a fully-rendered PSA page
        # and ALWAYS runs to the deadline, surviving only on the
        # PAGE_LOADED fallback below — measured 2026-08-07: every one of
        # five Proace lookups paid the full 10s, and three of the five
        # then silent-timed-out and re-submitted for a second 10s.
        #
        # Safe because a match proves the ATTRIBUTE BLOCK HAS RENDERED —
        # nothing more. (An earlier version of this comment claimed a
        # match meant the paint code was present; that was rule 3's
        # theory, and the Proace City falsified it: its colour row
        # matches this expression while its true code, KCA, appears
        # nowhere in the DOM.) Rendered is all the wait loop needs:
        # further polling cannot add content to a finished page, so
        # returning now merely converts a 10-20s wait into an immediate,
        # equally honest result — usually paint_data_missing, since no
        # code is derivable from this estate. Verified against five real
        # Proace dumps (matches the four that carry attributes, correctly
        # declines the 2024 van that carries none) and against the
        # conventional VW / Ford / Hyundai / Mini / PSA-Peugeot page
        # shapes, none of which it matches — no other estate renders
        # B-codes at all. This is a WHEN-to-stop signal only; it can
        # never influence WHAT is returned.
        if VEHICLE_DATA_NEEDLE.search(text) or PSA_BCODE_COLOUR_RE.search(text):
            return text
        # Catalogue-candidate table (old-Struts brands, e.g. Hyundai/Kia).
        # Carries no "please select" text, so unlike the model picker above
        # it cannot be gated behind a cheap substring test — it needs a real
        # locator call, which is a browser round trip. Hence its position
        # HERE, after the three in-Python needle checks: a page showing the
        # candidates table matches none of them (no vehicle data, no
        # not-found text, no brand-unavailable notice), so we still catch it
        # on the same iteration, but successful and cleanly-not-found
        # lookups never pay for the round trip at all.
        # Same one-shot guard as the picker: a re-appearing table must not
        # loop.
        if not candidates_handled and _handle_catalog_candidates(page):
            candidates_handled = True
            page.wait_for_timeout(interval)
            continue
        page.wait_for_timeout(interval)
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
    # Interior guard (see _match_is_interior). These pre-pattern
    # extractors run BEFORE the guarded pattern loop, so the guard
    # must be applied at their own match site or they bypass it
    # entirely — found 2026-08-08: "Interior Paint Code\\n851
    # (BLACK - leather)" returned "Black" as the exterior name.
    if _match_is_interior(text, m.start()):
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


RENAULT_BODY_COLOUR_RE = re.compile(
    r"(?m)^[ \t]*([A-Z0-9]{2,8})[ \t]*-[ \t]*BODY COLOURS?[ \t]*$")


def _extract_renault_body_colour(text: str) -> tuple[str, str]:
    """Renault/Dacia: the paint code is NOT in the "Vehicle data" panel at
    all — it lives in the separate "Equipment" accordion, in a two-column
    Equipment/Description table, as a row whose LEFT cell is
    "<CODE> - BODY COLOUR" and whose RIGHT cell is the colour name:

        OV369 - BODY COLOUR          ICE WHITE BC        (Clio V)
        OVDQH - BODY COLOUR          GREEN LICHEN GREY   (Dacia Spring)

    inner_text renders those two cells as consecutive lines, so the code
    is on the matched line and the name is on the next one.

    WHOLE-LINE ANCHORING IS LOAD-BEARING, not tidiness. The same table
    carries rows whose VALUE cell contains the phrase "BODY COLOUR":

        PGPRT2 - OUTSIDE DOOR HANDLE TYPE   DOOR HANDLE BODY COLOUR
        RENTC  - COLOR OF OUTSIDE MIRROR    NON-BODY COLOURED EXTERIO

    An unanchored search for the phrase captures "DOOR" and "NON" from
    those two (measured, both real pages, 2026-08-14). The pages also
    carry 108 (Dacia) and 160 (Clio) OTHER lines of the generic
    "CODE - LABEL" shape, so the literal label is the only safe anchor.

    WHY THIS BYPASSES _is_valid_code's DIGIT RULE — read that function's
    revisit block first. It drops 4+-letter digit-free codes (TEKPN,
    TERQH, PSTDD) and declined to relax, correctly, because no safe
    discriminator existed for a bare captured token. This site is not a
    bare token: the code arrives from a LABELLED POSITION, whole-line,
    left cell, label literally "BODY COLOUR". The label is what proves
    it is a code and not a colour word, so the shape heuristic is not
    needed here and would do only harm. OVDQH is dealer-confirmed
    (Renault Dialogys, 2026-08-14) and IS dropped by the digit rule —
    the tripwire in that block fired for real on this exact car. Every
    OTHER extractor and pattern keeps the digit rule unchanged; this is
    a trusted-context exemption, NOT a relaxation.

    Returns ("", "") on every non-Renault/Dacia page (the label does not
    occur), so this is a no-op elsewhere."""
    for m in RENAULT_BODY_COLOUR_RE.finditer(text):
        if _match_is_interior(text, m.start()):
            continue
        code = m.group(1)
        # Colour name = the next non-empty line, but ONLY if it is not
        # itself another "CODE - LABEL" row (i.e. the description cell was
        # empty and we have run into the following row). Verbatim: the
        # "BC" in "ICE WHITE BC" is the manufacturer's own string.
        desc = ""
        tail = text[m.end():].lstrip("\n")
        nxt = tail.split("\n", 1)[0].strip() if tail else ""
        if nxt and not RENAULT_BODY_COLOUR_RE.match(nxt) and " - " not in nxt:
            desc = nxt
        return code, desc
    return "", ""


def extract_paint_code(text: str) -> str:
    """Try each pattern in order; return the first match that survives
    validation. Patterns are ordered most-specific-first, so the natural
    case is that the first match is correct. The validation step exists
    to handle pages where a less-specific pattern (notably the Nissan-
    style 'Exterior color\\tCODE') matches a colour-name word like
    'ELECTRIC' or 'PHANTOM' as if it were a code. When that happens we
    skip and try the next pattern, rather than returning the bad token.
    """
    # Normalise line endings ONCE, here, before any pattern or helper
    # sees the text. Several patterns and _match_is_interior reason about
    # line structure with explicit \n and [ \t] classes, and a stray \r
    # silently breaks them — under CRLF the Suzuki two-row pattern failed
    # and extraction fell through to the TRIM code (a wrong answer, not a
    # miss), while Volvo lost its colour name entirely. Fixing the class
    # at the boundary is safer than auditing every pattern for \r
    # tolerance, and it is a no-op on real input (Playwright's inner_text
    # yields \n), so it costs nothing and closes the whole family.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # smart first: its two-part "Paint Code" field lists the tridion frame
    # code before the body code, so the generic patterns would grab the
    # frame. The helper picks the body-panel code (and is a no-op on every
    # non-smart page, anchored on the word "tridion").
    # Renault/Dacia "<CODE> - BODY COLOUR" equipment row FIRST: it is the
    # most specific anchor on any page we handle (a literal label on a
    # whole line), it occurs on no other brand, and it deliberately does
    # NOT go through _is_valid_code — see the helper for why that is a
    # trusted-context exemption and not a relaxation of the digit rule.
    rd_code, _ = _extract_renault_body_colour(text)
    if rd_code:
        return _normalise_code(rd_code.upper())
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
        # finditer, not search: the interior guard must skip the MATCH,
        # not abandon the PATTERN. With search() a page listing the
        # interior row BEFORE the exterior one lost its code entirely —
        # "Interior Colour\nBLACK (851)\nExterior Colour:\nCHILI RED
        # (851)" returned "" because the guard rejected the first match
        # and `continue` moved to the next pattern, never reaching the
        # exterior row the SAME pattern would have matched. Found by
        # metamorphic noise-invariance testing 2026-08-08; the guard
        # itself (added the same day) introduced it.
        for m in pat.finditer(text):
            if _match_is_interior(text, m.start()):
                continue          # skip THIS match, keep scanning this pattern
            candidate = _normalise_code(m.group(1).upper())
            if _is_valid_code(candidate):
                return candidate
            break                 # first non-interior match decides this
                                  # pattern, exactly as search() did — the
                                  # interior skip is the ONLY semantic change
    # PSA B-code reconstruction is DISABLED — see PSA_BCODE_COLOUR_RE.
    # The regex itself is still used by wait_for_vehicle_data (a timing
    # optimisation that cannot produce a wrong answer), but nothing
    # derives a CODE from it any more.
    return ""


_INTERIOR_RE = re.compile(r"interior", re.I)


def _match_is_interior(text: str, start: int) -> bool:
    r"""True when a pattern matched on a line that names an INTERIOR row.

    A post-match guard rather than a lookbehind on each pattern, so a
    pattern added later cannot reintroduce the leak by forgetting one.
    Found 2026-08-08 while auditing the Suzuki field-selection bug: two
    real leaks existed, and neither pattern looked suspicious on its own.

      - MINI/BMW paren pattern: the leading `(?:Exterior\s*)?` is
        OPTIONAL, so a bare "Colour:" matches — and nothing prevented
        "Interior Colour:\nBLACK (851)" from yielding 851/Black as the
        exterior paint.
      - The bare "Paint Code" pattern matches inside
        "Interior color / Paint Code\n8E / A7W". The VW pattern above it
        correctly declines that line; this one did not.

    Scope is deliberately ONE LINE — the line the match STARTS on, which
    is the line carrying the label. Widening it to neighbouring lines
    would be unsafe: partslink24 renders a catalogue nav entry called
    "INTERIOR PARTS" on these same pages (line 73 of the Suzuki dump,
    twenty lines from the colour rows), and a windowed check could
    suppress a legitimate row on a page where that heading happened to
    land nearby.
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if line_end == -1:
        line_end = len(text)
    return bool(_INTERIOR_RE.search(text[line_start:line_end]))


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

    THE REVISIT CONDITION IS NOW MET — recorded 2026-08-08, deliberately
    NOT acted on. Three real manufacturer codes are 4+ letters with no
    digit and are therefore DROPPED here:

        TEKPN  Renault      TERQH  Dacia      PSTDD  Ford

    (all three seen in VDG's output via coloureg; PN3BJ and OV369 from
    the same families pass, because they contain digits). All three
    makes ARE routed by this scraper, and five of the code patterns can
    capture a 5-letter token, so the drop is reachable: the code would
    be extracted correctly and then silently discarded here, surfacing
    as paint_data_missing rather than a wrong answer.

    NOT relaxed, because no safe discriminator exists on the evidence we
    have. The rule's job is to reject colour WORDS, and the obvious
    tests do not separate the two populations: a vowel-ratio threshold
    that admits TEKPN (0.2) also admits GREY (0.25), and a length rule
    cannot tell PSTDD from SLEEK. Guessing a discriminator is exactly
    the move that produced three wrong PSA rules the previous day.

    The correct fix, when a real case appears, is upstream: stop the
    name-shaped patterns capturing names in the first place, using the
    dump from that case. Until then this is a known FALSE-NEGATIVE class
    (silently returns nothing) and never a false positive, which is the
    right direction for it to fail in.

    TRIPWIRE: if a Renault, Dacia or Ford lookup returns
    paint_data_missing on a page that visibly shows a 4+-letter code,
    this function is the cause.
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
    # Interior guard (see _match_is_interior). The regex above uses
    # "Interior" as a TERMINATOR, which makes the normal two-row page
    # safe, but the LABEL itself still matches inside "Interior Paint
    # Code" — verified 2026-08-08: that label with parser-valid content
    # returned EAA/Black as the exterior paint. Until this guard, the
    # only thing preventing it was _smart_balanced_paren_groups happening
    # to decline most malformed inputs, which is safety by accident.
    if _match_is_interior(text, m.start()):
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


def _extract_mercedes_colour(text: str) -> tuple[str, str]:
    """(code, description) from Mercedes' "Paint Code" field.

    Two observed layouts:
      passenger: "191 (Cosmos black - Metallic finish)"   (3-digit code)
      vans:      "9147 (Arctic white paint MB 9147)"        (4-digit code)
    The generic code pattern recovers the code; this helper additionally
    recovers the NAME ("Cosmos black" / "Arctic white"), which was being
    dropped (Mercedes results were code-only).

    Name cleanup, applied in order, each defensive (no-op if absent):
      1. cut a trailing " - <finish>"            (passenger: "- Metallic finish")
      2. cut a trailing " paint ..."             (vans: "... paint MB 9147")
      3. cut a trailing " MB <digits>..."         (any leftover MB-code tail)
    Falls back to the whole parenthetical if none apply.

    Accepts a 3- OR 4-digit code (passenger uses 3, vans use 4). Requiring
    digits is also what keeps this from firing on smart's letter codes
    ("EB2") — smart has its own helper and runs first.

    NOTE: the van format is confirmed on a single Sprinter page so far;
    the cleanups are defensive, but more van samples would harden it.
    """
    m = re.search(r"Paint\s*Code\s*[:\n\t ]+(\d{3,4})\s*\(([^)]*)\)", text, re.I)
    if not m:
        return "", ""
    # Interior guard (see _match_is_interior). These pre-pattern
    # extractors run BEFORE the guarded pattern loop, so the guard
    # must be applied at their own match site or they bypass it
    # entirely — found 2026-08-08: "Interior Paint Code\\n851
    # (BLACK - leather)" returned "Black" as the exterior name.
    if _match_is_interior(text, m.start()):
        return "", ""
    code = m.group(1)
    name = m.group(2).strip()
    name = re.split(r"\s+-\s+", name)[0].strip()           # "- Metallic finish"
    name = re.split(r"\s+paint\b", name, flags=re.I)[0].strip()  # "paint MB 9147"
    name = re.sub(r"\s+MB\s+\d+.*$", "", name, flags=re.I).strip()
    return code, name


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
    # Interior guard (see _match_is_interior). These pre-pattern
    # extractors run BEFORE the guarded pattern loop, so the guard
    # must be applied at their own match site or they bypass it
    # entirely — found 2026-08-08: "Interior Paint Code\\n851
    # (BLACK - leather)" returned "Black" as the exterior name.
    if _match_is_interior(text, m.start()):
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
    # Normalise line endings ONCE, here, before any pattern or helper
    # sees the text. Several patterns and _match_is_interior reason about
    # line structure with explicit \n and [ \t] classes, and a stray \r
    # silently breaks them — under CRLF the Suzuki two-row pattern failed
    # and extraction fell through to the TRIM code (a wrong answer, not a
    # miss), while Volvo lost its colour name entirely. Fixing the class
    # at the boundary is safer than auditing every pattern for \r
    # tolerance, and it is a no-op on real input (Playwright's inner_text
    # yields \n), so it costs nothing and closes the whole family.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # smart two-part "Paint Code" — pick the body-panel colour word.
    # Renault/Dacia equipment row: the colour name sits in the adjacent
    # cell, so it comes back even when the dataset has no row for the code
    # (OVDQH resolves to nothing in paint_lookup.json as of 2026-08-14).
    _, rd_desc = _extract_renault_body_colour(text)
    if rd_desc:
        return _titlecase_colour(rd_desc)
    _, smart_desc = _extract_smart_colour(text)
    if smart_desc:
        return _titlecase_colour(smart_desc)
    # Mercedes "Paint Code\n<code> (<name> - <finish>)" — recover the name.
    _, merc_desc = _extract_mercedes_colour(text)
    if merc_desc:
        return _titlecase_colour(merc_desc)
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
        # finditer for the same reason as extract_paint_code: skip the
        # interior MATCH, never abandon the pattern.
        for m in pat.finditer(text):
            if _match_is_interior(text, m.start()):
                continue          # skip THIS match, keep scanning
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
    # Set True by lookup_vin ONLY for the specific transient combination
    # "catalog leg timed out AND dashboard returned could-not-assign" — a
    # known false-not-found pattern (a present VIN whose catalog leg timed
    # out twice, then hit a transient dashboard could-not-assign on the
    # same run). lookup_vin_with_retry honours this flag to grant one
    # whole-VIN retry. NOT a persisted column — write_results never reads
    # it; it's purely an in-process retry signal. Defaults False, so every
    # other outcome is untouched.
    retryable_transient: bool = False


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
                        source: str = "catalog",
                        debug_suffix: str = "",
                        error_prefix: str = "",
                        timeout_msg: str = "vehicle data did not load (timeout)",
                        no_paint_msg: str = "paint code not found on result page",
                        retry_on_timeout: int = 1,
                        ) -> tuple[bool, str | None]:
    """Wait for the post-submit result page, then extract the paint code.

    Shared by _try_catalog and _try_dashboard: both submit a VIN, wait
    for the same kind of vehicle-data page, run the same extractors, and
    have the same three failure modes (timeout, vin-not-found, no paint
    code). Differences are confined to the debug-filename suffix and the
    exact error wording, passed via kwargs so each caller keeps the same
    strings it produced before (historical results.csv rows depend on
    that wording).

    Silent-timeout retry (retry_on_timeout): partslink24 catalogue pages
    frequently time out at the 10s limit even though the VIN is present —
    proven by VINs that succeed on a re-run with the same code (e.g. a
    Fiat that returned page_load_timeout on one run and "231 / Bez
    Pastelna" on the very next). wait_for_vehicle_data returns None ONLY
    for this silent-timeout case: a not-found Tip or brand-unavailable
    notice returns the text instead (so those still fast-fail and are
    NOT retried), and a loaded page returns its text. So None is the
    precise, unambiguous signal that a fresh re-attempt is worth making.
    We re-submit the VIN on the same page (submit_vin is idempotent) and
    wait again, up to retry_on_timeout extra times. Recovery always came
    from a fresh attempt, never from waiting longer, so we re-submit
    rather than raise the 10s window. The retry is per-leg and capped, so
    it does not multiply across the commercial/Classic/dashboard chain;
    it is independent of the whole-lookup EXTRA_RETRIES wrapper (which
    only re-runs on a thrown exception, never on this clean None return).

    Returns (True, None) on success or (False, reason) on failure."""
    def dump():
        # Failure-path dumps: --debug OR --dump both write here.
        if debug or DUMP_ALWAYS:
            dump_debug(page, vin + debug_suffix)

    text = wait_for_vehicle_data(page, timeout_ms=10_000)

    # Silent timeout (None) → fresh re-attempt(s). Only None qualifies:
    # not-found / brand-unavailable return text and must not be retried.
    attempts_left = retry_on_timeout
    while text is None and attempts_left > 0:
        attempts_left -= 1
        log(f"silent timeout — re-submitting {vin} "
            f"({source}, {attempts_left} retr{'y' if attempts_left == 1 else 'ies'} left after this)")
        ok, _err = submit_vin(page, vin, source=source)
        if not ok:
            # Re-submit itself failed (box gone, etc.) — stop retrying and
            # fall through to the timeout return below with the original None.
            break
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
        # Renault/Dacia last chance: the code may be behind the collapsed
        # "Equipment" accordion. Costs one click and one re-collect, and
        # ONLY on a page that has the panel and has already failed — so
        # no successful lookup and no other brand pays for it.
        if _expand_equipment_panel(page):
            text = collect_all_text(page)
            _populate_from_text(result, text)
            if result.paint_code:
                log("paint code recovered from the Equipment panel")
    if not result.paint_code:
        dump()
        return False, f"{error_prefix}{no_paint_msg}"
    # Success path: dump ONLY under --dump. Plain --debug must stay
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
        # save_state_for: in the service this page is bound False, so the
        # recovery login does NOT write storage_state.json into the
        # container (see the _PAGE_SAVE_STATE comment). CLI pages are
        # unbound and keep the historical save-on-login behaviour.
        login(page, save_state=save_state_for(page))
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

    # The password-field probe works against the rebuilt landing page: the
    # header instance of <pl24-login-ui> is hidden, so :visible resolves to
    # the inline one only. The Attention branch is dormant for the same
    # reason as the one in open_catalog — retained, not load-bearing.
    if page.locator('input[type="password"]:visible').count():
        log("dashboard fallback: session expired, re-logging in")
        login(page, save_state=save_state_for(page))
    elif page.locator('h1, h2').filter(has_text="Attention").first.count():
        log("dashboard fallback: attention page shown, re-logging in")
        login(page, save_state=save_state_for(page))

    ok, err = submit_vin(page, vin, source="dashboard")
    if not ok:
        if debug:
            dump_debug(page, vin + "_dashboard")
        return False, f"dashboard fallback: {err}"

    log("dashboard VIN submitted, waiting up to 10s for vehicle data")
    return _process_result_page(
        page, vin, result, debug,
        source="dashboard",
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

        # Legacy-sibling retry (Opel/Vauxhall only): partslink24 moved the
        # live Opel/Vauxhall catalogue under PSA and kept the old catalogue as
        # a "legacy" one for pre-PSA-era vehicles. Same shape as Classic above
        # — try the live (PSA) catalogue first, fall back to legacy only if it
        # failed to identify the vehicle. Skip on "paint code not found" for
        # the same reason as Classic: the PSA catalogue positively identified
        # the car (page loaded, no code), so legacy can't supply a code
        # partslink24 doesn't have, and trying would just burn a ~10s timeout.
        # Confirmed: 2006 Vauxhall Astra "no results" on PSA -> 4CU on legacy.
        legacy = LEGACY_SIBLING.get(brand)
        if (legacy and legacy in LEGACY_CATALOG_SERVICE
                and "paint code not found" not in (last_leg_error or "").lower()):
            log(f"trying Legacy sibling: {legacy}")
            result.paint_code = ""
            result.paint_description = ""
            ok, err = _try_catalog(page, row.vin, legacy, result, debug)
            if ok:
                result.via = "catalog:legacy"
                return result
            log(f"Legacy sibling failed: {err}")
            catalog_error = f"{catalog_error}; {legacy}: {err}"
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

    # B2 — retryable-transient false-not-found.
    # The specific bad combination: the catalog leg(s) ended in a TIMEOUT
    # (not a real not-found — that returns "no data was found" text, and
    # not brand-unavailable — that skips the dashboard entirely above), AND
    # the dashboard then returned "could not be assigned to a distinct
    # model". Both can be transient on a struggling session, and together
    # they produce a present VIN mislabelled not_found_as_routed (the
    # could-not-assign branch in categorise() wins over the timeout). The
    # catalog leg already retried once on its silent timeout, so reaching
    # here means it timed out twice AND the dashboard transiently failed —
    # rare, but a silent false negative when it happens. Flag it so
    # lookup_vin_with_retry grants ONE whole-VIN retry; on that retry the
    # catalog will almost always either load the data (recovering the VIN)
    # or return a real not-found TEXT (which is not a timeout, so the flag
    # won't be set again and the honest not_found_as_routed stands). The
    # flag thus only ever recovers a false negative or leaves the result
    # unchanged — it can never make an outcome worse.
    #
    # Keys off catalog_error containing a timeout (the catalog leg's own
    # timeout wording) AND err being the could-not-assign verdict. The
    # toast "error while loading vehicle" is DEFINITIVE not-found (see the
    # VIN_NOT_FOUND_PHRASES comment) and is deliberately NOT treated as
    # retryable here.
    cat_lower = (catalog_error or "").lower()
    err_lower = (err or "").lower()
    catalog_timed_out = "timeout" in cat_lower or "did not load" in cat_lower
    dashboard_could_not_assign = (
        "could not be assigned to a distinct model" in err_lower
    )
    if catalog_timed_out and dashboard_could_not_assign:
        result.retryable_transient = True

    return result


def lookup_vin_with_retry(page: Page, row: LookupRow, debug: bool,
                          allow_dashboard_fallback: bool = True,
                          ) -> LookupResult:
    """Retry only on genuine browser-side exceptions (Playwright timeout,
    network errors, etc.) OR on the B2 retryable-transient flag. 'No data'
    / 'paint code not found' / 'VIN not in DB' are logical outcomes
    returned cleanly from lookup_vin, and retrying them just wastes time —
    they'll always produce the same result.

    The one clean-return exception is lookup_vin's `retryable_transient`
    flag, set ONLY for the "catalog timed out AND dashboard could-not-
    assign" combination — a transient false-not-found worth one more
    whole-VIN attempt (see lookup_vin). It shares this loop's existing
    bound (EXTRA_RETRIES) and spacing, so it cannot loop unbounded and
    respects the one-VIN-at-a-time constraint."""
    for attempt in range(EXTRA_RETRIES + 1):
        was_exception = False
        # C1 snapshot, taken PER ATTEMPT (see below).
        login_gen_before = LOGIN_GENERATION
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

        # C1 (2026-08-03, corrected here 2026-08-03): a session heal DURING
        # the lookup voids every leg that ran before it — the leg that
        # actually carries the VIN may have been the one driving a dead
        # session. Observed live: routed VW leg got the SPA shell and failed
        # "VIN box not visible", the commercial leg healed inline, and the
        # chain concluded not_found_as_routed for an A7N vehicle.
        #
        # This test lives HERE, not inside lookup_vin, and that placement is
        # the whole point. lookup_vin has EIGHT return statements; the first
        # version of C1 sat above the last one and so covered exactly one of
        # them. Two of the others are reachable with a heal behind them and
        # no paint code — the `not allow_dashboard_fallback` exit, and the
        # far more important "skipping dashboard fallback" exit, which the
        # SERVICE hits whenever a leg reports paint-code-not-found or
        # brand-unavailable. Both silently escaped the net. The wrapper is
        # the single choke point every exit funnels through, so checking
        # here cannot be outflanked by a return path.
        #
        # Snapshot is per ATTEMPT, so attempt 2 measures its own heals
        # rather than inheriting attempt 1's.
        healed_mid_lookup = (LOGIN_GENERATION != login_gen_before)

        # Reasons to retry: a thrown browser-side exception (original
        # behaviour), the B2 transient false-not-found flag, or a C1 heal.
        # A clean result with none of them is final and returns immediately,
        # as before. The `r.paint_code` guard below is what keeps C1 from
        # firing when a later leg already recovered the code.
        should_retry = (was_exception or r.retryable_transient
                        or healed_mid_lookup)
        if r.paint_code or not should_retry:
            r.outcome = categorise(r)
            return r

        if attempt < EXTRA_RETRIES:
            # Reason reported accurately per cause. The first version
            # labelled EVERY retry as B2's "catalog timeout + dashboard
            # could-not-assign", including C1's — which would have sent
            # anyone reading the Railway log for a real incident chasing a
            # transient-timeout theory for a session-death event.
            if was_exception:
                reason = "browser exception"
            elif r.retryable_transient:
                reason = ("transient not-found (catalog timeout + dashboard "
                          "could-not-assign)")
            else:
                reason = ("session re-established mid-lookup — legs that ran "
                          "before the heal are void")
            log(f"retrying {row.vin} — {reason} "
                f"(attempt {attempt + 2}/{EXTRA_RETRIES + 1})")
            page.wait_for_timeout(1_500)
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
        base.with_suffix(".html").write_text(
            _redact_page_html(page.content()), encoding="utf-8"
        )
    except Exception:
        pass
    for i, fr in enumerate(page.frames):
        # frames[0] IS the main frame, so its content duplicates the
        # <vin>.html written above byte for byte. Skip it; keep every child
        # frame, which are the ones carrying real extra signal (the PSA
        # paint popup renders its table in an iframe, which is why
        # collect_all_text walks frames at all).
        if fr is page.main_frame:
            continue
        # Skip the usercentrics cookie-consent cross-domain bridge — same
        # boilerplate JS in every dump, useless for diagnosing partslink24
        # issues, just adds clutter to _debug/.
        if "usercentrics.eu" in (fr.url or ""):
            continue
        try:
            (DEBUG_DIR / f"{vin}_frame_{i}.html").write_text(
                _redact_page_html(fr.content()), encoding="utf-8"
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
        with RESULTS_FILE.open("r", newline="", encoding="utf-8") as f:
            existing_header = next(csv.reader(f), [])
        if existing_header and existing_header != headers:
            archive = RESULTS_FILE.with_suffix(
                f".old-{datetime.now():%Y%m%d-%H%M%S}.csv"
            )
            shutil.move(str(RESULTS_FILE), str(archive))
            log(f"results.csv header changed; archived old file -> "
                f"{archive.name}")

    new_file = not RESULTS_FILE.exists()
    with RESULTS_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(headers)
        for r in results:
            row = [getattr(r, field, "") for field, _ in columns]
            w.writerow(row)


def _clear_stale_debug_dumps() -> None:
    """Delete this-script's .html/.png dumps from a previous run so the
    current run's _debug/ only contains what just happened. Only removes
    files we ourselves would have created, never anything else stashed
    there.

    squeeze_prompt.* is EXEMPT. That dump captures the session
    squeeze-out prompt, which appears rarely and only for the moment
    before it is confirmed — it is the one artefact we cannot reproduce on
    demand. Clearing it at the start of the next run (which is what
    happened on 2026-08-01: the prompt fired at 07:30:00 and the next CLI
    invocation wiped it 11s later) defeats the entire point of capturing
    it. It is overwritten naturally the next time a prompt occurs."""
    keep = {"squeeze_prompt.html", "squeeze_prompt.png"}
    if DEBUG_DIR.exists():
        cleared = 0
        for f in DEBUG_DIR.iterdir():
            if f.name in keep:
                continue
            if f.is_file() and f.suffix.lower() in (".html", ".png"):
                try:
                    f.unlink()
                    cleared += 1
                except OSError:
                    pass
        if cleared:
            log(f"cleared {cleared} stale file(s) from {DEBUG_DIR.name}/")


def _launch_browser_and_context(pw: Playwright, headed: bool,
                                use_saved_state: bool):
    """Launch Chromium and build a context with all the anti-detection
    setup, returning (browser, context, page).

    This is the browser-construction half of the old run() body, extracted
    verbatim so BOTH run() (one-shot CLI/batch) and Session (long-lived
    service) build an identical, identically-fingerprinted browser. Nothing
    here changed — same launch args, same context kwargs, same Sec-CH-UA
    coherence fix, same init scripts, same dialog handler.

    `use_saved_state` controls whether STATE_FILE is loaded as storage_state
    (callers decide; run() loads it if present, the service may choose not
    to depend on a persisted session file)."""
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
    if use_saved_state and STATE_FILE.exists():
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
    return browser, context, page


def _establish_session(page: Page, save_state: bool = True) -> None:
    """Ensure `page` ends up on a logged-in partslink24 dashboard.

    This is the session-establishment half of the old run() body, extracted
    verbatim. Cold start (no saved session) logs in fresh; a reused session
    is validated by a single navigation to HOME_URL — if it redirects to the
    login form we complete login in place (no second navigation). Behaviour
    is unchanged from the original inline block.

    `save_state` is threaded through to the login tail so the long-lived
    service can opt out of writing storage_state.json if desired; the CLI
    keeps the original save-on-login behaviour (save_state=True)."""
    if not STATE_FILE.exists():
        login(page, save_state=save_state)
    else:
        # We reused saved cookies — but partslink24 sessions expire and
        # nothing in storage_state.json tells us whether they're still
        # valid. Navigate to the home page ONCE and look at where we land:
        #   - still logged in  -> the dashboard renders; proceed directly.
        #   - session expired  -> we stay on the landing page, which
        #     hosts the login component inline. Since the form is ALREADY
        #     in front of us we fill it in place
        #     (_complete_login_from_current_page) rather than calling
        #     login(), which would navigate to the same page again. This
        #     single navigation is the check: no separate verify trip.
        log("loading partslink24 (checking session)")
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeoutError:
            log("could not load partslink24; logging in fresh")
            login(page, save_state=save_state)
        else:
            handle_cookie_consent(page)
            if is_logged_in(page):
                # Landing page redirects a token-holder to /portal-ui via
                # its own inline script; either way the cookie is the test.
                log("saved session OK")
            else:
                # Expired: we are sitting on the landing page, which hosts
                # the login component inline, so fill it in place rather
                # than navigating again.
                log("saved session expired; re-logging in (in place)")
                _complete_login_from_current_page(page, save_state=save_state)


class Session:
    """A long-lived, logged-in partslink24 browser session for the service.

    Holds one browser + context + page open across many lookups so we don't
    pay the ~9s login on every request. Reuses the exact same construction
    and login logic as the CLI (via the shared helpers above), so the
    fingerprint and behaviour are identical to a one-shot run.

    Designed for one-VIN-at-a-time use: the scraper assumes a single page
    driven sequentially, so callers (the FastAPI service) MUST serialise
    lookups with a lock. This class does not lock internally — it leaves
    concurrency control to the caller, matching how run() drives one VIN at
    a time.

    Self-healing: lookup() validates the session cheaply (is_logged_in, no
    navigation) before each VIN and re-logs-in in place if it died — no
    background pinging, recovery happens only when a real request needs it.
    If the whole browser/context has crashed, start() can be called again to
    rebuild from scratch.

    State files: the service has no use for results.csv (that's the CLI
    batch artifact) and an ephemeral container has no use for persisting
    storage_state.json, so save_state defaults to False here — the session
    lives in memory for the life of the process."""

    def __init__(self, pw: Playwright, *, headed: bool = False,
                 skip_brand_check: bool = False,  # no-op; see note below
                 allow_dashboard_fallback: bool = True,
                 save_state: bool = False,
                 creds: Pl24Credentials | None = None):
        self._pw = pw
        # Which partslink24 account this session logs in as. None = the
        # process-wide environment credentials (CLI and single-account
        # service). Set per session by a multi-account pool, and bound to
        # the page in start() so that EVERY login path — including the
        # re-logins buried in _try_catalog/_try_dashboard — uses this
        # account rather than the environment's.
        self._creds = creds
        self._headed = headed
        # Retained but unused: the partslink24 brand-list verification was
        # removed when the 2026-07 rebuild replaced the home grid's
        # <a id="<service>_lc" title="<Brand>"> anchors with a React
        # component that exposes titles and logo slugs but NO service ids.
        # The parameter stays so service.py (which passes
        # skip_brand_check=SKIP_BRAND_CHECK) keeps working unchanged.
        self._skip_brand_check = skip_brand_check
        self._allow_dashboard_fallback = allow_dashboard_fallback
        self._save_state = save_state
        self._browser = None
        self._context = None
        self._page = None
        # Monotonic timestamp of the last activity that PROVED the session was
        # alive (login, or a lookup that reached partslink24). Drives the
        # proactive idle re-login in lookup(). In-memory and process-local —
        # the worker holds one session for its lifetime, so this persists
        # across requests without any datastore, and a worker restart correctly
        # resets it (a fresh process means a fresh login). None until start().
        self._last_interaction: float | None = None

    def _mark_interaction(self) -> None:
        """Record that the session just did real work (is alive now). Called
        after login and after any lookup whose outcome proves partslink24
        served a response, so the proactive idle check measures from the last
        time the session was known-good."""
        self._last_interaction = time.monotonic()

    def _idle_seconds(self) -> float | None:
        """Seconds since the session last proved alive, or None if it has not
        been established yet (no start())."""
        if self._last_interaction is None:
            return None
        return time.monotonic() - self._last_interaction

    def start(self) -> None:
        """Launch the browser and log in. (An optional brand-list
        verification used to run here; it was removed 2026-08-01 with the
        rest of the home-grid scraping — see the skip_brand_check note in
        __init__.) Idempotent-ish: if called when already started it tears
        the old browser down first so a crashed session can be rebuilt by
        simply calling start() again."""
        if self._browser is not None:
            self.close()
        self._browser, self._context, self._page = _launch_browser_and_context(
            self._pw, headed=self._headed, use_saved_state=self._save_state,
        )
        # Bind BEFORE the first login: _establish_session may log in
        # immediately, and every later re-login (including the ones inside
        # _try_catalog / _try_dashboard, which never see this Session) will
        # resolve the account through the page. save_state is bound for the
        # same reason: those deep re-logins must inherit this Session's
        # no-disk preference, not login()'s CLI default.
        bind_credentials(self._page, self._creds)
        bind_save_state(self._page, self._save_state)
        _establish_session(self._page, save_state=self._save_state)
        # Fresh login => session is alive now; start the idle clock.
        self._mark_interaction()

    def _ensure_logged_in(self) -> None:
        """Cheap per-request session validity check + in-place re-login.

        is_logged_in() is a no-navigation cookie check (one IPC read of the
        PL24TOKEN cookie — cheaper than the DOM probing it replaced), so this
        is essentially free when the session is healthy (the common case).
        When the session has expired, navigate home and complete login in
        place — the same recovery run() does for a stale saved session, just
        triggered lazily per request instead of once at startup.

        NOTE this check can still be fooled by a HALF-ALIVE session: the
        cookie can outlive the server-side session (e.g. after a squeeze-out
        by another login), exactly as the previous DOM check could be fooled
        by a stale page that still looked logged in. That failure mode is not
        new and is not handled here — it is caught by layer 3 in
        Session.lookup (catalog_ui_error -> force re-login -> retry once),
        WITH ONE CAVEAT: _force_relogin still consults is_logged_in itself,
        so if the cookie survives a dead session then layer 3 short-circuits
        too. See the open question in _force_relogin's docstring."""
        if is_logged_in(self._page):
            return
        log("session not logged in at request time; re-logging in")
        try:
            self._page.goto(HOME_URL, wait_until="domcontentloaded",
                            timeout=20_000)
        except PlaywrightTimeoutError:
            login(self._page, save_state=self._save_state)
            return
        handle_cookie_consent(self._page)
        if is_logged_in(self._page):
            return
        _complete_login_from_current_page(self._page,
                                          save_state=self._save_state)

    def lookup(self, row: LookupRow, *, debug: bool = False) -> LookupResult:
        """Run one VIN lookup on the held-open session, re-logging in first
        if the session has expired. Delegates to the same
        lookup_vin_with_retry the CLI uses, so per-VIN retry/fallback
        behaviour is identical.

        Caller must hold a lock around this — one VIN at a time.

        Three layers keep a long-lived session healthy without background
        pinging (which would be a bot signal and round-the-clock waste):

        1. PROACTIVE idle re-login (fast path for the predictable case). The
           session survives idle for a while and every lookup refreshes it
           (measured), so if a request arrives after a longer idle gap than
           SESSION_IDLE_RELOGIN_S the session is PROBABLY dead — we re-login
           BEFORE attempting, turning a ~38s fail-then-heal into a ~10s clean
           re-login. Tunable via PL24_SESSION_IDLE_S (0 disables).

        2. Cheap per-request check (_ensure_logged_in): is_logged_in() with no
           navigation, re-logs-in in place if the session is plainly dead.

        3. Self-heal backstop (the unpredictable case). _ensure_logged_in's
           cheap check can be FOOLED by a HALF-ALIVE session — the leftover
           page from the previous lookup still looks logged in, so we proceed,
           but the session is too stale to load a fresh catalogue and the
           catalog leg fails with `catalog_ui_error` (a CLEAN result, not an
           exception, so the service's crash-retry misses it and the user sees
           a false miss). We detect that signature, force a real re-login, and
           retry once. This also covers sessions that die WITHIN the proactive
           window (e.g. a squeeze-out by another login) — so layer 1 being a
           guess only changes whether a stale lookup is fast or slow, never
           whether it's correct. Genuine outcomes (not_found_as_routed,
           name_only, etc.) are never the catalog_ui_error signature, so they
           are never retried.

        After the lookup, last_interaction is refreshed iff the outcome proves
        the session served a real response (so clustered lookups stay warm and
        a failed-because-dead lookup does NOT reset the idle clock)."""
        if self._page is None:
            raise RuntimeError("Session.lookup() called before start()")

        # Layer 1 — proactive idle re-login. If the session has been idle
        # longer than the threshold, it is probably stale; re-login up front
        # rather than discovering it via a failed attempt + self-heal. Skipped
        # when the threshold is 0 (disabled) or the idle time is unknown.
        idle = self._idle_seconds()
        if (SESSION_IDLE_RELOGIN_S > 0 and idle is not None
                and idle > SESSION_IDLE_RELOGIN_S):
            log(f"session idle {idle:.0f}s > {SESSION_IDLE_RELOGIN_S:.0f}s "
                f"threshold — proactively re-logging in before lookup")
            self._force_relogin()
            self._mark_interaction()

        # Layer 2 — cheap validity check + in-place re-login if plainly dead.
        self._ensure_logged_in()
        result = lookup_vin_with_retry(
            self._page, row, debug=debug,
            allow_dashboard_fallback=self._allow_dashboard_fallback,
        )
        # Layer 3 — half-alive-session self-heal: a catalog_ui_error with no
        # code is the stale-session tell. Force a real re-login and retry once.
        if result.outcome == "catalog_ui_error" and not result.paint_code:
            log(f"catalog_ui_error for {row.vin} — likely stale session; "
                f"forcing re-login and retrying once")
            self._force_relogin()
            result = lookup_vin_with_retry(
                self._page, row, debug=debug,
                allow_dashboard_fallback=self._allow_dashboard_fallback,
            )

        # Refresh the idle clock iff the (final) outcome proves the session
        # reached partslink24. Failures that signal a dead/unengaged session
        # (catalog_ui_error, auth_error, page_load_timeout, unknown) do NOT
        # count — leaving the clock stale so the next request re-logs-in.
        if result.outcome in _SESSION_PROVEN_ALIVE_OUTCOMES:
            self._mark_interaction()
        return result

    def _force_relogin(self) -> None:
        """Re-establish the login after layer 3 decided the session is
        probably stale. Navigate home — which shows the login form when the
        session is dead — and complete login in place; fall back to a full
        login() if the navigation itself fails.

        OPEN QUESTION, deliberately not "fixed" without evidence. The early
        return below asks is_logged_in(), which since 1c40dc5 (2026-08-01)
        reads the PL24TOKEN cookie rather than the DOM. This function was
        written 2026-06-11 against the DOM version, where "navigate, then
        check" was a genuine server-side test. NOTES.md and _ensure_logged_in
        both describe layer 3 as BYPASSING the cheap check that fooled layer
        2 — and consulting the cookie here does not bypass it.

        Whether that matters depends on something untested: does partslink24
        clear PL24TOKEN when it serves a request carrying a dead token?
          - If it does, this check is still a real server-side test and
            nothing is wrong.
          - If it does not, layer 3 short-circuits in exactly the squeeze-out
            case it exists for, and the slot stays broken until the idle
            threshold fires.

        The argument for the first is that partslink24's own landing page
        redirects to /portal-ui via an inline script keyed on the cookie's
        PRESENCE, so a server that left a dead token in place would send its
        own users into a redirect loop. That is reasoning, not a measurement.

        Clearing the cookie here would force a real login unconditionally,
        but it is NOT a free fix: it discards a session that may be perfectly
        live, and re-logging in while partslink24 still holds that session
        raises a squeeze-out against ourselves on every transient
        catalog_ui_error. That is a certain cost against an unconfirmed
        benefit, so the log line below exists instead — the next real
        occurrence in the Railway log settles it either way."""
        try:
            self._page.goto(HOME_URL, wait_until="domcontentloaded",
                            timeout=20_000)
        except PlaywrightTimeoutError:
            login(self._page, save_state=self._save_state)
            return
        handle_cookie_consent(self._page)
        if is_logged_in(self._page):
            # Either a transient render glitch on a live session (benign, the
            # retry runs on it), or the untested case above. Logged loudly so
            # the two can be told apart: if this line is followed by a SECOND
            # catalog_ui_error for the same VIN, the cookie survived a dead
            # session and layer 3 needs the cookie-clearing fix.
            log("force-relogin: PL24TOKEN still present after home nav — "
                "treating session as live and retrying without re-login "
                "(if this VIN fails again, the token outlived the session)")
            return
        _complete_login_from_current_page(self._page,
                                          save_state=self._save_state)

    def is_alive(self) -> bool:
        """Best-effort check that the browser is still connected. The
        service can call this to decide whether to start() a fresh session
        after a suspected crash."""
        try:
            return self._browser is not None and self._browser.is_connected()
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        """Tear down the context and browser. Safe to call multiple times."""
        try:
            if self._context is not None:
                self._context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        self._browser = self._context = self._page = None


def run(pw: Playwright, rows: list[LookupRow], headed: bool, debug: bool,
        fresh: bool, skip_brand_check: bool,
        allow_dashboard_fallback: bool,
        dump_always: bool = False,
        inter_vin_delay: tuple[float, float] = (0.0, 0.0),
        ) -> list[LookupResult]:
    """One-shot CLI/batch entry point. Now a thin user of the shared
    browser/session helpers: build browser+context, establish session,
    verify brands, loop the VINs, tear down. Behaviour is identical to the
    previous inline implementation — the body was extracted into
    _launch_browser_and_context / _establish_session, not changed."""
    global DUMP_ALWAYS
    DUMP_ALWAYS = dump_always

    if fresh and STATE_FILE.exists():
        STATE_FILE.unlink()

    if debug or dump_always:
        _clear_stale_debug_dumps()

    browser, context, page = _launch_browser_and_context(
        pw, headed=headed, use_saved_state=True,
    )
    _establish_session(page, save_state=True)

    results = []
    lo, hi = inter_vin_delay
    for i, row in enumerate(rows, 1):
        # Inter-VIN pacing — OFF by default (inter_vin_delay defaults to
        # 0). partslink24 is a login-gated paid service that can see all
        # activity server-side, and bursty back-to-back lookups on one
        # account are the behaviour that has historically caused access
        # problems. Typical usage here is one VIN at a time, so pacing
        # rarely applies — but pass e.g. --delay 20-60 to space out a
        # multi-VIN batch (or the future queue worker). When enabled, sleep
        # a randomised interval BEFORE each VIN except the first, so the
        # first VIN never waits and the cadence isn't a clean fixed signature.
        if i > 1 and hi > 0:
            pause = random.uniform(lo, hi)
            log(f"pacing: waiting {pause:.0f}s before next VIN")
            time.sleep(pause)
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
                    help="dump HTML on failure (headless unless --headed)")
    ap.add_argument("--dump", dest="dump_always", action="store_true",
                    help="dump HTML for every result page, including "
                         "successes (headless unless --headed)")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore saved session and log in fresh")
    ap.add_argument("--skip-brand-check", action="store_true",
                    help="no-op, retained for compatibility (the brand-list "
                         "check was removed when partslink24 dropped the "
                         "scrapable home-grid anchors)")
    ap.add_argument("--no-fallback", action="store_true",
                    help="disable dashboard SEARCH VIN fallback")
    ap.add_argument("--delay", default="0",
                    help="seconds to wait between VINs (multi-VIN runs "
                         "only; the first VIN never waits). A single "
                         "number is a fixed delay; 'LO-HI' (e.g. '20-60') "
                         "is a randomised range. Default '0' (off) — "
                         "typical usage is one VIN at a time, so pacing "
                         "rarely applies; set e.g. '20-60' for spaced-out "
                         "multi-VIN batches or the queue worker.")
    args = ap.parse_args()

    # Parse --delay into a (lo, hi) tuple. Accept "N" (fixed) or "LO-HI".
    def _parse_delay(spec: str) -> tuple[float, float]:
        spec = spec.strip()
        try:
            if "-" in spec:
                lo_s, hi_s = spec.split("-", 1)
                lo, hi = float(lo_s), float(hi_s)
            else:
                lo = hi = float(spec)
        except ValueError:
            sys.exit(f"--delay: could not parse {spec!r} "
                     f"(use 'N' or 'LO-HI', e.g. '30' or '20-60')")
        if lo < 0 or hi < 0 or hi < lo:
            sys.exit(f"--delay: invalid range {spec!r} "
                     f"(need 0 <= LO <= HI)")
        return (lo, hi)

    inter_vin_delay = _parse_delay(args.delay)

    if args.vin:
        if not args.make:
            sys.exit("--vin requires --make (we don't guess the make)")
        # clean_vin is the single validator shared with read_lookups and
        # the service endpoint — see its docstring for the ASCII-first
        # ordering and why the three sites must not drift.
        vin = clean_vin(args.vin)
        if vin is None:
            sys.exit(f"--vin: malformed VIN {args.vin!r} "
                     f"(need 17 chars, letters excluding I/O/Q, digits)")
        rows = [LookupRow(vin=vin, make=args.make,
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
                      headed=args.headed,
                      debug=args.debug,
                      fresh=args.fresh,
                      skip_brand_check=args.skip_brand_check,
                      allow_dashboard_fallback=not args.no_fallback,
                      dump_always=args.dump_always,
                      inter_vin_delay=inter_vin_delay)

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