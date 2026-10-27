# Pinned by digest, so CI and the deployment build from the same base. This
# build (2026-10-27) has Debian's ca-certificates 20250419+deb13u1, which drops
# "emSign ECC Root CA - G3" and "AAA Certificate Services", and pip 26.2.2, whose
# vendored certifi 2026.10.19 lacks the first. CI fails if the built image holds
# either root (tools/check-image-certificates.py).
FROM ghcr.io/astral-sh/uv:0.13.6-python3.13-trixie-slim@sha256:e586c1c65b7071aec78f02c5587c76c5011159cd76b3d6f1255f40e48e850abc

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

EXPOSE 8080
CMD ["uv", "run", "--no-sync", "uvicorn", "telemetry_collector.app:create_app_from_environment", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
