FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY telemetry_collector ./telemetry_collector
RUN uv sync --frozen --no-dev

EXPOSE 8080
CMD ["uv", "run", "--no-sync", "uvicorn", "telemetry_collector.app:create_app_from_environment", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
