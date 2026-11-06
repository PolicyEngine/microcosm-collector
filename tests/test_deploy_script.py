import json
import os
import re
import subprocess
from pathlib import Path

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
    "INTAKE_ENABLED": "1",
    "INTAKE_BUCKET": "intake-bucket",
    "WORKER_QUEUE": "worker-queue",
    "WORKER_JOB": "worker-job",
    "RECOVERY_JOB": "recovery-job",
    "WORKER_SERVICE_ACCOUNT": WORKER_ACCOUNT,
    "WORKER_INVOKER_SERVICE_ACCOUNT": INVOKER_ACCOUNT,
    "RECOVERY_SERVICE_ACCOUNT": RECOVERY_ACCOUNT,
}
# Only production's worker job mounts the collector's Hugging Face read token.
PRODUCTION_ONLY_SETTINGS = {"HF_READ_SECRET_NAME": "hugging-face-read-token"}
LOGGING_GCLOUD = (
    "printf 'gcloud' >> \"$COMMAND_LOG\"\n"
    'printf \' <%s>\' "$@" >> "$COMMAND_LOG"\n'
    "printf '\\n' >> \"$COMMAND_LOG\""
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
        "READ_SECRET_NAME DEPLOYMENT_ENVIRONMENT "
        "PRODUCTION_CLOUD_SQL_CONNECTION PRODUCTION_SERVICE "
        "INTAKE_ENABLED INTAKE_BUCKET WORKER_QUEUE WORKER_JOB RECOVERY_JOB "
        "WORKER_SERVICE_ACCOUNT WORKER_INVOKER_SERVICE_ACCOUNT "
        "RECOVERY_SERVICE_ACCOUNT\n"
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
        "READ_SECRET_NAME": "read-secret",
        "DEPLOYMENT_ENVIRONMENT": deployment_environment,
        "PRODUCTION_CLOUD_SQL_CONNECTION": "project:region:production",
        "PRODUCTION_SERVICE": "production-service",
        **DEPLOY_INTAKE_SETTINGS,
    }
    if deployment_environment == "production":
        configuration.update(PRODUCTION_ONLY_SETTINGS)
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


def test_require_configuration_requires_the_read_token_secret_only_in_production() -> (
    None
):
    production = complete_configuration("production")
    del production["HF_READ_SECRET_NAME"]

    production_result = run_deploy("require-configuration", environment=production)
    staging_result = run_deploy(
        "require-configuration", environment=complete_configuration("staging")
    )

    assert production_result.returncode == 1
    assert production_result.stderr == (
        "Missing telemetry deployment configuration: HF_READ_SECRET_NAME\n"
    )
    assert staging_result.returncode == 0, staging_result.stderr


@pytest.mark.parametrize("intake_switch", ["true", "on", "2", "1 "])
def test_require_configuration_accepts_only_0_or_1_as_the_intake_switch(
    intake_switch: str,
) -> None:
    configuration = complete_configuration("staging")
    configuration["INTAKE_ENABLED"] = intake_switch

    result = run_deploy("require-configuration", environment=configuration)

    assert result.returncode == 1
    assert result.stderr == "INTAKE_ENABLED must be 0 or 1.\n"


@pytest.mark.parametrize(
    ("deployment_environment", "expected_secrets"),
    [
        ("staging", ["database-url", "migration-database-url", "jwt", "read"]),
        (
            "production",
            [
                "database-url",
                "migration-database-url",
                "jwt",
                "read",
                "hugging-face-read-token",
            ],
        ),
    ],
)
def test_check_secrets_exist_includes_the_read_token_secret_only_in_production(
    tmp_path: Path, deployment_environment: str, expected_secrets: list[str]
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
            "READ_SECRET_NAME": "read",
            **PRODUCTION_ONLY_SETTINGS,
        },
    )

    assert result.returncode == 0, result.stderr
    assert command_log.read_text().splitlines() == [
        f"gcloud <secrets> <describe> <{secret}> <--project> <project> <--format=none>"
        for secret in expected_secrets
    ]


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


PROJECT_RESOURCES = "projects/project/locations/region"
# Each name the deploy checks, the collector's pattern for the setting it
# becomes, and how the deploy turns the name into that setting.
COLLECTOR_SETTING_SHAPES = {
    "INTAKE_BUCKET": (BUCKET_PATTERN, "{}"),
    "WORKER_QUEUE": (QUEUE_PATTERN, PROJECT_RESOURCES + "/queues/{}"),
    "WORKER_JOB": (JOB_PATTERN, PROJECT_RESOURCES + "/jobs/{}"),
    "RECOVERY_JOB": (JOB_PATTERN, PROJECT_RESOURCES + "/jobs/{}"),
    "RUNTIME_SERVICE_ACCOUNT": (SERVICE_ACCOUNT_PATTERN, "{}"),
    "WORKER_INVOKER_SERVICE_ACCOUNT": (SERVICE_ACCOUNT_PATTERN, "{}"),
}


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("INTAKE_BUCKET", "collector-intake.example"),
        ("INTAKE_BUCKET", "Intake-Bucket"),
        ("INTAKE_BUCKET", "-intake-bucket"),
        ("INTAKE_BUCKET", "gs://intake-bucket"),
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
        "READ_SECRET_NAME": "read-token",
        "DEPLOYMENT_ENVIRONMENT": deployment_environment,
        **DEPLOY_INTAKE_SETTINGS,
    }


def expected_service_settings(maintenance_mode: str, intake_switch: str) -> str:
    return (
        f"<--set-env-vars> <TELEMETRY_MAINTENANCE_MODE={maintenance_mode},"
        f"COLLECTOR_INTAKE_ENABLED={intake_switch},"
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
@pytest.mark.parametrize(
    ("deployment_environment", "intake_switch"),
    [("staging", "1"), ("staging", "0"), ("production", "1"), ("production", "0")],
)
def test_service_revisions_receive_the_environment_intake_settings(
    tmp_path: Path,
    deploy_command: str,
    maintenance_mode: str,
    deployment_environment: str,
    intake_switch: str,
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = service_deploy_environment(
        tmp_path, command_log, deployment_environment
    )
    environment["INTAKE_ENABLED"] = intake_switch

    result = run_deploy(deploy_command, environment=environment)

    assert result.returncode == 0, result.stderr
    command = command_log.read_text()
    assert f" {expected_service_settings(maintenance_mode, intake_switch)} " in (
        command
    )
    assert command.count("<--set-env-vars>") == 1


@pytest.mark.parametrize("deploy_command", ["deploy-maintenance", "deploy-candidate"])
def test_service_deploy_refuses_an_intake_switch_other_than_0_or_1(
    tmp_path: Path, deploy_command: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = service_deploy_environment(tmp_path, command_log, "staging")
    environment["INTAKE_ENABLED"] = "true"

    result = run_deploy(deploy_command, environment=environment)

    assert result.returncode == 1
    assert result.stderr == "INTAKE_ENABLED must be 0 or 1.\n"
    assert not command_log.exists()


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
        **PRODUCTION_ONLY_SETTINGS,
    }


@pytest.mark.parametrize(
    ("deployment_environment", "expected_secrets"),
    [
        ("staging", "DATABASE_URL=database-url:latest"),
        (
            "production",
            "DATABASE_URL=database-url:latest,"
            "COLLECTOR_HF_READ_TOKEN=hugging-face-read-token:latest",
        ),
    ],
)
def test_worker_job_update_mounts_the_read_token_only_in_production(
    tmp_path: Path, deployment_environment: str, expected_secrets: str
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
        f"<--set-secrets> <{expected_secrets}> "
        "<--set-env-vars> <COLLECTOR_INTAKE_BUCKET=intake-bucket> "
        "<--command> <uv> "
        "<--args> <run,--no-sync,python,-m,telemetry_collector.worker> "
        "<--tasks> <1> <--max-retries> <2> <--task-timeout> <1h> "
        "<--cpu> <1> <--memory> <2Gi> <--quiet>\n"
    )


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


def test_production_worker_job_update_requires_the_read_token_secret(
    tmp_path: Path,
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(tmp_path, "gcloud", LOGGING_GCLOUD)
    environment = job_deploy_environment(tmp_path, command_log, "production")
    del environment["HF_READ_SECRET_NAME"]

    result = run_deploy("configure-worker-job", environment=environment)

    assert result.returncode == 1
    assert result.stderr == (
        "Missing telemetry deployment configuration: HF_READ_SECRET_NAME\n"
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
