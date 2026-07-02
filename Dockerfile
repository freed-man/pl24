# pl24 paint-lookup service container.
#
# Runs the FastAPI service (service.py), which holds a warm pool of logged-in
# partslink24 Sessions and exposes GET /lookup-paint and GET /health. Phase 0
# proved this image's base + the scraper work headless on a Railway datacenter
# IP; this image turns it into the always-running service coloureg calls.
#
# Base image version moves in LOCKSTEP with the EXACT playwright pin in
# requirements.txt (playwright==1.60.0). Browser binaries are baked in here
# and we never run `playwright install`, so any driver/image version skew
# crashes the worker at startup. Proven 2026-07-02: an unpinned >=1.60
# resolved to the freshly-released 1.61.0 against this v1.60.0 image ->
# BrowserType.launch "Executable doesn't exist" -> healthcheck failure.
# Upgrading = change BOTH files together + re-run the test VINs first.
# -noble = Ubuntu 24.04.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

# Coherent locale. The scraper sets locale=en-GB / timezone=Europe/London on
# the browser context for fingerprint coherence; keep the OS layer consistent.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install Python deps first for layer caching. Browser binaries are already
# baked into the base image at /ms-playwright, so we do NOT run
# `playwright install`.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code: the scraper plus the service that wraps it. Secrets are passed at
# runtime as env vars, never copied into the image. .env / env.py stay on the
# host (and are excluded by .dockerignore).
COPY lookup.py .
COPY service.py .

# Run as the non-root user the base image ships (pwuser). Playwright recommends
# non-root for scraping; root also disables the Chromium sandbox. /app is owned
# by root from the COPYs, so give pwuser a writable workdir for any _debug dumps.
RUN mkdir -p /app/_debug && chown -R pwuser:pwuser /app
USER pwuser

# Railway provides $PORT at runtime and routes to it. uvicorn must bind 0.0.0.0
# on that port. Default 8000 for local `docker run -p 8000:8000`.
ENV PORT=8000
EXPOSE 8000

# Run the service. Shell form so $PORT expands at runtime.
#   docker run --rm --ipc=host -p 8000:8000 \
#     -e PARTSLINK24_COMPANY_ID=... -e PARTSLINK24_USERNAME=... \
#     -e PARTSLINK24_PASSWORD=... pl24
# then: curl "http://localhost:8000/lookup-paint?vin=WBABT32020LS20430&make=BMW"
#
# --ipc=host (on docker run) avoids Chromium running out of /dev/shm on
# content-heavy pages. On Railway this isn't needed/settable; the noble image
# + low concurrency (pool size 1) keeps shared-memory pressure low.
ENTRYPOINT ["sh", "-c", "uvicorn service:app --host 0.0.0.0 --port ${PORT}"]