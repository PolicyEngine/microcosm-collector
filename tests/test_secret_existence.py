"""Deployment inspects secret metadata without accessing credential values."""

import os
import subprocess
from pathlib import Path

import pytest

from tests.test_deploy_script import install_command_stub

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "telemetry-service/scripts/check-secrets-exist.sh"


@pytest.mark.parametrize("missing_secret", ["", "database", "migration", "jwt", "read"])
def test_checks_all_resources_without_accessing_payloads(
    tmp_path: Path, missing_secret: str
) -> None:
    command_log = tmp_path / "commands.log"
    install_command_stub(
        tmp_path,
        "gcloud",
        'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        '[[ "$1 $2" == "secrets describe" ]] || exit 99\n'
        '[[ "$3" != "$MISSING_SECRET" ]]',
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "database", "migration", "jwt", "read"],
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PROJECT_ID": "test-project",
            "COMMAND_LOG": str(command_log),
            "MISSING_SECRET": missing_secret,
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == (1 if missing_secret else 0), result.stderr
    assert command_log.read_text().splitlines() == [
        f"secrets describe {name} --project test-project --format=none"
        for name in ("database", "migration", "jwt", "read")
    ]
    if missing_secret:
        assert missing_secret in result.stderr


def test_missing_configuration_stops_before_contacting_google() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "read"],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "PROJECT_ID" in result.stderr


def test_requires_at_least_one_secret_name() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": os.environ["PATH"], "PROJECT_ID": "test-project"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "secret name" in result.stderr
