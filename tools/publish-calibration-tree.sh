#!/usr/bin/env bash
set -euo pipefail

# Publishes the npm package in the current directory unless npm already has
# its version. Run from packages/calibration-tree by
# .github/workflows/publish-calibration-tree.yaml, which authenticates through
# npm's trusted publishing.
name=$(node -p 'require("./package.json").name')
version=$(node -p 'require("./package.json").version')
spec="${name}@${version}"

error_file=$(mktemp)
trap 'rm -f "$error_file"' EXIT

# `npm view` prints the version when it is published and nothing when the
# package exists without it; for a package npm has never had it fails with
# E404. Any other failure means npm could not be asked.
if published=$(npm view "$spec" version 2>"$error_file"); then
  if [[ -n "$published" ]]; then
    echo "$spec is already on npm; nothing to publish."
    exit 0
  fi
elif ! grep -q "E404" "$error_file"; then
  cat "$error_file" >&2
  echo "Could not ask npm whether $spec is published." >&2
  exit 1
fi

npm publish --access public
