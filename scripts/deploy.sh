#!/usr/bin/env bash

set -Eeuo pipefail

require_variables() {
  local missing=()
  local name

  for name in "$@"; do
    if [[ -z "${!name-}" ]]; then
      missing+=("$name")
    fi
  done

  if (( ${#missing[@]} )); then
    echo "Missing telemetry deployment configuration: ${missing[*]}" >&2
    return 1
  fi
}

require_configuration() {
  require_variables \
    PROJECT_ID \
    REGION \
    ARTIFACT_REPOSITORY \
    CLOUD_SQL_CONNECTION \
    RUNTIME_SERVICE_ACCOUNT
}

build_and_push_image() {
  require_variables \
    REGION \
    PROJECT_ID \
    ARTIFACT_REPOSITORY \
    IMAGE_NAME \
    GITHUB_SHA \
    GITHUB_OUTPUT

  local image
  image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPOSITORY}/${IMAGE_NAME}:${GITHUB_SHA}"
  docker build \
    --file telemetry-service/Dockerfile \
    --tag "$image" \
    telemetry-service
  docker push "$image"
  printf 'uri=%s\n' "$image" >> "$GITHUB_OUTPUT"
}

capture_production_revision() {
  require_variables SERVICE PROJECT_ID REGION GITHUB_OUTPUT

  local revision
  local service_json
  service_json="$(
    gcloud run services describe "$SERVICE" \
      --project "$PROJECT_ID" \
      --region "$REGION" \
      --format=json
  )"
  revision="$(
    jq -er '
      [.status.traffic[]? | select((.percent // 0) == 100) | .revisionName]
      | if length == 1 then .[0]
        else error("expected exactly one production revision")
        end
    ' <<< "$service_json"
  )"
  printf 'revision=%s\n' "$revision" >> "$GITHUB_OUTPUT"
}

configure_migration_job() {
  require_variables \
    MIGRATION_JOB \
    PROJECT_ID \
    REGION \
    IMAGE_URI \
    RUNTIME_SERVICE_ACCOUNT \
    CLOUD_SQL_CONNECTION \
    DATABASE_SECRET

  gcloud run jobs deploy "$MIGRATION_JOB" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$IMAGE_URI" \
    --service-account "$RUNTIME_SERVICE_ACCOUNT" \
    --set-cloudsql-instances "$CLOUD_SQL_CONNECTION" \
    --set-secrets "DATABASE_URL=${DATABASE_SECRET}:latest" \
    --command uv \
    --args "run,--no-sync,python,-m,telemetry_collector.migrate" \
    --tasks 1 \
    --max-retries 0 \
    --task-timeout 10m \
    --quiet
}

apply_migrations() {
  require_variables MIGRATION_JOB PROJECT_ID REGION

  gcloud run jobs execute "$MIGRATION_JOB" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --wait \
    --quiet
}

deploy_candidate() {
  require_variables \
    SERVICE \
    PROJECT_ID \
    REGION \
    IMAGE_URI \
    RUNTIME_SERVICE_ACCOUNT \
    CLOUD_SQL_CONNECTION \
    DATABASE_SECRET \
    JWT_SECRET \
    READ_SECRET

  gcloud run deploy "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --platform managed \
    --image "$IMAGE_URI" \
    --service-account "$RUNTIME_SERVICE_ACCOUNT" \
    --add-cloudsql-instances "$CLOUD_SQL_CONNECTION" \
    --set-secrets "DATABASE_URL=${DATABASE_SECRET}:latest,TELEMETRY_JWT_SECRET=${JWT_SECRET}:latest,TELEMETRY_READ_TOKEN=${READ_SECRET}:latest" \
    --min 1 \
    --max 4 \
    --cpu 1 \
    --memory 512Mi \
    --concurrency 40 \
    --timeout 30 \
    --allow-unauthenticated \
    --default-url \
    --startup-probe "httpGet.path=/health,httpGet.port=8080,timeoutSeconds=10,periodSeconds=10,failureThreshold=12" \
    --no-traffic \
    --tag candidate \
    --quiet
}

resolve_and_verify_candidate() {
  require_variables SERVICE PROJECT_ID REGION GITHUB_OUTPUT

  local revision
  local service_json
  local url
  service_json="$(
    gcloud run services describe "$SERVICE" \
      --project "$PROJECT_ID" \
      --region "$REGION" \
      --format=json
  )"
  revision="$(jq -er '.status.latestCreatedRevisionName' <<< "$service_json")"
  url="$(
    jq -er '
      .status.traffic[]
      | select(.tag == "candidate")
      | .url
    ' <<< "$service_json"
  )"
  curl --fail --show-error --silent \
    --retry 12 --retry-all-errors --retry-delay 5 \
    "${url}/ready"
  {
    printf 'revision=%s\n' "$revision"
    printf 'url=%s\n' "$url"
  } >> "$GITHUB_OUTPUT"
}

route_production_traffic() {
  require_variables SERVICE PROJECT_ID REGION REVISION

  # REVISION is a required workflow environment variable, not the local
  # revision used by other commands in this script.
  # shellcheck disable=SC2153
  gcloud run services update-traffic "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --to-revisions "${REVISION}=100" \
    --quiet
}

verify_stable_service() {
  require_variables SERVICE PROJECT_ID REGION

  local stable_url
  stable_url="$(
    gcloud run services describe "$SERVICE" \
      --project "$PROJECT_ID" \
      --region "$REGION" \
      --format='value(status.url)'
  )"
  curl --fail --show-error --silent \
    --retry 12 --retry-all-errors --retry-delay 5 \
    "${stable_url}/ready"
}

usage() {
  cat >&2 <<'EOF'
Usage: deploy.sh COMMAND

Commands:
  require-configuration
  build-and-push-image
  capture-production-revision
  configure-migration-job
  apply-migrations
  deploy-candidate
  resolve-and-verify-candidate
  route-production-traffic
  verify-stable-service
EOF
}

main() {
  if (( $# != 1 )); then
    usage
    return 2
  fi

  case "$1" in
    require-configuration) require_configuration ;;
    build-and-push-image) build_and_push_image ;;
    capture-production-revision) capture_production_revision ;;
    configure-migration-job) configure_migration_job ;;
    apply-migrations) apply_migrations ;;
    deploy-candidate) deploy_candidate ;;
    resolve-and-verify-candidate) resolve_and_verify_candidate ;;
    route-production-traffic) route_production_traffic ;;
    verify-stable-service) verify_stable_service ;;
    *)
      echo "Unknown deployment command: $1" >&2
      usage
      return 2
      ;;
  esac
}

main "$@"
