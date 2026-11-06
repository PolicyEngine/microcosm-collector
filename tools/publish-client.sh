#!/usr/bin/env bash
set -euo pipefail

# Publish the read API client to npm, unless its version is already there.
# push.yaml's publish-client job runs this after production deployed the
# release whose read API the client describes, with the client's dependencies
# installed. npm accepts the job's GitHub OIDC token instead of a stored npm
# token (trusted publishing, which needs npm 11.5.1 or later) and attaches a
# provenance attestation. Publishing runs the client's prepack build first.
#
#   publish-client.sh [COLLECTOR_ROOT]
root="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$root/packages/collector-client"

package_field() {
  python3 -c 'import json, sys; print(json.load(open("package.json"))[sys.argv[1]])' "$1"
}
name="$(package_field name)"
version="$(package_field version)"
release="$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$root/pyproject.toml")"
if [ "$version" != "$release" ]; then
  echo "The client's version $version is not the release's version $release." >&2
  exit 1
fi

npm_version="$(npm --version)"
if ! python3 -c 'import sys; sys.exit(tuple(map(int, sys.argv[1].split("."))) < (11, 5, 1))' "$npm_version"; then
  echo "npm $npm_version cannot publish with trusted publishing; it needs 11.5.1 or later." >&2
  exit 1
fi

published="$(npm view "$name@$version" version 2>/dev/null || true)"
if [ "$published" = "$version" ]; then
  echo "$name@$version is already on npm."
  exit 0
fi
npm publish --access public
