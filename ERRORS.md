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

## Brands where partslink24 has the vehicle but no paint code

Different from above: partslink24 *does* carry these brands and
recognises the VIN, but its data doesn't include the manufacturer paint
code — only the colour name. The script captures the name into the
description column where it can, but `paint_code` stays empty.

- **Jaguar** — shows "Exterior Paint - <Name>" (e.g. "Caesium Blue")
- **Ford** — passenger car catalogues only show the colour name
- **Kia** — shows colour name in `Exterior color` field
- **Hyundai** — same as Kia

Common error for these: `paint code not found on result page`. The
dashboard fallback is automatically skipped because the catalog already
found the vehicle — same database, same outcome, no point retrying.

---

## Quick triage

| If you see... | It means... | Fix |
|---|---|---|
| `vehicle data did not load` | Page never showed paint-bearing data within 10s — could be a slow page, a dropped session, or partslink24 having no record of the VIN | Try `--debug` to inspect; if persistent, likely outside coverage |
| `paint code not found on result page` | Vehicle exists in partslink24 but the page has no paint code | Common for older PSA vehicles, Jaguar/Ford/Kia/Hyundai; nothing you can do |
| `could not be assigned to a distinct model` | Catalog couldn't pick a brand for this VIN | Add `category=N1` for vans, or VIN is genuinely outside coverage |
| `unknown make 'X'` | Make in `lookups.txt` doesn't match any in `MAKE_TO_BRAND` | Check spelling; if legitimate, the brand needs adding to the script |
| `no make supplied` | Empty make column in `lookups.txt` | Add the make |
| `login failed: <message>` | Login flow ended up not logged in | The message tells you why — check `env.py`, account status, or retry |
| `VIN box never became editable` | Catalog UI rendered but input stayed disabled within 10s | Often means partslink24 won't accept VIN lookups for this brand (subscription or outage); dashboard fallback runs anyway |
| `VIN box not visible` | Catalog UI didn't render the input at all | Run with `--debug` and check the screenshot |
| `dashboard fallback: SEARCH VIN box ...` | Same family of issues for the dashboard search | As above |
| `timeout: <details>` | Playwright's own browser-level timeout | Usually transient — retry happens automatically |
| `attention page detected but no Reload link...` | partslink24's bookmark-warning interstitial changed format | Script aborts; needs a code update to handle the new layout |
| `attention page still showing after Reload...` | Reload click didn't dismiss the interstitial | Script aborts; investigate manually whether the page format changed |

---

## "partslink24 doesn't have data for this VIN"

These are the most common in real-world batches. The script tries the
brand catalog first; if that fails, it tries the dashboard SEARCH VIN
as a fallback. If both fail, you get a combined error message.

### `vehicle data did not load (timeout); dashboard fallback: vehicle data did not load`

Catalog opened fine but never returned vehicle info. Dashboard fallback
also timed out. partslink24 simply has no record of this VIN.

Common for: Dacia, older imports, very new vehicles, vehicles outside
their geographic coverage. Time cost: ~25 seconds per VIN.

### `vehicle data did not load (timeout); dashboard fallback: could not be assigned to a distinct model`

Same outcome but the dashboard search came back with an explicit "could
not be assigned" message. Common when you forget to provide the category
for a van — the VIN exists somewhere on partslink24 but the catalog you
sent it to didn't recognise it.

**Fix**: provide `category=N1` in `lookups.txt` for vans (Sprinter,
Transit, Crafter, etc.).

### `paint code not found on result page; dashboard fallback: paint code not found`

Vehicle data loaded successfully, but no paint code is on the page.
Common for older PSA vehicles (e.g. pre-2005 Peugeot/Citroën): the
catalog returns a `PAINT TYPE` row but no `BODY COLOUR` row.

Vehicle exists, paint code doesn't. Fast (~2 seconds total) because both
sources returned data, just not the paint code.

**Fix**: nothing — partslink24 genuinely doesn't have this data.

### `vehicle not found` / `no vehicle found` / `no data found` / `kein fahrzeug` / `nicht gefunden` / `invalid vin` / `vin invalid`

Explicit "VIN not found" messages from partslink24. These come back
fast (~1 second) because the catalog says no immediately rather than
silently returning empty data.

### `could not be assigned to a distinct model`

Specifically the dashboard's universal search saying it can't pick a
brand. Usually means the VIN is malformed, foreign-market, or genuinely
not in partslink24's database.

---

## Multi-leg fallback chains (Mercedes, Fiat, Classic-sibling brands)

For Mercedes commercial vehicles (Vans/Trucks), the Fiat/Fiat Professional
pair, and any brand with a Classic sibling (BMW, MINI, Mercedes, Porsche,
VW, BMW Motorrad), a failed lookup may try multiple catalogues before
falling back to the dashboard. The resulting error string concatenates
every leg's outcome separated by `; `.

### `vehicle data did not load (timeout); Mercedes-Benz Classic: vehicle data did not load (timeout); dashboard fallback: vehicle data did not load`

Modern Mercedes catalogue timed out → Classic catalogue also timed out →
dashboard fallback also timed out. partslink24 doesn't have this VIN
under any Mercedes catalogue. Time cost: ~75 seconds (three timeouts).

### `vehicle not found; Mercedes-Benz Trucks: vehicle not found; Mercedes-Benz Classic: vehicle not found; dashboard fallback: could not be assigned to a distinct model`

Mercedes N1 Sprinter routed to Vans → not found → tried Trucks (the
commercial sibling) → not found → tried Classic → not found → dashboard
said couldn't assign. Either the VIN is genuinely outside partslink24's
Mercedes coverage, or VDG returned a wrong make and the vehicle isn't a
Mercedes at all.

### `vehicle not found; Fiat Professional: vehicle not found; dashboard fallback: ...`

Fiat M1 routed to passenger Fiat → not found → tried Fiat Professional
(commercial sibling) → not found → dashboard run. Catches mis-categorised
Doblòs, Pandas, and other Fiat models on the M1/N1 boundary.

### Reading these errors

The legs appear in execution order, separated by `; `. The leg that
matters for the dashboard-skip decision is the **last** catalog leg —
if its error contains "paint code not found", the dashboard is correctly
skipped (the page loaded, just no code); otherwise the dashboard runs.

---

## "Wrong make/category in lookups.txt"

### `unknown make 'X'`

The make in `lookups.txt` doesn't match any entry in `MAKE_TO_BRAND`.
Could be:

- A typo (`Volswagen` instead of `Volkswagen`)
- A brand we don't have a mapping for (Tesla, Lotus, Genesis, etc.)
- VDG returning a make name in a format we don't recognise

**Fix**: check the spelling in `lookups.txt`. If it's a legitimate
brand, the script needs `MAKE_TO_BRAND` updated to include it.

### `no make supplied`

Row in `lookups.txt` has an empty make column. The script doesn't
guess from the VIN — make is required.

**Fix**: add the make.

### `no catalog URL configured for X`

Make resolved to a brand, but that brand isn't in
`BRAND_CATALOG_SERVICE`. Shouldn't happen with the current code (every
brand in `MAKE_TO_BRAND` has a corresponding catalog), but possible if a
brand was added to one map without the other.

---

## Navigation / page loading errors

### `VIN box not visible (<brand> catalog)`

The catalog page opened, but the VIN input field never became visible
within 10 seconds. Usually means the catalog UI is broken or partslink24
is having issues. Run with `--debug` to see the page state.

### `VIN box visible but never became editable (<brand> catalog)`

The catalog rendered the VIN input box but it stayed disabled (greyed
out) for 10 seconds. Common causes:

- **partslink24 is showing a demo/locked catalog for this brand** — no
  subscription on this account, or partslink24 has temporarily disabled
  VIN identification for the brand. When you click into the input
  manually, you may see a tooltip like "We regret to inform you that
  the identification of VINs for this brand will not be available for
  an indefinite period of time".
- **JS init issue** — rare; the catalog's JavaScript didn't finish
  initialising in time.

**Diagnostic**: open the catalog manually in your browser to see what
state it's actually in. The dashboard fallback runs anyway in case the
VIN happens to be in another (subscribed) brand's database.

### `VIN box fill timed out (<brand> catalog)`

The input was visible and editable, but typing into it timed out
mid-fill. Rare; usually means the box was disabled again while we were
typing. Run with `--debug` and check the screenshot.

### `dashboard fallback: SEARCH VIN box not visible` / `... never became editable` / `... fill timed out`

Same family of errors as above but for the dashboard SEARCH VIN box.

### `could not open catalog after re-login`

The script tried to open the brand catalog, found the session expired,
re-logged in, tried again, still couldn't open. Usually means an auth
issue (account locked, password changed) or partslink24 outage.

**Fix**: try `--fresh` to force a clean login. If that doesn't work,
log in via your normal browser to see what's wrong with the account.

### `dashboard fallback: home page load timeout`

The dashboard fallback couldn't load `partslink24.com`. Network issue or
partslink24 down. Retry later.

### `dashboard fallback: SEARCH VIN box not found`

Dashboard loaded but no SEARCH VIN box visible. Either the script isn't
actually logged in (session check failed silently) or partslink24
changed the dashboard layout.

**Diagnostic**: `--debug` and look at the screenshot.

---

## Login errors

### `login failed: <message>`

The login form was submitted but the script ended up not logged in.
Common messages:

- **`Invalid login data`** — wrong company ID, username, or password.
  Check `env.py`.
- **`Your account has been temporarily locked`** — too many failed
  attempts. Wait or contact partslink24 support.
- **`Cookies must be enabled`** — browser config issue. Shouldn't
  happen with our setup, but worth checking if seen.
- **`(no error text on page; url=... title='...')`** — partslink24
  silently failed without a visible error. The URL and title tell you
  where the script ended up; useful for diagnosis.

### `login failed: form never became visible (state='timeout'; ...)`

Neither the squeeze-out prompt nor the password form appeared within
15 seconds. Likely partslink24 outage or a network issue. Try again in
a minute.

---

## Browser/Playwright exceptions

These are wrapped in retry logic — they get one automatic retry before
being recorded. They appear in the CSV as:

### `timeout: <Playwright timeout details>`

Playwright's own internal timeout fired — usually means a page didn't
load within Playwright's allowance (separate from the script's
`wait_for_vehicle_data` timeout). Browser-level network issue.

Often transient; the automatic retry usually fixes it. If you see it
persistently, check your internet connection and that partslink24 is
up.

### `<ExceptionType>: <message>`

Any other unexpected Python exception. Could be `BrowserClosedError`,
`NetworkError`, `JSONDecodeError`, etc.

If you see one of these consistently, the full message will tell you
what went wrong.

---

## The `Outcome` column

`results.csv` has a machine-parseable `Outcome` column alongside the
human-readable `Error` text. Use it for filtering and triage. Values:

| Outcome | Meaning |
|---|---|
| `success` | Paint code was extracted |
| `name_only` | Colour description captured but no code (Jaguar, old LR, etc.) |
| `not_found_as_routed` | All catalogues we tried said "not here". Could be the VIN is genuinely absent from partslink24, OR we routed it to the wrong catalogue (e.g. forgot `category=N1` for a Sprinter). The label asserts something about the *attempt*, not a definitive claim about partslink24's database. |
| `unsupported_brand` | The make isn't in `MAKE_TO_BRAND` at all (Honda, Maserati, Subaru, Tesla, Isuzu, Lotus, Genesis) |
| `paint_data_missing` | Page loaded but the extractors found no code (Ford passenger, Kia, Hyundai pages where partslink24 doesn't carry the code) |
| `page_load_timeout` | Catalog or dashboard never returned vehicle data within the timeout |
| `catalog_ui_error` | VIN input never became visible or editable |
| `auth_error` | Login or session-validation failure |
| `missing_input` | Empty make column in `lookups.txt` |
| `unknown` | Anything not yet categorised — open a ticket if you see this |

### Filtering by outcome

Bulk triage example: in Excel/LibreOffice, filter the `Outcome` column to
quickly group rows:

- `success` → already done
- `name_only` → coloureg can display the colour name; manual lookup is optional
- `unsupported_brand` → route straight to manual queue, no further automated attempt will work
- `not_found_as_routed` → check whether VDG provided category; retry with N1 if it's a van
- `paint_data_missing` → manual queue (data genuinely absent on partslink24)
- `page_load_timeout` → worth a retry; might be transient

---

## Tips

- For any error where you're not sure what happened, run with `--debug`.
  This adds:
  - Visible browser window (so you can watch what's happening)
  - HTML and screenshot dumps in `_debug/<vin>.{html,png}` on failure
  - `_debug/` is wiped at the start of each `--debug` run, so artifacts
    accumulate over a single batch but don't carry between runs
- For login problems specifically, `--fresh` ignores the saved session
  and starts clean — useful when `storage_state.json` is masking a
  problem.
- For batch runs where some VINs fail, the script keeps going and
  records each failure independently in `results.csv` — one bad VIN
  doesn't stop the rest.