#!/usr/bin/env bash

set -Eeuo pipefail

: "${COLLECTOR_URL:?COLLECTOR_URL is required}"
: "${DEPLOYMENT_ENVIRONMENT:?DEPLOYMENT_ENVIRONMENT is required}"
: "${TELEMETRY_HF_QUALIFICATION_TOKEN:?TELEMETRY_HF_QUALIFICATION_TOKEN is required}"
: "${TELEMETRY_READ_TOKEN:?TELEMETRY_READ_TOKEN is required}"
: "${GITHUB_RUN_ID:?GITHUB_RUN_ID is required}"
: "${GITHUB_RUN_ATTEMPT:?GITHUB_RUN_ATTEMPT is required}"

case "$DEPLOYMENT_ENVIRONMENT" in
  staging | production) ;;
  *)
    echo "Unsupported deployment environment: ${DEPLOYMENT_ENVIRONMENT}" >&2
    exit 1
    ;;
esac

run_id="deployment-qualification-${DEPLOYMENT_ENVIRONMENT}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
producer_id="${run_id}-producer"
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
  --header "Authorization: Bearer ${TELEMETRY_HF_QUALIFICATION_TOKEN}" \
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
    country_code: "ZZ",
    pipeline: "deployment-qualification",
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
  --arg deployment_environment "$DEPLOYMENT_ENVIRONMENT" \
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
      message: ($deployment_environment + " deployment qualification completed."),
      details: {deployment_environment: $deployment_environment},
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
  --header "X-Telemetry-Read-Token: ${TELEMETRY_READ_TOKEN}" \
  --output "$run_response" \
  "${COLLECTOR_URL}/v1/runs/${run_id}"
jq -e \
  --arg run_id "$run_id" \
  --arg event_id "$event_id" \
  '.run_id == $run_id and (.events | any(.event_id == $event_id))' \
  "$run_response" > /dev/null

echo "${DEPLOYMENT_ENVIRONMENT} telemetry deployment qualification passed."
