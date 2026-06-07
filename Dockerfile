# pl24 — Phase 0 container.
#
# Goal of this image: prove pl24 runs headless in a clean Linux container
# (no fonts/locale/codecs borrowed from Roland's Windows laptop) and returns
# a paint code for a known VIN. If `docker run` below yields a code, Phase 0
# is green and the Railway/FastAPI work can proceed.
#
# Base image MUST match the Playwright version in requirements.txt
# (playwright>=1.60). Playwright refuses to find its bundled browser if the
# image version and the pip package version disagree. The handoff's
# v1.40.0-jammy is stale relative to the current requirement — we use 1.60.
#
# -noble = Ubuntu 24.04, matching Roland's local Ubuntu 24 dev box.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

# Coherent locale. The scraper sets locale=en-GB / timezone=Europe/London on
# the browser context for fingerprint coherence; keep the OS layer consistent.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install Python deps first for layer caching. The browser binaries are ALREADY
# baked into this base image at /ms-playwright, so we do NOT run
# `playwright install` — that would download a second copy and can drift from
# the image's version. Installing the pip package alone is enough; it locates
# the pre-baked browser via PLAYWRIGHT_BROWSERS_PATH.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code. Only lookup.py is needed to run; secrets are passed at runtime as
# env vars (-e), never copied into the image. .env / env.py stay on the host.
COPY lookup.py .

# Run as the non-root user the base image ships (pwuser). Playwright's docs
# recommend a non-root user for web-scraping / crawling untrusted sites; root
# also disables the Chromium sandbox. /app is owned by root from the COPYs, so
# hand pwuser a writable workdir for storage_state.json / _debug / results.csv.
RUN mkdir -p /app/_debug && chown -R pwuser:pwuser /app
USER pwuser

# Default command runs a single VIN lookup. Override VIN/make at `docker run`
# time. Credentials come from -e PARTSLINK24_* env vars.
#   docker run --rm --ipc=host \
#     -e PARTSLINK24_COMPANY_ID=... \
#     -e PARTSLINK24_USERNAME=... \
#     -e PARTSLINK24_PASSWORD=... \
#     pl24 --vin WBABT32020LS20430 --make BMW --fresh
#
# --ipc=host is recommended by Playwright to avoid Chromium running out of
# shared memory (/dev/shm) and crashing on content-heavy pages.
ENTRYPOINT ["python", "lookup.py"]
CMD ["--help"]
