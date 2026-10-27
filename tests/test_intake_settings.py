from __future__ import annotations

import pytest

from telemetry_collector.app import CollectorSettings
from telemetry_collector.intake import IntakeSettings

INTAKE_ENVIRONMENT = {
    "COLLECTOR_INTAKE_BUCKET": "collector-intake-staging",
    "COLLECTOR_INTAKE_SIGNING_SERVICE_ACCOUNT": (
        "collector-runtime@example-project.iam.gserviceaccount.com"
    ),
    "COLLECTOR_WORKER_QUEUE": (
        "projects/example-project/locations/us-central1/queues/collector-intake"
    ),
    "COLLECTOR_WORKER_JOB": (
        "projects/example-project/locations/us-central1/jobs/collector-worker"
    ),
    "COLLECTOR_WORKER_INVOKER_SERVICE_ACCOUNT": (
        "collector-worker-invoker@example-project.iam.gserviceaccount.com"
    ),
}


@pytest.fixture
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in [*INTAKE_ENVIRONMENT, "COLLECTOR_INTAKE_ENABLED"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEMETRY_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("TELEMETRY_READ_TOKEN", "y" * 24)
    return monkeypatch


def test_intake_is_off_when_the_switch_is_unset(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    for name, value in INTAKE_ENVIRONMENT.items():
        clean_environment.setenv(name, value)

    assert IntakeSettings.from_environment() is None
    assert CollectorSettings.from_environment().intake is None


@pytest.mark.parametrize("switch_value", ["0", "true", "yes", ""])
def test_intake_is_on_only_for_the_value_one(
    clean_environment: pytest.MonkeyPatch, switch_value: str
) -> None:
    clean_environment.setenv("COLLECTOR_INTAKE_ENABLED", switch_value)

    assert IntakeSettings.from_environment() is None


def test_intake_reads_its_resources_when_switched_on(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    clean_environment.setenv("COLLECTOR_INTAKE_ENABLED", "1")
    for name, value in INTAKE_ENVIRONMENT.items():
        clean_environment.setenv(name, value)

    settings = CollectorSettings.from_environment().intake

    assert settings == IntakeSettings(
        bucket="collector-intake-staging",
        signing_service_account=(
            "collector-runtime@example-project.iam.gserviceaccount.com"
        ),
        worker_queue=(
            "projects/example-project/locations/us-central1/queues/collector-intake"
        ),
        worker_job=(
            "projects/example-project/locations/us-central1/jobs/collector-worker"
        ),
        worker_invoker_service_account=(
            "collector-worker-invoker@example-project.iam.gserviceaccount.com"
        ),
    )


@pytest.mark.parametrize("missing_name", sorted(INTAKE_ENVIRONMENT))
def test_switching_intake_on_requires_every_resource(
    clean_environment: pytest.MonkeyPatch, missing_name: str
) -> None:
    clean_environment.setenv("COLLECTOR_INTAKE_ENABLED", "1")
    for name, value in INTAKE_ENVIRONMENT.items():
        if name != missing_name:
            clean_environment.setenv(name, value)

    with pytest.raises(RuntimeError, match=missing_name):
        IntakeSettings.from_environment()


@pytest.mark.parametrize(
    ("name", "malformed_value"),
    [
        ("COLLECTOR_WORKER_QUEUE", "collector-intake"),
        ("COLLECTOR_WORKER_JOB", "projects/example-project/jobs/collector-worker"),
        ("COLLECTOR_INTAKE_SIGNING_SERVICE_ACCOUNT", "collector-runtime"),
        ("COLLECTOR_INTAKE_BUCKET", "gs://collector-intake-staging"),
    ],
)
def test_switching_intake_on_refuses_malformed_resources(
    clean_environment: pytest.MonkeyPatch, name: str, malformed_value: str
) -> None:
    clean_environment.setenv("COLLECTOR_INTAKE_ENABLED", "1")
    for setting_name, value in INTAKE_ENVIRONMENT.items():
        clean_environment.setenv(setting_name, value)
    clean_environment.setenv(name, malformed_value)

    with pytest.raises(RuntimeError, match=name):
        IntakeSettings.from_environment()
