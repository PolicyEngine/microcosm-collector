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
  local names=(
    PROJECT_ID
    REGION
    ARTIFACT_REGISTRY_NAME
    CLOUD_SQL_CONNECTION
    RUNTIME_SERVICE_ACCOUNT
    MIGRATION_SERVICE_ACCOUNT
    SERVICE
    MIGRATION_JOB
    IMAGE_NAME
    DATABASE_SECRET_NAME
    MIGRATION_DATABASE_SECRET_NAME
    JWT_SECRET_NAME
    JWT_SIGNING_KEY_SECRET_NAME
    READ_SECRET_NAME
    DEPLOYMENT_ENVIRONMENT
    PRODUCTION_CLOUD_SQL_CONNECTION
    PRODUCTION_SERVICE
    INTAKE_BUCKET
    WORKER_QUEUE
    WORKER_JOB
    RECOVERY_JOB
    WORKER_SERVICE_ACCOUNT
    WORKER_INVOKER_SERVICE_ACCOUNT
    RECOVERY_SERVICE_ACCOUNT
    HF_READ_SECRET_NAME
    CALIBRATION_TREE_BLOB_SECRET_NAME
    ORRERY_BUCKET
  )

  require_variables "${names[@]}"
  local failed=0
  require_intake_resource_names || failed=1
  require_resource_name ORRERY_BUCKET "$BUCKET_NAME_PATTERN" || failed=1
  return "$failed"
}

# The helpers below also run inside command substitutions, where bash does not
# apply set -e, so each one returns as soon as a check fails.

# The collector's patterns for the intake settings (telemetry_collector.intake),
# applied to the names the deploy builds them from, so a malformed name stops
# the deploy before it authenticates rather than when a revision or a job
# that reads it starts. The Orrery bucket's name has the intake bucket's
# pattern. The deploy script's tests compare these patterns and their verdicts
# with the collector's.
BUCKET_NAME_PATTERN='^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$'
QUEUE_NAME_PATTERN='^[A-Za-z0-9-]{1,100}$'
JOB_NAME_PATTERN='^[a-z][a-z0-9-]{0,62}$'
SERVICE_ACCOUNT_PATTERN='^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9.-]+\.iam\.gserviceaccount\.com$'

require_resource_name() {
  local name="$1"
  local pattern="$2"

  if [[ ! "${!name}" =~ $pattern ]]; then
    echo "${name} is not a valid resource name." >&2
    return 1
  fi
}

require_intake_resource_names() {
  require_variables \
    INTAKE_BUCKET \
    WORKER_QUEUE \
    WORKER_JOB \
    RECOVERY_JOB \
    RUNTIME_SERVICE_ACCOUNT \
    WORKER_INVOKER_SERVICE_ACCOUNT || return 1

  local failed=0
  require_resource_name INTAKE_BUCKET "$BUCKET_NAME_PATTERN" || failed=1
  require_resource_name WORKER_QUEUE "$QUEUE_NAME_PATTERN" || failed=1
  require_resource_name WORKER_JOB "$JOB_NAME_PATTERN" || failed=1
  require_resource_name RECOVERY_JOB "$JOB_NAME_PATTERN" || failed=1
  require_resource_name RUNTIME_SERVICE_ACCOUNT "$SERVICE_ACCOUNT_PATTERN" || failed=1
  require_resource_name WORKER_INVOKER_SERVICE_ACCOUNT "$SERVICE_ACCOUNT_PATTERN" || failed=1
  return "$failed"
}

# Each environment's worker job reads Hugging Face with that environment's
# own read token: each token has its own secret, which only that
# environment's worker service account may read. Prints the secret's name.
worker_huggingface_secret() {
  require_variables DEPLOYMENT_ENVIRONMENT || return 1

  case "$DEPLOYMENT_ENVIRONMENT" in
    staging | production)
      require_variables HF_READ_SECRET_NAME || return 1
      printf '%s\n' "$HF_READ_SECRET_NAME"
      ;;
    *)
      echo "Unsupported deployment environment: ${DEPLOYMENT_ENVIRONMENT}" >&2
      return 1
      ;;
  esac
}

worker_queue_resource() {
  printf 'projects/%s/locations/%s/queues/%s\n' "$PROJECT_ID" "$REGION" "$WORKER_QUEUE"
}

worker_job_resource() {
  printf 'projects/%s/locations/%s/jobs/%s\n' "$PROJECT_ID" "$REGION" "$WORKER_JOB"
}

# The intake settings every service revision gets: intake is on in every
# environment. The service signs upload URLs as its own runtime account.
service_intake_settings() {
  require_variables \
    PROJECT_ID \
    REGION \
    RUNTIME_SERVICE_ACCOUNT \
    INTAKE_BUCKET \
    WORKER_QUEUE \
    WORKER_JOB \
    WORKER_INVOKER_SERVICE_ACCOUNT || return 1
  require_intake_resource_names || return 1

  local queue_resource_name
  local worker_job_resource_name
  queue_resource_name="$(worker_queue_resource)"
  worker_job_resource_name="$(worker_job_resource)"
  printf '%s,' \
    "COLLECTOR_INTAKE_ENABLED=1" \
    "COLLECTOR_INTAKE_BUCKET=${INTAKE_BUCKET}" \
    "COLLECTOR_INTAKE_SIGNING_SERVICE_ACCOUNT=${RUNTIME_SERVICE_ACCOUNT}" \
    "COLLECTOR_WORKER_QUEUE=${queue_resource_name}" \
    "COLLECTOR_WORKER_JOB=${worker_job_resource_name}"
  printf '%s\n' \
    "COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT=${WORKER_INVOKER_SERVICE_ACCOUNT}"
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

# Staging is called only by release checks and occasional manual requests, so
# it scales to zero between releases. Production keeps one instance running.
minimum_instances() {
  require_variables DEPLOYMENT_ENVIRONMENT

  case "$DEPLOYMENT_ENVIRONMENT" in
    staging) echo 0 ;;
    production) echo 1 ;;
    *)
      echo "Unsupported deployment environment: ${DEPLOYMENT_ENVIRONMENT}" >&2
      return 1
      ;;
  esac
}

check_secrets_exist() {
  require_variables \
    PROJECT_ID DATABASE_SECRET_NAME MIGRATION_DATABASE_SECRET_NAME \
    JWT_SECRET_NAME JWT_SIGNING_KEY_SECRET_NAME READ_SECRET_NAME \
    CALIBRATION_TREE_BLOB_SECRET_NAME

  local huggingface_secret
  huggingface_secret="$(worker_huggingface_secret)" || return 1

  bash "$(dirname "$0")/check-secrets-exist.sh" \
    "$DATABASE_SECRET_NAME" \
    "$MIGRATION_DATABASE_SECRET_NAME" \
    "$JWT_SECRET_NAME" \
    "$JWT_SIGNING_KEY_SECRET_NAME" \
    "$READ_SECRET_NAME" \
    "$huggingface_secret" \
    "$CALIBRATION_TREE_BLOB_SECRET_NAME"
}

# The worker and recovery jobs, and who may run them, are created by hand; a
# release only updates them. Before anything changes, confirm that each job
# exists, already runs as the account this deploy gives it, and connects to
# this environment's Cloud SQL instance, which validate-environment-target has
# tied to the environment. A variable naming the other environment's job, or a
# job on another account, stops the deploy before production goes into
# maintenance mode. To move a job to another account, update the job by hand
# first.
check_intake_jobs() {
  require_variables \
    PROJECT_ID \
    REGION \
    CLOUD_SQL_CONNECTION \
    WORKER_JOB \
    RECOVERY_JOB \
    WORKER_SERVICE_ACCOUNT \
    RECOVERY_SERVICE_ACCOUNT

  local failed=0
  check_intake_job "$WORKER_JOB" "$WORKER_SERVICE_ACCOUNT" || failed=1
  check_intake_job "$RECOVERY_JOB" "$RECOVERY_SERVICE_ACCOUNT" || failed=1
  return "$failed"
}

# Runs in an `||` list, where bash does not apply set -e, so each check returns.
check_intake_job() {
  local job="$1"
  local expected_account="$2"
  local account
  local connection
  local job_json

  if ! job_json="$(
    gcloud run jobs describe "$job" \
      --project "$PROJECT_ID" \
      --region "$REGION" \
      --format=json
  )"; then
    echo "Cannot confirm the existence of Cloud Run job: ${job}" >&2
    return 1
  fi
  if ! account="$(
    jq -r '.spec.template.spec.template.spec.serviceAccountName // ""' \
      <<< "$job_json" 2> /dev/null
  )" || ! connection="$(
    jq -r '.spec.template.metadata.annotations["run.googleapis.com/cloudsql-instances"] // ""' \
      <<< "$job_json" 2> /dev/null
  )"; then
    echo "Cannot read the description of Cloud Run job: ${job}" >&2
    return 1
  fi
  if [[ "$account" != "$expected_account" ]]; then
    echo "Cloud Run job ${job} runs as another service account than this deployment gives it." >&2
    return 1
  fi
  if [[ "$connection" != "$CLOUD_SQL_CONNECTION" ]]; then
    echo "Cloud Run job ${job} connects to another Cloud SQL instance than this environment's." >&2
    return 1
  fi
}

build_and_push_image() {
  require_variables \
    REGION \
    PROJECT_ID \
    ARTIFACT_REGISTRY_NAME \
    IMAGE_NAME \
    GITHUB_SHA \
    GITHUB_OUTPUT

  local image
  image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REGISTRY_NAME}/${IMAGE_NAME}:${GITHUB_SHA}"
  docker build \
    --file Dockerfile \
    --tag "$image" \
    .
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

# The worker job runs one submission job per execution, started by a Cloud
# Task that sets COLLECTOR_WORKER_JOB_ID. Two task retries give the three
# attempts the worker counts (MAX_JOB_ATTEMPTS), each within the worker's
# one-hour task timeout (WORKER_TASK_TIMEOUT_SECONDS). --set-secrets replaces
# every mount, so it lists the database, the environment's Hugging Face read
# token and its calibration tree Blob store's read-write token together. Only
# the worker job mounts the Blob token. 8 GiB fits a calibration tree build:
# the largest current diagnostics file (128.7 MB) peaked at 3.5 GB, and Cloud
# Run keeps the job's files in memory too. Above 4 GiB Cloud Run needs at least
# 2 CPUs. An execution is billed only while it runs. `update` never creates a job: the job and the
# invoker's permission to run it are created by hand. Only the worker job gets
# the private Orrery bucket, which it publishes Orrery graph files to; the
# bucket and the worker account's access to it are created by hand too.
configure_worker_job() {
  require_variables \
    WORKER_JOB \
    PROJECT_ID \
    REGION \
    IMAGE_URI \
    WORKER_SERVICE_ACCOUNT \
    CLOUD_SQL_CONNECTION \
    DATABASE_SECRET_NAME \
    INTAKE_BUCKET \
    ORRERY_BUCKET
  require_resource_name INTAKE_BUCKET "$BUCKET_NAME_PATTERN"
  require_resource_name ORRERY_BUCKET "$BUCKET_NAME_PATTERN"

  local huggingface_secret
  huggingface_secret="$(worker_huggingface_secret)" || return 1
  require_variables CALIBRATION_TREE_BLOB_SECRET_NAME || return 1
  local secrets="DATABASE_URL=${DATABASE_SECRET_NAME}:latest"
  secrets+=",COLLECTOR_HF_READ_TOKEN=${huggingface_secret}:latest"
  secrets+=",COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN=${CALIBRATION_TREE_BLOB_SECRET_NAME}:latest"

  gcloud run jobs update "$WORKER_JOB" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$IMAGE_URI" \
    --service-account "$WORKER_SERVICE_ACCOUNT" \
    --set-cloudsql-instances "$CLOUD_SQL_CONNECTION" \
    --set-secrets "$secrets" \
    --set-env-vars "COLLECTOR_INTAKE_BUCKET=${INTAKE_BUCKET},COLLECTOR_ORRERY_BUCKET=${ORRERY_BUCKET}" \
    --command uv \
    --args "run,--no-sync,python,-m,telemetry_collector.worker" \
    --tasks 1 \
    --max-retries 2 \
    --task-timeout 1h \
    --cpu 2 \
    --memory 8Gi \
    --quiet
}

# Cloud Scheduler runs the recovery job every 10 minutes; the schedule and its
# permission to run the job are created by hand, and a release changes neither.
# Recovery dispatches through the worker queue as the invoker account.
configure_recovery_job() {
  require_variables \
    RECOVERY_JOB \
    PROJECT_ID \
    REGION \
    IMAGE_URI \
    RECOVERY_SERVICE_ACCOUNT \
    CLOUD_SQL_CONNECTION \
    DATABASE_SECRET_NAME \
    WORKER_QUEUE \
    WORKER_JOB \
    WORKER_INVOKER_SERVICE_ACCOUNT
  require_resource_name WORKER_QUEUE "$QUEUE_NAME_PATTERN"
  require_resource_name WORKER_JOB "$JOB_NAME_PATTERN"
  require_resource_name WORKER_INVOKER_SERVICE_ACCOUNT "$SERVICE_ACCOUNT_PATTERN"

  local queue_resource_name
  local worker_job_resource_name
  queue_resource_name="$(worker_queue_resource)"
  worker_job_resource_name="$(worker_job_resource)"

  gcloud run jobs update "$RECOVERY_JOB" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --image "$IMAGE_URI" \
    --service-account "$RECOVERY_SERVICE_ACCOUNT" \
    --set-cloudsql-instances "$CLOUD_SQL_CONNECTION" \
    --set-secrets "DATABASE_URL=${DATABASE_SECRET_NAME}:latest" \
    --set-env-vars "COLLECTOR_WORKER_QUEUE=${queue_resource_name},COLLECTOR_WORKER_JOB=${worker_job_resource_name},COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT=${WORKER_INVOKER_SERVICE_ACCOUNT}" \
    --command uv \
    --args "run,--no-sync,python,-m,telemetry_collector.operations,recover" \
    --tasks 1 \
    --max-retries 0 \
    --task-timeout 10m \
    --cpu 1 \
    --memory 512Mi \
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
    JWT_SIGNING_KEY_SECRET_NAME \
    READ_SECRET_NAME \
    DEPLOYMENT_ENVIRONMENT

  local intake_settings
  local minimum_instance_count
  intake_settings="$(service_intake_settings)"
  minimum_instance_count="$(minimum_instances)"

  # The signing key signs new session tokens with ES256. The HS256 secret stays
  # mounted so tokens issued before the switch are accepted until it is retired.
  gcloud run deploy "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --platform managed \
    --image "$IMAGE_URI" \
    --service-account "$RUNTIME_SERVICE_ACCOUNT" \
    --add-cloudsql-instances "$CLOUD_SQL_CONNECTION" \
    --set-secrets "DATABASE_URL=${DATABASE_SECRET_NAME}:latest,TELEMETRY_JWT_SECRET=${JWT_SECRET_NAME}:latest,TELEMETRY_JWT_SIGNING_KEY=${JWT_SIGNING_KEY_SECRET_NAME}:latest,TELEMETRY_READ_TOKEN=${READ_SECRET_NAME}:latest" \
    --set-env-vars "TELEMETRY_MAINTENANCE_MODE=1,${intake_settings}" \
    --min "$minimum_instance_count" \
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
    JWT_SIGNING_KEY_SECRET_NAME \
    READ_SECRET_NAME \
    DEPLOYMENT_ENVIRONMENT

  local intake_settings
  local minimum_instance_count
  intake_settings="$(service_intake_settings)"
  minimum_instance_count="$(minimum_instances)"

  # The signing key signs new session tokens with ES256. The HS256 secret stays
  # mounted so tokens issued before the switch are accepted until it is retired.
  gcloud run deploy "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --platform managed \
    --image "$IMAGE_URI" \
    --service-account "$RUNTIME_SERVICE_ACCOUNT" \
    --add-cloudsql-instances "$CLOUD_SQL_CONNECTION" \
    --set-secrets "DATABASE_URL=${DATABASE_SECRET_NAME}:latest,TELEMETRY_JWT_SECRET=${JWT_SECRET_NAME}:latest,TELEMETRY_JWT_SIGNING_KEY=${JWT_SIGNING_KEY_SECRET_NAME}:latest,TELEMETRY_READ_TOKEN=${READ_SECRET_NAME}:latest" \
    --set-env-vars "TELEMETRY_MAINTENANCE_MODE=0,${intake_settings}" \
    --min "$minimum_instance_count" \
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
  check-secrets-exist
  check-intake-jobs
  build-and-push-image
  deploy-maintenance
  resolve-and-verify-maintenance
  wait-for-cutover
  configure-migration-job
  apply-migrations
  configure-worker-job
  configure-recovery-job
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
    check-secrets-exist) check_secrets_exist ;;
    check-intake-jobs) check_intake_jobs ;;
    build-and-push-image) build_and_push_image ;;
    deploy-maintenance) deploy_maintenance ;;
    resolve-and-verify-maintenance) resolve_and_verify_maintenance ;;
    wait-for-cutover) wait_for_cutover ;;
    configure-migration-job) configure_migration_job ;;
    apply-migrations) apply_migrations ;;
    configure-worker-job) configure_worker_job ;;
    configure-recovery-job) configure_recovery_job ;;
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
