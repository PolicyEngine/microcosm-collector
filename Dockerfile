# The Orrery validator (orrery-validator) with its one pinned
# @axiom-foundation/orrery, and the Node that runs it: the release the npm
# publishing workflow also pins, on the same Debian release as the image below.
FROM node:24.21.0-trixie-slim@sha256:173f125896c3b47ddf056734c7ea789d04595a6a08769a8f78e0df642781fb66 AS orrery-validator
WORKDIR /orrery-validator
COPY orrery-validator/package.json orrery-validator/package-lock.json ./
RUN npm ci --omit=dev --omit=peer --ignore-scripts
COPY orrery-validator/validate-orrery-document.mjs ./

# Bun and the calibration tree package's production install, which the worker
# job's calibration tree handler runs (telemetry_collector/calibration_trees.py):
# the Bun release the package's CI job uses, on the same Debian release as the
# image below. Its tests, fixtures and test support stay out (.dockerignore).
FROM oven/bun:1.4.2-slim@sha256:cb3bbbb08e13a4a2ff400f24c7a2a1d5efa83f6ef8544d52d95a519631e2fc61 AS calibration-tree
WORKDIR /calibration-tree
COPY packages/calibration-tree/package.json packages/calibration-tree/bun.lock ./
RUN bun install --frozen-lockfile --production --ignore-scripts
COPY packages/calibration-tree/src ./src

# Astral rebuilds this tag from python:3.13-slim-trixie with each uv release.
# Since Debian's ca-certificates 20250419+deb13u1, its certificate store holds
# neither "emSign ECC Root CA - G3" nor "AAA Certificate Services".
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY telemetry_collector ./telemetry_collector
RUN uv sync --frozen --no-dev
# The worker job validates Orrery graph documents with these
# (telemetry_collector/orrery/validation.py); npm is not copied.
COPY --from=orrery-validator /usr/local/bin/node /usr/local/bin/node
COPY --from=orrery-validator /orrery-validator ./orrery-validator
# The worker job builds and publishes calibration trees with these.
COPY --from=calibration-tree /usr/local/bin/bun /usr/local/bin/bun
COPY --from=calibration-tree /calibration-tree ./packages/calibration-tree

EXPOSE 8080
CMD ["uv", "run", "--no-sync", "uvicorn", "telemetry_collector.app:create_app_from_environment", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
