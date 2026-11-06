#!/usr/bin/env bash
set -euo pipefail

# Publish the packed read API client to npm, unless its version is already
# there. push.yaml's publish-client job runs this after production deployed the
# release whose read API the client describes, on the tarball pack-client made
# with `npm pack`. It installs nothing and runs no package script, because the
# job can request an OIDC token. npm accepts that token instead of a stored npm
# token (trusted publishing, which needs npm 11.5.1 or later) and attaches a
# provenance attestation.
#
#   publish-client.sh TARBALL_DIRECTORY [COLLECTOR_ROOT]
tarball_directory="$1"
root="${2:-$(cd "$(dirname "$0")/.." && pwd)}"

shopt -s nullglob
tarballs=("$tarball_directory"/*.tgz)
shopt -u nullglob
if [ "${#tarballs[@]}" -ne 1 ]; then
  echo "Expected exactly one packed client in $tarball_directory, found ${#tarballs[@]}." >&2
  exit 1
fi
tarball="${tarballs[0]}"

manifest_field() {
  python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}
name="$(manifest_field name < "$root/packages/collector-client/package.json")"
release="$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$root/pyproject.toml")"
packed_name="$(tar -xOzf "$tarball" package/package.json | manifest_field name)"
packed_version="$(tar -xOzf "$tarball" package/package.json | manifest_field version)"
if [ "$packed_name" != "$name" ]; then
  echo "The packed client is $packed_name, not $name." >&2
  exit 1
fi
if [ "$packed_version" != "$release" ]; then
  echo "The client's version $packed_version is not the release's version $release." >&2
  exit 1
fi
# npm applies a packed manifest's publishConfig, such as another registry or no
# provenance, over its own settings. The checked-in manifest has none.
if tar -xOzf "$tarball" package/package.json | python3 -c 'import json, sys; sys.exit("publishConfig" not in json.load(sys.stdin))'; then
  echo "The packed client's manifest sets publishConfig; it must not." >&2
  exit 1
fi

npm_version="$(npm --version)"
if ! python3 -c 'import sys; sys.exit(tuple(map(int, sys.argv[1].split("."))) < (11, 5, 1))' "$npm_version"; then
  echo "npm $npm_version cannot publish with trusted publishing; it needs 11.5.1 or later." >&2
  exit 1
fi

# `npm view` exits 1 both for a package that does not exist yet and for a
# version it lacks. A network failure also leaves this empty; `npm publish`
# then fails on its own, or refuses a version that is already there.
published="$(npm view "$name@$release" version 2>/dev/null || true)"
if [ "$published" = "$release" ]; then
  echo "$name@$release is already on npm."
  exit 0
fi
npm publish "$tarball" --access public --ignore-scripts
