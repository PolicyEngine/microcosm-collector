FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# The base image's ca-certificates 20230311+deb12u1 trusts "emSign ECC Root CA -
# G3" and "AAA Certificate Services", the roots that the compromised eMudhra
# intermediates chain to, and its pip 25.3 vendors certifi 2025.10.5, which holds
# the first. Debian's ca-certificates 20250419~deb12u2 (bookworm-security) removes
# both roots, and pip 26.2.2 vendors certifi 2026.10.19, which lacks it. CI, and
# the deployment before it pushes its own build, fail if the image holds either
# root (tools/check-image-certificates.py).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --root-user-action=ignore \
        --disable-pip-version-check --upgrade "pip>=26.2.2"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY telemetry_collector ./telemetry_collector
RUN uv sync --frozen --no-dev

EXPOSE 8080
CMD ["uv", "run", "--no-sync", "uvicorn", "telemetry_collector.app:create_app_from_environment", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
