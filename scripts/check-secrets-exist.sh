#!/usr/bin/env bash

set -Eeuo pipefail

: "${PROJECT_ID:?PROJECT_ID is required}"
if (( $# == 0 )); then
  echo "At least one secret name is required." >&2
  exit 1
fi

# Only inspect resource metadata. Never access, compare, or print secret values.
failed=0
for secret_name in "$@"; do
  if [[ -z "$secret_name" ]]; then
    echo "An empty secret name was supplied." >&2
    failed=1
  elif ! gcloud secrets describe "$secret_name" \
    --project "$PROJECT_ID" --format=none; then
    echo "Cannot confirm the existence of secret: ${secret_name}" >&2
    failed=1
  fi
done
exit "$failed"
