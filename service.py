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
    logged-in page. Session validity is checked cheaply (is_logged_in, a DOM
    check, no navigation) per request and re-login happens lazily ONLY when a
    session has actually expired — no background pinging (which would be both a
    bot-detection signal and unreliable against absolute server-side timeouts).

  * SYNC scraper on a worker THREAD. Playwright's sync API can't run inside
    FastAPI's async event loop, and we don't want to rewrite the 2500-line
    scraper as async. So one dedicated worker thread owns the Playwright
    instance and the whole Session pool; async request handlers submit jobs to
    it via a queue and await the result. With POOL_SIZE=1 the single worker
    processing its queue IS the one-VIN-at-a-time lock the scraper requires.

  * POOL-READY, size 1 by default. POOL_SIZE controls how many warm sessions
    run in parallel. 1 = simple FIFO queue (requests serialise; safe, looks
    like one partslink24 user). Raising it gives true parallelism under
    concurrent load BUT means N simultaneous logins on one partslink24 account
    from one datacenter IP — more conspicuous, and partslink24's session
    squeeze-out behaviour may fight a multi-session pool. So the knob exists
    but stays at 1 until real traffic + partslink24 tolerance justify raising
    it. Going 1 -> N is a config change, not a rewrite.

  * CRASH RECOVERY. If a lookup throws because the browser/context died, the
    worker rebuilds that session (Session.start()) and retries the job once.

Config via env (all optional except credentials, which lookup.py requires):
    PL24_POOL_SIZE        number of warm sessions (default 1)
    PL24_SKIP_BRAND_CHECK "1" to skip the startup brand-list verify per session
    PL24_HEADED           "1" to run headed (local debugging only; never in
                          the container)
"""

import hmac
import os
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

from lookup import Session, LookupRow, log


POOL_SIZE = int(os.environ.get("PL24_POOL_SIZE", "1"))
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
    __slots__ = ("row", "debug", "done", "result", "error")

    def __init__(self, row: LookupRow, debug: bool):
        self.row = row
        self.debug = debug
        self.done = threading.Event()
        self.result = None      # LookupResult on success
        self.error = None       # Exception/string on hard failure


class PoolWorker:
    """Owns the Playwright instance and the Session pool on ONE thread.

    All browser interaction happens here. Jobs arrive on a queue; the worker
    pulls a free session, runs the lookup, returns it to the free pool, and
    signals the job. With POOL_SIZE > 1 the worker uses a small thread pool
    internally so sessions run in parallel; with POOL_SIZE == 1 it's a plain
    sequential loop (the FIFO lock).
    """

    def __init__(self, pool_size: int):
        self._pool_size = pool_size
        self._jobs: "queue.Queue[_Job | None]" = queue.Queue()
        self._sessions: list[Session] = []
        self._free: "queue.Queue[Session]" = queue.Queue()
        self._pw = None
        self._thread = threading.Thread(target=self._run, name="pl24-pool",
                                        daemon=True)
        self._started = threading.Event()
        self._start_error: Exception | None = None
        # When pool_size > 1, jobs are dispatched to worker sub-threads so
        # multiple sessions run concurrently. Each sub-thread borrows/returns
        # a session from self._free.
        self._dispatch_pool: list[threading.Thread] = []

    # ---- lifecycle ----------------------------------------------------
    def start(self) -> None:
        self._thread.start()
        # Wait until sessions are up (or startup failed) before serving.
        self._started.wait()
        if self._start_error is not None:
            raise self._start_error

    def _run(self) -> None:
        """Thread entry: build Playwright + sessions, then serve jobs."""
        try:
            self._pw = sync_playwright().start()
            for i in range(self._pool_size):
                s = Session(
                    self._pw,
                    headed=HEADED,
                    skip_brand_check=SKIP_BRAND_CHECK,
                    allow_dashboard_fallback=True,
                    save_state=False,   # in-memory session; ephemeral container
                )
                log(f"[pool] starting session {i + 1}/{self._pool_size}")
                s.start()
                self._sessions.append(s)
                self._free.put(s)
            log(f"[pool] {len(self._sessions)} session(s) ready")
        except Exception as e:  # noqa: BLE001
            self._start_error = e
            self._started.set()
            return

        self._started.set()

        if self._pool_size == 1:
            self._serve_sequential()
        else:
            self._serve_concurrent()

    def _serve_sequential(self) -> None:
        """POOL_SIZE == 1: the simple FIFO lock. One job at a time."""
        session = self._sessions[0]
        while True:
            job = self._jobs.get()
            if job is None:  # shutdown sentinel
                break
            session = self._run_one(job, session)

    def _serve_concurrent(self) -> None:
        """POOL_SIZE > 1: dispatch each job to a sub-thread that borrows a
        free session, so up to POOL_SIZE lookups run in parallel."""
        while True:
            job = self._jobs.get()
            if job is None:
                break
            t = threading.Thread(target=self._dispatch_one, args=(job,),
                                 daemon=True)
            t.start()

    def _dispatch_one(self, job: _Job) -> None:
        session = self._free.get()          # blocks until a session is free
        session = self._run_one(job, session)
        self._free.put(session)

    def _run_one(self, job: _Job, session: Session) -> Session:
        """Run one job on `session`, with one crash-recovery retry. Returns
        the (possibly rebuilt) session so the caller can keep using it."""
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
            # The worker is still grinding (or stuck). We can't cancel the
            # in-flight Playwright work cleanly, so we surface a timeout to
            # the caller; the job will complete and be discarded.
            job.error = f"service timeout after {timeout:.0f}s"
        return job

    def sessions_alive(self) -> int:
        return sum(1 for s in self._sessions if s.is_alive())

    def stop(self) -> None:
        # Signal the serve loop to exit, then close sessions + Playwright.
        self._jobs.put(None)
        for s in self._sessions:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


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
    missing = [v for v in _REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"missing required env vars: {', '.join(missing)}"
        )
    worker = PoolWorker(POOL_SIZE)
    # start() blocks until sessions are logged in (or raises on failure), so
    # the service only reports healthy once it can actually serve.
    worker.start()
    log(f"[service] ready; pool_size={POOL_SIZE}")
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()


app = FastAPI(title="pl24 paint lookup", lifespan=lifespan)


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
    vin: str = Query(..., min_length=11, max_length=20,
                     description="full VIN"),
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
        if not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    if worker is None:
        return JSONResponse({"error": "service not ready"}, status_code=503)

    row = LookupRow(vin=vin.strip().upper(), make=make.strip(),
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