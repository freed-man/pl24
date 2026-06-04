# lookup.py — error reference

Catalogue of every error string the script can write to the `Error`
column of `results.csv`, what each means, and how to fix or triage it.

## Brands not carried by partslink24

The following brands appear in vehicle data sources (DVLA, VDG, etc.)
but are **not advertised on partslink24's brand-selection page**, so the
script has no catalogue to route to. They will always fall through to
the dashboard, which also won't find them. Don't bother retrying —
these need different data sources entirely (physical colour label,
paint factor database, manufacturer dealer system, etc.).

- **Honda**
- **Maserati**
- **Subaru**
- **Tesla**
- **Isuzu**
- **Lotus**
- **Genesis**

For these you'll see `unknown make 'X'; dashboard fallback: could not
be assigned to a distinct model`. That's the script behaving correctly,
not a bug.

> The partslink24 brand grid (~52 brands) is the authoritative coverage
> list. Confirmed present (and tested): all the carried brands below, plus
> **Suzuki** (an earlier note wrongly called it unsupported — it is carried
> and works). Carried but with no VINs in the test set: Alpine, BMW
> Motorrad, the Classic sub-brands, Unimog.

## Brands where partslink24 carries the vehicle but no paint *code*

partslink24 recognises the VIN and loads the page, but the data has no
manufacturer paint *code* — only a colour name (or, for some, nothing).
Behaviour by brand (updated after the 2026-06 extraction work):

- **Ford** (passenger **and** Transit / Ford Pro) — `Exterior Paint\n<name>`,
  e.g. "Frozen White", "Shadow Black (Mica)". Name captured (finish kept);
  no code exists → `name_only`. (Ford uses the label "Exterior Paint", not
  "Paint Exterior Body Colour".)
- **Jaguar / Land Rover** — name-only on some records ("Caesium Blue",
  "Fuji White"); others carry a `JBC####` code and resolve to `success`.
- **IVECO, MAN** — heavy-commercial catalogues carry **no paint field at
  all** (mechanical data only). Always `paint_data_missing`; nothing to
  extract. Not a bug.
- **Old Alfa 916** (`ZAR91600006066429`) — a paint-data *absence*, not an
  unknown format. `paint_data_missing`.

Common error for the name-only cases: `paint code not found on result
page`. The dashboard/siblings are skipped because the catalog already
found the vehicle — same database, same outcome, no point retrying.

### Brands that now DO yield a colour name (recovered in 2026-06)

These previously came back blank; dedicated extractors now capture the
name (and code where present):

- **BMW / MINI** — `Color\n<NAME> (<CODE>)`; some MINIs use
  `<NAME> (METALLIC) (<CODE>)` (two parens — the code is the *second*).
  Finish kept: "Moonwalk Grey (Metallic)".
- **Mercedes (passenger)** — `Paint Code\n<3-digit> (<name> - <finish>)`
  → "Cosmos Black", "Manufaktur South Seas Blue".
- **Mercedes (vans)** — `Paint Code\n<4-digit> (<name> paint MB <code>)`
  → "Arctic White", "Amber Red Metallic".
- **smart** — two-part body: `<frame> (…tridion…) <body> (…)`. We return
  the **body** colour, not the tridion frame (often marked "Invalid").
- **Volvo / Polestar** — joined `498 Caspian Blue` OR separated
  `…color\n62600\n…color\nCLOUD BLUE`. Both handled → "Cloud Blue", "Void".
- **Hyundai / Kia** — colour NAME lives in the `Exterior color` field
  (where Nissan puts a code); optional `[CODE]` suffix. → "Champion Blue",
  "Creamy White [TCW]". Messy Kia values stored verbatim.
- **PSA (Peugeot/Citroën/DS)** — `BODY COLOUR` normaliser, 3 formats →
  "Platinum Grey", two-tone "Whisper + Black Onyx".

### Brands that are code-only by design (blank description is correct)

VW group never carries colour names — only the `<roof> / <body>` paint
codes: **Audi, Cupra, SEAT, Škoda, Bentley, Porsche, VW (passenger +
commercial)**. Likewise the Nissan family — **Nissan, Infiniti, Lexus,
Toyota, Suzuki** — gives a bare `Exterior color\t<CODE>`. Also
**Opel/Vauxhall** (`Color Option\t<CODE>`), **Mitsubishi**, and the
**Fiat-family English `EXTERNAL COLOR (code)`** variant (the Italian
`COLORE ESTERNO (name)` variant does give a name, in market language).

---

## Quick triage

| If you see... | It means... | Fix |
|---|---|---|
| `vehicle data did not load` | Page never showed paint-bearing data within 10s — slow page, dropped session, or no record of the VIN | Try `--debug`; **often a transient timeout — re-run** (see Transient timeouts below) |
| `paint code not found on result page` | Vehicle exists in partslink24 but the page has no paint code | Common for Jaguar/Ford/Kia/Hyundai and older PSA; nothing you can do |
| `brand VIN identification unavailable (indefinite)` | partslink24 has VIN-ID switched OFF for the whole brand | Retryable — works the instant they restore it. Currently affects **Renault + Dacia** |
| `could not be assigned to a distinct model` | Dashboard couldn't pick a brand for this VIN | Add `category=N1` for vans, or VIN is genuinely outside coverage |
| `unknown make 'X'` | Make in `lookups.txt` not in `MAKE_TO_BRAND` | Check spelling; if legitimate, add the brand |
| `no make supplied` | Empty make column | Add the make |
| `login failed: <message>` | Login flow ended up not logged in | The message tells you why — check `env.py`, account status, or retry |
| `VIN box never became editable` | Input stayed disabled within 10s | Often brand VIN-ID disabled (now usually surfaces as `brand_unavailable`); dashboard fallback runs anyway |
| `VIN box not visible` | Catalog UI didn't render the input | Run `--debug`, check the screenshot |
| `timeout: <details>` | Playwright browser-level timeout | Usually transient — retry happens automatically |
| `attention page detected but no Reload link...` | partslink24's bookmark interstitial changed | Script aborts; needs a code update |

---

## "partslink24 doesn't have data for this VIN"

The script tries the brand catalog first; if that fails it tries
sibling catalogues and finally the dashboard SEARCH VIN. Combined errors
read left-to-right in execution order, separated by `; `.

### `vehicle data did not load (timeout); dashboard fallback: vehicle data did not load`

Catalog opened but never returned vehicle info; dashboard also timed out.
**Often a transient timeout, not a real absence** — re-run before trusting
it (see Transient timeouts). If it persists, partslink24 has no record.

### `vehicle data did not load (timeout); dashboard fallback: could not be assigned to a distinct model`

The dashboard came back with an explicit "could not be assigned". Common
when you forget `category=N1` for a van. **Fix**: add `category=N1`.

### `paint code not found on result page; dashboard fallback: paint code not found`

Vehicle data loaded, no paint code on the page. Common for Jaguar/Ford/
Kia/Hyundai (now usually `name_only` once the name is captured) and older
PSA. Fast (~2s). **Fix**: nothing — partslink24 genuinely lacks the code.

### `no data was found` (Tip) / `vehicle not found` / `kein fahrzeug` / `invalid vin`

Explicit "VIN not found". Comes back fast (~1s). The "No data was found"
Tip is partslink24's authoritative not-in-DB signal and appears on both
the old popup UI and the newer React/SPA. **European-market coverage:**
US-built VINs are absent even for carried brands (`1FA…` Ford, `1C4…` US
Jeep), and some brands exclude pre-2006 (Jeep states this explicitly).

### `could not be assigned to a distinct model`

The dashboard's universal search can't pick a brand. Malformed,
foreign-market, or genuinely not in partslink24.

---

## Brand-unavailable (partslink24 switched VIN-ID off)

### `brand VIN identification unavailable (indefinite)`

partslink24 has disabled VIN identification for the **entire brand** (not
just this VIN). The catalogue shows a message: *"the identification of
VINs for this brand will not be available for an indefinite period of
time."* On the newer SPA the page may just hang on a loading spinner — the
message is still in the frame HTML, which `BRAND_UNAVAILABLE_RE` matches
(including the "will not\nbe available" newline split).

- Outcome: **`brand_unavailable`** — distinct and **retryable**. The script
  re-checks every run (no skip-list) and works the instant partslink24
  restores the brand. Fast-fails (~1s); dashboard is skipped (it routes
  into the same disabled catalogue).
- Currently affects the **Renault group: Renault and Dacia.** Renault's
  paint format therefore **cannot be verified** until partslink24 restores
  it.

---

## Multi-leg fallback chains (Mercedes, Fiat, Ford, Classic-sibling brands)

Commercial vehicles (MB Vans↔Trucks, Fiat↔Fiat Professional, Ford↔Ford
Pro) and Classic-sibling brands (BMW, MINI, Mercedes, Porsche, VW, BMW
Motorrad) may try multiple catalogues before the dashboard. The error
string concatenates every leg separated by `; `, and the **`Via`** column
now records which leg won: `catalog`, `catalog:commercial`,
`catalog:classic`, or `dashboard`.

### `vehicle data did not load (timeout); Mercedes-Benz Trucks: ... ; Mercedes-Benz Classic: ... ; dashboard fallback: ...`

The full Mercedes commercial chain: Vans → Trucks (commercial sibling) →
Classic → dashboard. Confirmed working: `WDF63960123202629` timed out on
all three catalog legs and the dashboard recovered `3548 / Amber Red
Metallic`. (Up to ~four legs; mostly transient timeouts — see below.)

### `vehicle not found; Fiat Professional: vehicle not found; dashboard fallback: ...`

Fiat M1 → passenger Fiat → Fiat Professional (commercial sibling) →
dashboard. Catches mis-categorised Doblòs etc. on the M1/N1 boundary.

### Skip rules (so legs aren't wasted)

- On **"paint code not found"** the vehicle WAS identified in the modern
  catalogue, so it's definitively not a Classic/commercial/dashboard case:
  all three are **skipped** (the description survives the skip →
  `name_only`). This avoids ~10s timeouts on modern MINIs/BMWs etc.
- On **"brand unavailable"** the commercial sibling and dashboard are
  skipped (same disabled catalogue).

### `JMAL`-prefix "Lancia" = Mitsubishi rebadges

VINs like `JMALMCX4A9U000204` are Mitsubishi-built Lancia rebadges. The
Lancia catalogue misses them; the **dashboard recovers them** via the real
manufacturer (`via=dashboard`). Confirms the dashboard's re-badge purpose;
the VIN legitimately appears under both Lancia and Mitsubishi.

---

## Transient timeouts (important reliability note)

Catalogue pages frequently time out at the 10s limit **even though the
data is present** — proven by VINs that succeed on a re-run or via the
dashboard with the *same* code. Seen across Fiat, Jaguar, MINI, Mercedes,
Smart, Porsche, BMW, Škoda. Example: `WP0ZZZ98ZAU770664` returned `M7X`
on some runs and `page_load_timeout` on others — decided purely by load
timing.

- A `page_load_timeout` is a **clean return, not an exception**, so the
  `EXTRA_RETRIES=1` wrapper does **not** retry it (it only retries thrown
  exceptions). This is the top open dev item — see `PL24_HANDOFF.md`.
- Recovery always came from a **fresh attempt** (re-run / dashboard), never
  from waiting longer. The dashboard currently acts as an accidental retry,
  which is why most timeouts still recover.
- **Triage:** treat a lone `page_load_timeout` or a `... timeout; dashboard
  fallback: ... timeout` as "re-run it" before believing the VIN is absent.

---

## "Wrong make/category in lookups.txt"

### `unknown make 'X'`
Make doesn't match any `MAKE_TO_BRAND` entry: a typo, an unmapped brand
(Tesla, Lotus, Genesis…), or a VDG format we don't recognise. **Fix**:
check spelling; if legitimate, add to `MAKE_TO_BRAND`.

### `no make supplied`
Empty make column. Make is required — the script doesn't guess from VIN.

### `no catalog URL configured for X`
Make resolved to a brand not in `BRAND_CATALOG_SERVICE`. Shouldn't happen
with current code; possible if a brand was added to one map but not both.

---

## Navigation / page loading errors

### `VIN box not visible (<brand> catalog)`
Catalog opened but the VIN input never became visible within 10s. Catalog
UI broken or partslink24 having issues. `--debug` to inspect.

### `VIN box visible but never became editable (<brand> catalog)`
Input rendered but stayed disabled for 10s. Causes:
- **Brand VIN-ID disabled** — now usually surfaces distinctly as
  `brand_unavailable` (see above), but an older/edge layout may still land
  here. Manually opening the catalog shows the "indefinite period" tooltip.
- **JS init issue** — rare.

The dashboard fallback runs anyway in case the VIN is in another brand's DB.

### `VIN box fill timed out (<brand> catalog)`
Visible and editable but typing timed out mid-fill. Rare. `--debug`.

### `dashboard fallback: SEARCH VIN box not visible` / `... never became editable` / `... fill timed out`
Same family for the dashboard SEARCH VIN box.

### `could not open catalog after re-login`
Opened catalog → session expired → re-logged in → still couldn't open.
Usually auth (locked/changed password) or outage. **Fix**: `--fresh`.

### `dashboard fallback: home page load timeout`
Couldn't load `partslink24.com`. Network or partslink24 down. Retry later.

### `dashboard fallback: SEARCH VIN box not found`
Dashboard loaded but no SEARCH VIN box — either not actually logged in or
layout changed. `--debug`.

---

## Login errors

### `login failed: <message>`
Form submitted but ended up not logged in. Common messages:
- **`Invalid login data`** — wrong company ID/username/password. Check `env.py`.
- **`Your account has been temporarily locked`** — too many attempts. Wait/contact support.
- **`Cookies must be enabled`** — browser config; shouldn't happen with our setup.
- **`(no error text on page; url=... title='...')`** — silent failure; URL/title aid diagnosis.

### `login failed: form never became visible (state='timeout'; ...)`
Neither squeeze-out prompt nor password form appeared within 15s. Likely
outage or network. Retry.

---

## Browser/Playwright exceptions

Wrapped in retry logic — one automatic retry before being recorded.

### `timeout: <Playwright timeout details>`
Playwright's own internal timeout (separate from `wait_for_vehicle_data`).
Browser-level network issue; usually transient, auto-retry usually fixes it.

### `<ExceptionType>: <message>`
Any other unexpected Python exception (`BrowserClosedError`, etc.). The
full message tells you what went wrong.

---

## The `Outcome` column

`results.csv` has a machine-parseable `Outcome` column alongside the
human-readable `Error`. Values:

| Outcome | Meaning |
|---|---|
| `success` | Paint code was extracted (and often a name) |
| `name_only` | Colour name captured but no code (Ford, Jaguar, some LR, Hyundai/Kia, etc.) |
| `not_found_as_routed` | All catalogues tried said "not here". Could be genuinely absent OR mis-routed (e.g. forgot `category=N1`). Asserts about the *attempt*, not partslink24's DB. |
| `brand_unavailable` | partslink24 has VIN-ID switched OFF for the whole brand (Renault, Dacia). **Retryable.** |
| `unsupported_brand` | Make not in `MAKE_TO_BRAND` (Honda, Maserati, Subaru, Tesla, Isuzu, Lotus, Genesis) |
| `paint_data_missing` | Page loaded but no code AND no name (IVECO, MAN, old Alfa, empty-cell Ford) |
| `page_load_timeout` | Catalog/dashboard never returned data within the timeout — **often transient, re-run** |
| `catalog_ui_error` | VIN input never became visible or editable |
| `auth_error` | Login or session-validation failure |
| `missing_input` | Empty make column |
| `unknown` | Anything not yet categorised — investigate if seen |

### The `Via` column

Records which leg resolved a success: `catalog` (primary),
`catalog:commercial` (MB Vans↔Trucks, Fiat↔Pro, Ford↔Pro),
`catalog:classic` (Classic sibling), or `dashboard`. Useful for spotting
Classic-resolved cars and the transient-timeout pattern (dashboard
recoveries) in the data.

### Filtering by outcome

- `success` → done
- `name_only` → coloureg can show the colour name; manual code lookup optional
- `unsupported_brand` → manual queue, no automated attempt will work
- `brand_unavailable` → leave queued; retry later (partslink24-side)
- `not_found_as_routed` → check category; retry with N1 if it's a van
- `paint_data_missing` → manual queue (data genuinely absent)
- `page_load_timeout` → **re-run; usually transient**

---

## Tips

- For any uncertain error, run with `--debug` (visible browser + HTML/PNG
  dumps in `_debug/<vin>.{html,png}` on failure; `_debug/` wiped at the
  start of each run). Use `--dump-always` to dump on *every* result,
  including successes (implies headed).
- For login problems, `--fresh` ignores the saved session and starts clean.
- Batch runs keep going on failure — one bad VIN doesn't stop the rest.
  But run **one VIN at a time, spaced out** in normal use (see NOTES.md /
  the operating constraint); bulk runs risk partslink24 flagging access.
