#!/usr/bin/env bash

set -Eeuo pipefail

: "${COLLECTOR_URL:?COLLECTOR_URL is required}"
: "${HF_TOKEN:?HF_TOKEN is required}"
: "${STAGING_READ_TOKEN:?STAGING_READ_TOKEN is required}"
: "${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
: "${GITHUB_RUN_ATTEMPT:?GITHUB_RUN_ATTEMPT is required}"

run_id="staging-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
producer_id="qualification-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
event_id="$producer_id"
timestamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
temporary_directory="$(mktemp -d)"

cleanup() {
  rm -rf "$temporary_directory"
}
trap cleanup EXIT

exchange_response="${temporary_directory}/exchange.json"
registration_payload="${temporary_directory}/registration.json"
event_payload="${temporary_directory}/event.json"
run_response="${temporary_directory}/run.json"

curl --fail --show-error --silent \
  --request POST \
  --header "Authorization: Bearer ${HF_TOKEN}" \
  --header "Content-Type: application/json" \
  --data '{}' \
  --output "$exchange_response" \
  "${COLLECTOR_URL}/v1/auth/huggingface/exchange"
session_token="$(jq -er '.access_token' "$exchange_response")"

jq -n \
  --arg run_id "$run_id" \
  --arg producer_id "$producer_id" \
  '{
    run_id: $run_id,
    producer_id: $producer_id,
    country_code: "US",
    pipeline: "staging-qualification",
    candidate_id: null,
    release_id: null,
    run_kind: "qualification"
  }' > "$registration_payload"
curl --fail --show-error --silent \
  --request POST \
  --header "Authorization: Bearer ${session_token}" \
  --header "Content-Type: application/json" \
  --data-binary "@${registration_payload}" \
  --output /dev/null \
  "${COLLECTOR_URL}/v1/runs"

jq -n \
  --arg event_id "$event_id" \
  --arg run_id "$run_id" \
  --arg producer_id "$producer_id" \
  --arg timestamp "$timestamp" \
  '{
    events: [{
      schema_version: 1,
      event_id: $event_id,
      run_id: $run_id,
      producer_id: $producer_id,
      sequence: 1,
      timestamp: $timestamp,
      event_type: "run",
      stage_id: "complete",
      status: "completed",
      message: "Staging qualification completed.",
      details: {},
      resources: null
    }]
  }' > "$event_payload"
curl --fail --show-error --silent \
  --request POST \
  --header "Authorization: Bearer ${session_token}" \
  --header "Content-Type: application/json" \
  --data-binary "@${event_payload}" \
  --output /dev/null \
  "${COLLECTOR_URL}/v1/runs/${run_id}/events"

curl --fail --show-error --silent \
  --header "X-Telemetry-Read-Token: ${STAGING_READ_TOKEN}" \
  --output "$run_response" \
  "${COLLECTOR_URL}/v1/runs/${run_id}"
jq -e \
  --arg run_id "$run_id" \
  --arg event_id "$event_id" \
  '.run_id == $run_id and (.events | any(.event_id == $event_id))' \
  "$run_response" > /dev/null

echo "Staging telemetry qualification passed."
