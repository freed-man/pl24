# partslink24 paint code lookup

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# edit .env with your partslink24 ID, username, password
```

## Usage

Add VINs to `vins.txt` (one per line), then:

```bash
# first run — use --headed so you can watch and confirm everything works
python lookup.py --headed

# subsequent runs (session is cached in storage_state.json)
python lookup.py

# one-off
python lookup.py --vin WBA12345678901234
```

Results append to `results.csv` with timestamp, VIN, paint code, and any error.

## If something breaks

The login form and VIN search box selectors are best-effort because
partslink24 routes differ by manufacturer subscription. If a step fails:

1. Run with `--headed` and watch where it stops.
2. Open DevTools, inspect the field that should have been filled, copy its
   `name` or a unique attribute.
3. Add it to the relevant locator list in `lookup.py`:
   - login fields: search for `id_field`, `user_field` in `login()`
   - VIN box: search for `vin_box` in `lookup_vin()`
   - paint code extraction: `extract_paint_code()` — add a regex or table
     label in your catalog's language.

Delete `storage_state.json` to force a fresh login.
