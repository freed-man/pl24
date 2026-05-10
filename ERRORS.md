# lookup.py — error reference

Catalogue of every error string the script can write to the `Error`
column of `results.csv`, what each means, and how to fix or triage it.

## Quick triage

| If you see... | It means... | Fix |
|---|---|---|
| `vehicle data did not load` | partslink24 doesn't recognise the VIN | Try with the right `category` (e.g. `N1` for vans), or accept it's not covered |
| `paint code not found on result page` | Vehicle exists in partslink24 but the page has no paint code | Common for older PSA vehicles; nothing you can do |
| `could not be assigned to a distinct model` | Catalog couldn't pick a brand for this VIN | Add `category=N1` for vans, or VIN is genuinely outside coverage |
| `unknown make 'X'` | Make in `lookups.txt` doesn't match any in `MAKE_TO_BRAND` | Check spelling; if legitimate, the brand needs adding to the script |
| `no make supplied` | Empty make column in `lookups.txt` | Add the make |
| `login failed: <message>` | Login flow ended up not logged in | The message tells you why — check `env.py`, account status, or retry |
| `VIN box never became editable` | Catalog UI rendered but input stayed disabled within 12s | Often means partslink24 won't accept VIN lookups for this brand (subscription or outage); dashboard fallback runs anyway |
| `VIN box not visible` | Catalog UI didn't render the input at all | Run with `--debug` and check the screenshot |
| `dashboard fallback: SEARCH VIN box ...` | Same family of issues for the dashboard search | As above |
| `timeout: <details>` | Playwright's own browser-level timeout | Usually transient — retry happens automatically |

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
within 12 seconds. Usually means the catalog UI is broken or partslink24
is having issues. Run with `--debug` to see the page state.

### `VIN box visible but never became editable (<brand> catalog)`

The catalog rendered the VIN input box but it stayed disabled (greyed
out) for 12 seconds. Common causes:

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

## Tips

- For any error where you're not sure what happened, run with `--debug`.
  This adds:
  - Visible browser window (so you can watch what's happening)
  - HTML and screenshot dumps in `_debug/<vin>.{html,png}` on failure
  - 30-second pause at the end so you can inspect the final state
- For login problems specifically, `--fresh` ignores the saved session
  and starts clean — useful when `storage_state.json` is masking a
  problem.
- For batch runs where some VINs fail, the script keeps going and
  records each failure independently in `results.csv` — one bad VIN
  doesn't stop the rest.