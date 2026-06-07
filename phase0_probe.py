#!/usr/bin/env python3
"""
phase0_probe.py  --  Throwaway Railway Phase 0 login probe.

This is NOT part of the eventual service. Its only job is to answer the one
question that can kill the whole synchronous-fallback design:

    Can pl24 log in to partslink24 and return a paint code when running from
    a Railway datacenter IP, inside the headless container?

It runs ONE VIN lookup through pl24's real run() (same code path the CLI uses),
prints the result and the login/auth signal as loudly as possible so it's
readable in Railway's deploy logs, then SLEEPS instead of exiting. The sleep
matters: pl24's CLI exits when done, and Railway treats a clean exit as a
crashed service and restart-loops it. Sleeping holds the container open for one
clean run so we can read the logs, then we tear the service down by hand.

Config via env vars (set in Railway's service Variables tab):
    PARTSLINK24_COMPANY_ID / _USERNAME / _PASSWORD   -- credentials (required)
    PROBE_VIN     -- VIN to test     (default: a known-good BMW from VDG data)
    PROBE_MAKE    -- make for the VIN (default: BMW)
    PROBE_DEBUG   -- "1" to dump login HTML/screenshots on failure (default 1)

Read the logs for the lines bracketed by '==== PHASE 0' below.
"""

import os
import sys
import time
import traceback

# Import the real scraper. The probe deliberately reuses run() / LookupRow so
# it exercises the identical login + scrape path the production service will.
from lookup import run, LookupRow
from playwright.sync_api import sync_playwright


def banner(msg):
    line = "=" * 70
    print(f"\n{line}\n==== PHASE 0  {msg}\n{line}", flush=True)


def main():
    # Credentials must be present or there's nothing to probe.
    missing = [v for v in ("PARTSLINK24_COMPANY_ID", "PARTSLINK24_USERNAME",
                           "PARTSLINK24_PASSWORD") if not os.environ.get(v)]
    if missing:
        banner(f"ABORT: missing env vars: {', '.join(missing)}")
        # Sleep rather than exit, so the log line stays readable and Railway
        # doesn't instantly restart-loop into the same message.
        _hold()
        return

    vin = os.environ.get("PROBE_VIN", "WBABT32020LS20430")
    make = os.environ.get("PROBE_MAKE", "BMW")
    debug = os.environ.get("PROBE_DEBUG", "1") == "1"

    banner(f"START  vin={vin} make={make} debug={debug}")
    print(f"  running from container; if login works here it works on a "
          f"datacenter IP.", flush=True)

    t0 = time.monotonic()
    try:
        row = LookupRow(vin=vin.upper(), make=make)
        with sync_playwright() as pw:
            results = run(
                pw, [row],
                headed=False,            # the whole point: headless container
                debug=debug,
                fresh=True,              # no cached session; force a real login
                skip_brand_check=False,
                allow_dashboard_fallback=True,
                dump_always=False,
                inter_vin_delay=(0.0, 0.0),
            )
        dur = time.monotonic() - t0

        if not results:
            banner(f"RESULT: no result object returned (dur={dur:.1f}s)")
        else:
            r = results[0]
            banner("RESULT")
            print(f"  vin         : {r.vin}", flush=True)
            print(f"  paint_code  : {r.paint_code!r}", flush=True)
            print(f"  description : {r.paint_description!r}", flush=True)
            print(f"  via         : {r.via!r}", flush=True)
            print(f"  outcome     : {r.outcome!r}", flush=True)
            print(f"  error       : {r.error!r}", flush=True)
            print(f"  duration    : {dur:.1f}s", flush=True)
            print("", flush=True)

            # Interpret for the one question we care about.
            if r.paint_code:
                banner("VERDICT: GREEN — logged in AND returned a paint code "
                       "from a datacenter container. Phase 0 passes.")
            elif r.via:
                # Reached a catalog/dashboard page => login succeeded, the VIN
                # just didn't yield paint. That still answers the gate question.
                banner("VERDICT: AMBER — login appears to have SUCCEEDED "
                       "(reached the catalog/dashboard) but no paint for this "
                       "VIN. Login is NOT the blocker; try another VIN.")
            else:
                banner("VERDICT: investigate — no paint and no catalog reached. "
                       "Check error above; if it mentions 'login failed' the "
                       "datacenter IP may be blocked. See _debug/ dumps.")
    except Exception:
        banner("EXCEPTION during probe")
        traceback.print_exc()
        print("", flush=True)
        print("  If this is a login/auth failure, the _debug/ folder holds "
              "login_failed.png/html — but note the container is ephemeral, so "
              "those dumps vanish on teardown. The traceback above + the deploy "
              "log is the durable record.", flush=True)

    _hold()


def _hold():
    banner("HOLDING — probe done. Container will sleep so logs stay readable. "
           "Delete this Railway service when finished.")
    # Sleep in a loop rather than one long sleep so the process stays a clean,
    # interruptible foreground process for Railway.
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
