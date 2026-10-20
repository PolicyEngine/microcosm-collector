from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from telemetry_collector.app import CollectorSettings, create_app
from telemetry_collector.auth import HuggingFacePrincipal
from telemetry_collector.intake import IntakeServices
from telemetry_collector.submissions import (
    SubmissionRequest,
    derive_submission_id,
    files_digest,
    intake_object_name,
    receipt_digest,
)
from tests.fakes import (
    FakeIntakeObjectStore,
    FakeSubmissionStore,
    FakeTelemetryStore,
    FakeWorkerQueue,
)
from tests.test_session_scopes import intake_settings
from tests.test_submissions import calibration_submission

DIAGNOSTICS_BYTES = b'{"schema_version": 8}'
MANIFEST_BYTES = b'{"build": "uk-frs-calibration-attempt-1"}'


class SwitchableAuthenticator:
    """Authenticates whichever Hugging Face user the test names."""

    def __init__(self) -> None:
        self.user_id = "hf-user-1"

    def authenticate(self, token: str, required_org: str) -> HuggingFacePrincipal:
        return HuggingFacePrincipal(
            user_id=self.user_id,
            username=f"builder-{self.user_id}",
            organizations=(required_org,),
        )


@dataclass
class IntakeHarness:
    client: TestClient
    submissions: FakeSubmissionStore
    objects: FakeIntakeObjectStore
    worker_queue: FakeWorkerQueue
    authenticator: SwitchableAuthenticator

    def token(self, scope: str = "artifacts:submit") -> str:
        response = self.client.post(
            "/v1/auth/huggingface/exchange",
            headers={"Authorization": "Bearer hf_example"},
            json={"scope": scope},
        )
        assert response.status_code == 200
        return response.json()["access_token"]

    def call(self, method: str, path: str, *, token: str | None = None, **request: Any):
        headers = {"Authorization": f"Bearer {token or self.token()}"}
        return self.client.request(method, path, headers=headers, **request)


def submission_with_real_digests() -> dict[str, Any]:
    submission = calibration_submission()
    for file, content in zip(
        submission["files"], [DIAGNOSTICS_BYTES, MANIFEST_BYTES], strict=True
    ):
        file["size"] = len(content)
        file["sha256"] = hashlib.sha256(content).hexdigest()
    return submission


SUBMISSION_ID = derive_submission_id(
    SubmissionRequest.model_validate(submission_with_real_digests())
)


def upload_everything(harness: IntakeHarness) -> None:
    for file, content in zip(
        submission_with_real_digests()["files"],
        [DIAGNOSTICS_BYTES, MANIFEST_BYTES],
        strict=True,
    ):
        harness.objects.upload(
            intake_object_name(SUBMISSION_ID, file["name"]),
            content,
            sha256=file["sha256"],
        )


@pytest.fixture
def harness() -> IntakeHarness:
    submissions = FakeSubmissionStore()
    objects = FakeIntakeObjectStore()
    worker_queue = FakeWorkerQueue()
    authenticator = SwitchableAuthenticator()
    settings = CollectorSettings(
        jwt_secret="test-secret-that-is-long-enough-32",
        read_token="dashboard-read-token",
        intake=intake_settings(),
    )
    client = TestClient(
        create_app(
            settings=settings,
            store=FakeTelemetryStore(),
            huggingface_authenticator=authenticator,
            intake_services=IntakeServices(
                submissions=submissions,
                objects=objects,
                worker_queue=worker_queue,
            ),
        )
    )
    return IntakeHarness(client, submissions, objects, worker_queue, authenticator)


def create(harness: IntakeHarness, **request: Any):
    return harness.call(
        "POST", "/v1/submissions", json=submission_with_real_digests(), **request
    )


def complete(harness: IntakeHarness):
    return harness.call("POST", f"/v1/submissions/{SUBMISSION_ID}/complete")


def test_submission_endpoints_do_not_exist_while_intake_is_off() -> None:
    client = TestClient(
        create_app(
            settings=CollectorSettings(
                jwt_secret="test-secret-that-is-long-enough-32",
                read_token="dashboard-read-token",
            ),
            store=FakeTelemetryStore(),
            huggingface_authenticator=SwitchableAuthenticator(),
        )
    )

    for method, path in [
        ("POST", "/v1/submissions"),
        ("GET", "/v1/submissions/sub-1"),
        ("POST", "/v1/submissions/sub-1/complete"),
        ("GET", "/v1/submissions/sub-1/receipt"),
    ]:
        response = client.request(method, path, json={})
        assert response.status_code == 404, path
        assert response.json() == {
            "detail": "Artifact intake is not enabled on this collector.",
            "code": "intake_not_enabled",
        }
    assert not any(
        path.startswith("/v1/submissions")
        for path in client.get("/openapi.json").json()["paths"]
    )


def test_switching_intake_on_without_its_services_is_refused() -> None:
    with pytest.raises(ValueError, match="services"):
        create_app(
            settings=CollectorSettings(
                jwt_secret="test-secret-that-is-long-enough-32",
                read_token="dashboard-read-token",
                intake=intake_settings(),
            ),
            store=FakeTelemetryStore(),
            huggingface_authenticator=SwitchableAuthenticator(),
        )


def test_creating_a_submission_returns_an_upload_for_every_file(
    harness: IntakeHarness,
) -> None:
    response = create(harness)

    assert response.status_code == 201
    body = response.json()
    assert body["submission_id"] == SUBMISSION_ID
    assert body["status"] == "awaiting_upload"
    assert body["kind"] == "calibration-diagnostics"
    uploads = {upload["name"]: upload for upload in body["uploads"]}
    assert set(uploads) == {"build_manifest.json", "calibration_diagnostics.json"}
    diagnostics = uploads["calibration_diagnostics.json"]
    assert diagnostics["method"] == "PUT"
    assert diagnostics["url"].startswith("https://")
    assert diagnostics["headers"] == {
        "Content-Type": "application/octet-stream",
        "x-goog-meta-sha256": hashlib.sha256(DIAGNOSTICS_BYTES).hexdigest(),
        "x-goog-content-length-range": (
            f"{len(DIAGNOSTICS_BYTES)},{len(DIAGNOSTICS_BYTES)}"
        ),
        "x-goog-if-generation-match": "0",
    }
    assert diagnostics["expires_at"]


def test_creating_again_returns_200_and_only_files_still_missing(
    harness: IntakeHarness,
) -> None:
    create(harness)
    harness.objects.upload(
        intake_object_name(SUBMISSION_ID, "calibration_diagnostics.json"),
        DIAGNOSTICS_BYTES,
        sha256=hashlib.sha256(DIAGNOSTICS_BYTES).hexdigest(),
    )

    response = create(harness)

    assert response.status_code == 200
    assert [upload["name"] for upload in response.json()["uploads"]] == [
        "build_manifest.json"
    ]


def test_a_telemetry_token_cannot_submit_artifacts(harness: IntakeHarness) -> None:
    response = create(harness, token=harness.token("telemetry:write"))

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Collector credential cannot submit artifacts."
    }


def test_a_submission_breaking_the_contract_is_refused(
    harness: IntakeHarness,
) -> None:
    submission = submission_with_real_digests()
    del submission["licensed_data"]

    response = harness.call("POST", "/v1/submissions", json=submission)

    assert response.status_code == 422


def test_another_user_cannot_create_read_or_complete_a_submission(
    harness: IntakeHarness,
) -> None:
    create(harness)
    harness.authenticator.user_id = "hf-user-2"

    responses = [
        create(harness),
        harness.call("GET", f"/v1/submissions/{SUBMISSION_ID}"),
        complete(harness),
        harness.call("GET", f"/v1/submissions/{SUBMISSION_ID}/receipt"),
    ]

    for response in responses:
        assert response.status_code == 403
        assert response.json() == {
            "detail": "Submission belongs to another Hugging Face user.",
            "code": "submission_owned_by_another_user",
        }


def test_completion_lists_missing_and_mismatched_files(
    harness: IntakeHarness,
) -> None:
    create(harness)
    harness.objects.upload(
        intake_object_name(SUBMISSION_ID, "calibration_diagnostics.json"),
        DIAGNOSTICS_BYTES + b" ",
        sha256=hashlib.sha256(DIAGNOSTICS_BYTES).hexdigest(),
    )

    response = complete(harness)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Some files are not stored as declared.",
        "code": "files_missing",
        "problems": [
            {"name": "build_manifest.json", "problem": "missing"},
            {"name": "calibration_diagnostics.json", "problem": "size_mismatch"},
        ],
    }
    assert harness.worker_queue.calls == []
    status = harness.call("GET", f"/v1/submissions/{SUBMISSION_ID}").json()
    assert status["status"] == "awaiting_upload"


def test_completion_refuses_an_object_recording_another_digest(
    harness: IntakeHarness,
) -> None:
    create(harness)
    upload_everything(harness)
    manifest_object = intake_object_name(SUBMISSION_ID, "build_manifest.json")
    harness.objects.upload(manifest_object, MANIFEST_BYTES, sha256="0" * 64)

    response = complete(harness)

    assert response.status_code == 409
    assert response.json()["problems"] == [
        {"name": "build_manifest.json", "problem": "sha256_mismatch"}
    ]


def test_completion_queues_one_job_and_enqueues_it_once(
    harness: IntakeHarness,
) -> None:
    create(harness)
    upload_everything(harness)

    first = complete(harness)
    second = complete(harness)

    assert first.status_code == 202
    assert first.json()["status"] == "queued"
    assert second.status_code == 202
    record = harness.submissions.get_submission(SUBMISSION_ID)
    assert record is not None
    assert harness.worker_queue.calls == [record.job_id]
    assert record.job_enqueued is True
    assert create(harness).json()["uploads"] == []


def test_completion_enqueues_again_after_the_queue_was_unavailable(
    harness: IntakeHarness,
) -> None:
    create(harness)
    upload_everything(harness)
    harness.worker_queue.available = False

    unavailable = complete(harness)
    harness.worker_queue.available = True
    retried = complete(harness)

    assert unavailable.status_code == 503
    assert unavailable.headers["Retry-After"] == "30"
    assert unavailable.json() == {
        "detail": "The worker queue is unavailable; retry completion.",
        "code": "worker_queue_unavailable",
    }
    assert retried.status_code == 202
    record = harness.submissions.get_submission(SUBMISSION_ID)
    assert record is not None
    assert harness.worker_queue.enqueued == [record.job_id]


def finish_successfully(harness: IntakeHarness) -> None:
    create(harness)
    upload_everything(harness)
    complete(harness)
    record = harness.submissions.get_submission(SUBMISSION_ID)
    assert record is not None and record.job_id is not None
    harness.submissions.claim_job(record.job_id, holder="worker", lease_seconds=60)
    harness.submissions.finish_job(
        record.job_id,
        holder="worker",
        status="succeeded",
        result={"verified_files": 2},
        rejection=None,
    )


def test_completion_of_a_finished_submission_returns_200(
    harness: IntakeHarness,
) -> None:
    finish_successfully(harness)

    response = complete(harness)

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


def test_status_and_receipt_of_an_unknown_submission_are_not_found(
    harness: IntakeHarness,
) -> None:
    for method, path in [
        ("GET", "/v1/submissions/sub-unknown"),
        ("GET", "/v1/submissions/sub-unknown/receipt"),
        ("POST", "/v1/submissions/sub-unknown/complete"),
    ]:
        response = harness.call(method, path)
        assert response.status_code == 404
        assert response.json() == {
            "detail": "Submission was not found.",
            "code": "submission_not_found",
        }


def test_the_receipt_is_refused_until_the_submission_is_final(
    harness: IntakeHarness,
) -> None:
    create(harness)

    response = harness.call("GET", f"/v1/submissions/{SUBMISSION_ID}/receipt")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Submission is not final yet.",
        "code": "submission_not_final",
        "status": "awaiting_upload",
    }


def test_the_receipt_records_files_generations_result_and_its_digest(
    harness: IntakeHarness,
) -> None:
    finish_successfully(harness)

    receipt = harness.call("GET", f"/v1/submissions/{SUBMISSION_ID}/receipt").json()

    assert receipt["contract_version"] == 1
    assert receipt["submission_id"] == SUBMISSION_ID
    assert receipt["status"] == "succeeded"
    assert receipt["licensed_data"] is True
    assert receipt["provenance"] == submission_with_real_digests()["provenance"]
    assert receipt["submitted_by"] == "builder-hf-user-1"
    assert [file["name"] for file in receipt["files"]] == [
        "build_manifest.json",
        "calibration_diagnostics.json",
    ]
    assert all(file["generation"] for file in receipt["files"])
    assert receipt["job"]["attempts"] == 1
    assert receipt["result"] == {"verified_files": 2}
    assert receipt["rejection"] is None
    assert receipt["files_sha256"] == files_digest(receipt["files"])
    assert "inventory_sha256" not in receipt
    assert receipt["receipt_sha256"] == receipt_digest(receipt)


def test_status_reports_progress_without_upload_urls(
    harness: IntakeHarness,
) -> None:
    create(harness)

    status = harness.call("GET", f"/v1/submissions/{SUBMISSION_ID}").json()

    assert status["status"] == "awaiting_upload"
    assert "uploads" not in status
    assert status["files_sha256"] == files_digest(
        submission_with_real_digests()["files"]
    )
    assert "inventory_sha256" not in status


class StandInIntakeAdapter:
    def __init__(self, **settings: str) -> None:
        self.settings = settings


@pytest.mark.parametrize(("switch", "mounted"), [("1", True), (None, False)])
def test_the_deployed_app_mounts_intake_only_when_switched_on(
    monkeypatch: pytest.MonkeyPatch, switch: str | None, mounted: bool
) -> None:
    import telemetry_collector.google_cloud as google_cloud
    from telemetry_collector.app import create_app_from_environment
    from tests.test_intake_settings import INTAKE_ENVIRONMENT

    monkeypatch.setattr(google_cloud, "GoogleCloudStorageIntake", StandInIntakeAdapter)
    monkeypatch.setattr(google_cloud, "CloudTasksWorkerQueue", StandInIntakeAdapter)
    monkeypatch.setenv("TELEMETRY_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("TELEMETRY_READ_TOKEN", "y" * 24)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@127.0.0.1:1/none")
    for name, value in INTAKE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    if switch is None:
        monkeypatch.delenv("COLLECTOR_INTAKE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("COLLECTOR_INTAKE_ENABLED", switch)

    paths = create_app_from_environment().openapi()["paths"]

    assert ("/v1/submissions" in paths) is mounted
    assert ("/v1/submissions/{submission_id}/complete" in paths) is mounted
