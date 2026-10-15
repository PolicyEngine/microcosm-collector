#!/usr/bin/env bash

# Extract telemetry-service/ from PolicyEngine/calibration-diagnostics, with its
# history, into a new git directory whose root is the collector.
#
# Usage: tools/extract-from-calibration-diagnostics.sh OUTPUT_DIR RECORD_FILE
#
# OUTPUT_DIR must not exist. It receives a `main` branch holding every source
# commit that touched telemetry-service/, rewritten so that directory is the
# root. RECORD_FILE records the source commit and the extracted commit. When it
# already exists, its extracted commit must be an ancestor of the new `main`:
# git-filter-repo rewrites the same source commits to the same commits, so a
# re-run only adds the telemetry-service commits merged since, and they can be
# merged into this repo without duplicating its history.
#
# Environment:
#   EXTRACTION_SOURCE_URL  git URL or path of calibration-diagnostics
#                          (default: https://github.com/PolicyEngine/calibration-diagnostics.git)
#   EXTRACTION_SOURCE_REF  branch, tag or commit to extract (default: main)

set -Eeuo pipefail

readonly DEFAULT_SOURCE_URL="https://github.com/PolicyEngine/calibration-diagnostics.git"
readonly SOURCE_DIRECTORY="telemetry-service"
readonly FILTER_REPO_VERSION="2.47.0"

usage() {
  echo "Usage: extract-from-calibration-diagnostics.sh OUTPUT_DIR RECORD_FILE" >&2
}

record_value() {
  local record_file="$1"
  local key="$2"
  sed -n "s/^${key}=//p" "$record_file" | tail -n 1
}

main() {
  if (( $# != 2 )); then
    usage
    return 2
  fi

  local output_dir="$1"
  local record_file="$2"
  local source_url="${EXTRACTION_SOURCE_URL:-$DEFAULT_SOURCE_URL}"
  local source_ref="${EXTRACTION_SOURCE_REF:-main}"

  if [[ -e "$output_dir" ]]; then
    echo "Output directory already exists: ${output_dir}" >&2
    return 1
  fi

  local previous_extracted_commit=""
  if [[ -f "$record_file" ]]; then
    previous_extracted_commit="$(record_value "$record_file" extracted_commit)"
    if [[ -z "$previous_extracted_commit" ]]; then
      echo "The record ${record_file} has no extracted_commit." >&2
      return 1
    fi
  fi

  git init --quiet --initial-branch=main "$output_dir"
  git -C "$output_dir" fetch --quiet --no-tags "$source_url" "$source_ref"

  local source_commit
  local source_tree
  source_commit="$(git -C "$output_dir" rev-parse --verify 'FETCH_HEAD^{commit}')"
  if ! source_tree="$(git -C "$output_dir" rev-parse --verify --quiet \
    "${source_commit}:${SOURCE_DIRECTORY}")"; then
    echo "Source commit ${source_commit} has no ${SOURCE_DIRECTORY}/ directory." >&2
    return 1
  fi
  git -C "$output_dir" update-ref refs/heads/main "$source_commit"

  (
    cd "$output_dir"
    uvx --quiet --from "git-filter-repo==${FILTER_REPO_VERSION}" git-filter-repo \
      --force --quiet --subdirectory-filter "$SOURCE_DIRECTORY"
  )

  local extracted_commit
  extracted_commit="$(git -C "$output_dir" rev-parse --verify 'refs/heads/main^{commit}')"
  if [[ "$(git -C "$output_dir" rev-parse 'refs/heads/main^{tree}')" != "$source_tree" ]]; then
    echo "The extracted tree differs from ${SOURCE_DIRECTORY}/ at ${source_commit}." >&2
    return 1
  fi

  if [[ -n "$previous_extracted_commit" ]]; then
    if ! git -C "$output_dir" merge-base --is-ancestor \
      "$previous_extracted_commit" "$extracted_commit" 2>/dev/null; then
      echo "The recorded extraction ${previous_extracted_commit} is not part of the" \
        "new history. The source history or the filter changed; do not merge it." >&2
      return 1
    fi
  fi

  local commit_count
  commit_count="$(git -C "$output_dir" rev-list --count "$extracted_commit")"
  mkdir -p "$(dirname "$record_file")"
  {
    echo "# Written by tools/extract-from-calibration-diagnostics.sh. Rerun it; do not edit."
    echo "source_url=${source_url}"
    echo "source_ref=${source_ref}"
    echo "source_commit=${source_commit}"
    echo "source_directory=${SOURCE_DIRECTORY}"
    echo "extracted_commit=${extracted_commit}"
    echo "filter=git-filter-repo ${FILTER_REPO_VERSION} --subdirectory-filter ${SOURCE_DIRECTORY}"
  } > "$record_file"

  echo "Extracted ${commit_count} commits from ${source_commit} into ${output_dir}."
  if [[ -n "$previous_extracted_commit" ]]; then
    echo "New since the last extraction:"
    git -C "$output_dir" log --oneline "${previous_extracted_commit}..${extracted_commit}"
  fi
}

main "$@"
