import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = ROOT / "telemetry-service" / "scripts" / "deploy.sh"


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
        "ARTIFACT_REPOSITORY CLOUD_SQL_CONNECTION RUNTIME_SERVICE_ACCOUNT "
        "MIGRATION_SERVICE_ACCOUNT SERVICE MIGRATION_JOB IMAGE_NAME "
        "DATABASE_SECRET_NAME MIGRATION_DATABASE_SECRET_NAME JWT_SECRET_NAME "
        "READ_SECRET_NAME DEPLOYMENT_ENVIRONMENT "
        "PRODUCTION_CLOUD_SQL_CONNECTION PRODUCTION_SERVICE\n"
    )


def test_require_configuration_accepts_complete_environment() -> None:
    result = run_deploy(
        "require-configuration",
        environment={
            "PROJECT_ID": "project",
            "REGION": "region",
            "ARTIFACT_REPOSITORY": "repository",
            "CLOUD_SQL_CONNECTION": "connection",
            "RUNTIME_SERVICE_ACCOUNT": "service-account",
            "MIGRATION_SERVICE_ACCOUNT": "migration-service-account",
            "SERVICE": "service",
            "MIGRATION_JOB": "migration-job",
            "IMAGE_NAME": "image",
            "DATABASE_SECRET_NAME": "database-secret",
            "MIGRATION_DATABASE_SECRET_NAME": "migration-database-secret",
            "JWT_SECRET_NAME": "jwt-secret",
            "READ_SECRET_NAME": "read-secret",
            "DEPLOYMENT_ENVIRONMENT": "staging",
            "PRODUCTION_CLOUD_SQL_CONNECTION": "project:region:production",
            "PRODUCTION_SERVICE": "production-service",
        },
    )

    assert result.returncode == 0, result.stderr


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
            "ARTIFACT_REPOSITORY": "containers",
            "IMAGE_NAME": "collector",
        },
    )

    image = "us-central1-docker.pkg.dev/example-project/containers/collector:abc123"
    assert result.returncode == 0, result.stderr
    assert command_log.read_text().splitlines() == [
        "docker <build> <--file> <telemetry-service/Dockerfile> "
        f"<--tag> <{image}> <telemetry-service>",
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


def test_unknown_command_fails_with_usage() -> None:
    result = run_deploy("not-a-command")

    assert result.returncode == 2
    assert "Unknown deployment command: not-a-command" in result.stderr
    assert "Usage: deploy.sh COMMAND" in result.stderr
