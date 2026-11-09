#!/usr/bin/env bash
set -euo pipefail

# Fails unless node and npm are the given versions. Run by
# .github/workflows/publish-calibration-tree.yaml after actions/setup-node and
# before publishing: npm's trusted publishing needs npm 11.5.1 or later, and
# the workflow pins the Node release whose bundled npm it expects.
if (( $# != 2 )); then
  echo "Usage: $0 NODE_VERSION NPM_VERSION" >&2
  exit 2
fi
expected_node="v$1"
expected_npm="$2"

node_version=$(node --version)
npm_version=$(npm --version)
echo "node $node_version, npm $npm_version"

if [[ "$node_version" != "$expected_node" ]]; then
  echo "Expected node $expected_node, found $node_version." >&2
  exit 1
fi
if [[ "$npm_version" != "$expected_npm" ]]; then
  echo "Expected npm $expected_npm, found $npm_version." >&2
  exit 1
fi
