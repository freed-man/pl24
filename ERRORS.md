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
| `login failed: login component never rendered` | partslink24's React login component (`<pl24-login-ui>`) did not mount, or its `data-test-id`s changed | See `_debug/login_failed.*`; needs a selector update in `_complete_login_from_current_page` |
| `session squeeze-out prompt appeared but the Confirm button was not clickable` | The squeeze-out prompt's markup changed | See `_debug/squeeze_prompt.*` (exempt from the stale-dump wipe, so it survives) |

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

The catalog leg timed out AND the dashboard came back "could not be
assigned". Two sub-cases, and the script now distinguishes them:

- **Genuine miss** — usually a missing `category=N1` for a van, or a VIN
  outside coverage. **Fix**: add `category=N1`; if it persists, the VIN
  isn't carried.
- **Transient false-not-found** — both legs can fail transiently on a
  struggling session, mislabelling a *present* VIN as not-found. Because
  this exact combination (catalog **timeout** + dashboard
  **could-not-assign**) is a known transient pattern, the script now flags
  it `retryable_transient` and grants **one automatic whole-VIN retry**
  (see Transient timeouts). You'll see a `retrying <VIN> — transient
  not-found ...` log line. On the retry the VIN usually either loads
  (recovering it) or returns a real not-found, so the auto-retry only ever
  recovers a false negative or leaves the outcome unchanged — never worse.
  Only the literal "could not be assigned to a distinct model" triggers
  this; the definitive `error while loading vehicle` toast does **not**.

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

### Model picker (`Please select:`) — auto-handled, older Mercedes etc.

Some VINs don't resolve to a single vehicle: partslink24 shows a
`Please select:` dropdown of sales-type variants (different markets, e.g.
`Valid for: AU` / `JP` / `CA, US` or unmarked) and waits for a pick before
loading the vehicle page with the paint code. `wait_for_vehicle_data` detects
this picker during its normal poll and auto-clicks the first sales-type, then
keeps waiting for the vehicle page (handled in `_handle_model_picker`).

**Why auto-picking is safe:** confirmed on `WDB2010242F790734` (a UK 1991
190 E) — clicking *every* variant resolves to the **same** Paint Code (`441`);
the sales-types differ only in parts catalogue/market, not paint. So picking
the first cannot produce a wrong colour. If a future VIN is ever found where
variants carry *different* paint codes, this assumption needs revisiting, but
the observed Mercedes behaviour is shared paint across sales-types.

Before this was handled, such VINs timed out on every leg (catalog → silent
retry → Classic sibling → dashboard) and then the B2 transient-retry ran the
whole chain again — ~2 min ending in a false `not_found_as_routed`. Now the
picker is clicked in ~1s and the first leg succeeds (`441`, `via=catalog`),
so the slow chain never runs. Look for `model picker ('Please select')
detected -> picking first sales-type` in the log.

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

## Multi-leg fallback chains (Mercedes, Fiat, Ford, VW, Citroën, Classic-sibling brands, Opel/Vauxhall legacy)

Every family with both a passenger and a commercial catalogue is
cross-linked in `COMMERCIAL_FALLBACK` (MB base→Vans plus Vans↔Trucks,
Fiat↔Fiat Professional, Ford↔Ford Commercial, VW↔VW Commercial Vehicles,
and Citroën↔DS — the last a brand split, not M1/N1, but the same failure
shape). Note: earlier revisions of this doc listed Ford as wired when the
map didn't actually contain it — fixed alongside the VW Caddy case below.
Classic-sibling brands (BMW, MINI, Mercedes, Porsche, VW, BMW
Motorrad), and Opel/Vauxhall (live PSA catalogue ↔ legacy catalogue) may try
multiple catalogues before the dashboard. The error string concatenates every
leg separated by `; `, and the **`Via`** column now records which leg won:
`catalog`, `catalog:commercial`, `catalog:classic`, `catalog:legacy`, or
`dashboard`. Full walk order: routed catalogue → commercial sibling → Classic
sibling → Legacy sibling → dashboard.

### `no results for the specified search; Vauxhall legacy: ... ` → `catalog:legacy`

Opel/Vauxhall split: partslink24 moved the LIVE Opel/Vauxhall catalogue under
PSA (`psa_opel_parts` / `psa_vauxhall_parts`) and kept the OLD catalogue
(`opel_parts` / `vauxhall_parts`) as a "legacy" catalogue for pre-PSA-era
vehicles. The live PSA catalogue returns "no results" for older cars; we then
fall back to the legacy catalogue before the dashboard. Confirmed working:
`W0L0AHL3565157973` (2006 Vauxhall Astra) returns "no results" on
`psa_vauxhall_parts` and resolves to `4CU` (Color Option) on Vauxhall legacy
→ `via=catalog:legacy`. Same skip rule as Classic: not tried on "paint code
not found" (the PSA catalogue positively identified the car, so legacy can't
add a code partslink24 doesn't have). Routing lives in `LEGACY_SIBLING` /
`LEGACY_CATALOG_SERVICE`; the legacy names stay in `BRANDS_KNOWN_UNROUTED` so
the brand-list verify used to skip them (that verification was removed on
2026-08-01 — see NOTES.md).

### `vehicle data did not load (timeout); Mercedes-Benz Trucks: ... ; Mercedes-Benz Classic: ... ; dashboard fallback: ...`

The full Mercedes commercial chain: Vans → Trucks (commercial sibling) →
Classic → dashboard. Confirmed working: `WDF63960123202629` timed out on
all three catalog legs and the dashboard recovered `3548 / Amber Red
Metallic`. (Up to ~four legs; mostly transient timeouts — see below.)

### `vehicle not found; Fiat Professional: vehicle not found; dashboard fallback: ...`

Fiat M1 → passenger Fiat → Fiat Professional (commercial sibling) →
dashboard. Catches mis-categorised Doblòs etc. on the M1/N1 boundary.

### VW category misclassification → commercial sibling (the Caddy case)

The upstream provider's M1/N1 category can simply be wrong. Confirmed:
a 2026 Caddy Maxi (`WV2ZZZSK7TX044364`, reg RK26LTJ) came through as
**M1** → routed to passenger VW → "VIN could not be assigned" → and the
chain walked Classic → dashboard → fail, because no VW entry existed in
`COMMERCIAL_FALLBACK`. The commercial catalogue resolves the same VIN in
~2s (`M7P`, proven with `--category N1`). With VW now cross-linked, the
misrouted lookup self-heals as `via=catalog:commercial`. Cost of the
wider map: a genuinely-unfindable vehicle in these families pays one
extra ~2–10s leg before Classic/dashboard; the "paint code not found"
skip rule still applies, so positively-identified vehicles never pay it.

### Skip rules (so legs aren't wasted)

- On **"paint code not found"** the vehicle WAS identified in the modern
  catalogue, so it's definitively not a Classic/commercial/legacy/dashboard
  case: all are **skipped** (the description survives the skip →
  `name_only`). This avoids ~10s timeouts on modern MINIs/BMWs etc.
- On **"brand unavailable"** the commercial sibling and dashboard are
  skipped (same disabled catalogue).

### Cross-branding: the dashboard routes a VIN to its real manufacturer

When a VIN's badge (from DVLA/VDG) differs from its actual builder, the
catalog leg for the badged brand returns "no data", and the **dashboard
re-routes to the true manufacturer's catalogue**. Two confirmed cases:

- **`JMAL`-prefix "Lancia" = Mitsubishi rebadges** (e.g. `JMALMCX4A9U000204`).
  Lancia catalogue misses them; the dashboard recovers them via Mitsubishi
  (`via=dashboard`). The VIN legitimately appears under both.
- **`MCA…JFA`-prefix "Jeep" = Fiat-built** (e.g. `MCANJPCH7JFA19302`). The
  `MCA` WMI is Italian Stellantis, not a real Jeep WMI (`1C4`/`ZAC`). The
  Jeep catalog shows "No data was found"; the dashboard correctly cross-brands
  to **Fiat** (Fiat logo + Fiat model list). NB: in this observed case Fiat
  *also* lacked the specific VIN, so it still ended not-found — but the
  cross-branding itself (Jeep → Fiat) is the dashboard working as designed,
  not a routing bug.

So a dashboard leg landing in a *different* brand than the one routed is
expected behaviour, not an error.

### Dashboard "Error while loading vehicle" toast = definitive not-found

On the React/SPA dashboard, a red MUI snackbar reading exactly **"Error
while loading vehicle"** appears when the universal search can't load a
VIN. Observed only on genuinely-uncarried VINs (US-built `1C4…` Jeeps, a
Fiat, and the `MCANJPCH7JFA19302` Jeep→Fiat case above); VINs that ARE
carried load a full vehicle-data page instead. So it is a **definitive
not-found**, not a transient load error. It is included in
`VIN_NOT_FOUND_PHRASES` so the dashboard leg fast-fails (~300ms) instead of
eating the full 10s wait → outcome `not_found_as_routed`. Caveat: the
snackbar auto-dismisses after a few seconds, so detection is best-effort
within the poll window; a missed toast simply reverts to the slower 10s
timeout path (same outcome, just slower).

---

## Transient timeouts (important reliability note)

Catalogue pages frequently time out at the 10s limit **even though the
data is present** — proven by VINs that succeed on a re-run or via the
dashboard with the *same* code. Seen across Fiat, Jaguar, MINI, Mercedes,
Smart, Porsche, BMW, Škoda. Example: `WP0ZZZ98ZAU770664` returned `M7X`
on some runs and `page_load_timeout` on others — decided purely by load
timing.

Two layers of automatic recovery now handle this:

- **Silent-timeout retry (per leg).** When a leg's wait returns a clean
  silent timeout (no data, no not-found text, no brand-unavailable
  notice), the script re-submits the VIN once on the same page before
  giving up. You'll see `silent timeout — re-submitting <VIN> (...)`.
  Recovery always came from a **fresh attempt**, never from waiting
  longer, so it re-submits rather than lengthening the 10s window. This is
  per-leg and capped — it does not multiply across the
  catalog/sibling/dashboard chain.
- **Whole-VIN retry (B2).** The specific combination "catalog **timeout**
  + dashboard **could-not-assign**" is a known transient false-not-found.
  It's flagged `retryable_transient` and gets **one** whole-VIN retry via
  the `EXTRA_RETRIES=1` wrapper (which otherwise only retries thrown
  exceptions). Logged as `retrying <VIN> — transient not-found ...`. The
  retry can only recover a false negative or leave the outcome unchanged.

- **Triage:** with both layers in place, most transient timeouts now
  recover automatically. A `page_load_timeout` or `... timeout; dashboard
  fallback: ... timeout` that *survives* the automatic retries is worth one
  manual re-run before believing the VIN is absent — but this is now the
  exception, not the rule.

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


---

## Login errors

### `login failed: <message>`
Form submitted but ended up not logged in. Common messages:
- **`Invalid login data`** — wrong company ID/username/password. Check `env.py`.
- **`Your account has been temporarily locked`** — too many attempts. Wait/contact support.
- **`Cookies must be enabled`** — browser config; shouldn't happen with our setup.
- **`(no error text on page; url=... title='...')`** — silent failure; URL/title aid diagnosis.

> Historical: `login failed: form never became visible (state='timeout')`
> and `dashboard fallback: SEARCH VIN box not found` were produced by
> `_wait_for_squeeze_or_form` / an older dashboard probe, both removed
> 2026-08-01 with the login rebuild. They can no longer occur; rows in old
> results.csv files carrying them predate the rebuild.

---

## Service (worker) HTTP rejections — no `results.csv` row

These come from `service.py` before or instead of a lookup, so they appear
in coloureg's client log and the worker log, never in `results.csv`:

- **`400 malformed VIN`** — the VIN failed strict validation (17 chars,
  ISO 3779 alphabet, spaces stripped first). Rejected in ~0ms; partslink24
  is never touched. Fix the caller's input.
- **`401 unauthorized`** — missing/wrong `X-API-Key` (compared
  constant-time, as bytes, so hostile header bytes still get a clean 401).
- **`503 service not ready`** — the pool hasn't finished starting.
- **`504 service timeout after Ns`** — the request exceeded
  `PL24_REQUEST_TIMEOUT_S` including queue wait. If the job was still
  queued it is skipped at dequeue (`skipping abandoned job <VIN>` in the
  worker log) and partslink24 is never engaged for it; if it was already
  in flight it finishes in the background and is discarded.
- **`502 <ExceptionType>: …`** — the lookup threw even after the slot's
  rebuild-and-retry; the message names the exception.

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
| `catalog_ui_error` | VIN input never became visible or editable. On the **deployed worker** this is also the half-alive-stale-session signature: the worker forces a re-login and retries once (see NOTES.md "Deployed worker"). On the CLI it's reported as-is. |
| `auth_error` | Login or session-validation failure |
| `missing_input` | Empty make column |
| `unknown` | Anything not yet categorised — investigate if seen |

### The `Via` column

Records which leg resolved a success: `catalog` (primary),
`catalog:commercial` (MB base→Vans + Vans↔Trucks, Fiat↔Pro,
Ford↔Commercial, VW↔Commercial, Citroën↔DS),
`catalog:classic` (Classic sibling), `catalog:legacy`, or `dashboard`. Useful for spotting
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

- For any uncertain error, run with `--debug` (dumps HTML/PNG to
  `_debug/<vin>.{html,png}` on failure; `_debug/` wiped at the start of each
  run). Use `--dump` to dump on *every* result, including successes. Both run
  **headless by default** — add `--headed` to watch the browser live (e.g.
  `--debug --headed`). Neither flag implies a window any more.
- For login problems, `--fresh` ignores the saved session and starts clean.
- Batch runs keep going on failure — one bad VIN doesn't stop the rest.
  But run **one VIN at a time, spaced out** in normal use (see NOTES.md /
  the operating constraint); bulk runs risk partslink24 flagging access.