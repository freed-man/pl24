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
  route to the right commercial catalogue. A wrong or missing category on a
  van is no longer fatal: `COMMERCIAL_FALLBACK` cross-links every family
  with both a passenger and a commercial catalogue (Mercedes, Fiat, Ford,
  VW, plus Citroën↔DS), so an M1-misclassified van self-heals via
  `catalog:commercial` at the cost of one extra leg. Correct category is
  still preferred — it resolves on the first leg.
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
| `--skip-brand-check` | **No-op**, accepted only for compatibility. The brand-list verification was removed on 2026-08-01: the 2026-07 partslink24 rebuild replaced the home grid's `<a id="<service>_lc">` anchors with a React component exposing titles and logo slugs but no service ids, so there is nothing left to scrape. `service.py` still passes the flag, hence the parameter remains. |
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

Some VINs (notably older multi-variant Mercedes) show a `Please select:`
sales-type picker instead of resolving straight to a vehicle. pl24
auto-clicks the first variant and continues — safe because all variants of a
given VIN share the same paint code (confirmed; the sales-types differ only
by market/parts-catalogue). See the "Model picker" note in `ERRORS.md`.

## Deployed worker (Railway)

Besides the CLI, `lookup.py`'s `Session` class is driven by `service.py`, a
small FastAPI worker deployed on Railway that coloureg calls over the private
network (`GET /lookup-paint?vin=…&make=…&category=…`, gated by an API key;
`/health` is unauthenticated). The endpoint validates the VIN before spending
any browser time: embedded spaces are stripped, then it must be 17 chars of
the ISO 3779 alphabet (no I/O/Q) or the request gets an immediate `400` —
malformed input never reaches partslink24. The interactive docs
(`/docs`, `/redoc`, `/openapi.json`) are disabled so the public URL doesn't
enumerate the API surface; the worker also logs a loud startup WARNING if
`PL24_API_KEY` is unset, since that leaves `/lookup-paint` open. The worker holds **one** logged-in partslink24
session warm for its lifetime and serialises requests through it. The pool is
one thread per slot, each owning its **own** Playwright, browser, session and
account — Playwright's sync API is thread-affine, so a browser created on one
thread cannot be driven from another. More than one slot therefore requires
one partslink24 login PER SLOT (see `PL24_ACCOUNTS`); never run two concurrent
sessions on the same credential, it triggers partslink24's squeeze-out —
confirmed 2026-08-01, where confirming the prompt killed the older session. All of the
session-management logic below lives in `Session` so it applies to any caller;
the CLI doesn't exercise it because each `python lookup.py` run does one
lookup against a fresh session and exits.

### Keeping the long-lived session healthy

A warm partslink24 session expires after a while, and the cheap "am I logged
in?" check can be fooled by a HALF-ALIVE session (the PL24TOKEN cookie can
outlive the server-side session, so we proceed, but the session is too stale
to load a fresh catalogue). `Session.lookup()` handles this with three layers,
in order, so the common case is fast and every case stays correct:

1. **Proactive idle re-login** (fast path). The session tracks the time since
   it last did real work (`last_interaction`, in-memory — no datastore; it
   resets correctly on worker restart). If a request arrives after a longer
   idle gap than the threshold, the session is *probably* stale, so we
   re-login **before** attempting — turning a ~38s fail-then-heal into a ~10s
   clean re-login. Any successful lookup refreshes the clock, so clustered
   lookups stay warm and never trigger a needless re-login.
2. **Cheap per-request check** (`_ensure_logged_in`) — a no-navigation read
   of the `PL24TOKEN` cookie (it was a DOM check until 2026-08-01; the
   rebuilt `/portal-ui` has no "Log out" link to key on) that re-logs-in in
   place if the session is plainly dead.
3. **Self-heal backstop** (the unpredictable case). If a lookup still fails
   with `catalog_ui_error` and no code — the half-alive signature, also
   covering a session killed *within* the idle window by a squeeze-out — we
   force a re-login and retry the lookup once. Note `_force_relogin` still
   consults `is_logged_in` after navigating home, so it only bypasses the
   fooled check if partslink24 clears `PL24TOKEN` for a dead session — see
   the open question in that function. Look for `catalog_ui_error … forcing
   re-login and retrying once` in the worker log. Genuine outcomes
   (`not_found_as_routed`, `name_only`, etc.) are never this signature, so
   they're never retried.

A fourth net wraps the lookup (C1, 2026-08-03): if any leg heals the
session mid-chain (inline re-login on detected expiry) and the lookup
still ends without a paint code, the pre-heal legs are treated as void
and the whole VIN is retried once on the live session. It lives in
`lookup_vin_with_retry`, not in `lookup_vin` — the latter has eight
return statements and a check before any one of them covers only that
one; the wrapper is the single point every exit passes through — because the
one leg that carries the VIN may have been the one that ran dead. This is
the net that catches the SPA-estate half-alive case, where a dead session
gets the catalogue's shell (no VIN box, no password field) instead of a
login redirect, so neither layer 3's signature nor B2's timeout wording
ever matches.

Layer 3 is what keeps layer 1's threshold from mattering much: get it
slightly wrong and a stale lookup is merely *slow* rather than *wrong* —
subject to the `_force_relogin` caveat above, which is unmeasured. The idle
clock is refreshed only on outcomes that prove the session reached
partslink24 (`success`, `name_only`, `not_found_as_routed`,
`unsupported_brand`, `brand_unavailable`, `paint_data_missing`) — a
failed-because-dead lookup deliberately does **not** reset it.

Measured (Jun 2026, against the live worker): a session survives **≥23 min**
idle and each successful lookup refreshes it — it outlives the 600s access
token, which refreshes silently underneath. The proactive threshold default
(900s / 15 min) is therefore conservative; it can be raised once you're
confident in the ceiling.

### Worker environment variables (Railway dashboard)

| Var | Default | Meaning |
|---|---|---|
| `PL24_SESSION_IDLE_S` | `900` | Proactive re-login threshold in seconds (idle since last interaction). `0` disables the proactive check (self-heal still covers staleness). Tune here — no code change/redeploy. |
| `PL24_ACCOUNTS` | unset | JSON list of partslink24 logins, one per warm session — `[{"company_id":"…","username":"…","password":"…"}, …]`. **The pool size is derived from this list**, so it cannot be set independently. Unset = one session on the `PARTSLINK24_*` env credentials (the normal case). Each entry MUST be a distinct user: partslink24 allows one live session per user, so two slots sharing a login would squeeze each other out in a loop. Duplicates are rejected at startup. |
| `PL24_POOL_START_TIMEOUT_S` | `180` | Ceiling on total pool startup. A slot that hangs launching its browser (rather than failing) would otherwise block startup forever with no diagnostic. |
| `PL24_API_KEY` | — | Shared secret coloureg sends as the `X-API-Key` header. Header ONLY — the old `?api_key=` query form was removed because query strings land in access logs. |
| `PL24_REQUEST_TIMEOUT_S` | `120` | Per-request timeout for the worker's queue, **queue wait included**. Raised from 60: the fallback chain can now walk four catalogue legs + dashboard (~100s absolute worst case). On timeout the caller gets a `504`; a job that was still **queued** is then skipped entirely at dequeue (never sent to partslink24), while a job already **in flight** completes in the background and is discarded. coloureg keeps its own shorter client timeout. |
| `PL24_SKIP_BRAND_CHECK` | off | **No-op**, read but ignored (the brand-list verification was removed 2026-08-01). Retained only so an existing Railway variable doesn't need clearing. |
| `PL24_HEADED` | off | Run the worker's browser headed (debugging only; normally headless). |
| `PARTSLINK24_*` | — | partslink24 credentials. |

`service.py` itself needs no per-VIN changes when editing `Session`; the HTTP
plumbing and queue are independent of the session-health logic.

## Upgrading Playwright (lockstep runbook)

_Deliberately self-contained: when an upgrade is due, paste this whole
section into a fresh chat and work through it top to bottom._

**Why this exists.** The Docker base image bakes the browser binaries in and
the service never runs `playwright install`, so the pip `playwright` version
and the image tag MUST match exactly. Proven 2026-07-02: an unpinned `>=1.60`
resolved to the freshly released 1.61.0 against the `v1.60.0-noble` image;
the worker crashed at startup (`BrowserType.launch: Executable doesn't
exist`) and the deploy failed healthcheck. Both files have been exact-pinned
in lockstep since. Upgrades are deliberate, both-files-together events.

**When to upgrade** — whichever comes first:

- partslink24 starts challenging or squeezing sessions more than usual (an
  ageing browser fingerprint scores worse over time), or
- a security advisory lands against the pinned version, or
- Microsoft deprecates the pinned base-image tag, or
- roughly a year has passed since the last bump.

Jump straight to the current version — do not step through intermediates
(bisect only if the jump fails). The cost of an upgrade grows with the gap,
which is why "annually, on a quiet day" beats "eventually, during an
incident".

**Procedure** (test runs are live lookups — one VIN at a time, spaced out,
per the operating constraint):

1. Pick the target `X.Y.Z`. Skim the Playwright Python release notes for
   sync-API changes (rare — the sync API has been stable for years). Confirm
   `mcr.microsoft.com/playwright/python:vX.Y.Z-noble` exists — the `-noble`
   suffix will change with a future Ubuntu LTS, so check the available tags.
2. Local venv first: `pip install playwright==X.Y.Z` then
   `playwright install chromium`. The second step is where driver/browser
   skew bites — never skip it.
3. Run the live test battery with `--dump`, spaced out. Each VIN exercises a
   mechanism that a new browser engine's timing/rendering could disturb:
   - `WDB2010242F790734` (Mercedes 190E) → `441 / catalog` — the "Please
     select" model-picker auto-handler
   - `TSMNZC72S00618058` (Suzuki Swift) → `ZCF` — SPA catalogue timing and
     the silent-timeout retry path
   - `W0L0AHL3565157973` (2006 Vauxhall Astra) → `4CU / catalog:legacy` —
     the legacy-sibling fallback
   - `WV2ZZZSK7TX044364` (VW Caddy), NO `--category` → `M7P /
     catalog:commercial` — the full commercial-fallback chain; then WITH
     `--category N1` → `M7P / catalog` (direct route)
   - `TRUZZZ8J6B1011103` (Audi TT) → `X5Q` — VW-group compound
     "Exterior color / Paint Code" extraction
   - `WVWZZZAUZFW002714` (passenger VW) → `A7N / catalog` — plain first-leg
     regression
   Any regression → roll local back (`pip install playwright==<old>` +
   `playwright install chromium`), stop, and investigate with the dumps
   before going further.
4. All green → edit BOTH files in the same commit, and update the version
   numbers inside their lockstep comments too:
   - `requirements.txt`: `playwright==X.Y.Z`
   - `Dockerfile`: `FROM mcr.microsoft.com/playwright/python:vX.Y.Z-noble`
5. Commit `.`, push. Watch the Railway build log for
   `Collecting playwright==X.Y.Z` and a green healthcheck.
6. Verify deployed: Railway console `pip show playwright` → `X.Y.Z`, then
   one real lookup through the worker.
7. Rollback if production misbehaves: revert the commit. The exact pin makes
   the previous state byte-identical — that is what it is for.
8. Update the handoff doc's Packages paragraph with the new version.

The base-image bump also brings a newer Python and OS packages along for
free; nothing in pl24 depends on a specific Python minor version.

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