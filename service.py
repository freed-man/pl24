"""
pl24 paint-lookup HTTP service.

Wraps the pl24 scraper (lookup.py) as an always-running FastAPI service so
coloureg can call it over Railway's private network when a VDG bundle call
returns no paint code.

    GET /lookup-paint?vin=...&make=...&category=...&year=...
        -> {"vin", "paint_code", "paint_description", "via", "outcome",
            "error", "elapsed_s"}
    GET /health
        -> {"status": "ok", "sessions_alive": N, "pool_size": N}

DESIGN (speed-first):
  * WARM sessions. A pool of logged-in browser Sessions is created at startup
    and kept alive, so the ~9s partslink24 login is paid once per session, not
    per request. A request just runs the ~2-5s catalog scrape on an already-
    logged-in page. Session validity is checked cheaply per request
    (is_logged_in — a PL24TOKEN cookie read, no navigation) and re-login
    happens lazily ONLY when a session has actually expired — no background
    pinging (which would be both a bot-detection signal and unreliable
    against absolute server-side timeouts).

  * ONE THREAD, ONE PLAYWRIGHT, ONE ACCOUNT PER SLOT. Playwright's sync API
    can't run inside FastAPI's async event loop, and we don't want to rewrite
    the 3000-line scraper as async — so all browser work happens on worker
    threads and async handlers submit jobs to a queue and await the result.
    Crucially, each slot runs its OWN sync_playwright(): the sync API is
    thread-affine (SyncBase._sync() captures greenlet.getcurrent(), and
    greenlets are thread-local), so a browser created on one thread cannot be
    driven from another — it RAISES. An earlier design started a single
    Playwright on one pool thread and dispatched jobs to sub-threads that
    borrowed sessions; that could never have worked above one slot. See
    PoolWorker._session_thread.

  * POOL SIZE IS DERIVED, NOT SET. POOL_SIZE == len(ACCOUNTS), because
    partslink24 permits one live session per USER: two slots sharing a login
    would take turns squeezing each other out, each eviction surfacing as
    catalog_ui_error and triggering a forced re-login that evicts the sibling
    again — slower and less reliable than a single session. So scaling means
    buying additional partslink24 users and listing them in PL24_ACCOUNTS;
    there is deliberately no independent size knob, and duplicate users are
    rejected at startup. Unset PL24_ACCOUNTS = one slot on the PARTSLINK24_*
    env creds. Note each slot is a real Chromium at roughly 150-300MB.

  * FIFO. All slots serve one shared jobs queue, so ordering is the queue's
    and does not depend on which waiter a Condition happens to wake.

  * CRASH RECOVERY. If a lookup throws because the browser/context died, the
    slot rebuilds its session (Session.start()) and retries the job once.

Config via env (all optional except credentials, which lookup.py requires):
    PL24_ACCOUNTS         JSON list of partslink24 logins, one per warm
                          session; POOL SIZE IS DERIVED FROM ITS LENGTH.
                          Unset = one session on the PARTSLINK24_* env creds.
    PL24_POOL_START_TIMEOUT_S  ceiling on total pool startup (default 180)
    PL24_SKIP_BRAND_CHECK no-op; the brand-list verify was removed 2026-08-01
    PL24_HEADED           "1" to run headed (local debugging only; never in
                          the container)
"""

import hmac
import json
import os
import re
import queue
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Header
from fastapi.responses import JSONResponse

# Load credentials the same way lookup.py's CLI does, so running the service
# locally picks up env.py / .env. In the container these come from real env
# vars and neither file exists, which is fine.
from pathlib import Path
import sys
_ROOT = Path(__file__).parent
if (_ROOT / "env.py").exists():
    sys.path.insert(0, str(_ROOT))
    import env  # noqa: F401
else:
    try:
        from dotenv import load_dotenv
        load_dotenv(_ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass

from playwright.sync_api import sync_playwright

from lookup import Session, LookupRow, Pl24Credentials, log, clean_vin


def _load_accounts() -> list[Pl24Credentials | None]:
    """Credentials for each warm session, one per pool slot.

    PL24_ACCOUNTS, when set, is a JSON list of accounts and is the ONLY
    thing that should size a multi-session pool:

        [{"company_id": "xx-000000", "username": "user1", "password": "..."},
         {"company_id": "xx-000000", "username": "user2", "password": "..."}]

    company_id may repeat (additional users under one subscription) or
    differ (wholly separate accounts) — both give independent sessions.

    WHY ONE ACCOUNT PER SESSION IS MANDATORY, not merely tidy: partslink24
    permits one live session per USER. Logging the same user in twice
    triggers the session squeeze-out prompt, and confirming it KILLS the
    older session (observed repeatedly on 2026-08-01). Two pool sessions
    sharing one account would therefore take turns evicting each other —
    each eviction surfacing as catalog_ui_error, each triggering layer 3's
    forced re-login, which evicts the sibling again. The pool would be
    slower and less reliable than a single session.

    So POOL_SIZE is derived from the account list rather than set
    independently: the two cannot drift into that state. With no
    PL24_ACCOUNTS the pool is a single slot on the environment
    credentials, exactly as before.
    """
    raw = os.environ.get("PL24_ACCOUNTS", "").strip()
    if not raw:
        return [None]          # single slot, environment credentials
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"PL24_ACCOUNTS is not valid JSON: {e}") from e
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("PL24_ACCOUNTS must be a non-empty JSON list")

    accounts: list[Pl24Credentials | None] = []
    seen: set[tuple[str, str]] = set()
    for i, entry in enumerate(entries):
        try:
            creds = Pl24Credentials(entry["company_id"], entry["username"],
                                    entry["password"])
        except (TypeError, KeyError) as e:
            raise RuntimeError(
                f"PL24_ACCOUNTS[{i}] needs company_id, username, password"
            ) from e
        key = (creds.company_id, creds.username)
        if key in seen:
            # Would guarantee the squeeze-out war described above.
            raise RuntimeError(
                f"PL24_ACCOUNTS[{i}] repeats {creds.company_id}/"
                f"{creds.username}; each pool session needs its own user"
            )
        seen.add(key)
        accounts.append(creds)
    return accounts


ACCOUNTS = _load_accounts()
POOL_SIZE = len(ACCOUNTS)
# Ceiling on total pool startup (all slots). Generous: a cold container
# launching Chromium and completing a partslink24 login can legitimately
# take tens of seconds per slot.
POOL_START_TIMEOUT_S = float(os.environ.get("PL24_POOL_START_TIMEOUT_S", "180"))
SKIP_BRAND_CHECK = os.environ.get("PL24_SKIP_BRAND_CHECK", "") == "1"
HEADED = os.environ.get("PL24_HEADED", "") == "1"

# Shared secret. When set, /lookup-paint requires header `X-API-Key: <value>`
# and rejects anything else with 401. HEADER ONLY — a ?api_key= query
# alternative used to exist and was removed: query strings land verbatim in
# uvicorn/Railway access logs, which would leak the secret. This is what
# stops the public Railway URL being called by anyone who finds it — only
# coloureg, which knows the secret, can spend partslink24 effort on the
# account. If PL24_API_KEY is unset the check is DISABLED (open) — fine for
# local dev, but it MUST be set in the Railway service for the public URL.
# /health is intentionally left unauthenticated so Railway's healthcheck and
# uptime probes can reach it.
API_KEY = os.environ.get("PL24_API_KEY", "")

_REQUIRED_ENV = ("PARTSLINK24_COMPANY_ID", "PARTSLINK24_USERNAME",
                 "PARTSLINK24_PASSWORD")


# ---------------------------------------------------------------------------
# Job plumbing: async handlers submit a _Job to the worker thread and wait on
# its threading.Event for the result.
# ---------------------------------------------------------------------------
class _Job:
    __slots__ = ("row", "debug", "done", "result", "error", "abandoned")

    def __init__(self, row: LookupRow, debug: bool):
        self.row = row
        self.debug = debug
        self.done = threading.Event()
        self.result = None      # LookupResult on success
        self.error = None       # Exception/string on hard failure
        # Set by submit() when the caller's wait times out while the job is
        # still QUEUED (or in flight). A queued-but-abandoned job is skipped
        # at dequeue instead of driving a real partslink24 lookup nobody is
        # waiting for. Plain bool flipped once across threads — the GIL makes
        # the read/write safe, and the only race (worker dequeues in the same
        # instant the caller times out) degrades to the old behaviour: the
        # lookup runs and its result is discarded. In-flight work is NOT
        # cancelled — Playwright work can't be interrupted cleanly mid-call.
        self.abandoned = False


class PoolWorker:
    """Runs the Session pool: one thread per slot, each owning its own
    Playwright, browser, session and partslink24 account.

    All browser interaction happens on those threads and nowhere else — the
    sync API is thread-affine, so even teardown has to run on the owning
    thread (see stop()). Every slot loops on the SAME jobs queue, so a job
    goes to whichever slot frees up first and ordering is FIFO. With a single
    slot that loop IS the one-VIN-at-a-time lock the scraper requires; with N
    slots the requirement still holds, because each slot drives its own page
    sequentially and never touches a sibling's.
    """

    def __init__(self, accounts: "list[Pl24Credentials | None]"):
        self._accounts = accounts
        self._pool_size = len(accounts)
        self._jobs: "queue.Queue[_Job | None]" = queue.Queue()
        self._sessions: list[Session] = []
        self._threads: list[threading.Thread] = []
        # Each slot signals readiness (or failure) independently; start()
        # waits for all of them.
        self._ready = threading.Semaphore(0)
        self._start_errors: list[Exception] = []
        self._lock = threading.Lock()

    # ---- lifecycle ----------------------------------------------------
    def start(self) -> None:
        for idx, creds in enumerate(self._accounts):
            t = threading.Thread(target=self._session_thread,
                                 args=(idx, creds),
                                 name=f"pl24-pool-{idx}", daemon=True)
            t.start()
            self._threads.append(t)
        # Bounded wait. A slot signals _ready exactly once — on success or
        # on failure — so an un-signalled slot means it is STUCK rather than
        # broken: sync_playwright().start() or the browser launch can hang
        # rather than raise (most plausibly under memory pressure, which is
        # precisely what several Chromium instances on a small container
        # produce). Without a deadline the app would then block in lifespan
        # forever, serving nothing and logging nothing, until the platform
        # healthcheck eventually killed it. Fail loudly instead.
        deadline = time.monotonic() + POOL_START_TIMEOUT_S
        for i in range(len(self._threads)):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._ready.acquire(timeout=remaining):
                raise RuntimeError(
                    f"pool startup timed out after {POOL_START_TIMEOUT_S:.0f}s: "
                    f"{i}/{len(self._threads)} session(s) reported in. A slot "
                    f"is stuck launching its browser or logging in."
                )
        if self._start_errors:
            raise self._start_errors[0]
        log(f"[pool] {len(self._sessions)} session(s) ready")

    def _session_thread(self, idx: int,
                        creds: "Pl24Credentials | None") -> None:
        """One pool slot: its OWN Playwright, browser, session and account,
        serving jobs from the shared queue for the life of the process.

        Playwright's sync API is THREAD-AFFINE. SyncBase._sync() captures
        greenlet.getcurrent() and drives the work by switching to a
        dispatcher fiber created when sync_playwright().start() ran;
        greenlets cannot be switched to across threads. So a browser
        created on thread A simply cannot be driven from thread B — it
        raises rather than merely being unsafe.

        An earlier design started one Playwright on a single pool thread
        and dispatched each job to a fresh sub-thread that borrowed a
        session from a free-queue. That could never have worked for
        POOL_SIZE > 1 (every lookup would have hit the greenlet error),
        and it also spawned one unbounded OS thread per queued request and
        lost FIFO ordering, since queue.Queue makes no promise about which
        waiter wins. This design fixes all three: Playwright per thread,
        a fixed number of threads, and ordering owned by the single jobs
        queue.
        """
        pw = None
        session = None
        try:
            pw = sync_playwright().start()
            session = Session(
                pw,
                headed=HEADED,
                skip_brand_check=SKIP_BRAND_CHECK,
                allow_dashboard_fallback=True,
                save_state=False,   # in-memory session; ephemeral container
                creds=creds,
            )
            who = f"{creds.company_id}/{creds.username}" if creds else "env"
            log(f"[pool] starting session {idx + 1}/{self._pool_size} ({who})")
            session.start()
            with self._lock:
                self._sessions.append(session)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._start_errors.append(e)
            self._ready.release()
            # session.start() can fail AFTER the browser is up (a failed
            # login is the likely case), leaving a live Chromium behind.
            # pw.stop() would probably reap it as a child of the driver
            # process, but closing explicitly is free and not conditional
            # on that. Order matters: close the browser, then Playwright.
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
            if pw is not None:
                try:
                    pw.stop()
                except Exception:  # noqa: BLE001
                    pass
            return

        self._ready.release()
        try:
            while True:
                job = self._jobs.get()
                if job is None:
                    # Re-post so sibling slots also see the sentinel.
                    self._jobs.put(None)
                    break
                session = self._run_one(job, session)
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass

    def _run_one(self, job: _Job, session: Session) -> Session:
        """Run one job on `session`, with one crash-recovery retry. Returns
        the (possibly rebuilt) session so the caller can keep using it."""
        if job.abandoned:
            # Caller timed out while this job sat in the queue; nobody is
            # waiting on the result. Skipping it is pure saving: no
            # partslink24 traffic, and the next (live) job starts sooner.
            log(f"[pool] skipping abandoned job {job.row.vin} "
                f"(caller timed out while queued)")
            job.done.set()
            return session
        try:
            job.result = session.lookup(job.row, debug=job.debug)
        except Exception as first_err:  # noqa: BLE001
            # Could be a dead browser/context. Rebuild and retry once.
            log(f"[pool] lookup error ({type(first_err).__name__}); "
                f"rebuilding session and retrying once")
            try:
                session.start()  # tears down + relaunches + re-logins
                job.result = session.lookup(job.row, debug=job.debug)
            except Exception as second_err:  # noqa: BLE001
                job.error = f"{type(second_err).__name__}: {second_err}"
        finally:
            job.done.set()
        return session

    # ---- submission ----------------------------------------------------
    def submit(self, row: LookupRow, debug: bool, timeout: float) -> _Job:
        job = _Job(row, debug)
        self._jobs.put(job)
        finished = job.done.wait(timeout=timeout)
        if not finished:
            # The worker is still grinding (or stuck) — or the job hasn't
            # even STARTED (queued behind a slow lookup at POOL_SIZE=1,
            # where this wait covers queue time too). We can't cancel
            # in-flight Playwright work cleanly, so we surface a timeout to
            # the caller; marking the job abandoned lets the worker skip it
            # if it was still queued, so a caller who already got their 504
            # doesn't cost the account a pointless lookup that also delays
            # everyone queued behind it.
            job.abandoned = True
            job.error = f"service timeout after {timeout:.0f}s"
        return job

    def sessions_alive(self) -> int:
        return sum(1 for s in self._sessions if s.is_alive())

    def stop(self) -> None:
        """Post the shutdown sentinel and wait for the slots to wind down.

        Teardown deliberately happens ON each slot's own thread (the finally
        block in _session_thread), never here: browser and Playwright
        objects are thread-affine, so closing them from the FastAPI thread
        would raise a greenlet error instead of closing anything. All this
        method does is signal and join.
        """
        self._jobs.put(None)
        for t in self._threads:
            t.join(timeout=10)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
worker: PoolWorker | None = None

# Hard ceiling on how long a single request will wait for the worker. The
# fallback chain has grown since this default was 60s: worst case is now a
# proactive re-login (~10s) + up to FOUR catalogue legs (routed -> commercial
# sibling -> Classic -> legacy), each a 10s wait + one 10s silent-timeout
# re-submit, + the dashboard — ~100s if every leg times out. That's rare
# (most legs fast-fail in ~1-3s; the Caddy fallback run was 17s total), but
# 120s covers the true worst case. On timeout the job is abandoned to finish
# in the background and the NEXT request queues behind it (pool_size=1), so
# the coloureg side should keep its own shorter client timeout and treat a
# timeout as "no paint from pl24" rather than blocking the user.
REQUEST_TIMEOUT_S = float(os.environ.get("PL24_REQUEST_TIMEOUT_S", "120"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker
    # The single-account env vars are required ONLY when at least one pool
    # slot falls back to them (i.e. no PL24_ACCOUNTS). In multi-account mode
    # every slot carries its own credentials, so demanding them anyway would
    # reject a correctly-configured deployment that had cleanly removed the
    # old vars.
    if any(c is None for c in ACCOUNTS):
        missing = [v for v in _REQUIRED_ENV if not os.environ.get(v)]
        if missing:
            raise RuntimeError(
                f"missing required env vars: {', '.join(missing)} "
                f"(needed because no PL24_ACCOUNTS is configured)"
            )
    worker = PoolWorker(ACCOUNTS)
    # start() blocks until sessions are logged in (or raises on failure), so
    # the service only reports healthy once it can actually serve.
    #
    # On PARTIAL failure (one slot's login fails, or the whole thing hits
    # POOL_START_TIMEOUT_S) the slots that DID come up are already sitting on
    # the jobs queue with live Chromiums. The finally below can't reach them,
    # because it only runs once we've reached the yield. Tear them down here
    # instead. Process exit would reap the Chromiums anyway, but doing it
    # explicitly keeps the failure path honest and bounded.
    if not API_KEY:
        # Deliberate design (open = local dev convenience), but on Railway
        # an unset key means the PUBLIC URL will spend real partslink24
        # account effort for anyone who finds it. Say so where it will be
        # seen — the deploy log — rather than relying on someone re-reading
        # the config comment.
        log("[service] WARNING: PL24_API_KEY is not set; /lookup-paint is "
            "UNAUTHENTICATED. Fine locally, wrong on Railway.")
    try:
        worker.start()
    except Exception:
        try:
            worker.stop()
        except Exception:  # noqa: BLE001
            pass
        raise
    log(f"[service] ready; pool_size={POOL_SIZE}")
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()


# docs_url/redoc_url/openapi_url=None: FastAPI would otherwise serve
# interactive docs and the full OpenAPI schema UNAUTHENTICATED on the public
# Railway URL — enumerating the endpoint, its parameters and the X-API-Key
# header name for anyone who finds the service. The key still gates actual
# lookups either way; this just stops handing strangers the map. The two
# consumers (coloureg, and Roland with curl) both know the interface.
app = FastAPI(title="pl24 paint lookup", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health():
    if worker is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    return {
        "status": "ok",
        "pool_size": POOL_SIZE,
        "sessions_alive": worker.sessions_alive(),
    }


@app.get("/lookup-paint")
async def lookup_paint(
    vin: str = Query(..., min_length=1, max_length=25,
                     description="full VIN (17 chars; embedded spaces "
                                 "tolerated and stripped)"),
    make: str = Query(..., min_length=1, max_length=40,
                      description="VDG-style make, e.g. 'BMW', 'Volkswagen'"),
    category: str | None = Query(None, max_length=4,
                                 description="EU category M1/N1/N2/N3"),
    year: str | None = Query(None, max_length=8,
                             description="model year (currently unused)"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    """Look up a paint code for one VIN. Returns the scraper's result as JSON.

    A non-empty `paint_code` means success. Otherwise inspect `outcome`
    (e.g. 'not_found_as_routed', 'unsupported_brand', 'brand_unavailable',
    'auth_error') and `error` to decide what to do — typically fall through
    to the manual-lookup offer on the coloureg side.

    Requires the shared secret (X-API-Key header) when PL24_API_KEY is
    configured on the service.
    """
    # Auth: reject unless the shared secret matches (when one is configured).
    if API_KEY:
        # Constant-time comparison — cheap hardening against timing probes
        # on the public URL. Header only (see the API_KEY note above).
        # COMPARED AS BYTES, not str: hmac.compare_digest raises TypeError
        # on non-ASCII str input, so a crafted header like "X-API-Key: é"
        # would 500 (with a traceback in the Railway log) instead of 401 —
        # a free error-page probe on the public URL. Encoding both sides
        # first makes any header content compare cleanly and fail closed.
        # errors="replace" (not surrogateescape, which itself raises on a
        # lone non-DC surrogate): replace NEVER raises, and a replaced byte
        # can only make the comparison fail — which is the right outcome
        # for a key that wasn't the key.
        supplied = (x_api_key or "").encode("utf-8", "replace")
        if not hmac.compare_digest(supplied, API_KEY.encode("utf-8")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    if worker is None:
        return JSONResponse({"error": "service not ready"}, status_code=503)

    # Strict VIN validation, mirroring read_lookups() exactly (strip
    # spaces, then 17 chars of the ISO 3779 alphabet — no I/O/Q). The
    # Query() bounds above are transport-level only (they stop megabyte
    # strings at the framework door); THIS regex is the single source of
    # truth, so a spaced-but-valid VIN cleans and passes rather than
    # tripping a framework 422. The CLI has always enforced this; the
    # service previously accepted 11-20 chars of anything and let a typo
    # burn a full ~10-47s lookup, typing the garbage into partslink24's own
    # search on the account. Reject it here in ~0ms instead. 400, not a
    # LookupResult: this is caller error, not a lookup outcome, and keeping
    # it out of OUTCOMES keeps that vocabulary meaning what it says.
    # clean_vin (lookup.py) is the single validator shared with
    # read_lookups and --vin; ASCII is checked BEFORE case-folding there,
    # because .upper() can EXPAND non-ASCII input ('\ufb00' -> 'FF') and
    # manufacture a 17th character after the fact. Echo the RAW input back
    # in the error, truncated: echoing the cleaned form printed the
    # artefact of our own normalisation rather than what the caller sent.
    vin_clean = clean_vin(vin)
    if vin_clean is None:
        return JSONResponse(
            {"error": "malformed VIN: must be 17 chars, letters (no I/O/Q) "
                      "and digits", "vin": vin.strip()[:32]},
            status_code=400,
        )

    row = LookupRow(vin=vin_clean, make=make.strip(),
                    category=category, year=year)

    t0 = time.monotonic()
    # Run the blocking submit() in a threadpool so we don't block the event
    # loop while awaiting the worker. (submit itself waits on a thread Event.)
    import anyio
    job = await anyio.to_thread.run_sync(
        worker.submit, row, False, REQUEST_TIMEOUT_S
    )
    elapsed = round(time.monotonic() - t0, 2)

    if job.error and job.result is None:
        return JSONResponse(
            {
                "vin": row.vin,
                "paint_code": "",
                "paint_description": "",
                "via": "",
                "outcome": "service_error",
                "error": job.error,
                "elapsed_s": elapsed,
            },
            status_code=504 if "timeout" in (job.error or "") else 502,
        )

    r = job.result
    return {
        "vin": r.vin,
        "paint_code": r.paint_code,
        "paint_description": r.paint_description,
        "via": r.via,
        "outcome": r.outcome,
        "error": r.error,
        "elapsed_s": elapsed,
    }


# Convenience for `python service.py` local runs (uvicorn import string also
# works: `uvicorn service:app`).
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("service:app", host="0.0.0.0", port=port, log_level="info")