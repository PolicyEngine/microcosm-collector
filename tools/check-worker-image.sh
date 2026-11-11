#!/usr/bin/env bash
set -euo pipefail

# Checks that a built collector image validates Orrery graph documents with its
# own Node and Orrery: it must accept a document the collector's projection
# wrote and refuse one Orrery refuses. Run from the repo root by ci.yml after
# the image build:
#
#   bash tools/check-worker-image.sh IMAGE
if (( $# != 1 )); then
  echo "Usage: $0 IMAGE" >&2
  exit 2
fi
image="$1"
validate=(uv run --no-sync python -m telemetry_collector.orrery.validation)
fixtures="$(pwd)/tests/fixtures/orrery_projection/toy-declarations-only"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# Orrery refuses a document without nodes or edges.
printf '{}' > "$work/refused.orrery.json"

accepted=0
docker run --rm --volume "$fixtures:/fixtures:ro" "$image" \
  "${validate[@]}" /fixtures/graph.orrery.json || accepted=$?
refused=0
docker run --rm --volume "$work:/work:ro" "$image" \
  "${validate[@]}" /work/refused.orrery.json || refused=$?

if (( accepted != 0 )); then
  echo "The image did not accept a projected document (exit $accepted)." >&2
  exit 1
fi
if (( refused != 2 )); then
  echo "The image did not refuse an invalid document (exit $refused, expected 2)." >&2
  exit 1
fi
echo "The image validates Orrery graph documents."
