import importlib
import importlib.util
import json
import os
import re
import subprocess
import urllib.parse
from pathlib import Path
from types import ModuleType

import pytest

from telemetry_collector.intake import (
    BUCKET_PATTERN,
    JOB_PATTERN,
    QUEUE_PATTERN,
    SERVICE_ACCOUNT_PATTERN,
    IntakeSettings,
    resource_setting,
)
from telemetry_collector.submissions import MAX_JOB_ATTEMPTS
from telemetry_collector.worker import WORKER_TASK_TIMEOUT_SECONDS

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy.sh"
# User-managed accounts, in the shape the collector's settings accept.
RUNTIME_ACCOUNT = "collector-runtime@example-project.iam.gserviceaccount.com"
WORKER_ACCOUNT = "collector-worker@example-project.iam.gserviceaccount.com"
INVOKER_ACCOUNT = "collector-invoker@example-project.iam.gserviceaccount.com"
RECOVERY_ACCOUNT = "collector-recovery@example-project.iam.gserviceaccount.com"
# What the deploy reads to configure intake, in both environments.
DEPLOY_INTAKE_SETTINGS = {
    "INTAKE_BUCKET": "intake-bucket",
    "WORKER_QUEUE": "worker-queue",
    "WORKER_JOB": "worker-job",
    "RECOVERY_JOB": "recovery-job",
    "WORKER_SERVICE_ACCOUNT": WORKER_ACCOUNT,
    "WORKER_INVOKER_SERVICE_ACCOUNT": INVOKER_ACCOUNT,
    "RECOVERY_SERVICE_ACCOUNT": RECOVERY_ACCOUNT,
}
# Each environment's worker job mounts its own Hugging Face read token.
HUGGINGFACE_SETTINGS = {"HF_READ_SECRET_NAME": "hugging-face-read-token"}
# ...and its own calibration tree Blob store's read-write token.
CALIBRATION_TREE_SETTINGS = {
    "CALIBRATION_TREE_BLOB_SECRET_NAME": "calibration-tree-blob-token"
}
# ...and its own private Orrery bucket, which the worker job publishes Orrery
# graph files to and the Orrery read service reads them back from.
ORRERY_SETTINGS = {"ORRERY_BUCKET": "orrery-bucket"}
# The Google service account of microcosm-runs' deployments for each
# environment, the only member of the Orrery read service's IAM policy.
READER_ACCOUNT = "microcosm-runs-reader@example-project.iam.gserviceaccount.com"
ORRERY_READER_SETTINGS = {"ORRERY_READER_SERVICE_ACCOUNT": READER_ACCOUNT}
# The Vercel projects and environments whose Vercel OIDC tokens read API v2
# accepts, with the team they belong to: optional, all three or none.
# Placeholders, not the real team's or projects' values.
VERCEL_OIDC_SETTINGS = {
    "VERCEL_OIDC_TEAM_SLUG": "example-team",
    "VERCEL_OIDC_TEAM_ID": "team_example",
    "VERCEL_OIDC_READERS": "prj_dashboard:preview prj_dashboard:production",
}
# What each becomes on the service revisions.
COLLECTOR_VERCEL_OIDC_SETTING_NAMES = {
    "VERCEL_OIDC_TEAM_SLUG": "TELEMETRY_VERCEL_OIDC_TEAM_SLUG",
    "VERCEL_OIDC_TEAM_ID": "TELEMETRY_VERCEL_OIDC_TEAM_ID",
    "VERCEL_OIDC_READERS": "TELEMETRY_VERCEL_OIDC_READERS",
}
VERCEL_OIDC_MODULE = "telemetry_collector.vercel_oidc"
LOGGING_GCLOUD = (
    "printf 'gcloud' >> \"$COMMAND_LOG\"\n"
    'printf \' <%s>\' "$@" >> "$COMMAND_LOG"\n'
    "printf '\\n' >> \"$COMMAND_LOG\""
)
# The Orrery read service: the environment's service name and this suffix.
READ_SERVICE = "telemetry-service-orrery"
# The read service's URL, as `gcloud run services describe` gives it
# (`status.url`). microcosm-runs requests its identity tokens for it.
READ_SERVICE_URL = "https://telemetry-service-orrery-stand-in.example"
# The call the deploy makes without a token, once the read service's IAM
# policy is set: a listing for a run ID in the shape the read service
# accepts, so that a service that let the call through would answer it.
UNAUTHENTICATED_CHECK_URL = (
    f"{READ_SERVICE_URL}/v1/orrery-graphs?run_id=deploy-access-check"
)
# Cloud Run refuses a longer service name.
CLOUD_RUN_SERVICE_NAME_LIMIT = 49
# What the read service's IAM policy grants, in the policy's `bindings`
# field: the Cloud Run Invoker role, to the reader account alone.
READER_INVOKER_GRANTS = [
    {"role": "roles/run.invoker", "members": [f"serviceAccount:{READER_ACCOUNT}"]}
]
# Logs each call as LOGGING_GCLOUD does. Keeps the policy that `run services
# set-iam-policy SERVICE FILE` sets in $IAM_POLICY, and answers `run services
# get-iam-policy` as Cloud Run does, with the kept policy and an etag, or with
# $IAM_POLICY_ANSWER where a test gives one. Answers `run services describe`
# with $READ_SERVICE_URL.
IAM_POLICY_GCLOUD = (
    LOGGING_GCLOUD
    + '\nif [[ "$1 $2 $3" == "run services set-iam-policy" ]]; then\n'
    + '  cp "$5" "$IAM_POLICY"\n'
    + "fi\n"
    + 'if [[ "$1 $2 $3" == "run services get-iam-policy" ]]; then\n'
    + '  if [[ -f "${IAM_POLICY_ANSWER-}" ]]; then\n'
    + '    cat "$IAM_POLICY_ANSWER"\n'
    + "  else\n"
    + '    jq \'. + {etag: "BwYexample=", version: 1}\' "$IAM_POLICY"\n'
    + "  fi\n"
    + "fi\n"
    + 'if [[ "$1 $2 $3" == "run services describe" ]]; then\n'
    + "  printf '%s\\n' \"$READ_SERVICE_URL\"\n"
    + "fi"
)
# Logs each call as LOGGING_GCLOUD does, and prints the HTTP status code
# $READ_SERVICE_STATUS, as curl's `--write-out '%{http_code}'` does for the
# read service's answer. Where $READ_SERVICE_STATUS is "unreachable", it
# fails as curl does when it cannot connect: code 000 and exit status 7.
UNAUTHENTICATED_CALL_CURL = (
    "printf 'curl' >> \"$COMMAND_LOG\"\n"
    'printf \' <%s>\' "$@" >> "$COMMAND_LOG"\n'
    "printf '\\n' >> \"$COMMAND_LOG\"\n"
    'if [[ "$READ_SERVICE_STATUS" == unreachable ]]; then\n'
    "  printf '000'\n"
    "  exit 7\n"
    "fi\n"
    "printf '%s' \"$READ_SERVICE_STATUS\""
)


def run_deploy(
    command: str,
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"]}
    env.update(environment or {})
    return subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), command],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )


def install_command_stub(directory: Path, name: str, body: str) -> None:
    command = directory / name
    command.write_text(f"#!/usr/bin/env bash\nset -Eeuo pipefail\n{body}\n")
    command.chmod(0o755)


def vercel_oidc() -> ModuleType:
    assert importlib.util.find_spec(VERCEL_OIDC_MODULE) is not None
    return importlib.import_module(VERCEL_OIDC_MODULE)


def test_deploy_script_has_valid_bash_syntax_and_is_executable() -> None:
    result = subprocess.run(
        ["bash", "-n", str(DEPLOY_SCRIPT)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert os.access(DEPLOY_SCRIPT, os.X_OK)


def test_require_configuration_lists_every_missing_variable() -> None:
    result = run_deploy("require-configuration")

    assert result.returncode == 1
    assert result.stderr == (
        "Missing telemetry deployment configuration: PROJECT_ID REGION "
        "ARTIFACT_REGISTRY_NAME CLOUD_SQL_CONNECTION RUNTIME_SERVICE_ACCOUNT "
        "MIGRATION_SERVICE_ACCOUNT SERVICE MIGRATION_JOB IMAGE_NAME "
        "DATABASE_SECRET_NAME MIGRATION_DATABASE_SECRET_NAME JWT_SECRET_NAME "
        "JWT_SIGNING_KEY_SECRET_NAME READ_SECRET_NAME DEPLOYMENT_ENVIRONMENT "
        "PRODUCTION_CLOUD_SQL_CONNECTION PRODUCTION_SERVICE "
        "INTAKE_BUCKET WORKER_QUEUE WORKER_JOB RECOVERY_JOB "
        "WORKER_SERVICE_ACCOUNT WORKER_INVOKER_SERVICE_ACCOUNT "
        "RECOVERY_SERVICE_ACCOUNT HF_READ_SECRET_NAME "
        "CALIBRATION_TREE_BLOB_SECRET_NAME ORRERY_BUCKET "
        "ORRERY_READER_SERVICE_ACCOUNT\n"
    )


def complete_configuration(deployment_environment: str) -> dict[str, str]:
    configuration = {
        "PROJECT_ID": "project",
        "REGION": "region",
        "ARTIFACT_REGISTRY_NAME": "container-images",
        "CLOUD_SQL_CONNECTION": "connection",
        "RUNTIME_SERVICE_ACCOUNT": RUNTIME_ACCOUNT,
        "MIGRATION_SERVICE_ACCOUNT": "migration-service-account",
        "SERVICE": "service",
        "MIGRATION_JOB": "migration-job",
        "IMAGE_NAME": "image",
        "DATABASE_SECRET_NAME": "database-secret",
        "MIGRATION_DATABASE_SECRET_NAME": "migration-database-secret",
        "JWT_SECRET_NAME": "jwt-secret",
        "JWT_SIGNING_KEY_SECRET_NAME": "jwt-signing-key-secret",
        "READ_SECRET_NAME": "read-secret",
        "DEPLOYMENT_ENVIRONMENT": deployment_environment,
        "PRODUCTION_CLOUD_SQL_CONNECTION": "project:region:production",
        "PRODUCTION_SERVICE": "production-service",
        **DEPLOY_INTAKE_SETTINGS,
        **HUGGINGFACE_SETTINGS,
        **CALIBRATION_TREE_SETTINGS,
        **ORRERY_SETTINGS,
        **ORRERY_READER_SETTINGS,
    }
    return configuration


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_require_configuration_accepts_complete_environment(
    deployment_environment: str,
) -> None:
    result = run_deploy(
        "require-configuration",
        environment=complete_configuration(deployment_environment),
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_require_configuration_requires_the_read_token_secret_in_both_environments(
    deployment_environment: str,
) -> None:
    configuration = complete_configuration(deployment_environment)
    del configuration["HF_READ_SECRET_NAME"]

    result = run_deploy("require-configuration", environment=configuration)

    assert result.returncode == 1
    assert result.stderr == (
        "Missing telemetry deployment configuration: HF_READ_SECRET_NAME\n"
    )


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_check_secrets_exist_includes_the_read_token_secret_in_both_environments(
    tmp_path: Path, deployment_environment: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)

    result = run_deploy(
        "check-secrets-exist",
        environment={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "COMMAND_LOG": str(command_log),
            "PROJECT_ID": "project",
            "DEPLOYMENT_ENVIRONMENT": deployment_environment,
            "DATABASE_SECRET_NAME": "database-url",
            "MIGRATION_DATABASE_SECRET_NAME": "migration-database-url",
            "JWT_SECRET_NAME": "jwt",
            "JWT_SIGNING_KEY_SECRET_NAME": "jwt-signing-key",
            "READ_SECRET_NAME": "read",
            **HUGGINGFACE_SETTINGS,
            **CALIBRATION_TREE_SETTINGS,
        },
    )

    assert result.returncode == 0, result.stderr
    assert command_log.read_text().splitlines() == [
        f"gcloud <secrets> <describe> <{secret}> <--project> <project> <--format=none>"
        for secret in [
            "database-url",
            "migration-database-url",
            "jwt",
            "jwt-signing-key",
            "read",
            "hugging-face-read-token",
            "calibration-tree-blob-token",
        ]
    ]


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_require_configuration_requires_the_calibration_tree_blob_secret(
    deployment_environment: str,
) -> None:
    configuration = complete_configuration(deployment_environment)
    del configuration["CALIBRATION_TREE_BLOB_SECRET_NAME"]

    result = run_deploy("require-configuration", environment=configuration)

    assert result.returncode == 1
    assert result.stderr == (
        "Missing telemetry deployment configuration: "
        "CALIBRATION_TREE_BLOB_SECRET_NAME\n"
    )


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_require_configuration_requires_the_orrery_bucket(
    deployment_environment: str,
) -> None:
    configuration = complete_configuration(deployment_environment)
    del configuration["ORRERY_BUCKET"]

    result = run_deploy("require-configuration", environment=configuration)

    assert result.returncode == 1
    assert result.stderr == (
        "Missing telemetry deployment configuration: ORRERY_BUCKET\n"
    )


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_require_configuration_requires_the_orrery_reader_account(
    deployment_environment: str,
) -> None:
    configuration = complete_configuration(deployment_environment)
    del configuration["ORRERY_READER_SERVICE_ACCOUNT"]

    result = run_deploy("require-configuration", environment=configuration)

    assert result.returncode == 1
    assert result.stderr == (
        "Missing telemetry deployment configuration: ORRERY_READER_SERVICE_ACCOUNT\n"
    )


def test_require_configuration_names_every_malformed_bucket() -> None:
    configuration = complete_configuration("production")
    configuration["INTAKE_BUCKET"] = "gs://intake-bucket"
    configuration["ORRERY_BUCKET"] = "gs://orrery-bucket"

    result = run_deploy("require-configuration", environment=configuration)

    assert result.returncode == 1
    assert result.stderr == (
        "INTAKE_BUCKET is not a valid resource name.\n"
        "ORRERY_BUCKET is not a valid resource name.\n"
    )


STAGING_CONNECTION = "example-project:region:staging-database"
PRODUCTION_CONNECTION = "example-project:region:production-database"
# Answers `gcloud run jobs describe JOB` with $JOB_DESCRIPTIONS/JOB.json, and
# fails as gcloud does for a job that has no description there.
DESCRIBING_GCLOUD = (
    LOGGING_GCLOUD
    + '\ndescription="$JOB_DESCRIPTIONS/$4.json"\n'
    + '[[ -f "$description" ]] || exit 1\n'
    + 'cat "$description"'
)


def write_job_description(
    directory: Path, job: str, *, account: str, connection: str
) -> None:
    """Save what `gcloud run jobs describe --format=json` shows of a job."""

    description = {
        "metadata": {"name": job},
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"run.googleapis.com/cloudsql-instances": connection}
                },
                "spec": {"template": {"spec": {"serviceAccountName": account}}},
            }
        },
    }
    directory.mkdir(exist_ok=True)
    (directory / f"{job}.json").write_text(json.dumps(description))


def job_check_environment(
    stub_directory: Path, command_log: Path, descriptions: Path
) -> dict[str, str]:
    return {
        "PATH": f"{stub_directory}:{os.environ['PATH']}",
        "COMMAND_LOG": str(command_log),
        "JOB_DESCRIPTIONS": str(descriptions),
        "PROJECT_ID": "project",
        "REGION": "region",
        "CLOUD_SQL_CONNECTION": STAGING_CONNECTION,
        **DEPLOY_INTAKE_SETTINGS,
    }


def test_check_intake_jobs_accepts_this_environments_jobs(tmp_path: Path) -> None:
    command_log = tmp_path / "commands.log"
    descriptions = tmp_path / "jobs"
    install_command_stub(tmp_path, "gcloud", DESCRIBING_GCLOUD)
    write_job_description(
        descriptions,
        "worker-job",
        account=WORKER_ACCOUNT,
        connection=STAGING_CONNECTION,
    )
    write_job_description(
        descriptions,
        "recovery-job",
        account=RECOVERY_ACCOUNT,
        connection=STAGING_CONNECTION,
    )

    result = run_deploy(
        "check-intake-jobs",
        environment=job_check_environment(tmp_path, command_log, descriptions),
    )

    assert result.returncode == 0, result.stderr
    assert command_log.read_text().splitlines() == [
        f"gcloud <run> <jobs> <describe> <{job}> <--project> <project> "
        "<--region> <region> <--format=json>"
        for job in ("worker-job", "recovery-job")
    ]


@pytest.mark.parametrize(
    "missing_jobs",
    [["worker-job"], ["recovery-job"], ["worker-job", "recovery-job"]],
)
def test_check_intake_jobs_names_each_missing_job(
    tmp_path: Path, missing_jobs: list[str]
) -> None:
    descriptions = tmp_path / "jobs"
    install_command_stub(tmp_path, "gcloud", DESCRIBING_GCLOUD)
    accounts = {"worker-job": WORKER_ACCOUNT, "recovery-job": RECOVERY_ACCOUNT}
    descriptions.mkdir()
    for job, account in accounts.items():
        if job not in missing_jobs:
            write_job_description(
                descriptions, job, account=account, connection=STAGING_CONNECTION
            )

    result = run_deploy(
        "check-intake-jobs",
        environment=job_check_environment(
            tmp_path, tmp_path / "commands.log", descriptions
        ),
    )

    assert result.returncode == 1
    assert result.stderr == "".join(
        f"Cannot confirm the existence of Cloud Run job: {job}\n"
        for job in missing_jobs
    )


@pytest.mark.parametrize(
    ("job", "account", "connection", "expected_error"),
    [
        # Staging's worker variable naming production's worker job.
        (
            "worker-job",
            "collector-worker-production@example-project.iam.gserviceaccount.com",
            PRODUCTION_CONNECTION,
            "Cloud Run job worker-job runs as another service account than "
            "this deployment gives it.\n",
        ),
        # A job on this environment's account but another environment's database.
        (
            "recovery-job",
            RECOVERY_ACCOUNT,
            PRODUCTION_CONNECTION,
            "Cloud Run job recovery-job connects to another Cloud SQL instance "
            "than this environment's.\n",
        ),
        # The deploy's account variable naming another account than the job's.
        (
            "recovery-job",
            "collector-other@example-project.iam.gserviceaccount.com",
            STAGING_CONNECTION,
            "Cloud Run job recovery-job runs as another service account than "
            "this deployment gives it.\n",
        ),
    ],
)
def test_check_intake_jobs_refuses_a_job_of_another_environment_or_account(
    tmp_path: Path, job: str, account: str, connection: str, expected_error: str
) -> None:
    command_log = tmp_path / "commands.log"
    descriptions = tmp_path / "jobs"
    install_command_stub(tmp_path, "gcloud", DESCRIBING_GCLOUD)
    expected_accounts = {"worker-job": WORKER_ACCOUNT, "recovery-job": RECOVERY_ACCOUNT}
    for described_job, expected_account in expected_accounts.items():
        if described_job == job:
            write_job_description(
                descriptions, described_job, account=account, connection=connection
            )
        else:
            write_job_description(
                descriptions,
                described_job,
                account=expected_account,
                connection=STAGING_CONNECTION,
            )

    result = run_deploy(
        "check-intake-jobs",
        environment=job_check_environment(tmp_path, command_log, descriptions),
    )

    assert result.returncode == 1
    assert result.stderr == expected_error


@pytest.mark.parametrize(
    ("description", "expected_error"),
    [
        (
            {"spec": {"template": {"metadata": {"annotations": {}}}}},
            "Cloud Run job worker-job runs as another service account than "
            "this deployment gives it.\n",
        ),
        (
            {
                "spec": {
                    "template": {
                        "spec": {
                            "template": {"spec": {"serviceAccountName": WORKER_ACCOUNT}}
                        }
                    }
                }
            },
            "Cloud Run job worker-job connects to another Cloud SQL instance "
            "than this environment's.\n",
        ),
        (
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "run.googleapis.com/cloudsql-instances": (
                                    f"{STAGING_CONNECTION},{PRODUCTION_CONNECTION}"
                                )
                            }
                        },
                        "spec": {
                            "template": {"spec": {"serviceAccountName": WORKER_ACCOUNT}}
                        },
                    }
                }
            },
            "Cloud Run job worker-job connects to another Cloud SQL instance "
            "than this environment's.\n",
        ),
        (
            "not a job description",
            "Cannot read the description of Cloud Run job: worker-job\n",
        ),
    ],
    ids=["no account", "no Cloud SQL instance", "two instances", "unreadable"],
)
def test_check_intake_jobs_refuses_a_description_it_cannot_match(
    tmp_path: Path, description: dict | str, expected_error: str
) -> None:
    descriptions = tmp_path / "jobs"
    install_command_stub(tmp_path, "gcloud", DESCRIBING_GCLOUD)
    write_job_description(
        descriptions,
        "recovery-job",
        account=RECOVERY_ACCOUNT,
        connection=STAGING_CONNECTION,
    )
    (descriptions / "worker-job.json").write_text(
        description if isinstance(description, str) else json.dumps(description)
    )

    result = run_deploy(
        "check-intake-jobs",
        environment=job_check_environment(
            tmp_path, tmp_path / "commands.log", descriptions
        ),
    )

    assert result.returncode == 1
    assert result.stderr == expected_error


PROJECT_RESOURCES = "projects/project/locations/region"
# Each name the deploy checks, the collector's pattern for the setting it
# becomes, and how the deploy turns the name into that setting.
COLLECTOR_SETTING_SHAPES = {
    "INTAKE_BUCKET": (BUCKET_PATTERN, "{}"),
    # The worker's COLLECTOR_ORRERY_BUCKET has the intake bucket's pattern.
    "ORRERY_BUCKET": (BUCKET_PATTERN, "{}"),
    "WORKER_QUEUE": (QUEUE_PATTERN, PROJECT_RESOURCES + "/queues/{}"),
    "WORKER_JOB": (JOB_PATTERN, PROJECT_RESOURCES + "/jobs/{}"),
    "RECOVERY_JOB": (JOB_PATTERN, PROJECT_RESOURCES + "/jobs/{}"),
    "RUNTIME_SERVICE_ACCOUNT": (SERVICE_ACCOUNT_PATTERN, "{}"),
    "WORKER_INVOKER_SERVICE_ACCOUNT": (SERVICE_ACCOUNT_PATTERN, "{}"),
    # No setting: the one member of the Orrery read service's IAM policy,
    # checked as the collector checks its user-managed accounts.
    "ORRERY_READER_SERVICE_ACCOUNT": (SERVICE_ACCOUNT_PATTERN, "{}"),
}


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("INTAKE_BUCKET", "collector-intake.example"),
        ("INTAKE_BUCKET", "Intake-Bucket"),
        ("INTAKE_BUCKET", "-intake-bucket"),
        ("INTAKE_BUCKET", "gs://intake-bucket"),
        ("ORRERY_BUCKET", "collector-orrery.example"),
        ("ORRERY_BUCKET", "Orrery-Bucket"),
        ("ORRERY_BUCKET", "gs://orrery-bucket"),
        ("ORRERY_BUCKET", "a" * 63),
        ("ORRERY_BUCKET", "a" * 64),
        ("WORKER_QUEUE", "Worker-Queue-2"),
        ("WORKER_QUEUE", "projects/project/locations/region/queues/worker-queue"),
        ("WORKER_QUEUE", "worker_queue"),
        ("WORKER_JOB", "collector-worker-2"),
        ("WORKER_JOB", "Worker-Job"),
        ("WORKER_JOB", "2-worker-job"),
        ("WORKER_JOB", "projects/project/locations/region/jobs/worker-job"),
        ("RECOVERY_JOB", "recovery_job"),
        ("RUNTIME_SERVICE_ACCOUNT", RUNTIME_ACCOUNT),
        ("RUNTIME_SERVICE_ACCOUNT", "runtime@example.invalid"),
        ("RUNTIME_SERVICE_ACCOUNT", "abc@example-project.iam.gserviceaccount.com"),
        (
            "WORKER_INVOKER_SERVICE_ACCOUNT",
            "123456789-compute@developer.gserviceaccount.com",
        ),
        (
            "WORKER_INVOKER_SERVICE_ACCOUNT",
            "Invoker-1@example-project.iam.gserviceaccount.com",
        ),
        ("ORRERY_READER_SERVICE_ACCOUNT", READER_ACCOUNT),
        ("ORRERY_READER_SERVICE_ACCOUNT", "microcosm-runs-reader"),
        (
            "ORRERY_READER_SERVICE_ACCOUNT",
            "123456789-compute@developer.gserviceaccount.com",
        ),
        # Each pattern's length bounds, on both sides.
        ("INTAKE_BUCKET", "abc"),
        ("INTAKE_BUCKET", "ab"),
        ("INTAKE_BUCKET", "a" * 63),
        ("INTAKE_BUCKET", "a" * 64),
        ("WORKER_QUEUE", "q"),
        ("WORKER_QUEUE", "q" * 100),
        ("WORKER_QUEUE", "q" * 101),
        ("WORKER_JOB", "w"),
        ("WORKER_JOB", "w" * 63),
        ("WORKER_JOB", "w" * 64),
        (
            "RUNTIME_SERVICE_ACCOUNT",
            "a" * 6 + "@example-project.iam.gserviceaccount.com",
        ),
        (
            "RUNTIME_SERVICE_ACCOUNT",
            "a" * 5 + "@example-project.iam.gserviceaccount.com",
        ),
        (
            "RUNTIME_SERVICE_ACCOUNT",
            "a" * 30 + "@example-project.iam.gserviceaccount.com",
        ),
        (
            "RUNTIME_SERVICE_ACCOUNT",
            "a" * 31 + "@example-project.iam.gserviceaccount.com",
        ),
        (
            "RUNTIME_SERVICE_ACCOUNT",
            "runtime-1-@example-project.iam.gserviceaccount.com",
        ),
    ],
)
def test_require_configuration_refuses_exactly_the_names_the_collector_refuses(
    variable: str, value: str
) -> None:
    pattern, setting_format = COLLECTOR_SETTING_SHAPES[variable]
    collector_accepts = re.fullmatch(pattern, setting_format.format(value)) is not None
    configuration = complete_configuration("production")
    configuration[variable] = value

    result = run_deploy("require-configuration", environment=configuration)

    if collector_accepts:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert result.stderr == f"{variable} is not a valid resource name.\n"


def deploy_script_pattern(name: str) -> str:
    match = re.search(rf"^{name}='([^']*)'$", DEPLOY_SCRIPT.read_text(), re.MULTILINE)
    assert match is not None, name
    return match.group(1)


def test_deploy_script_patterns_are_the_collectors() -> None:
    # The queue and job patterns name the last part of a resource name, which
    # is all the deploy reads; the other two are whole settings.
    assert deploy_script_pattern("BUCKET_NAME_PATTERN") == BUCKET_PATTERN
    assert deploy_script_pattern("SERVICE_ACCOUNT_PATTERN") == SERVICE_ACCOUNT_PATTERN
    queue_name = deploy_script_pattern("QUEUE_NAME_PATTERN")
    job_name = deploy_script_pattern("JOB_NAME_PATTERN")
    assert queue_name.startswith("^")
    assert QUEUE_PATTERN.endswith("/queues/" + queue_name.removeprefix("^"))
    assert job_name.startswith("^")
    assert JOB_PATTERN.endswith("/jobs/" + job_name.removeprefix("^"))


@pytest.mark.parametrize("with_vercel_oidc", [False, True])
@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_require_configuration_accepts_all_or_none_of_the_vercel_oidc_settings(
    deployment_environment: str, with_vercel_oidc: bool
) -> None:
    configuration = complete_configuration(deployment_environment)
    if with_vercel_oidc:
        configuration.update(VERCEL_OIDC_SETTINGS)

    result = run_deploy("require-configuration", environment=configuration)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "missing_variables",
    [
        ["VERCEL_OIDC_TEAM_SLUG"],
        ["VERCEL_OIDC_TEAM_ID"],
        ["VERCEL_OIDC_READERS"],
        ["VERCEL_OIDC_TEAM_SLUG", "VERCEL_OIDC_TEAM_ID"],
        ["VERCEL_OIDC_TEAM_ID", "VERCEL_OIDC_READERS"],
    ],
)
# Blank as the collector reads its settings: only the ASCII whitespace that
# Python's str.strip() removes.
@pytest.mark.parametrize("missing_value", [None, "", " ", "\r\n", "\t\v\f\r\x1f"])
def test_require_configuration_names_the_missing_vercel_oidc_settings(
    missing_variables: list[str], missing_value: str | None
) -> None:
    configuration = {**complete_configuration("production"), **VERCEL_OIDC_SETTINGS}
    for variable in missing_variables:
        if missing_value is None:
            del configuration[variable]
        else:
            configuration[variable] = missing_value

    result = run_deploy("require-configuration", environment=configuration)

    assert result.returncode == 1
    assert result.stderr == (
        f"Missing telemetry deployment configuration: {' '.join(missing_variables)}\n"
    )


VERCEL_OIDC_REFUSALS = {
    "VERCEL_OIDC_TEAM_SLUG": "VERCEL_OIDC_TEAM_SLUG is not a valid Vercel team slug.\n",
    "VERCEL_OIDC_TEAM_ID": "VERCEL_OIDC_TEAM_ID is not a valid Vercel team ID.\n",
    "VERCEL_OIDC_READERS": (
        "VERCEL_OIDC_READERS is not a whitespace-separated list of "
        "<project ID>:<environment> entries.\n"
    ),
}


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("VERCEL_OIDC_TEAM_SLUG", "e"),
        ("VERCEL_OIDC_TEAM_SLUG", "example-team-2"),
        ("VERCEL_OIDC_TEAM_SLUG", "e" * 100),
        ("VERCEL_OIDC_TEAM_SLUG", "e" * 101),
        ("VERCEL_OIDC_TEAM_SLUG", "Example-Team"),
        ("VERCEL_OIDC_TEAM_SLUG", "-example-team"),
        ("VERCEL_OIDC_TEAM_SLUG", "example-team-"),
        ("VERCEL_OIDC_TEAM_SLUG", "example_team"),
        ("VERCEL_OIDC_TEAM_SLUG", "https://vercel.com/example-team"),
        ("VERCEL_OIDC_TEAM_ID", "team_" + "e" * 64),
        ("VERCEL_OIDC_TEAM_ID", "team_" + "e" * 65),
        ("VERCEL_OIDC_TEAM_ID", "team_"),
        ("VERCEL_OIDC_TEAM_ID", "team_example-1"),
        ("VERCEL_OIDC_TEAM_ID", "example-team"),
        ("VERCEL_OIDC_READERS", "prj_dashboard:development"),
        ("VERCEL_OIDC_READERS", "prj_" + "d" * 64 + ":preview"),
        ("VERCEL_OIDC_READERS", "prj_" + "d" * 65 + ":preview"),
        ("VERCEL_OIDC_READERS", "  prj_dashboard:preview\n\tprj_other:production "),
        ("VERCEL_OIDC_READERS", "prj_dashboard:preview,prj_other:production"),
        ("VERCEL_OIDC_READERS", "prj_dashboard:preview prj_other"),
        ("VERCEL_OIDC_READERS", "prj_dashboard:staging"),
        ("VERCEL_OIDC_READERS", "prj_dashboard:Preview"),
        ("VERCEL_OIDC_READERS", "prj_dashboard:*"),
        ("VERCEL_OIDC_READERS", "*"),
        ("VERCEL_OIDC_READERS", "team_example:preview"),
        # The collector splits and trims on all the ASCII whitespace that
        # Python's str.split() and str.strip() use, not only spaces, tabs and
        # newlines: one reader per line with Windows line endings, carriage
        # returns, vertical tabs, form feeds and the unit separator.
        ("VERCEL_OIDC_READERS", "prj_dashboard:preview\r\nprj_other:production\r\n"),
        ("VERCEL_OIDC_READERS", "prj_dashboard:preview\r\n\r\nprj_other:production"),
        ("VERCEL_OIDC_READERS", "prj_dashboard:preview\rprj_other:production\r"),
        ("VERCEL_OIDC_READERS", "prj_dashboard:preview\vprj_other:production"),
        ("VERCEL_OIDC_READERS", "\fprj_dashboard:preview\fprj_other:production\f"),
        ("VERCEL_OIDC_READERS", "prj_dashboard:preview\x1fprj_other:production"),
        ("VERCEL_OIDC_READERS", "prj_dashboard:preview\r\nprj_other\r\n"),
        ("VERCEL_OIDC_TEAM_SLUG", "example-team\r\n"),
        ("VERCEL_OIDC_TEAM_SLUG", "\vexample-team\f"),
        ("VERCEL_OIDC_TEAM_SLUG", "example\rteam"),
        ("VERCEL_OIDC_TEAM_ID", "team_example\r"),
        ("VERCEL_OIDC_TEAM_ID", "\fteam_example\v\r\n"),
        ("VERCEL_OIDC_TEAM_ID", "team_\rexample"),
    ],
)
def test_require_configuration_refuses_exactly_the_vercel_oidc_settings_the_collector_refuses(
    variable: str, value: str
) -> None:
    collector_settings = {
        COLLECTOR_VERCEL_OIDC_SETTING_NAMES[name]: setting_value
        for name, setting_value in {**VERCEL_OIDC_SETTINGS, variable: value}.items()
    }
    try:
        vercel_oidc().VercelOidcSettings.from_environment(collector_settings)
    except RuntimeError:
        collector_accepts = False
    else:
        collector_accepts = True
    configuration = {
        **complete_configuration("production"),
        **VERCEL_OIDC_SETTINGS,
        variable: value,
    }

    result = run_deploy("require-configuration", environment=configuration)

    if collector_accepts:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert result.stderr == VERCEL_OIDC_REFUSALS[variable]


def test_deploy_script_vercel_oidc_patterns_are_the_collectors() -> None:
    module = vercel_oidc()

    assert deploy_script_pattern("VERCEL_OIDC_TEAM_SLUG_PATTERN") == (
        module.TEAM_SLUG_PATTERN
    )
    assert deploy_script_pattern("VERCEL_OIDC_TEAM_ID_PATTERN") == (
        module.TEAM_ID_PATTERN
    )
    assert deploy_script_pattern("VERCEL_OIDC_READER_PATTERN") == (
        module.READER_PATTERN
    )


@pytest.mark.parametrize(
    ("deploy_command", "variable", "value"),
    [
        ("deploy-maintenance", "WORKER_QUEUE", "worker_queue"),
        ("deploy-candidate", "INTAKE_BUCKET", "Intake-Bucket"),
        (
            "deploy-candidate",
            "WORKER_INVOKER_SERVICE_ACCOUNT",
            "invoker@example.invalid",
        ),
        ("deploy-orrery-read-service", "ORRERY_BUCKET", "gs://orrery-bucket"),
        (
            "deploy-orrery-read-service",
            "ORRERY_READER_SERVICE_ACCOUNT",
            "reader@example.invalid",
        ),
        ("configure-worker-job", "INTAKE_BUCKET", "gs://intake-bucket"),
        ("configure-worker-job", "ORRERY_BUCKET", "gs://orrery-bucket"),
        ("configure-recovery-job", "WORKER_QUEUE", PROJECT_RESOURCES + "/queues/q"),
        ("configure-recovery-job", "WORKER_JOB", "Worker-Job"),
        (
            "configure-recovery-job",
            "WORKER_INVOKER_SERVICE_ACCOUNT",
            "invoker@x.invalid",
        ),
    ],
)
def test_commands_that_write_intake_settings_check_the_names_again(
    tmp_path: Path, deploy_command: str, variable: str, value: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    if deploy_command.startswith("deploy-"):
        environment = service_deploy_environment(tmp_path, command_log, "production")
    else:
        environment = job_deploy_environment(tmp_path, command_log, "production")
    environment[variable] = value

    result = run_deploy(deploy_command, environment=environment)

    assert result.returncode == 1
    assert result.stderr == f"{variable} is not a valid resource name.\n"
    assert not command_log.exists()


def logged_setting_values(command: str) -> dict[str, str]:
    match = re.search(r"<--set-env-vars> <([^>]*)>", command)
    assert match is not None, command
    return dict(pair.split("=", 1) for pair in match.group(1).split(","))


def test_service_intake_settings_are_ones_the_collector_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)

    result = run_deploy(
        "deploy-candidate",
        environment=service_deploy_environment(tmp_path, command_log, "staging"),
    )

    assert result.returncode == 0, result.stderr
    for name, value in logged_setting_values(command_log.read_text()).items():
        monkeypatch.setenv(name, value)
    assert IntakeSettings.from_environment() == IntakeSettings(
        bucket="intake-bucket",
        signing_service_account=RUNTIME_ACCOUNT,
        worker_queue=f"{PROJECT_RESOURCES}/queues/worker-queue",
        worker_job=f"{PROJECT_RESOURCES}/jobs/worker-job",
        worker_invoker_service_account=INVOKER_ACCOUNT,
    )


def logged_secret_mounts(command: str) -> dict[str, str]:
    match = re.search(r"<--set-secrets> <([^>]*)>", command)
    assert match is not None, command
    return dict(pair.split("=", 1) for pair in match.group(1).split(","))


def read_service_deploy_line(command_log: Path) -> str:
    [deploy_line] = [
        line
        for line in command_log.read_text().splitlines()
        if line.startswith(f"gcloud <run> <deploy> <{READ_SERVICE}> ")
    ]
    return deploy_line


def test_the_read_service_settings_are_ones_the_read_service_accepts(
    tmp_path: Path,
) -> None:
    serving = importlib.import_module("telemetry_collector.orrery_serving")
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", IAM_POLICY_GCLOUD)

    result = run_deploy(
        "deploy-orrery-read-service",
        environment=read_service_deploy_environment(tmp_path, command_log, "staging"),
    )

    assert result.returncode == 0, result.stderr
    deploy_line = read_service_deploy_line(command_log)
    settings = logged_setting_values(deploy_line)
    # DATABASE_URL comes from the runtime database secret, as for the service.
    assert logged_secret_mounts(deploy_line) == {"DATABASE_URL": "database-url:latest"}
    assert settings == {"COLLECTOR_ORRERY_BUCKET": "orrery-bucket"}
    read_service_settings = serving.OrreryServingSettings.from_environment(
        {**settings, "DATABASE_URL": "postgresql://user@localhost/telemetry"}
    )
    assert read_service_settings.bucket == "orrery-bucket"


def test_recovery_job_settings_are_ones_the_operations_command_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)

    result = run_deploy(
        "configure-recovery-job",
        environment=job_deploy_environment(tmp_path, command_log, "production"),
    )

    assert result.returncode == 0, result.stderr
    for name, value in logged_setting_values(command_log.read_text()).items():
        monkeypatch.setenv(name, value)
    accepted = [
        resource_setting(name, pattern, required_by="recovery runs")
        for name, pattern in (
            ("COLLECTOR_WORKER_QUEUE", QUEUE_PATTERN),
            ("COLLECTOR_WORKER_JOB", JOB_PATTERN),
            ("COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT", SERVICE_ACCOUNT_PATTERN),
        )
    ]
    assert accepted == [
        f"{PROJECT_RESOURCES}/queues/worker-queue",
        f"{PROJECT_RESOURCES}/jobs/worker-job",
        INVOKER_ACCOUNT,
    ]


def test_build_and_push_image_records_the_immutable_uri(tmp_path: Path) -> None:
    command_log = tmp_path / "commands.log"
    github_output = tmp_path / "github-output"
    install_command_stub(
        tmp_path,
        "docker",
        "printf 'docker' >> \"$COMMAND_LOG\"\n"
        'printf \' <%s>\' "$@" >> "$COMMAND_LOG"\n'
        "printf '\\n' >> \"$COMMAND_LOG\"",
    )

    result = run_deploy(
        "build-and-push-image",
        environment={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "COMMAND_LOG": str(command_log),
            "GITHUB_OUTPUT": str(github_output),
            "GITHUB_SHA": "abc123",
            "REGION": "us-central1",
            "PROJECT_ID": "example-project",
            "ARTIFACT_REGISTRY_NAME": "containers",
            "IMAGE_NAME": "collector",
        },
    )

    image = "us-central1-docker.pkg.dev/example-project/containers/collector:abc123"
    assert result.returncode == 0, result.stderr
    assert command_log.read_text().splitlines() == [
        f"docker <build> <--file> <Dockerfile> <--tag> <{image}> <.>",
        f"docker <push> <{image}>",
    ]
    assert github_output.read_text() == f"uri={image}\n"


def test_route_service_traffic_passes_the_requested_revision(
    tmp_path: Path,
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(
        tmp_path,
        "gcloud",
        "printf 'gcloud' >> \"$COMMAND_LOG\"\n"
        'printf \' <%s>\' "$@" >> "$COMMAND_LOG"\n'
        "printf '\\n' >> \"$COMMAND_LOG\"",
    )

    result = run_deploy(
        "route-service-traffic",
        environment={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "COMMAND_LOG": str(command_log),
            "SERVICE": "microcosm-telemetry",
            "PROJECT_ID": "example-project",
            "REGION": "us-central1",
            "REVISION": "microcosm-telemetry-00042",
        },
    )

    assert result.returncode == 0, result.stderr
    assert command_log.read_text() == (
        "gcloud <run> <services> <update-traffic> <microcosm-telemetry> "
        "<--project> <example-project> <--region> <us-central1> "
        "<--to-revisions> <microcosm-telemetry-00042=100> <--quiet>\n"
    )


def test_staging_target_must_not_use_production_resources() -> None:
    result = run_deploy(
        "validate-environment-target",
        environment={
            "DEPLOYMENT_ENVIRONMENT": "staging",
            "CLOUD_SQL_CONNECTION": "project:region:production",
            "PRODUCTION_CLOUD_SQL_CONNECTION": "project:region:production",
            "SERVICE": "staging-service",
            "PRODUCTION_SERVICE": "production-service",
        },
    )

    assert result.returncode == 1
    assert "Staging Cloud SQL target matches production" in result.stderr


def test_production_target_must_match_declared_production_resources() -> None:
    result = run_deploy(
        "validate-environment-target",
        environment={
            "DEPLOYMENT_ENVIRONMENT": "production",
            "CLOUD_SQL_CONNECTION": "project:region:not-production",
            "PRODUCTION_CLOUD_SQL_CONNECTION": "project:region:production",
            "SERVICE": "production-service",
            "PRODUCTION_SERVICE": "production-service",
        },
    )

    assert result.returncode == 1
    assert "Production Cloud SQL target does not match" in result.stderr


def test_migration_job_uses_separate_identity_and_database_secret(
    tmp_path: Path,
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(
        tmp_path,
        "gcloud",
        "printf 'gcloud' >> \"$COMMAND_LOG\"\n"
        'printf \' <%s>\' "$@" >> "$COMMAND_LOG"\n'
        "printf '\\n' >> \"$COMMAND_LOG\"",
    )

    result = run_deploy(
        "configure-migration-job",
        environment={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "COMMAND_LOG": str(command_log),
            "MIGRATION_JOB": "migration-job",
            "PROJECT_ID": "project",
            "REGION": "region",
            "IMAGE_URI": "registry/image:sha",
            "MIGRATION_SERVICE_ACCOUNT": "migrator@example.invalid",
            "CLOUD_SQL_CONNECTION": "project:region:database",
            "MIGRATION_DATABASE_SECRET_NAME": "migration-database-url",
        },
    )

    assert result.returncode == 0, result.stderr
    command = command_log.read_text()
    assert "<--service-account> <migrator@example.invalid>" in command
    assert "<--set-secrets> <DATABASE_URL=migration-database-url:latest>" in command
    assert "runtime" not in command


def test_database_backup_uses_instance_from_connection_name(tmp_path: Path) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(
        tmp_path,
        "gcloud",
        "printf 'gcloud' >> \"$COMMAND_LOG\"\n"
        'printf \' <%s>\' "$@" >> "$COMMAND_LOG"\n'
        "printf '\\n' >> \"$COMMAND_LOG\"",
    )

    result = run_deploy(
        "create-database-backup",
        environment={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "COMMAND_LOG": str(command_log),
            "PROJECT_ID": "project",
            "CLOUD_SQL_CONNECTION": "project:region:telemetry-production",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
        },
    )

    assert result.returncode == 0, result.stderr
    assert command_log.read_text() == (
        "gcloud <sql> <backups> <create> <--project> <project> "
        "<--instance> <telemetry-production> "
        "<--description> <telemetry-release-123-2> <--quiet>\n"
    )


def service_deploy_environment(
    stub_directory: Path, command_log: Path, deployment_environment: str
) -> dict[str, str]:
    return {
        "PATH": f"{stub_directory}:{os.environ['PATH']}",
        "COMMAND_LOG": str(command_log),
        "SERVICE": "telemetry-service",
        "PROJECT_ID": "project",
        "REGION": "region",
        "IMAGE_URI": "registry/image:sha",
        "RUNTIME_SERVICE_ACCOUNT": RUNTIME_ACCOUNT,
        "CLOUD_SQL_CONNECTION": "project:region:database",
        "DATABASE_SECRET_NAME": "database-url",
        "JWT_SECRET_NAME": "jwt-secret",
        "JWT_SIGNING_KEY_SECRET_NAME": "jwt-signing-key-secret",
        "READ_SECRET_NAME": "read-token",
        "DEPLOYMENT_ENVIRONMENT": deployment_environment,
        **DEPLOY_INTAKE_SETTINGS,
        **ORRERY_SETTINGS,
        **ORRERY_READER_SETTINGS,
    }


def expected_service_settings(maintenance_mode: str) -> str:
    return (
        f"<--set-env-vars> <TELEMETRY_MAINTENANCE_MODE={maintenance_mode},"
        "COLLECTOR_INTAKE_ENABLED=1,"
        "COLLECTOR_INTAKE_BUCKET=intake-bucket,"
        f"COLLECTOR_INTAKE_SIGNING_SERVICE_ACCOUNT={RUNTIME_ACCOUNT},"
        "COLLECTOR_WORKER_QUEUE=projects/project/locations/region/queues/worker-queue,"
        "COLLECTOR_WORKER_JOB=projects/project/locations/region/jobs/worker-job,"
        f"COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT={INVOKER_ACCOUNT}>"
    )


@pytest.mark.parametrize(
    ("deploy_command", "maintenance_mode"),
    [("deploy-maintenance", "1"), ("deploy-candidate", "0")],
)
@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
@pytest.mark.parametrize(
    "leftover_switch",
    [None, "0"],
    ids=["no switch variable", "an earlier switch variable at 0"],
)
def test_service_revisions_switch_intake_on_in_every_environment(
    tmp_path: Path,
    deploy_command: str,
    maintenance_mode: str,
    deployment_environment: str,
    leftover_switch: str | None,
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = service_deploy_environment(
        tmp_path, command_log, deployment_environment
    )
    if leftover_switch is not None:
        environment["INTAKE_ENABLED"] = leftover_switch

    result = run_deploy(deploy_command, environment=environment)

    assert result.returncode == 0, result.stderr
    command = command_log.read_text()
    assert f" {expected_service_settings(maintenance_mode)} " in command
    assert command.count("<--set-env-vars>") == 1


@pytest.mark.parametrize("with_vercel_oidc", [False, True])
@pytest.mark.parametrize(
    ("deploy_command", "maintenance_mode"),
    [("deploy-maintenance", "1"), ("deploy-candidate", "0")],
)
def test_service_revisions_get_the_vercel_oidc_settings_only_when_they_are_set(
    tmp_path: Path, deploy_command: str, maintenance_mode: str, with_vercel_oidc: bool
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = service_deploy_environment(tmp_path, command_log, "staging")
    if with_vercel_oidc:
        environment.update(VERCEL_OIDC_SETTINGS)

    result = run_deploy(deploy_command, environment=environment)

    assert result.returncode == 0, result.stderr
    command = command_log.read_text()
    assert command.count("<--set-env-vars>") == 1
    settings = logged_setting_values(command)
    oidc_settings = {
        name: value for name, value in settings.items() if "VERCEL_OIDC" in name
    }
    if with_vercel_oidc:
        assert oidc_settings == {
            COLLECTOR_VERCEL_OIDC_SETTING_NAMES[name]: value
            for name, value in VERCEL_OIDC_SETTINGS.items()
        }
        assert expected_service_settings(maintenance_mode).removesuffix(">") in command
    else:
        assert oidc_settings == {}
        assert f" {expected_service_settings(maintenance_mode)} " in command
        assert "VERCEL" not in command


# A GitHub variable may hold its readers on several lines, with Unix or
# Windows line endings, and a value may end with a line ending.
@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["unix", "windows"])
def test_the_vercel_oidc_settings_the_revisions_get_are_ones_the_collector_accepts(
    tmp_path: Path, line_ending: str
) -> None:
    module = vercel_oidc()
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = {
        **service_deploy_environment(tmp_path, command_log, "production"),
        "VERCEL_OIDC_TEAM_SLUG": f"example-team{line_ending}",
        "VERCEL_OIDC_TEAM_ID": f"team_example{line_ending}",
        "VERCEL_OIDC_READERS": (
            f"prj_dashboard:production{line_ending}  prj_other:preview{line_ending}"
        ),
    }

    result = run_deploy("deploy-candidate", environment=environment)

    assert result.returncode == 0, result.stderr
    settings = logged_setting_values(command_log.read_text())
    # One line per setting, with no line ending left: the readers are passed
    # separated by single spaces.
    assert {
        name: value for name, value in settings.items() if "VERCEL_OIDC" in name
    } == {
        "TELEMETRY_VERCEL_OIDC_TEAM_SLUG": "example-team",
        "TELEMETRY_VERCEL_OIDC_TEAM_ID": "team_example",
        "TELEMETRY_VERCEL_OIDC_READERS": "prj_dashboard:production prj_other:preview",
    }
    assert module.VercelOidcSettings.from_environment(settings) == (
        module.VercelOidcSettings(
            team_slug="example-team",
            team_id="team_example",
            readers=frozenset(
                {
                    module.VercelOidcReader("prj_dashboard", "production"),
                    module.VercelOidcReader("prj_other", "preview"),
                }
            ),
        )
    )


@pytest.mark.parametrize("deploy_command", ["deploy-maintenance", "deploy-candidate"])
def test_service_revision_deploys_stop_before_gcloud_with_part_of_the_vercel_oidc_settings(
    tmp_path: Path, deploy_command: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = {
        **service_deploy_environment(tmp_path, command_log, "production"),
        "VERCEL_OIDC_TEAM_SLUG": VERCEL_OIDC_SETTINGS["VERCEL_OIDC_TEAM_SLUG"],
    }

    result = run_deploy(deploy_command, environment=environment)

    assert result.returncode == 1
    assert result.stderr == (
        "Missing telemetry deployment configuration: "
        "VERCEL_OIDC_TEAM_ID VERCEL_OIDC_READERS\n"
    )
    assert not command_log.exists()


@pytest.mark.parametrize("deploy_command", ["deploy-maintenance", "deploy-candidate"])
def test_service_revision_deploys_stop_before_gcloud_with_a_malformed_vercel_oidc_setting(
    tmp_path: Path, deploy_command: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = {
        **service_deploy_environment(tmp_path, command_log, "production"),
        **VERCEL_OIDC_SETTINGS,
        "VERCEL_OIDC_READERS": "prj_dashboard:preview,prj_dashboard:production",
    }

    result = run_deploy(deploy_command, environment=environment)

    assert result.returncode == 1
    assert result.stderr == VERCEL_OIDC_REFUSALS["VERCEL_OIDC_READERS"]
    assert not command_log.exists()


def test_the_jobs_and_the_orrery_read_service_never_get_the_vercel_oidc_settings(
    tmp_path: Path,
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", IAM_POLICY_GCLOUD)
    environment = {
        **job_deploy_environment(tmp_path, command_log, "production"),
        **read_service_deploy_environment(tmp_path, command_log, "production"),
        **VERCEL_OIDC_SETTINGS,
        "MIGRATION_JOB": "migration-job",
        "MIGRATION_SERVICE_ACCOUNT": "migrator@example.invalid",
        "MIGRATION_DATABASE_SECRET_NAME": "migration-database-url",
    }

    for deploy_command in (
        "configure-migration-job",
        "configure-worker-job",
        "configure-recovery-job",
        "deploy-orrery-read-service",
    ):
        result = run_deploy(deploy_command, environment=environment)
        assert result.returncode == 0, (deploy_command, result.stderr)

    command = command_log.read_text()
    assert "VERCEL" not in command
    for value in VERCEL_OIDC_SETTINGS.values():
        assert value not in command


def job_deploy_environment(
    stub_directory: Path, command_log: Path, deployment_environment: str
) -> dict[str, str]:
    return {
        "PATH": f"{stub_directory}:{os.environ['PATH']}",
        "COMMAND_LOG": str(command_log),
        "PROJECT_ID": "project",
        "REGION": "region",
        "IMAGE_URI": "registry/image:sha",
        "CLOUD_SQL_CONNECTION": "project:region:database",
        "DATABASE_SECRET_NAME": "database-url",
        "DEPLOYMENT_ENVIRONMENT": deployment_environment,
        **DEPLOY_INTAKE_SETTINGS,
        **HUGGINGFACE_SETTINGS,
        **CALIBRATION_TREE_SETTINGS,
        **ORRERY_SETTINGS,
        **ORRERY_READER_SETTINGS,
    }


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_worker_job_update_mounts_the_read_token_in_both_environments(
    tmp_path: Path, deployment_environment: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)

    result = run_deploy(
        "configure-worker-job",
        environment=job_deploy_environment(
            tmp_path, command_log, deployment_environment
        ),
    )

    assert result.returncode == 0, result.stderr
    assert command_log.read_text() == (
        "gcloud <run> <jobs> <update> <worker-job> "
        "<--project> <project> <--region> <region> "
        "<--image> <registry/image:sha> "
        f"<--service-account> <{WORKER_ACCOUNT}> "
        "<--set-cloudsql-instances> <project:region:database> "
        "<--set-secrets> <DATABASE_URL=database-url:latest,"
        "COLLECTOR_HF_READ_TOKEN=hugging-face-read-token:latest,"
        "COLLECTOR_CALIBRATION_TREE_BLOB_TOKEN=calibration-tree-blob-token:latest> "
        "<--set-env-vars> <COLLECTOR_INTAKE_BUCKET=intake-bucket,"
        "COLLECTOR_ORRERY_BUCKET=orrery-bucket> "
        "<--command> <uv> "
        "<--args> <run,--no-sync,python,-m,telemetry_collector.worker> "
        "<--tasks> <1> <--max-retries> <2> <--task-timeout> <1h> "
        "<--cpu> <2> <--memory> <8Gi> <--quiet>\n"
    )


def test_worker_job_settings_name_a_bucket_the_orrery_handler_accepts(
    tmp_path: Path,
) -> None:
    import importlib

    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)

    result = run_deploy(
        "configure-worker-job",
        environment=job_deploy_environment(tmp_path, command_log, "production"),
    )

    assert result.returncode == 0, result.stderr
    settings = logged_setting_values(command_log.read_text())
    orrery_graphs = importlib.import_module("telemetry_collector.orrery_graphs")
    assert orrery_graphs.orrery_bucket(settings) == "orrery-bucket"


def test_the_recovery_job_gets_neither_orrery_setting(tmp_path: Path) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)

    result = run_deploy(
        "configure-recovery-job",
        environment=job_deploy_environment(tmp_path, command_log, "staging"),
    )

    assert result.returncode == 0, result.stderr
    command = command_log.read_text()
    assert "ORRERY" not in command
    assert "orrery-bucket" not in command
    assert READER_ACCOUNT not in command


@pytest.mark.parametrize("deploy_command", ["deploy-maintenance", "deploy-candidate"])
def test_service_revisions_get_no_orrery_setting(
    tmp_path: Path, deploy_command: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)

    result = run_deploy(
        deploy_command,
        environment=service_deploy_environment(tmp_path, command_log, "production"),
    )

    # The collector's service mounts no Orrery graph route; the read service
    # serves the files.
    assert result.returncode == 0, result.stderr
    command = command_log.read_text()
    assert "ORRERY" not in command
    assert "orrery-bucket" not in command
    assert READER_ACCOUNT not in command


def test_only_the_read_service_s_iam_policy_names_the_reader_account(
    tmp_path: Path,
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", IAM_POLICY_GCLOUD)
    environment = {
        **job_deploy_environment(tmp_path, command_log, "production"),
        **read_service_deploy_environment(tmp_path, command_log, "production"),
    }

    for deploy_command in (
        "deploy-maintenance",
        "deploy-candidate",
        "configure-worker-job",
        "configure-recovery-job",
        "deploy-orrery-read-service",
    ):
        result = run_deploy(deploy_command, environment=environment)
        assert result.returncode == 0, (deploy_command, result.stderr)

    # No revision, job or container setting gets the address: only the
    # policy file that sets who may call the read service.
    assert READER_ACCOUNT not in command_log.read_text()
    assert json.loads(Path(environment["IAM_POLICY"]).read_text()) == {
        "bindings": READER_INVOKER_GRANTS
    }


def read_service_deploy_environment(
    stub_directory: Path, command_log: Path, deployment_environment: str
) -> dict[str, str]:
    """What the read service's deploy runs with, and a stand-in curl in
    ``stub_directory`` that answers its call without a token as Cloud Run
    does, with 403, unless a test changes ``READ_SERVICE_STATUS``."""

    install_command_stub(stub_directory, "curl", UNAUTHENTICATED_CALL_CURL)
    return {
        **service_deploy_environment(
            stub_directory, command_log, deployment_environment
        ),
        "IAM_POLICY": str(stub_directory / "iam-policy.json"),
        "READ_SERVICE_URL": READ_SERVICE_URL,
        "READ_SERVICE_STATUS": "403",
    }


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_the_read_service_is_deployed_and_only_the_reader_account_may_call_it(
    tmp_path: Path, deployment_environment: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", IAM_POLICY_GCLOUD)
    environment = read_service_deploy_environment(
        tmp_path, command_log, deployment_environment
    )

    result = run_deploy("deploy-orrery-read-service", environment=environment)

    assert result.returncode == 0, result.stderr
    (
        deploy_line,
        policy_line,
        policy_check_line,
        describe_line,
        unauthenticated_call_line,
    ) = command_log.read_text().splitlines()
    assert deploy_line == (
        f"gcloud <run> <deploy> <{READ_SERVICE}> "
        "<--project> <project> <--region> <region> <--platform> <managed> "
        "<--image> <registry/image:sha> "
        f"<--service-account> <{RUNTIME_ACCOUNT}> "
        "<--set-cloudsql-instances> <project:region:database> "
        "<--set-secrets> <DATABASE_URL=database-url:latest> "
        "<--set-env-vars> <COLLECTOR_ORRERY_BUCKET=orrery-bucket> "
        "<--command> <uv> "
        "<--args> <run,--no-sync,uvicorn,"
        "telemetry_collector.orrery_read_app:create_orrery_read_app_from_environment,"
        "--factory,--host,0.0.0.0,--port,8080,--no-access-log> "
        "<--port> <8080> "
        "<--min> <0> <--max> <2> <--cpu> <1> <--memory> <512Mi> "
        "<--concurrency> <10> <--timeout> <300> "
        "<--no-allow-unauthenticated> <--invoker-iam-check> "
        "<--clear-custom-audiences> "
        "<--default-url> "
        "<--startup-probe> "
        "<tcpSocket.port=8080,timeoutSeconds=10,periodSeconds=10,failureThreshold=12> "
        "<--quiet>"
    )
    policy_call = re.fullmatch(
        rf"gcloud <run> <services> <set-iam-policy> <{READ_SERVICE}> <([^>]+)> "
        "<--project> <project> <--region> <region> <--format=none> <--quiet>",
        policy_line,
    )
    assert policy_call is not None, policy_line
    # The policy file is removed once the policy is set.
    assert not Path(policy_call.group(1)).exists()
    assert json.loads(Path(environment["IAM_POLICY"]).read_text()) == {
        "bindings": READER_INVOKER_GRANTS
    }
    assert policy_check_line == (
        f"gcloud <run> <services> <get-iam-policy> <{READ_SERVICE}> "
        "<--project> <project> <--region> <region> <--format=json>"
    )
    # Then the deploy calls the deployed service at its URL without a token.
    assert describe_line == (
        f"gcloud <run> <services> <describe> <{READ_SERVICE}> "
        "<--project> <project> <--region> <region> <--format=value(status.url)>"
    )
    assert unauthenticated_call_line == (
        "curl <--silent> <--output> </dev/null> <--write-out> <%{http_code}> "
        "<--retry> <12> <--retry-all-errors> <--retry-delay> <5> "
        f"<{UNAUTHENTICATED_CHECK_URL}>"
    )
    assert result.stdout == (
        "The Orrery read service refused a call without a token with HTTP 403.\n"
    )


def test_the_read_service_runs_the_read_application_as_it_was_sized(
    tmp_path: Path,
) -> None:
    read_app = importlib.import_module("telemetry_collector.orrery_read_app")
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", IAM_POLICY_GCLOUD)

    result = run_deploy(
        "deploy-orrery-read-service",
        environment=read_service_deploy_environment(
            tmp_path, command_log, "production"
        ),
    )

    assert result.returncode == 0, result.stderr
    deploy_line = read_service_deploy_line(command_log)

    def flag_value(flag: str) -> str:
        match = re.search(rf"<{flag}> <([^>]*)>", deploy_line)
        assert match is not None, flag
        return match.group(1)

    arguments = flag_value("--args").split(",")
    module_name, factory_name = arguments[arguments.index("--factory") - 1].split(":")
    assert module_name == read_app.__name__
    assert callable(getattr(read_app, factory_name))
    # The application listens on the port Cloud Run sends requests to and
    # probes.
    listening_port = arguments[arguments.index("--port") + 1]
    assert flag_value("--port") == listening_port
    assert f"tcpSocket.port={listening_port}," in flag_value("--startup-probe")
    assert int(flag_value("--timeout")) == read_app.REQUEST_TIMEOUT_SECONDS
    # Every deploy leaves the service without a custom audience: microcosm-runs
    # requests its tokens for the service's URL, which Cloud Run always
    # accepts.
    assert "<--clear-custom-audiences>" in deploy_line
    assert "<--set-custom-audiences>" not in deploy_line
    assert "<--add-custom-audiences>" not in deploy_line
    assert "<--allow-unauthenticated>" not in deploy_line
    assert "<--no-invoker-iam-check>" not in deploy_line


@pytest.mark.parametrize(
    "answered_grants",
    [
        [
            *READER_INVOKER_GRANTS,
            {"role": "roles/run.invoker", "members": ["allUsers"]},
        ],
        [
            {
                "role": "roles/run.invoker",
                "members": [
                    "allAuthenticatedUsers",
                    f"serviceAccount:{READER_ACCOUNT}",
                ],
            }
        ],
        [
            {
                "role": "roles/run.invoker",
                "members": [
                    f"serviceAccount:{READER_ACCOUNT}",
                    f"serviceAccount:{RUNTIME_ACCOUNT}",
                ],
            }
        ],
        [
            *READER_INVOKER_GRANTS,
            {
                "role": "roles/run.developer",
                "members": ["group:collector-developers@example.com"],
            },
        ],
        [{"role": "roles/run.viewer", "members": [f"serviceAccount:{READER_ACCOUNT}"]}],
        [],
    ],
    ids=[
        "everyone",
        "every Google account",
        "a second invoker",
        "another role",
        "no invoker",
        "an empty policy",
    ],
)
def test_the_read_service_deploy_fails_unless_its_policy_is_the_reader_account_alone(
    tmp_path: Path, answered_grants: list[dict]
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", IAM_POLICY_GCLOUD)
    environment = read_service_deploy_environment(tmp_path, command_log, "staging")
    answered_policy = tmp_path / "answered-policy.json"
    answered_policy.write_text(
        json.dumps({"bindings": answered_grants, "etag": "BwYexample=", "version": 1})
    )
    environment["IAM_POLICY_ANSWER"] = str(answered_policy)

    result = run_deploy("deploy-orrery-read-service", environment=environment)

    assert result.returncode == 1
    assert result.stderr == (
        "The Orrery read service's IAM policy is not exactly the Cloud Run "
        "Invoker role for ORRERY_READER_SERVICE_ACCOUNT.\n"
    )
    # The service is called without a token only once its policy is right.
    assert not [
        line
        for line in command_log.read_text().splitlines()
        if line.startswith("curl") or "<describe>" in line
    ]


@pytest.mark.parametrize("refusal_status", ["401", "403"])
def test_the_read_service_deploy_passes_when_the_service_refuses_a_call_without_a_token(
    tmp_path: Path, refusal_status: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", IAM_POLICY_GCLOUD)
    environment = read_service_deploy_environment(tmp_path, command_log, "staging")
    environment["READ_SERVICE_STATUS"] = refusal_status

    result = run_deploy("deploy-orrery-read-service", environment=environment)

    assert result.returncode == 0, result.stderr
    # Only the status code is printed: neither the URL nor the answer.
    assert result.stdout == (
        "The Orrery read service refused a call without a token with "
        f"HTTP {refusal_status}.\n"
    )
    assert result.stderr == ""


@pytest.mark.parametrize("answered_status", ["200", "404", "422", "500", "503"])
def test_the_read_service_deploy_fails_unless_the_service_refuses_a_call_without_a_token(
    tmp_path: Path, answered_status: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", IAM_POLICY_GCLOUD)
    environment = read_service_deploy_environment(tmp_path, command_log, "staging")
    environment["READ_SERVICE_STATUS"] = answered_status

    result = run_deploy("deploy-orrery-read-service", environment=environment)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "The Orrery read service answered a call without a token with "
        f"HTTP {answered_status}, not 401 or 403.\n"
    )


def test_the_read_service_deploy_fails_when_the_service_does_not_answer(
    tmp_path: Path,
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", IAM_POLICY_GCLOUD)
    environment = read_service_deploy_environment(tmp_path, command_log, "staging")
    environment["READ_SERVICE_STATUS"] = "unreachable"

    result = run_deploy("deploy-orrery-read-service", environment=environment)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "The Orrery read service did not answer the deploy's call without a token.\n"
    )
    assert READ_SERVICE_URL not in result.stderr


def test_the_call_without_a_token_asks_the_listing_for_a_run_id_it_accepts() -> None:
    routes = importlib.import_module("telemetry_collector.orrery_graph_api")
    models = importlib.import_module("telemetry_collector.models")
    check_url = urllib.parse.urlsplit(UNAUTHENTICATED_CHECK_URL)
    query = urllib.parse.parse_qs(check_url.query)

    # So a service that let the call through would answer it with a listing,
    # never with 401 or 403.
    assert check_url.path == routes.ORRERY_GRAPHS_PATH
    assert list(query) == ["run_id"]
    [run_id] = query["run_id"]
    assert re.fullmatch(models.SAFE_IDENTIFIER, run_id)


@pytest.mark.parametrize(
    ("service_name_length", "accepted"),
    [(CLOUD_RUN_SERVICE_NAME_LIMIT - len("-orrery"), True), (43, False)],
)
@pytest.mark.parametrize(
    "deploy_command", ["require-configuration", "deploy-orrery-read-service"]
)
def test_the_read_service_s_name_must_fit_cloud_run_s_limit(
    tmp_path: Path, service_name_length: int, accepted: bool, deploy_command: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", IAM_POLICY_GCLOUD)
    service_name = "telemetry-" + "s" * (service_name_length - len("telemetry-"))
    environment = {
        **complete_configuration("staging"),
        **read_service_deploy_environment(tmp_path, command_log, "staging"),
        "SERVICE": service_name,
    }

    result = run_deploy(deploy_command, environment=environment)

    if accepted:
        assert result.returncode == 0, result.stderr
        return
    assert result.returncode == 1
    assert result.stderr == (
        f"The Orrery read service's name, {service_name}-orrery, is longer than "
        f"Cloud Run's limit of {CLOUD_RUN_SERVICE_NAME_LIMIT} characters.\n"
    )
    assert not command_log.exists()


@pytest.mark.parametrize(
    "missing_variable",
    [
        "SERVICE",
        "IMAGE_URI",
        "RUNTIME_SERVICE_ACCOUNT",
        "CLOUD_SQL_CONNECTION",
        "DATABASE_SECRET_NAME",
        "ORRERY_BUCKET",
        "ORRERY_READER_SERVICE_ACCOUNT",
    ],
)
def test_the_read_service_deploy_stops_before_gcloud_without_a_setting(
    tmp_path: Path, missing_variable: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", IAM_POLICY_GCLOUD)
    environment = read_service_deploy_environment(tmp_path, command_log, "production")
    del environment[missing_variable]

    result = run_deploy("deploy-orrery-read-service", environment=environment)

    assert result.returncode == 1
    assert result.stderr == (
        f"Missing telemetry deployment configuration: {missing_variable}\n"
    )
    assert not command_log.exists()


def test_worker_job_gives_each_job_the_attempts_and_time_the_worker_counts_on(
    tmp_path: Path,
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)

    result = run_deploy(
        "configure-worker-job",
        environment=job_deploy_environment(tmp_path, command_log, "staging"),
    )

    assert result.returncode == 0, result.stderr
    command = command_log.read_text()
    retries = re.search(r"<--max-retries> <(\d+)>", command)
    timeout_hours = re.search(r"<--task-timeout> <(\d+)h>", command)
    assert retries is not None and timeout_hours is not None, command
    assert int(retries.group(1)) + 1 == MAX_JOB_ATTEMPTS
    assert int(timeout_hours.group(1)) * 60 * 60 == WORKER_TASK_TIMEOUT_SECONDS


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_worker_job_update_requires_the_read_token_secret(
    tmp_path: Path, deployment_environment: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = job_deploy_environment(tmp_path, command_log, deployment_environment)
    del environment["HF_READ_SECRET_NAME"]

    result = run_deploy("configure-worker-job", environment=environment)

    assert result.returncode == 1
    assert result.stderr == (
        "Missing telemetry deployment configuration: HF_READ_SECRET_NAME\n"
    )
    assert not command_log.exists()


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_worker_job_update_requires_the_calibration_tree_blob_secret(
    tmp_path: Path, deployment_environment: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = job_deploy_environment(tmp_path, command_log, deployment_environment)
    del environment["CALIBRATION_TREE_BLOB_SECRET_NAME"]

    result = run_deploy("configure-worker-job", environment=environment)

    assert result.returncode == 1
    assert result.stderr == (
        "Missing telemetry deployment configuration: "
        "CALIBRATION_TREE_BLOB_SECRET_NAME\n"
    )
    assert not command_log.exists()


@pytest.mark.parametrize(
    "deploy_command", ["configure-worker-job", "check-secrets-exist"]
)
def test_worker_secrets_refuse_an_unknown_environment(
    tmp_path: Path, deploy_command: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = job_deploy_environment(tmp_path, command_log, "preview")
    environment.update(
        MIGRATION_DATABASE_SECRET_NAME="migration-database-url",
        JWT_SECRET_NAME="jwt",
        JWT_SIGNING_KEY_SECRET_NAME="jwt-signing-key",
        READ_SECRET_NAME="read",
    )

    result = run_deploy(deploy_command, environment=environment)

    assert result.returncode == 1
    assert "Unsupported deployment environment: preview" in result.stderr
    assert not command_log.exists()


@pytest.mark.parametrize("deployment_environment", ["staging", "production"])
def test_recovery_job_update_dispatches_through_the_worker_queue(
    tmp_path: Path, deployment_environment: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)

    result = run_deploy(
        "configure-recovery-job",
        environment=job_deploy_environment(
            tmp_path, command_log, deployment_environment
        ),
    )

    assert result.returncode == 0, result.stderr
    assert command_log.read_text() == (
        "gcloud <run> <jobs> <update> <recovery-job> "
        "<--project> <project> <--region> <region> "
        "<--image> <registry/image:sha> "
        f"<--service-account> <{RECOVERY_ACCOUNT}> "
        "<--set-cloudsql-instances> <project:region:database> "
        "<--set-secrets> <DATABASE_URL=database-url:latest> "
        "<--set-env-vars> <"
        "COLLECTOR_WORKER_QUEUE=projects/project/locations/region/queues/worker-queue,"
        "COLLECTOR_WORKER_JOB=projects/project/locations/region/jobs/worker-job,"
        f"COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT={INVOKER_ACCOUNT}> "
        "<--command> <uv> "
        "<--args> <run,--no-sync,python,-m,telemetry_collector.operations,recover> "
        "<--tasks> <1> <--max-retries> <0> <--task-timeout> <10m> "
        "<--cpu> <1> <--memory> <512Mi> <--quiet>\n"
    )


@pytest.mark.parametrize(
    ("deploy_command", "missing_variable"),
    [
        ("configure-worker-job", "WORKER_SERVICE_ACCOUNT"),
        ("configure-worker-job", "INTAKE_BUCKET"),
        ("configure-worker-job", "ORRERY_BUCKET"),
        ("configure-recovery-job", "RECOVERY_SERVICE_ACCOUNT"),
        ("configure-recovery-job", "WORKER_INVOKER_SERVICE_ACCOUNT"),
    ],
)
def test_job_updates_stop_before_gcloud_when_a_setting_is_missing(
    tmp_path: Path, deploy_command: str, missing_variable: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = job_deploy_environment(tmp_path, command_log, "production")
    del environment[missing_variable]

    result = run_deploy(deploy_command, environment=environment)

    assert result.returncode == 1
    assert result.stderr == (
        f"Missing telemetry deployment configuration: {missing_variable}\n"
    )
    assert not command_log.exists()


@pytest.mark.parametrize("deploy_command", ["deploy-maintenance", "deploy-candidate"])
@pytest.mark.parametrize(
    ("deployment_environment", "expected_minimum"),
    [("staging", "0"), ("production", "1")],
)
def test_service_minimum_instances_depend_on_environment(
    tmp_path: Path,
    deploy_command: str,
    deployment_environment: str,
    expected_minimum: str,
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(
        tmp_path,
        "gcloud",
        "printf 'gcloud' >> \"$COMMAND_LOG\"\n"
        'printf \' <%s>\' "$@" >> "$COMMAND_LOG"\n'
        "printf '\\n' >> \"$COMMAND_LOG\"",
    )

    result = run_deploy(
        deploy_command,
        environment=service_deploy_environment(
            tmp_path, command_log, deployment_environment
        ),
    )

    assert result.returncode == 0, result.stderr
    command = command_log.read_text()
    assert command.startswith("gcloud <run> <deploy> <telemetry-service> ")
    assert f"<--min> <{expected_minimum}> <--max> <4>" in command


@pytest.mark.parametrize("deploy_command", ["deploy-maintenance", "deploy-candidate"])
def test_service_deploy_refuses_unknown_environment(
    tmp_path: Path, deploy_command: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(
        tmp_path,
        "gcloud",
        "printf 'gcloud' >> \"$COMMAND_LOG\"",
    )

    result = run_deploy(
        deploy_command,
        environment=service_deploy_environment(tmp_path, command_log, "preview"),
    )

    assert result.returncode == 1
    assert "Unsupported deployment environment: preview" in result.stderr
    assert not command_log.exists()


def test_unknown_command_fails_with_usage() -> None:
    result = run_deploy("not-a-command")

    assert result.returncode == 2
    assert "Unknown deployment command: not-a-command" in result.stderr
    assert "Usage: deploy.sh COMMAND" in result.stderr


@pytest.mark.parametrize("command", ["deploy-maintenance", "deploy-candidate"])
def test_every_revision_mounts_the_signing_key_beside_the_hs256_secret(
    tmp_path: Path, command: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)

    result = run_deploy(
        command,
        environment=service_deploy_environment(tmp_path, command_log, "staging"),
    )

    assert result.returncode == 0, result.stderr
    assert (
        "<--set-secrets> <DATABASE_URL=database-url:latest,"
        "TELEMETRY_JWT_SECRET=jwt-secret:latest,"
        "TELEMETRY_JWT_SIGNING_KEY=jwt-signing-key-secret:latest,"
        "TELEMETRY_READ_TOKEN=read-token:latest>"
    ) in command_log.read_text()


@pytest.mark.parametrize("command", ["deploy-maintenance", "deploy-candidate"])
def test_no_revision_deploys_without_the_signing_key_secret_name(
    tmp_path: Path, command: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = service_deploy_environment(tmp_path, command_log, "staging")
    del environment["JWT_SIGNING_KEY_SECRET_NAME"]

    result = run_deploy(command, environment=environment)

    assert result.returncode == 1
    assert result.stderr == (
        "Missing telemetry deployment configuration: JWT_SIGNING_KEY_SECRET_NAME\n"
    )
    assert not command_log.exists()
