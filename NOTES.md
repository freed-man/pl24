# partslink24 paint code lookup

Looks up vehicle paint codes (and colour names, where partslink24 carries
them) by VIN, for feeding into coloureg. Drives partslink24 with Playwright.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Credentials are read from the environment as `PARTSLINK24_COMPANY_ID`,
`PARTSLINK24_USERNAME`, `PARTSLINK24_PASSWORD`. Two ways to provide them
(the script prefers the first):

1. **`env.py`** in the project root (same pattern as coloureg) — a small
   Python file that sets the three vars in `os.environ`.
2. **`.env`** in the project root — loaded via python-dotenv as a fallback.

## Input — `lookups.txt`

One CSV row per vehicle (whitespace tolerated, `#` for comments):

```
vin,make,category,year
WDD2120022A341787,Mercedes-Benz,M1,2010
WV1ZZZ2EZ76030517,Volkswagen,N1,2007
WF0YXXTTGYFT38981,Ford,N1,2015
```

- `vin` — required.
- `make` — required (the script does **not** guess from the VIN; a missing
  make records the row as an error). Use the everyday make name, e.g.
  `Mercedes-Benz`, `Volkswagen`, `Citroën`.
- `category` — optional; defaults to passenger. Set **`N1`** (and `N2`/`N3`)
  for commercial vehicles / vans (Sprinter, Transit, Crafter, etc.) so they
  route to the right commercial catalogue.
- `year` — optional; captured but currently unused (reserved for future
  Classic-catalogue cutoff routing).

## Usage

```bash
# first run — use --headed so you can watch and confirm login works
python lookup.py --headed

# subsequent runs (session cached in storage_state.json)
python lookup.py                         # process all rows in lookups.txt

# one-off single VIN (make is required; add --category for vans)
python lookup.py --vin WVWZZZ... --make Volkswagen
python lookup.py --vin WV1ZZZ... --make Volkswagen --category N1
```

> **Run one VIN at a time, spaced out — never bulk/batch runs.** partslink24
> flags concurrent/bulk access. This is an operating constraint of the
> system, not a preference.

### Flags

| Flag | What it does |
|---|---|
| `--headed` | Show the browser window (watch what happens). Add it to `--debug` or `--dump` to also watch while dumping; on its own it just shows the window. |
| `--vin VIN --make MAKE` | Look up a single VIN instead of `lookups.txt` |
| `--category N1` | Category for the single-VIN mode (vans, etc.) |
| `--fresh` | Ignore the saved session and log in clean (was: deleting `storage_state.json` by hand) |
| `--debug` | Dump HTML/screenshot to `_debug/<vin>.{html,png}` **on failure** (`_debug/` is wiped at the start of each run). Headless by default — add `--headed` to watch. |
| `--dump` | Dump on **every** result incl. successes — for inspecting a page that returns a wrong/blank value. Headless by default — add `--headed` to watch. (Renamed from `--dump-always`; no longer forces a window.) |
| `--skip-brand-check` | Skip the partslink24 brand-list verification at startup |
| `--no-fallback` | Disable the dashboard SEARCH VIN fallback |
| `--delay N` / `--delay LO-HI` | Seconds to wait between VINs (multi-VIN runs only; the first VIN never waits). A single number is a fixed delay; `LO-HI` (e.g. `20-60`) is a randomised range. **Default `0` (off)** — typical one-at-a-time usage doesn't need it; use it to space out a multi-VIN batch or the queue worker. |

`--debug` and `--dump` control HTML dumping only and run **headless** unless
you also pass `--headed`. Neither implies the other — this changed (they used
to force a window); pass `--headed` explicitly when you want to watch.

## Output — `results.csv`

Each run appends rows with: `Timestamp, Vin, Paint code, Paint description,
Via, Outcome, Error`.

- `Paint description` — colour name where partslink24 carries one.
- `Via` — which leg resolved it: `catalog`, `catalog:commercial`,
  `catalog:classic`, `catalog:legacy` (Opel/Vauxhall old catalogue), or
  `dashboard`.
- `Outcome` — machine-parseable status (`success`, `name_only`,
  `paint_data_missing`, `brand_unavailable`, `not_found_as_routed`,
  `unsupported_brand`, `page_load_timeout`, `catalog_ui_error`,
  `auth_error`, `missing_input`, `unknown`). See `ERRORS.md` for the full
  meaning of each and how to triage.

## If something breaks

Selectors are best-effort because partslink24's routes differ by
manufacturer subscription. If a step fails:

1. Run with `--debug --headed` and watch where it stops (and check the
   dumped `_debug/<vin>.html` / `.png`). `--debug` alone dumps headless; add
   `--headed` when you want to watch the browser live.
2. Inspect the field that should have been filled; copy its `name` or a
   unique attribute.
3. Add it to the relevant locator/extractor in `lookup.py`:
   - login fields → `login()`
   - VIN box → `lookup_vin()` / `submit_vin()`
   - paint extraction → the `PAINT_CODE_PATTERNS` / `PAINT_DESCRIPTION_PATTERNS`
     lists and the per-brand `_extract_*_colour` helpers (add a regex or a
     table label in the catalogue's language)

`ERRORS.md` is the full reference for every error string and `Outcome`.
For login problems specifically, `--fresh` forces a clean login.