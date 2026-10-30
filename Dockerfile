# ============================================================
# Stage 1: build telegram-bot-api (Local Bot API Server) from source.
# Official build instructions: https://github.com/tdlib/telegram-bot-api
# This needs a full C++ toolchain, which is exactly what root access on
# Railway (vs. the old no-root Alwaysdata shared host) makes possible.
# ============================================================
FROM debian:bookworm AS botapi-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates cmake g++ make gperf zlib1g-dev libssl-dev xz-utils curl \
    && rm -rf /var/lib/apt/lists/*

# Source is fetched as a GitHub tarball instead of `git clone --recursive`:
# Railway build sandboxes have intermittently failed git's HTTPS credential
# prompt ("could not read Username for 'https://github.com'") even though
# plain tarball downloads to codeload.github.com work fine from the same
# network — git's smart-HTTP protocol is a different endpoint that gets
# blocked/ratelimited more aggressively. A pinned tarball is also more
# reproducible. TDLIB_COMMIT is the tdlib submodule pin; bump both together
# when updating.
# Pinned telegram-bot-api commit (was: TBAPI_REF=master, a moving target that
# could break any redeploy). e3e9dd8e is master HEAD as of 2026-08-25, which
# predates the last known-good image build, so (TBAPI_COMMIT + TDLIB_COMMIT
# below) reproduces the proven combination. To bump: take the new master HEAD
# SHA from https://github.com/tdlib/telegram-bot-api/commits/master and keep
# TDLIB_COMMIT in sync with what that commit expects (see its td/ submodule
# pointer) — a mismatch fails loudly here at build time, not at runtime.
ARG TBAPI_COMMIT=e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1
ARG TDLIB_COMMIT=bc9c263e2bfee06aaab41e82db51a103376030bc

WORKDIR /src
# Retry loop: transient GitHub/CDN hiccups shouldn't fail an otherwise
# healthy Railway deploy (3 attempts, 10s apart).
RUN set -eux; \
    for i in 1 2 3; do \
        curl -fSL --retry 3 --retry-delay 5 -o /tmp/tbapi.tar.gz \
            "https://codeload.github.com/tdlib/telegram-bot-api/tar.gz/${TBAPI_COMMIT}" \
        && break || { [ "$i" -lt 3 ] && sleep 10; }; \
    done; \
    [ -s /tmp/tbapi.tar.gz ] || { echo "FATAL: could not download telegram-bot-api source after retries" >&2; exit 1; }; \
    mkdir -p /src/td; \
    tar -xzf /tmp/tbapi.tar.gz -C /src --strip-components=1; \
    rm -f /tmp/tbapi.tar.gz

# The --recursive clone used to pull the tdlib submodule; replicate that by
# downloading the pinned tdlib commit directly (same retry strategy). The
# build only needs td/ as a plain source directory — no .git metadata needed.
RUN set -eux; \
    for i in 1 2 3; do \
        curl -fSL --retry 3 --retry-delay 5 -o /tmp/tdlib.tar.gz \
            "https://codeload.github.com/tdlib/td/tar.gz/${TDLIB_COMMIT}" \
        && break || { [ "$i" -lt 3 ] && sleep 10; }; \
    done; \
    [ -s /tmp/tdlib.tar.gz ] || { echo "FATAL: could not download tdlib source after retries" >&2; exit 1; }; \
    rm -rf /src/td; \
    mkdir -p /src/td; \
    tar -xzf /tmp/tdlib.tar.gz -C /src/td --strip-components=1; \
    rm -f /tmp/tdlib.tar.gz

RUN mkdir build && cd build \
    && cmake -DCMAKE_BUILD_TYPE=Release .. \
    && cmake --build . --target install -- -j"$(nproc)"
# Produces /usr/local/bin/telegram-bot-api (confirmed via the project's own
# CMakeLists.txt: RUNTIME DESTINATION "bin" under the default install prefix).

# ============================================================
# Stage 2: runtime image — Python bot + the compiled server binary.
# Kept separate from the build stage so the final image doesn't carry the
# ~1GB+ of build tooling (cmake, g++, git, dev headers) needed only to
# compile telegram-bot-api once.
# ============================================================
FROM python:3.13-slim

# Runtime-only shared libs for the dynamically-linked telegram-bot-api
# binary (the -dev/headers packages from Stage 1 are NOT needed here).
# ffmpeg: a real apt-installed build, not the portable imageio-ffmpeg
# binary bot.py falls back to on hosts without root — that portable binary
# was causing "Postprocessing: Conversion failed!" errors merging YouTube's
# separate video/audio streams in this container. Root access on Railway
# (unlike the old no-root Alwaysdata host) is exactly what makes a proper
# apt-installed ffmpeg possible here, so bot.py now prefers it automatically
# whenever it's on PATH.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 zlib1g ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=botapi-builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

# deno: JS runtime for yt-dlp's EJS challenge framework. Since late 2025
# YouTube gates player API responses behind JS signature/n-parameter
# challenges; without a JS runtime yt-dlp can't solve them and extraction
# fails with "The page needs to be reloaded" / "Sign in to confirm you're
# not a bot". Only deno is enabled by default in yt-dlp (min version 2.3.0),
# so we ship a pinned static deno binary — no npm/node needed.
ARG DENO_VERSION=2.9.5
# SHA-256 of the upstream zip (computed 2026-10-01). Verified before
# extraction so a corrupt download or a tampered artifact fails the build
# loudly instead of shipping a broken/malicious deno. Recompute with
# `sha256sum` when bumping DENO_VERSION.
ARG DENO_SHA256=8b010a3b1a4a0188a67cdb8a7a27348b2a501af78aec7fc74f2ace167368d530
RUN python3 -c "import urllib.request; urllib.request.urlretrieve('https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip', '/tmp/deno.zip')" \
    && echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - \
    && python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('/usr/local/bin')" \
    && chmod +x /usr/local/bin/deno \
    && rm -f /tmp/deno.zip \
    && deno --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py main.py wiring.py config.py timeutil.py sessions.py limits.py metrics.py infra.py db.py ai.py bots.py ig_relay.py miniapp_server.py start.sh ./
COPY services/ ./services/
COPY handlers/ ./handlers/
COPY miniapp/ ./miniapp/
COPY repositories/ ./repositories/
COPY games/ ./games/
COPY realtime/ ./realtime/
RUN chmod +x start.sh

# Railway: attach a Volume mounted at this path (via the Railway dashboard,
# NOT this Dockerfile — Railway rejects the standard Docker VOLUME
# instruction and requires volumes to be configured through its own UI) so
# the SQLite DB, Instagram/YouTube temp-download dirs, and the Pyrogram/
# session files survive across redeploys and restarts.
ENV DATA_DIR=/data

CMD ["./start.sh"]
