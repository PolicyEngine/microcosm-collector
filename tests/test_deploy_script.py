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
        "ARTIFACT_REPOSITORY CLOUD_SQL_CONNECTION RUNTIME_SERVICE_ACCOUNT\n"
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


def test_route_production_traffic_passes_the_requested_revision(
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
        "route-production-traffic",
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


def test_unknown_command_fails_with_usage() -> None:
    result = run_deploy("not-a-command")

    assert result.returncode == 2
    assert "Unknown deployment command: not-a-command" in result.stderr
    assert "Usage: deploy.sh COMMAND" in result.stderr
