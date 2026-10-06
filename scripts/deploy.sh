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
    RUNTIME_SERVICE_ACCOUNT \
    MIGRATION_SERVICE_ACCOUNT \
    SERVICE \
    MIGRATION_JOB \
    IMAGE_NAME \
    DATABASE_SECRET_NAME \
    MIGRATION_DATABASE_SECRET_NAME \
    JWT_SECRET_NAME \
    READ_SECRET_NAME \
    DEPLOYMENT_ENVIRONMENT \
    PRODUCTION_CLOUD_SQL_CONNECTION \
    PRODUCTION_SERVICE
}

validate_environment_target() {
  require_variables \
    DEPLOYMENT_ENVIRONMENT \
    CLOUD_SQL_CONNECTION \
    PRODUCTION_CLOUD_SQL_CONNECTION \
    SERVICE \
    PRODUCTION_SERVICE

  case "$DEPLOYMENT_ENVIRONMENT" in
    staging)
      if [[ "$CLOUD_SQL_CONNECTION" == "$PRODUCTION_CLOUD_SQL_CONNECTION" ]]; then
        echo "Staging Cloud SQL target matches production." >&2
        return 1
      fi
      if [[ "$SERVICE" == "$PRODUCTION_SERVICE" ]]; then
        echo "Staging Cloud Run service matches production." >&2
        return 1
      fi
      ;;
    production)
      if [[ "$CLOUD_SQL_CONNECTION" != "$PRODUCTION_CLOUD_SQL_CONNECTION" ]]; then
        echo "Production Cloud SQL target does not match its declared resource." >&2
        return 1
      fi
      if [[ "$SERVICE" != "$PRODUCTION_SERVICE" ]]; then
        echo "Production Cloud Run service does not match its declared resource." >&2
        return 1
      fi
      ;;
    *)
      echo "Unsupported deployment environment: ${DEPLOYMENT_ENVIRONMENT}" >&2
      return 1
      ;;
  esac
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

configure_migration_job() {
  require_variables \
    MIGRATION_JOB \
    PROJECT_ID \
    REGION \
    IMAGE_URI \
    MIGRATION_SERVICE_ACCOUNT \
    CLOUD_SQL_CONNECTION \
    MIGRATION_DATABASE_SECRET_NAME

  gcloud run jobs deploy "$MIGRATION_JOB" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$IMAGE_URI" \
    --service-account "$MIGRATION_SERVICE_ACCOUNT" \
    --set-cloudsql-instances "$CLOUD_SQL_CONNECTION" \
    --set-secrets "DATABASE_URL=${MIGRATION_DATABASE_SECRET_NAME}:latest" \
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

create_database_backup() {
  require_variables \
    PROJECT_ID \
    CLOUD_SQL_CONNECTION \
    GITHUB_RUN_ID \
    GITHUB_RUN_ATTEMPT

  local connection_project
  local connection_region
  local instance
  local remainder
  IFS=: read -r connection_project connection_region instance remainder \
    <<< "$CLOUD_SQL_CONNECTION"
  if [[ -z "$connection_project" || -z "$connection_region" || -z "$instance" || -n "${remainder-}" ]]; then
    echo "CLOUD_SQL_CONNECTION must be project:region:instance." >&2
    return 1
  fi
  if [[ "$connection_project" != "$PROJECT_ID" ]]; then
    echo "Cloud SQL connection project does not match PROJECT_ID." >&2
    return 1
  fi

  gcloud sql backups create \
    --project "$PROJECT_ID" \
    --instance "$instance" \
    --description "telemetry-release-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" \
    --quiet
}

deploy_maintenance() {
  require_variables \
    SERVICE \
    PROJECT_ID \
    REGION \
    IMAGE_URI \
    RUNTIME_SERVICE_ACCOUNT \
    CLOUD_SQL_CONNECTION \
    DATABASE_SECRET_NAME \
    JWT_SECRET_NAME \
    READ_SECRET_NAME

  gcloud run deploy "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --platform managed \
    --image "$IMAGE_URI" \
    --service-account "$RUNTIME_SERVICE_ACCOUNT" \
    --add-cloudsql-instances "$CLOUD_SQL_CONNECTION" \
    --set-secrets "DATABASE_URL=${DATABASE_SECRET_NAME}:latest,TELEMETRY_JWT_SECRET=${JWT_SECRET_NAME}:latest,TELEMETRY_READ_TOKEN=${READ_SECRET_NAME}:latest" \
    --set-env-vars "TELEMETRY_MAINTENANCE_MODE=1" \
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
    --tag maintenance \
    --quiet
}

resolve_and_verify_maintenance() {
  require_variables SERVICE PROJECT_ID REGION GITHUB_OUTPUT

  local revision
  local service_json
  local status_code
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
      | select(.tag == "maintenance")
      | .url
    ' <<< "$service_json"
  )"
  curl --fail --show-error --silent \
    --retry 12 --retry-all-errors --retry-delay 5 \
    "${url}/health"
  status_code="$(
    curl --show-error --silent \
      --output /dev/null \
      --write-out '%{http_code}' \
      --request POST \
      "${url}/v1/auth/huggingface/exchange"
  )"
  if [[ "$status_code" != "503" ]]; then
    echo "Maintenance revision accepted a data request with HTTP ${status_code}." >&2
    return 1
  fi
  printf 'revision=%s\n' "$revision" >> "$GITHUB_OUTPUT"
}

wait_for_cutover() {
  sleep 35
}

deploy_candidate() {
  require_variables \
    SERVICE \
    PROJECT_ID \
    REGION \
    IMAGE_URI \
    RUNTIME_SERVICE_ACCOUNT \
    CLOUD_SQL_CONNECTION \
    DATABASE_SECRET_NAME \
    JWT_SECRET_NAME \
    READ_SECRET_NAME

  gcloud run deploy "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --platform managed \
    --image "$IMAGE_URI" \
    --service-account "$RUNTIME_SERVICE_ACCOUNT" \
    --add-cloudsql-instances "$CLOUD_SQL_CONNECTION" \
    --set-secrets "DATABASE_URL=${DATABASE_SECRET_NAME}:latest,TELEMETRY_JWT_SECRET=${JWT_SECRET_NAME}:latest,TELEMETRY_READ_TOKEN=${READ_SECRET_NAME}:latest" \
    --set-env-vars "TELEMETRY_MAINTENANCE_MODE=0" \
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

route_service_traffic() {
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
  validate-environment-target
  build-and-push-image
  deploy-maintenance
  resolve-and-verify-maintenance
  wait-for-cutover
  configure-migration-job
  apply-migrations
  create-database-backup
  deploy-candidate
  resolve-and-verify-candidate
  route-service-traffic
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
    validate-environment-target) validate_environment_target ;;
    build-and-push-image) build_and_push_image ;;
    deploy-maintenance) deploy_maintenance ;;
    resolve-and-verify-maintenance) resolve_and_verify_maintenance ;;
    wait-for-cutover) wait_for_cutover ;;
    configure-migration-job) configure_migration_job ;;
    apply-migrations) apply_migrations ;;
    create-database-backup) create_database_backup ;;
    deploy-candidate) deploy_candidate ;;
    resolve-and-verify-candidate) resolve_and_verify_candidate ;;
    route-service-traffic) route_service_traffic ;;
    verify-stable-service) verify_stable_service ;;
    *)
      echo "Unknown deployment command: $1" >&2
      usage
      return 2
      ;;
  esac
}

main "$@"
