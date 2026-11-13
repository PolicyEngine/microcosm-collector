#!/usr/bin/env bash
set -euo pipefail

# Checks that a built collector image builds calibration trees with its own Bun
# and the tree package's production install: its tree worker must build a
# small staged run's tree, its publication command must start and, given no
# Blob token, refuse to run as a request error (exit 1), and so must its latest
# command, with its token message, which shows the command ships in the image.
# Run from the repo root by ci.yml after the image build:
#
#   bash tools/check-calibration-tree-image.sh IMAGE
if (( $# != 1 )); then
  echo "Usage: $0 IMAGE" >&2
  exit 2
fi
image="$1"
package=/app/packages/calibration-tree
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
printf '{"schema_version": 8, "targets": []}' > "$work/calibration_diagnostics.json"
cat > "$work/request.json" <<'JSON'
{
  "kind": "staging",
  "country": "uk",
  "runId": "image-check-run",
  "label": "image-check-run",
  "hfRepo": "policyengine/populace-uk-private",
  "hfCommitSha": "0123456789abcdef0123456789abcdef01234567",
  "createdAt": "2026-10-20T09:00:00Z",
  "artifacts": {
    "calibrationDiagnostics": {
      "path": "staged/image-check-run/calibration_diagnostics.json",
      "file": "/work/calibration_diagnostics.json"
    },
    "buildManifest": null,
    "releaseManifest": null,
    "demographics": null
  }
}
JSON

# The tree is written inside the container, so nothing is written to the
# read-only mount.
built=0
answer=$(docker run --rm --volume "$work:/work:ro" "$image" \
  bun run "$package/src/calibration-tree-worker.ts" /work/request.json /tmp/trees) || built=$?
published=0
docker run --rm "$image" \
  bun run "$package/src/calibration-tree-publication.ts" /work/publication.json || published=$?
latest=0
latest_refusal=$(docker run --rm "$image" \
  bun run "$package/src/calibration-tree-latest.ts" /work/latest.json 2>&1 >/dev/null) || latest=$?

if (( built != 0 )); then
  echo "The image did not build a calibration tree (exit $built)." >&2
  exit 1
fi
if ! printf '%s' "$answer" | python3 -c '
import json, sys
answer = json.loads(sys.stdin.read().strip().splitlines()[-1])
sys.exit(0 if answer["manifestEntry"]["kind"] == "staging" and len(answer["files"]) >= 2 else 1)
' 2>/dev/null; then
  echo "The image's tree worker did not answer with a staging tree." >&2
  exit 1
fi
if (( published != 1 )); then
  echo "The image's publication command did not refuse to run without a token (exit $published, expected 1)." >&2
  exit 1
fi
if (( latest != 1 )) || [[ "$latest_refusal" != *"CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN is required."* ]]; then
  echo "The image's latest command did not refuse to run without a token (exit $latest, expected 1 with its token message)." >&2
  exit 1
fi
echo "The image builds calibration trees."
