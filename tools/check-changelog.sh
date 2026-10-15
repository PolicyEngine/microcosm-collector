#!/usr/bin/env bash
set -euo pipefail

# Refuse a pull request whose merge could not be released: changelog.d must hold
# at least one typed fragment, and pyproject.toml exactly one version line.
uv run --no-project python tools/bump-version.py --check
