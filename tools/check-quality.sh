#!/usr/bin/env bash
set -euo pipefail

# Check style, and that every release, extraction and deployment script parses,
# so a broken script fails a pull request rather than the next release or deploy.
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
for script in tools/*.sh scripts/*.sh; do
  bash -n "$script"
done
