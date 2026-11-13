"""``POST /v1/calibration-tree-builds``: a Vercel OIDC caller queues a tree build.

The dashboard's Hugging Face webhook asks the collector to build the trees of
one country and kind. The collector checks the caller's Vercel OIDC token
against its own list of allowed callers, records the request, and creates one
named Cloud Task per country, kind and 10-minute window, which runs the
worker job's ``reconcile-calibration-trees`` command at the window's end.
These tests stand in for Vercel's key set, the request table and the queue.
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import inspect
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from telemetry_collector.app import CollectorSettings, create_app
from telemetry_collector.intake import IntakeServices, WorkerQueueUnavailableError
from telemetry_collector.vercel_oidc import (
    VercelOidcReader,
    VercelOidcSettings,
    VercelOidcVerifier,
)
from tests.fakes import (
    FakeIntakeObjectStore,
    FakeSubmissionStore,
    FakeTelemetryStore,
    FakeWorkerQueue,
)
from tests.read_api_contract import ContractAuthenticator
from tests.test_session_scopes import intake_settings
from tests.vercel_oidc_fakes import (
    DASHBOARD_PROJECT_ID,
    OTHER_PROJECT_ID,
    SETTINGS_ENVIRONMENT,
    FakeClock,
    FakeKeySetAddress,
    key_set_answer,
    rsa_private_key,
    rsa_public_jwk,
    signed_token,
    token_claims,
)

BUILDS_MODULE = "telemetry_collector.calibration_tree_builds"
BUILDS_PATH = "/v1/calibration-tree-builds"
SIGNING_KEY = rsa_private_key()
SIGNING_KEY_ID = "signing-key-1"
#: The dashboard's production deployments may queue builds; its preview
#: deployments may only read.
DASHBOARD_PRODUCTION = VercelOidcReader(DASHBOARD_PROJECT_ID, "production")
CALLERS = frozenset({DASHBOARD_PRODUCTION})
READERS_ONLY_PREVIEW = {
    **SETTINGS_ENVIRONMENT,
    "TELEMETRY_VERCEL_OIDC_READERS": "prj_dashboard:preview",
}
#: 2026-11-13T10:03:20Z, 200 seconds into its 10-minute window.
REQUESTED_AT = datetime(2026, 11, 13, 10, 3, 20, tzinfo=UTC)
WINDOW = int(REQUESTED_AT.timestamp()) // 600
WINDOW_END = datetime.fromtimestamp((WINDOW + 1) * 600, UTC)
UK_STAGING_TASK = f"calibration-trees-uk-staging-{WINDOW}"
COMMIT = "a" * 40
UK_STAGING_REQUEST = {
    "country": "uk",
    "kind": "staging",
    "hf_repo": "policyengine/populace-uk-staging",
    "hf_ref": "refs/heads/main",
    "hf_commit": COMMIT,
}
US_RELEASE_REQUEST = {
    "country": "us",
    "kind": "release",
    "hf_repo": "policyengine/populace-us",
    "hf_ref": "refs/tags/microcosm-us-2025-20261020T090000Z-0a1b2c3d",
    "hf_commit": "b" * 40,
}

TOKEN_REQUIRED = {"detail": "A Vercel OIDC token is required."}
INVALID_TOKEN = {"detail": "The Vercel OIDC token is invalid or expired."}
CALLER_NOT_ALLOWED = {
    "detail": (
        "This Vercel project and environment may not queue calibration tree builds."
    )
}
KEYS_UNAVAILABLE = {"detail": "Vercel's token signing keys could not be read."}


def builds_module() -> ModuleType:
    if importlib.util.find_spec(BUILDS_MODULE) is None:
        pytest.fail(f"{BUILDS_MODULE} does not exist yet.")
    return importlib.import_module(BUILDS_MODULE)


def require_collector_support() -> None:
    """Fails, rather than raising TypeError, while the collector lacks the
    callers setting or the endpoint's services."""

    if "calibration_tree_build_callers" not in {
        settings_field.name for settings_field in dataclasses.fields(CollectorSettings)
    }:
        pytest.fail("CollectorSettings has no calibration_tree_build_callers yet.")
    if (
        "calibration_tree_build_services"
        not in inspect.signature(create_app).parameters
    ):
        pytest.fail("create_app takes no calibration_tree_build_services yet.")


# Stand-ins -------------------------------------------------------------------


@dataclass
class BuildRequests:
    """The request table, in memory: one row per task ID."""

    events: list[tuple[str, str]]
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    available: bool = True

    def record_request(
        self,
        *,
        task_id: str,
        country: str,
        kind: str,
        caller: VercelOidcReader,
        hf_repo: str,
        hf_ref: str | None,
        hf_commit: str | None,
        not_before: datetime,
        requested_at: datetime,
    ) -> None:
        self.events.append(("record", task_id))
        if not self.available:
            raise builds_module().BuildRequestStoreUnavailableError("unavailable")
        row = self.rows.setdefault(
            task_id,
            {
                "task_id": task_id,
                "country": country,
                "kind": kind,
                "first_requested_at": requested_at,
                "request_count": 0,
                "queued_at": None,
                "execution": None,
                "attempts": 0,
                "started_at": None,
                "finished_at": None,
                "summary": None,
            },
        )
        row.update(
            last_requested_at=requested_at,
            request_count=row["request_count"] + 1,
            last_caller_project_id=caller.project_id,
            last_caller_environment=caller.environment,
            hf_repo=hf_repo,
            last_hf_ref=hf_ref,
            last_hf_commit=hf_commit,
            not_before=not_before,
        )

    def mark_queued(self, task_id: str, *, at: datetime) -> None:
        self.events.append(("queued", task_id))
        if not self.available:
            raise builds_module().BuildRequestStoreUnavailableError("unavailable")
        row = self.rows[task_id]
        row["queued_at"] = row["queued_at"] or at


@dataclass
class BuildQueue:
    """The worker queue's named, scheduled tasks."""

    events: list[tuple[str, str]]
    calls: list[dict[str, Any]] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    available: bool = True

    def schedule_run(
        self, task_id: str, *, run_at: datetime, arguments: Sequence[str]
    ) -> bool:
        self.events.append(("schedule", task_id))
        self.calls.append(
            {"task_id": task_id, "run_at": run_at, "arguments": list(arguments)}
        )
        if not self.available:
            raise WorkerQueueUnavailableError("The worker queue is unavailable.")
        if task_id in self.task_ids:
            return False
        self.task_ids.append(task_id)
        return True


class SteppingClock:
    def __init__(self, now: datetime = REQUESTED_AT) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class BuildCollector:
    client: TestClient
    requests: BuildRequests
    queue: BuildQueue
    clock: SteppingClock
    key_set_address: FakeKeySetAddress
    events: list[tuple[str, str]]

    def post(self, body: Any, *, token: str | None = None, **request: Any):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        if isinstance(body, bytes):
            return self.client.post(
                BUILDS_PATH,
                content=body,
                headers={**headers, "Content-Type": "application/json"},
            )
        return self.client.post(BUILDS_PATH, json=body, headers=headers, **request)


def build_collector(
    *,
    callers: frozenset[VercelOidcReader] | None = CALLERS,
    intake: bool = True,
    oidc_environment: dict[str, str] = SETTINGS_ENVIRONMENT,
    key_set: httpx.Response | Exception | None = None,
) -> BuildCollector:
    require_collector_support()
    module = builds_module()
    oidc_settings = VercelOidcSettings.from_environment(oidc_environment)
    assert oidc_settings is not None
    settings = CollectorSettings(
        jwt_secret="test-secret-that-is-long-enough-32",
        read_token="dashboard-read-token",
        intake=intake_settings() if intake else None,
        vercel_oidc=oidc_settings,
        calibration_tree_build_callers=callers,
    )
    key_set_address = FakeKeySetAddress(
        key_set_answer(rsa_public_jwk(SIGNING_KEY, SIGNING_KEY_ID))
        if key_set is None
        else key_set
    )
    events: list[tuple[str, str]] = []
    requests = BuildRequests(events)
    queue = BuildQueue(events)
    clock = SteppingClock()
    client = TestClient(
        create_app(
            settings=settings,
            store=FakeTelemetryStore(),
            huggingface_authenticator=ContractAuthenticator(),
            intake_services=(
                IntakeServices(
                    submissions=FakeSubmissionStore(),
                    objects=FakeIntakeObjectStore(),
                    worker_queue=FakeWorkerQueue(),
                )
                if intake
                else None
            ),
            vercel_oidc_verifier=VercelOidcVerifier(
                oidc_settings, http_get=key_set_address.get, clock=FakeClock()
            ),
            calibration_tree_build_services=module.CalibrationTreeBuildServices(
                requests=requests, queue=queue, clock=clock
            ),
        )
    )
    return BuildCollector(client, requests, queue, clock, key_set_address, events)


def caller_token(**claim_changes: Any) -> str:
    claims = {"environment": "production", **claim_changes}
    return signed_token(SIGNING_KEY, SIGNING_KEY_ID, token_claims(**claims))


def reconcile_arguments(country: str, kind: str, task_id: str) -> list[str]:
    return [
        "run",
        "--no-sync",
        "python",
        "-m",
        "telemetry_collector.operations",
        "reconcile-calibration-trees",
        "--country",
        country,
        "--kind",
        kind,
        "--request-id",
        task_id,
    ]


def logged_requests(output: str) -> list[dict[str, Any]]:
    documents = [json.loads(text) for text in output.splitlines() if text.strip()]
    return [
        document["calibration_tree_build_request"]
        for document in documents
        if "calibration_tree_build_request" in document
    ]


# Queuing -------------------------------------------------------------------------


def test_a_caller_s_request_queues_one_named_task_for_its_window_s_end() -> None:
    collector = build_collector()

    response = collector.post(UK_STAGING_REQUEST, token=caller_token())

    assert response.status_code == 202, response.text
    assert response.json() == {
        "status": "queued",
        "task_id": UK_STAGING_TASK,
        "country": "uk",
        "kind": "staging",
        "not_before": WINDOW_END.isoformat(),
    }
    assert collector.queue.calls == [
        {
            "task_id": UK_STAGING_TASK,
            "run_at": WINDOW_END,
            "arguments": reconcile_arguments("uk", "staging", UK_STAGING_TASK),
        }
    ]
    # The row is written before the task is created, and marked queued after.
    assert collector.events == [
        ("record", UK_STAGING_TASK),
        ("schedule", UK_STAGING_TASK),
        ("queued", UK_STAGING_TASK),
    ]
    row = collector.requests.rows[UK_STAGING_TASK]
    assert row["country"] == "uk"
    assert row["kind"] == "staging"
    assert row["request_count"] == 1
    assert row["first_requested_at"] == REQUESTED_AT
    assert row["last_caller_project_id"] == DASHBOARD_PROJECT_ID
    assert row["last_caller_environment"] == "production"
    assert row["hf_repo"] == "policyengine/populace-uk-staging"
    assert row["last_hf_ref"] == "refs/heads/main"
    assert row["last_hf_commit"] == COMMIT
    assert row["not_before"] == WINDOW_END
    assert row["queued_at"] == REQUESTED_AT


def test_a_release_request_queues_a_release_run_of_its_country() -> None:
    collector = build_collector()
    task_id = f"calibration-trees-us-release-{WINDOW}"

    response = collector.post(US_RELEASE_REQUEST, token=caller_token())

    assert response.status_code == 202, response.text
    assert response.json()["task_id"] == task_id
    assert collector.queue.calls[0]["arguments"] == reconcile_arguments(
        "us", "release", task_id
    )


def test_belgium_s_release_repo_is_accepted_for_a_release_run() -> None:
    collector = build_collector()

    response = collector.post(
        {
            "country": "be",
            "kind": "release",
            "hf_repo": "policyengine/populace-be-private",
        },
        token=caller_token(),
    )

    assert response.status_code == 202, response.text
    assert response.json()["task_id"] == f"calibration-trees-be-release-{WINDOW}"
    row = collector.requests.rows[response.json()["task_id"]]
    assert row["last_hf_ref"] is None
    assert row["last_hf_commit"] is None


def test_a_later_request_in_the_same_window_is_already_queued() -> None:
    collector = build_collector()
    first = collector.post(UK_STAGING_REQUEST, token=caller_token())
    collector.clock.now = REQUESTED_AT + timedelta(seconds=300)

    second = collector.post(
        {**UK_STAGING_REQUEST, "hf_commit": "c" * 40}, token=caller_token()
    )

    assert first.status_code == 202
    assert second.status_code == 202, second.text
    assert second.json() == {**first.json(), "status": "already_queued"}
    assert collector.queue.task_ids == [UK_STAGING_TASK]
    row = collector.requests.rows[UK_STAGING_TASK]
    assert row["request_count"] == 2
    assert row["first_requested_at"] == REQUESTED_AT
    assert row["last_requested_at"] == REQUESTED_AT + timedelta(seconds=300)
    assert row["last_hf_commit"] == "c" * 40
    # Cloud Tasks first accepted it at the first request.
    assert row["queued_at"] == REQUESTED_AT


def test_a_request_in_the_next_window_queues_another_task() -> None:
    collector = build_collector()
    collector.post(UK_STAGING_REQUEST, token=caller_token())
    collector.clock.now = WINDOW_END

    response = collector.post(UK_STAGING_REQUEST, token=caller_token())

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["task_id"] == f"calibration-trees-uk-staging-{WINDOW + 1}"
    assert (
        response.json()["not_before"]
        == (WINDOW_END + timedelta(seconds=600)).isoformat()
    )
    assert len(collector.queue.task_ids) == 2


def test_each_country_and_kind_has_its_own_task_in_a_window() -> None:
    collector = build_collector()

    for body in (
        UK_STAGING_REQUEST,
        {
            **UK_STAGING_REQUEST,
            "kind": "release",
            "hf_repo": "policyengine/populace-uk-private",
        },
        US_RELEASE_REQUEST,
    ):
        assert collector.post(body, token=caller_token()).status_code == 202

    assert collector.queue.task_ids == [
        UK_STAGING_TASK,
        f"calibration-trees-uk-release-{WINDOW}",
        f"calibration-trees-us-release-{WINDOW}",
    ]


# Who may call ----------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic ZGFzaGJvYXJkOnNlY3JldA=="},
        {"Authorization": "Bearer"},
        {"X-Telemetry-Read-Token": "dashboard-read-token"},
    ],
    ids=["no credential", "basic", "an empty bearer", "the read credential"],
)
def test_a_request_without_a_bearer_token_is_refused(headers: dict[str, str]) -> None:
    collector = build_collector()

    response = collector.client.post(
        BUILDS_PATH, json=UK_STAGING_REQUEST, headers=headers
    )

    assert response.status_code == 401
    assert response.json() == TOKEN_REQUIRED
    assert collector.events == []


@pytest.mark.parametrize(
    "token",
    [
        lambda: caller_token(exp=int(datetime.now(UTC).timestamp()) - 120),
        lambda: "not-a-jwt",
        lambda: caller_token(iss="https://oidc.vercel.com/other-team"),
    ],
    ids=["expired", "not a JWT", "another issuer"],
)
def test_an_invalid_or_expired_token_is_refused(token: Any) -> None:
    collector = build_collector()
    presented = token()

    response = collector.post(UK_STAGING_REQUEST, token=presented)

    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN
    assert presented not in response.text
    assert collector.events == []


@pytest.mark.parametrize(
    "claim_changes",
    [
        {"owner_id": "team_other"},
        {"project_id": OTHER_PROJECT_ID},
        {"environment": "development"},
        # A reader, but not a caller.
        {"environment": "preview"},
    ],
    ids=["another team", "another project", "another environment", "a reader"],
)
def test_a_valid_token_of_a_project_that_is_not_a_caller_is_refused(
    claim_changes: dict[str, str],
) -> None:
    collector = build_collector()

    response = collector.post(UK_STAGING_REQUEST, token=caller_token(**claim_changes))

    assert response.status_code == 403
    assert response.json() == CALLER_NOT_ALLOWED
    assert collector.events == []


def test_a_caller_need_not_be_a_reader() -> None:
    collector = build_collector(oidc_environment=READERS_ONLY_PREVIEW)

    response = collector.post(UK_STAGING_REQUEST, token=caller_token())

    assert response.status_code == 202, response.text


def test_the_request_answers_503_while_vercel_s_keys_cannot_be_read() -> None:
    collector = build_collector(key_set=httpx.ConnectError("Vercel cannot be reached."))

    response = collector.post(UK_STAGING_REQUEST, token=caller_token())

    assert response.status_code == 503
    assert response.json() == KEYS_UNAVAILABLE
    assert "Retry-After" not in response.headers
    assert collector.events == []


# The request -------------------------------------------------------------------

INVALID_BODIES: dict[str, Any] = {
    "an unknown field": {**UK_STAGING_REQUEST, "branch": "main"},
    "an unregistered country": {**UK_STAGING_REQUEST, "country": "fr"},
    "an upper-case country": {**UK_STAGING_REQUEST, "country": "UK"},
    "staging for Belgium": {
        "country": "be",
        "kind": "staging",
        "hf_repo": "policyengine/populace-be-private",
    },
    "another kind": {**UK_STAGING_REQUEST, "kind": "comparison"},
    "no country": {
        name: value for name, value in UK_STAGING_REQUEST.items() if name != "country"
    },
    "no kind": {
        name: value for name, value in UK_STAGING_REQUEST.items() if name != "kind"
    },
    "no repo": {
        name: value for name, value in UK_STAGING_REQUEST.items() if name != "hf_repo"
    },
    "a malformed repo": {**UK_STAGING_REQUEST, "hf_repo": "populace-uk-staging"},
    "a short commit": {**UK_STAGING_REQUEST, "hf_commit": "a" * 39},
    "an upper-case commit": {**UK_STAGING_REQUEST, "hf_commit": "A" * 40},
    "a ref outside heads and tags": {**UK_STAGING_REQUEST, "hf_ref": "refs/pr/1"},
    "a branch name instead of a ref": {**UK_STAGING_REQUEST, "hf_ref": "main"},
    "a number as country": {**UK_STAGING_REQUEST, "country": 7},
    "a list": [UK_STAGING_REQUEST],
}


@pytest.mark.parametrize("case", sorted(INVALID_BODIES))
def test_an_invalid_body_is_refused_as_fastapi_refuses_one(case: str) -> None:
    collector = build_collector()

    response = collector.post(INVALID_BODIES[case], token=caller_token())

    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)
    assert collector.events == []


def test_a_body_that_is_not_json_is_refused() -> None:
    collector = build_collector()

    response = collector.post(b"{not json", token=caller_token())

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert collector.events == []


@pytest.mark.parametrize(
    "unknown_key",
    ["a-key-only-this-caller-chose-7f3c", "x" * 4096],
    ids=["a-chosen-key", "a-long-key"],
)
def test_a_422_for_an_unknown_field_repeats_none_of_the_body(unknown_key: str) -> None:
    # Review round 1, bug 3: pydantic's loc for extra_forbidden is the
    # caller's own key, so the answer repeated it.
    collector = build_collector()

    response = collector.post(
        {**UK_STAGING_REQUEST, unknown_key: "a value"}, token=caller_token()
    )

    assert response.status_code == 422
    assert unknown_key not in response.text
    assert "a value" not in response.text
    assert response.json()["detail"] == [
        {
            "type": "extra_forbidden",
            "loc": ["body", "(unknown field)"],
            "msg": "Extra inputs are not permitted",
        }
    ]


def test_a_422_names_the_request_s_own_fields() -> None:
    collector = build_collector()

    response = collector.post(
        {**UK_STAGING_REQUEST, "hf_commit": "A" * 40}, token=caller_token()
    )

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "hf_commit"]
    assert "A" * 40 not in response.text


@pytest.mark.parametrize("case", ["an unknown field", "a list"])
def test_the_token_is_checked_before_the_body(case: str) -> None:
    collector = build_collector()

    without_token = collector.post(INVALID_BODIES[case])
    not_a_caller = collector.post(
        INVALID_BODIES[case], token=caller_token(project_id=OTHER_PROJECT_ID)
    )
    not_json = collector.post(b"{not json")

    assert without_token.status_code == 401
    assert not_a_caller.status_code == 403
    assert not_json.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {**UK_STAGING_REQUEST, "hf_repo": "policyengine/populace-uk-private"},
        {**US_RELEASE_REQUEST, "hf_repo": "policyengine/populace-us-staging"},
        {**US_RELEASE_REQUEST, "hf_repo": "policyengine/populace-uk-private"},
        {**UK_STAGING_REQUEST, "hf_repo": "someone-else/populace-uk-staging"},
    ],
    ids=[
        "release repo for staging",
        "staging repo for release",
        "another country's",
        "another owner's",
    ],
)
def test_a_repo_that_is_not_the_collector_s_for_the_country_and_kind_is_refused(
    body: dict[str, Any],
) -> None:
    collector = build_collector()

    response = collector.post(body, token=caller_token())

    assert response.status_code == 422
    assert response.json()["code"] == "repo_not_registered"
    assert body["hf_repo"] in response.json()["detail"]
    assert collector.events == []


# When the queue or the table is unavailable -------------------------------------


def test_an_unavailable_queue_answers_503_with_retry_after() -> None:
    collector = build_collector()
    collector.queue.available = False

    response = collector.post(UK_STAGING_REQUEST, token=caller_token())

    assert response.status_code == 503
    assert response.json()["code"] == "worker_queue_unavailable"
    assert response.headers["Retry-After"] == "30"
    # The request is recorded, but not as queued.
    assert collector.requests.rows[UK_STAGING_TASK]["queued_at"] is None


def test_a_retry_after_an_unavailable_queue_queues_the_window_s_task() -> None:
    collector = build_collector()
    collector.queue.available = False
    collector.post(UK_STAGING_REQUEST, token=caller_token())
    collector.queue.available = True

    response = collector.post(UK_STAGING_REQUEST, token=caller_token())

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    row = collector.requests.rows[UK_STAGING_TASK]
    assert row["request_count"] == 2
    assert row["queued_at"] == REQUESTED_AT


def test_an_unavailable_request_table_answers_503_before_any_task_is_created() -> None:
    collector = build_collector()
    collector.requests.available = False

    response = collector.post(UK_STAGING_REQUEST, token=caller_token())

    assert response.status_code == 503
    assert response.json()["code"] == "worker_queue_unavailable"
    assert response.headers["Retry-After"] == "30"
    assert collector.queue.calls == []


# Not enabled -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("callers", "intake"),
    [(None, True), (CALLERS, False), (None, False)],
    ids=["without callers", "without intake", "without either"],
)
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_without_callers_or_intake_the_path_answers_not_enabled(
    callers: frozenset[VercelOidcReader] | None, intake: bool, method: str
) -> None:
    collector = build_collector(callers=callers, intake=intake)

    response = collector.client.request(
        method,
        BUILDS_PATH,
        json=UK_STAGING_REQUEST,
        headers={"Authorization": f"Bearer {caller_token()}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "calibration_tree_builds_not_enabled"
    assert collector.events == []
    assert collector.key_set_address.requests == []


def test_callers_need_the_vercel_oidc_settings() -> None:
    require_collector_support()

    with pytest.raises(ValueError):
        CollectorSettings(
            jwt_secret="test-secret-that-is-long-enough-32",
            read_token="dashboard-read-token",
            intake=intake_settings(),
            calibration_tree_build_callers=CALLERS,
        )


def test_the_collector_refuses_callers_without_the_endpoint_s_services() -> None:
    require_collector_support()
    settings = CollectorSettings(
        jwt_secret="test-secret-that-is-long-enough-32",
        read_token="dashboard-read-token",
        intake=intake_settings(),
        vercel_oidc=VercelOidcSettings.from_environment(SETTINGS_ENVIRONMENT),
        calibration_tree_build_callers=CALLERS,
    )

    with pytest.raises(ValueError):
        create_app(
            settings=settings,
            store=FakeTelemetryStore(),
            huggingface_authenticator=ContractAuthenticator(),
            intake_services=IntakeServices(
                submissions=FakeSubmissionStore(),
                objects=FakeIntakeObjectStore(),
                worker_queue=FakeWorkerQueue(),
            ),
        )


def test_the_endpoint_stays_out_of_read_api_v2_s_document() -> None:
    collector = build_collector()

    document = collector.client.get("/v2/openapi.json").json()

    assert not any("calibration-tree" in path for path in document["paths"])


# What is logged and kept --------------------------------------------------------


def test_each_request_writes_one_json_log_document(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collector = build_collector()
    capsys.readouterr()

    collector.post(UK_STAGING_REQUEST, token=caller_token())
    collector.post(UK_STAGING_REQUEST, token=caller_token())
    collector.post(UK_STAGING_REQUEST, token=caller_token(project_id=OTHER_PROJECT_ID))
    collector.post(UK_STAGING_REQUEST)

    logged = logged_requests(capsys.readouterr().out)
    assert len(logged) == 4
    assert logged[0] == {
        "caller_project_id": DASHBOARD_PROJECT_ID,
        "caller_environment": "production",
        "country": "uk",
        "kind": "staging",
        "hf_repo": "policyengine/populace-uk-staging",
        "hf_ref": "refs/heads/main",
        "hf_commit": COMMIT,
        "task_id": UK_STAGING_TASK,
        "not_before": WINDOW_END.isoformat(),
        "outcome": "queued",
        "status": 202,
        "code": None,
    }
    assert logged[1]["outcome"] == "already_queued"
    assert logged[2]["outcome"] == "refused"
    assert logged[2]["status"] == 403
    assert logged[2]["caller_project_id"] == OTHER_PROJECT_ID
    assert logged[3]["outcome"] == "refused"
    assert logged[3]["status"] == 401
    assert logged[3]["caller_project_id"] is None


def test_refusals_after_the_token_are_logged_with_their_status_and_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collector = build_collector()
    capsys.readouterr()

    collector.post(
        {**UK_STAGING_REQUEST, "hf_repo": "policyengine/populace-uk-private"},
        token=caller_token(),
    )
    collector.post(INVALID_BODIES["an unknown field"], token=caller_token())
    collector.queue.available = False
    collector.post(UK_STAGING_REQUEST, token=caller_token())

    logged = logged_requests(capsys.readouterr().out)
    assert [(entry["status"], entry["code"]) for entry in logged] == [
        (422, "repo_not_registered"),
        (422, "invalid_request"),
        (503, "worker_queue_unavailable"),
    ]
    # An invalid body's values are not logged.
    assert logged[1]["country"] is None
    assert logged[1]["hf_repo"] is None


def test_no_token_reaches_an_answer_a_log_document_or_a_row(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collector = build_collector()
    tokens = [
        caller_token(),
        caller_token(),
        caller_token(project_id=OTHER_PROJECT_ID),
        caller_token(exp=int(datetime.now(UTC).timestamp()) - 120),
    ]
    answers = [collector.post(UK_STAGING_REQUEST, token=token) for token in tokens]
    answers.append(
        collector.post({**UK_STAGING_REQUEST, "extra": tokens[0]}, token=tokens[0])
    )

    output = capsys.readouterr()
    stored = json.dumps(collector.requests.rows, default=str)
    for token in tokens:
        for part in token.split("."):
            for answer in answers:
                assert part not in answer.text
            assert part not in output.out
            assert part not in output.err
            assert part not in stored


def test_the_maintenance_mode_answer_stays_in_front_of_the_endpoint() -> None:
    require_collector_support()
    events: list[tuple[str, str]] = []
    requests = BuildRequests(events)
    queue = BuildQueue(events)
    settings = CollectorSettings(
        jwt_secret="test-secret-that-is-long-enough-32",
        read_token="dashboard-read-token",
        intake=intake_settings(),
        vercel_oidc=VercelOidcSettings.from_environment(SETTINGS_ENVIRONMENT),
        calibration_tree_build_callers=CALLERS,
        maintenance_mode=True,
    )
    client = TestClient(
        create_app(
            settings=settings,
            store=FakeTelemetryStore(),
            huggingface_authenticator=ContractAuthenticator(),
            intake_services=IntakeServices(
                submissions=FakeSubmissionStore(),
                objects=FakeIntakeObjectStore(),
                worker_queue=FakeWorkerQueue(),
            ),
            calibration_tree_build_services=builds_module().CalibrationTreeBuildServices(
                requests=requests, queue=queue, clock=SteppingClock()
            ),
        )
    )

    response = client.post(
        BUILDS_PATH,
        json=UK_STAGING_REQUEST,
        headers={"Authorization": f"Bearer {caller_token()}"},
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert events == []
