#!/usr/bin/env bash
set -euo pipefail

# Prepare a release: bump pyproject.toml from the pending fragment types, collect
# the fragments into CHANGELOG.md, and record the collector's new version in
# uv.lock so that `uv sync --locked` still passes.
version="$(uv run --no-project python tools/bump-version.py)"
uvx --from "towncrier==26.9.0" towncrier build --yes --version "$version"
uv lock
