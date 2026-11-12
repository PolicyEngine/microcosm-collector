"""The queued tree build's task name, its worker job arguments and its table row.

Each request for a country and kind falls in a 10-minute window, which names
one Cloud Task and schedules it for the window's end. The task runs the worker
job's ``reconcile-calibration-trees`` command with arguments built only from
constants and checked values. The ``calibration_tree_build_requests`` table
keeps one row per task: the service counts the requests and records when
Cloud Tasks accepted the task, and the execution records its attempts,
finish and summary.
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from types import ModuleType
from typing import Any

import pytest

from telemetry_collector import operations
from telemetry_collector.database import create_database_engine
from telemetry_collector.migrate import upgrade_database
from telemetry_collector.vercel_oidc import VercelOidcReader

BUILDS_MODULE = "telemetry_collector.calibration_tree_builds"
WINDOW_SECONDS = 600
REQUESTED_AT = datetime(2026, 11, 13, 10, 3, 20, tzinfo=UTC)
DASHBOARD_PRODUCTION = VercelOidcReader("prj_dashboard", "production")


def builds_module() -> ModuleType:
    if importlib.util.find_spec(BUILDS_MODULE) is None:
        pytest.fail(f"{BUILDS_MODULE} does not exist yet.")
    return importlib.import_module(BUILDS_MODULE)


def builds_attribute(name: str) -> Any:
    attribute = getattr(builds_module(), name, None)
    if attribute is None:
        pytest.fail(f"{BUILDS_MODULE} has no {name} yet.")
    return attribute


# The window and the task name -----------------------------------------------------


def test_a_window_is_ten_minutes_of_unix_time() -> None:
    task_window = builds_attribute("task_window")
    window_end = builds_attribute("window_end")
    start = datetime.fromtimestamp(2_960_000 * WINDOW_SECONDS, UTC)

    assert builds_attribute("WINDOW_SECONDS") == WINDOW_SECONDS
    assert task_window(start) == 2_960_000
    assert task_window(start + timedelta(seconds=599, microseconds=999_999)) == (
        2_960_000
    )
    assert task_window(start + timedelta(seconds=600)) == 2_960_001
    assert window_end(2_960_000) == start + timedelta(seconds=600)


def test_any_offset_gives_the_same_window_and_a_naive_time_is_refused() -> None:
    task_window = builds_attribute("task_window")
    four_hours_east = timezone(timedelta(hours=4))

    assert task_window(REQUESTED_AT) == int(REQUESTED_AT.timestamp()) // 600
    assert task_window(REQUESTED_AT.astimezone(four_hours_east)) == (
        task_window(REQUESTED_AT)
    )
    with pytest.raises(ValueError):
        task_window(REQUESTED_AT.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("country", "kind"),
    [
        ("us", "release"),
        ("us", "staging"),
        ("uk", "release"),
        ("uk", "staging"),
        ("be", "release"),
    ],
)
def test_the_task_is_named_by_country_kind_and_window(country: str, kind: str) -> None:
    build_task_id = builds_attribute("build_task_id")

    assert build_task_id(country, kind, 2_960_000) == (
        f"calibration-trees-{country}-{kind}-2960000"
    )


# The worker job's arguments --------------------------------------------------------


def test_the_task_runs_the_reconcile_command_for_its_country_kind_and_request() -> None:
    reconcile_arguments = builds_attribute("reconcile_arguments")
    task_id = "calibration-trees-uk-staging-2960000"

    arguments = reconcile_arguments("uk", "staging", task_id)

    assert arguments == [
        "run",
        "--no-sync",
        "python",
        "-m",
        "telemetry_collector.operations",
        "reconcile-calibration-trees",
        "--country",
        "uk",
        "--kind",
        "staging",
        "--request-id",
        task_id,
    ]
    # The arguments after `python -m telemetry_collector.operations` are a
    # command the operations module accepts.
    parsed = operations._argument_parser().parse_args(arguments[5:])
    assert parsed.command == "reconcile-calibration-trees"
    assert (parsed.country, parsed.kind, parsed.request_id) == (
        "uk",
        "staging",
        task_id,
    )


@pytest.mark.parametrize(
    ("country", "kind", "task_id"),
    [
        ("fr", "release", "calibration-trees-fr-release-1"),
        ("be", "staging", "calibration-trees-be-staging-1"),
        ("us", "comparison", "calibration-trees-us-comparison-1"),
        ("us", "release", "calibration-trees-uk-release-1"),
        ("us", "release", "calibration-trees-us-staging-1"),
        ("us", "release", "calibration-trees-us-release-1,--dry-run"),
        ("us", "release", "calibration-trees-us-release-"),
        ("us", "release", "calibration-trees-us-release-1 --kind"),
        ("us,--dry-run", "release", "calibration-trees-us-release-1"),
    ],
)
def test_arguments_are_built_only_from_checked_values(
    country: str, kind: str, task_id: str
) -> None:
    reconcile_arguments = builds_attribute("reconcile_arguments")

    with pytest.raises(ValueError):
        reconcile_arguments(country, kind, task_id)


def test_a_request_id_names_one_country_and_kind() -> None:
    is_request_id = builds_attribute("is_request_id")

    assert is_request_id("calibration-trees-uk-staging-2960000", "uk", "staging")
    assert not is_request_id("calibration-trees-uk-staging-2960000", "uk", "release")
    assert not is_request_id("calibration-trees-uk-staging-2960000", "us", "staging")
    assert not is_request_id("calibration-trees-uk-staging-x", "uk", "staging")
    assert not is_request_id("calibration-trees-uk-staging-1\n", "uk", "staging")


# The table row, on Postgres -----------------------------------------------------------


@pytest.fixture
def build_requests(postgres_url: str) -> Iterator[Any]:
    store_class = builds_attribute("PostgresCalibrationTreeBuildRequests")
    upgrade_database(postgres_url)
    engine = create_database_engine(postgres_url)
    try:
        yield store_class(engine)
    finally:
        engine.dispose()


def record_request(store: Any, task_id: str, *, at: datetime, **changes: Any) -> None:
    request = {
        "task_id": task_id,
        "country": "uk",
        "kind": "staging",
        "caller": DASHBOARD_PRODUCTION,
        "hf_repo": "policyengine/populace-uk-staging",
        "hf_ref": "refs/heads/main",
        "hf_commit": "a" * 40,
        "not_before": datetime(2026, 11, 13, 10, 10, tzinfo=UTC),
        "requested_at": at,
        **changes,
    }
    store.record_request(**request)


def stored_row(store: Any, task_id: str) -> dict[str, Any]:
    record = store.get_request(task_id)
    assert record is not None
    return dataclasses.asdict(record)


TASK_ID = "calibration-trees-uk-staging-2960000"


def test_the_first_request_creates_the_row(build_requests: Any) -> None:
    record_request(build_requests, TASK_ID, at=REQUESTED_AT)

    assert stored_row(build_requests, TASK_ID) == {
        "task_id": TASK_ID,
        "country": "uk",
        "kind": "staging",
        "first_requested_at": REQUESTED_AT,
        "last_requested_at": REQUESTED_AT,
        "request_count": 1,
        "last_caller_project_id": "prj_dashboard",
        "last_caller_environment": "production",
        "hf_repo": "policyengine/populace-uk-staging",
        "last_hf_ref": "refs/heads/main",
        "last_hf_commit": "a" * 40,
        "not_before": datetime(2026, 11, 13, 10, 10, tzinfo=UTC),
        "queued_at": None,
        "execution": None,
        "attempts": 0,
        "started_at": None,
        "finished_at": None,
        "summary": None,
    }


def test_each_later_request_counts_and_keeps_the_last_caller_and_commit(
    build_requests: Any,
) -> None:
    record_request(build_requests, TASK_ID, at=REQUESTED_AT)
    record_request(
        build_requests,
        TASK_ID,
        at=REQUESTED_AT + timedelta(seconds=60),
        caller=VercelOidcReader("prj_other", "preview"),
        hf_ref=None,
        hf_commit="b" * 40,
    )

    row = stored_row(build_requests, TASK_ID)
    assert row["request_count"] == 2
    assert row["first_requested_at"] == REQUESTED_AT
    assert row["last_requested_at"] == REQUESTED_AT + timedelta(seconds=60)
    assert (row["last_caller_project_id"], row["last_caller_environment"]) == (
        "prj_other",
        "preview",
    )
    assert row["last_hf_ref"] is None
    assert row["last_hf_commit"] == "b" * 40


def test_queued_at_is_when_cloud_tasks_first_accepted_the_task(
    build_requests: Any,
) -> None:
    record_request(build_requests, TASK_ID, at=REQUESTED_AT)

    build_requests.mark_queued(TASK_ID, at=REQUESTED_AT + timedelta(seconds=1))
    build_requests.mark_queued(TASK_ID, at=REQUESTED_AT + timedelta(seconds=90))

    assert stored_row(build_requests, TASK_ID)["queued_at"] == (
        REQUESTED_AT + timedelta(seconds=1)
    )


def test_each_attempt_records_its_start_and_its_finish(build_requests: Any) -> None:
    record_request(build_requests, TASK_ID, at=REQUESTED_AT)
    first_start = REQUESTED_AT + timedelta(minutes=10)
    first_finish = first_start + timedelta(minutes=5)

    assert build_requests.record_start(
        TASK_ID, execution="worker-job-abc12", at=first_start
    )
    assert build_requests.record_finish(
        TASK_ID, at=first_finish, summary={"incomplete": True}
    )
    after_first = stored_row(build_requests, TASK_ID)
    second_start = first_finish + timedelta(seconds=30)
    assert build_requests.record_start(
        TASK_ID, execution="worker-job-abc12", at=second_start
    )
    while_running = stored_row(build_requests, TASK_ID)
    assert build_requests.record_finish(
        TASK_ID,
        at=second_start + timedelta(minutes=2),
        summary={"incomplete": False, "outcomes": {"built": 1}},
    )

    assert after_first["attempts"] == 1
    assert after_first["started_at"] == first_start
    assert after_first["finished_at"] == first_finish
    assert after_first["summary"] == {"incomplete": True}
    # A new attempt clears the last attempt's finish until it finishes.
    assert while_running["attempts"] == 2
    assert while_running["started_at"] == second_start
    assert while_running["finished_at"] is None
    assert while_running["summary"] is None
    finished = stored_row(build_requests, TASK_ID)
    assert finished["execution"] == "worker-job-abc12"
    assert finished["finished_at"] == second_start + timedelta(minutes=2)
    assert finished["summary"] == {"incomplete": False, "outcomes": {"built": 1}}


def test_an_execution_of_a_request_without_a_row_records_nothing(
    build_requests: Any,
) -> None:
    assert (
        build_requests.record_start(TASK_ID, execution=None, at=REQUESTED_AT) is False
    )
    assert build_requests.record_finish(TASK_ID, at=REQUESTED_AT, summary={}) is False
    assert build_requests.get_request(TASK_ID) is None


def test_a_summary_jsonb_cannot_hold_is_stored_with_u_fffd(build_requests: Any) -> None:
    record_request(build_requests, TASK_ID, at=REQUESTED_AT)
    build_requests.record_start(TASK_ID, execution=None, at=REQUESTED_AT)

    build_requests.record_finish(
        TASK_ID,
        at=REQUESTED_AT,
        summary={"reasons": ["a\x00b", "\ud800"], "key\x00": 1},
    )

    assert stored_row(build_requests, TASK_ID)["summary"] == {
        "reasons": ["a�b", "�"],
        "key�": 1,
    }


def test_an_unavailable_database_is_reported_as_the_store_s_own_error() -> None:
    store_class = builds_attribute("PostgresCalibrationTreeBuildRequests")
    unavailable = builds_attribute("BuildRequestStoreUnavailableError")
    # Nothing listens on port 1.
    engine = create_database_engine(
        "postgresql://user:password@localhost:1/telemetry_test"
    )
    try:
        store = store_class(engine)
        with pytest.raises(unavailable):
            record_request(store, TASK_ID, at=REQUESTED_AT)
        with pytest.raises(unavailable):
            store.mark_queued(TASK_ID, at=REQUESTED_AT)
    finally:
        engine.dispose()
