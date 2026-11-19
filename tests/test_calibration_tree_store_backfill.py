"""The ``backfill-calibration-trees`` command's steps, in order.

One execution fills the collector's calibration tree store: it stores each
country's ``releases.json``, builds what production's manifest lists and the
store lacks (only when given that manifest's address), covers every older
staging run with the reconcile command's code, and then stores the original
files missing for every build in the store's own manifest. These tests stand
in for each step, so they check only what the steps are given, their order and
the summary.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest

from telemetry_collector.calibration_tree_backfill import BackfillOutcome
from telemetry_collector.calibration_tree_reconciliation import ReconcileOutcome
from telemetry_collector.calibration_trees import CopiedReleasesRecord
from tests.test_calibration_tree_backfill import MINUTE, ManualClock

STORE_BACKFILL_MODULE = "telemetry_collector.calibration_tree_store_backfill"
SOURCE_COPIES_MODULE = "telemetry_collector.calibration_tree_source_copies"
BACKFILL_MODULE = "telemetry_collector.calibration_tree_backfill"
RECONCILIATION_MODULE = "telemetry_collector.calibration_tree_reconciliation"
PRODUCTION_URL = "https://dashboard.example.test/api/microcosm/tree-manifest"
HEAD = "c" * 40
TIME_LIMIT = 40 * MINUTE


def module_named(name: str) -> ModuleType:
    if importlib.util.find_spec(name) is None:
        pytest.fail(f"{name} does not exist yet.")
    return importlib.import_module(name)


def store_backfill() -> ModuleType:
    return module_named(STORE_BACKFILL_MODULE)


def source_files_outcome(**fields: Any) -> Any:
    outcome_class = module_named(SOURCE_COPIES_MODULE).SourceFilesOutcome
    return outcome_class(**fields)


@dataclass
class StandInSteps:
    """Records what each step is given and when, and yields what a test sets."""

    clock: ManualClock = field(default_factory=ManualClock)
    events: list[str] = field(default_factory=list)
    given: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Minutes of the clock each step takes.
    minutes: dict[str, float] = field(default_factory=dict)
    production_outcomes: list[Any] = field(default_factory=list)
    staging_outcomes: dict[str, list[Any]] = field(default_factory=dict)
    staging_failures: dict[str, BaseException] = field(default_factory=dict)
    source_outcomes: list[Any] = field(default_factory=list)
    record_failures: dict[str, BaseException] = field(default_factory=dict)
    record_outcome: str = "stored"

    def _record(self, step: str, options: dict[str, Any]) -> None:
        self.given.setdefault(step, []).append(options)
        self.clock.now += self.minutes.get(step, 0.0) * MINUTE

    def copy_record(self, country: str, *, dry_run: bool = False) -> Any:
        self.events.append(f"releases.json {country}")
        self._record("releases_record", {"country": country, "dry_run": dry_run})
        if country in self.record_failures:
            raise self.record_failures[country]
        return CopiedReleasesRecord(
            country=country,
            outcome="would_store" if dry_run else self.record_outcome,
            hf_repo=f"policyengine/populace-{country}",
            hf_commit=HEAD,
            has_record=True,
        )

    def production(self, **options: Any) -> Iterator[Any]:
        self.events.append("production builds")
        self._record("production", options)
        yield from self.production_outcomes

    def reconcile(self, **options: Any) -> Iterator[Any]:
        country = options["country"]
        self.events.append(f"staging runs {country}")
        self._record("staging", options)
        yield from self.staging_outcomes.get(country, [])
        if country in self.staging_failures:
            raise self.staging_failures[country]

    def source_files(self, **options: Any) -> Iterator[Any]:
        self.events.append("source files")
        self._record("source_files", options)
        yield from self.source_outcomes


def install(monkeypatch: pytest.MonkeyPatch, steps: StandInSteps) -> None:
    monkeypatch.setattr(
        module_named(BACKFILL_MODULE), "backfill_calibration_trees", steps.production
    )
    monkeypatch.setattr(
        module_named(RECONCILIATION_MODULE),
        "reconcile_calibration_trees",
        steps.reconcile,
    )
    monkeypatch.setattr(
        module_named(SOURCE_COPIES_MODULE),
        "store_missing_source_files",
        steps.source_files,
    )


SHARED = {
    "reader": object(),
    "commands": object(),
    "submissions": object(),
    "lock": object(),
    "follow_latest": object(),
}


def run(
    monkeypatch: pytest.MonkeyPatch, steps: StandInSteps, **options: Any
) -> list[Any]:
    install(monkeypatch, steps)
    options.setdefault("countries", ["us", "uk", "be"])
    return list(
        store_backfill().backfill_calibration_tree_store(
            **SHARED,
            copy_record=steps.copy_record,
            clock=steps.clock,
            time_limit_seconds=TIME_LIMIT,
            **options,
        )
    )


# The steps ----------------------------------------------------------------------


def test_the_steps_run_in_order_without_production_s_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = StandInSteps()

    run(monkeypatch, steps)

    assert steps.events == [
        "releases.json us",
        "releases.json uk",
        "releases.json be",
        "staging runs us",
        "staging runs uk",
        "source files",
    ]
    assert "production" not in steps.given


def test_production_s_manifest_is_backfilled_only_when_its_address_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = StandInSteps()
    client = object()

    run(
        monkeypatch,
        steps,
        production_manifest_url=PRODUCTION_URL,
        production_client=client,
    )

    assert steps.events == [
        "releases.json us",
        "releases.json uk",
        "releases.json be",
        "production builds",
        "staging runs us",
        "staging runs uk",
        "source files",
    ]
    [given] = steps.given["production"]
    assert given["production_manifest_url"] == PRODUCTION_URL
    assert given["production_client"] is client
    assert given["reader"] is SHARED["reader"]
    assert given["commands"] is SHARED["commands"]
    assert list(given["countries"]) == ["us", "uk", "be"]
    assert sorted(given["kinds"]) == ["release", "staging"]
    assert given["dry_run"] is False


def test_older_staging_runs_are_covered_by_the_reconcile_code_per_country(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = StandInSteps()

    run(monkeypatch, steps)

    given = steps.given["staging"]
    assert [options["country"] for options in given] == ["us", "uk"]
    for options in given:
        assert list(options["kinds"]) == ["staging"]
        assert options["include_older_runs"] is True
        # Each execution and retry repeats a refusal recorded for an unchanged
        # folder instead of building it again.
        assert options.get("retry_refusals") is False
        assert options["dry_run"] is False
        for name, value in SHARED.items():
            assert options[name] is value
        assert options["copy_record"] == steps.copy_record


def test_original_files_are_stored_for_the_selected_countries_and_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = StandInSteps()

    run(monkeypatch, steps, countries=["uk"], kinds=["staging"])

    [given] = steps.given["source_files"]
    assert list(given["countries"]) == ["uk"]
    assert list(given["kinds"]) == ["staging"]
    assert given["reader"] is SHARED["reader"]
    assert given["commands"] is SHARED["commands"]
    assert given["dry_run"] is False


def test_a_release_only_run_has_no_staging_runs_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = StandInSteps()

    run(monkeypatch, steps, kinds=["release"])

    assert steps.events == [
        "releases.json us",
        "releases.json uk",
        "releases.json be",
        "source files",
    ]


def test_a_staging_only_run_stores_no_releases_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = StandInSteps()

    run(monkeypatch, steps, kinds=["staging"])

    assert steps.events == ["staging runs us", "staging runs uk", "source files"]


def test_a_dry_run_is_passed_to_every_step(monkeypatch: pytest.MonkeyPatch) -> None:
    steps = StandInSteps()

    outcomes = run(
        monkeypatch,
        steps,
        dry_run=True,
        production_manifest_url=PRODUCTION_URL,
        production_client=object(),
    )

    for step in ("releases_record", "production", "staging", "source_files"):
        assert all(options["dry_run"] is True for options in steps.given[step]), step
    records = [outcome for outcome in outcomes if not hasattr(outcome, "kind")]
    assert {record.outcome for record in records} == {"would_store"}


def test_each_step_gets_the_time_the_execution_has_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = StandInSteps(minutes={"releases_record": 1, "production": 5, "staging": 7})

    run(
        monkeypatch,
        steps,
        production_manifest_url=PRODUCTION_URL,
        production_client=object(),
    )

    assert steps.given["production"][0]["time_limit_seconds"] == TIME_LIMIT - 3 * MINUTE
    assert [options["time_limit_seconds"] for options in steps.given["staging"]] == [
        TIME_LIMIT - 8 * MINUTE,
        TIME_LIMIT - 15 * MINUTE,
    ]
    assert steps.given["source_files"][0]["time_limit_seconds"] == (
        TIME_LIMIT - 22 * MINUTE
    )
    for step in ("production", "staging", "source_files"):
        assert all(options["clock"] is steps.clock for options in steps.given[step])


def test_the_time_left_is_never_below_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = StandInSteps(minutes={"releases_record": 15})

    run(monkeypatch, steps)

    assert steps.given["source_files"][0]["time_limit_seconds"] == 0


def test_no_staging_country_starts_after_the_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = StandInSteps(minutes={"staging": 45})

    outcomes = run(monkeypatch, steps)

    assert [options["country"] for options in steps.given["staging"]] == ["us"]
    staging = [outcome for outcome in outcomes if isinstance(outcome, ReconcileOutcome)]
    assert [(outcome.country, outcome.outcome) for outcome in staging] == [
        ("uk", "deferred")
    ]
    assert staging[0].incomplete is True
    assert staging[0].reason
    # The copy step still runs, and defers each build itself.
    assert steps.given["source_files"][0]["time_limit_seconds"] == 0


def test_a_staging_country_starts_only_with_time_left_for_its_lock_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reconcile code may wait 15 minutes for its lock before it checks the
    # time, so a country that starts later could pass the task's hour.
    steps = StandInSteps(minutes={"staging": 21})

    outcomes = run(monkeypatch, steps)

    assert [options["country"] for options in steps.given["staging"]] == ["us"]
    staging = [outcome for outcome in outcomes if isinstance(outcome, ReconcileOutcome)]
    assert [(outcome.country, outcome.outcome) for outcome in staging] == [
        ("uk", "deferred")
    ]
    assert "lock" in staging[0].reason
    assert steps.given["source_files"][0]["time_limit_seconds"] == (
        TIME_LIMIT - 21 * MINUTE
    )


def test_a_dry_run_covers_every_staging_country_whatever_the_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = StandInSteps(minutes={"staging": 45})

    run(monkeypatch, steps, dry_run=True)

    assert [options["country"] for options in steps.given["staging"]] == ["us", "uk"]


# Failures -----------------------------------------------------------------------


def test_a_failed_releases_json_copy_leaves_the_run_incomplete_and_it_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = StandInSteps(record_failures={"uk": RuntimeError("The store failed.")})

    outcomes = run(monkeypatch, steps)

    records = {
        outcome.country: outcome for outcome in outcomes if not hasattr(outcome, "kind")
    }
    assert records["uk"].outcome == "error"
    assert records["uk"].incomplete is True
    assert "The store failed." in records["uk"].reason
    assert records["be"].outcome == "stored"
    assert steps.events[-1] == "source files"
    summary = store_backfill().summarize(
        outcomes,
        countries=["us", "uk", "be"],
        kinds=["release", "staging"],
        dry_run=False,
        production_builds=False,
    )
    assert summary["incomplete"] is True


def test_a_staging_step_that_stops_is_an_error_for_its_country_and_it_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = ReconcileOutcome(
        country="us", kind="staging", outcome="built", source_id="run-1"
    )
    steps = StandInSteps(
        staging_outcomes={"us": [built]},
        staging_failures={"us": RuntimeError("The listing stopped.")},
    )

    outcomes = run(monkeypatch, steps)

    staging = [outcome for outcome in outcomes if isinstance(outcome, ReconcileOutcome)]
    assert [(outcome.country, outcome.outcome) for outcome in staging] == [
        ("us", "built"),
        ("us", "error"),
    ]
    assert staging[1].incomplete is True
    assert "The listing stopped." in staging[1].reason
    assert steps.events[-2:] == ["staging runs uk", "source files"]


# The summary and the documents --------------------------------------------------


def test_the_summary_has_one_part_per_step(monkeypatch: pytest.MonkeyPatch) -> None:
    production = BackfillOutcome(
        country="us", outcome="already_stored", kind="release", source_id="r"
    )
    staging = ReconcileOutcome(
        country="uk", kind="staging", outcome="built", source_id="run-2"
    )
    copied = source_files_outcome(
        country="us",
        outcome="copied",
        kind="release",
        source_id="r",
        files={"reform_validation.json": "copied"},
    )
    steps = StandInSteps(
        production_outcomes=[production],
        staging_outcomes={"uk": [staging]},
        source_outcomes=[copied],
    )

    outcomes = run(
        monkeypatch,
        steps,
        countries=["us", "uk"],
        production_manifest_url=PRODUCTION_URL,
        production_client=object(),
    )
    summary = store_backfill().summarize(
        outcomes,
        countries=["us", "uk"],
        kinds=["release", "staging"],
        dry_run=False,
        production_builds=True,
    )

    assert summary["dry_run"] is False
    assert summary["incomplete"] is False
    assert set(summary["releases_records"]) == {"us", "uk"}
    assert summary["releases_records"]["us"]["outcome"] == "stored"
    assert summary["production_builds"]["outcomes"] == {"already_stored": 1}
    assert set(summary["staging_runs"]) == {"us", "uk"}
    assert summary["staging_runs"]["uk"]["outcomes"] == {"built": 1}
    assert summary["staging_runs"]["uk"]["kinds"] == ["staging"]
    assert summary["staging_runs"]["us"]["outcomes"] == {}
    assert summary["source_files"]["outcomes"] == {"copied": 1}
    assert summary["source_files"]["files"] == {"copied": 1}


def test_without_production_s_manifest_the_summary_has_no_production_part() -> None:
    summary = store_backfill().summarize(
        [],
        countries=["be"],
        kinds=["release", "staging"],
        dry_run=True,
        production_builds=False,
    )

    assert summary["production_builds"] is None
    # Belgium has no staging repo.
    assert summary["staging_runs"] == {}
    assert summary["dry_run"] is True
    assert summary["incomplete"] is False


@pytest.mark.parametrize(
    "incomplete_outcome",
    [
        BackfillOutcome(country="us", outcome="deferred", incomplete=True),
        ReconcileOutcome(
            country="us", kind="staging", outcome="error", incomplete=True
        ),
    ],
    ids=["production", "staging"],
)
def test_any_incomplete_step_makes_the_run_incomplete(incomplete_outcome: Any) -> None:
    summary = store_backfill().summarize(
        [incomplete_outcome],
        countries=["us"],
        kinds=["release", "staging"],
        dry_run=False,
        production_builds=True,
    )

    assert summary["incomplete"] is True


def test_an_incomplete_copy_makes_the_run_incomplete() -> None:
    deferred = source_files_outcome(
        country="us", outcome="deferred", kind="release", incomplete=True
    )

    summary = store_backfill().summarize(
        [deferred],
        countries=["us"],
        kinds=["release"],
        dry_run=False,
        production_builds=False,
    )

    assert summary["incomplete"] is True


def test_each_outcome_is_printed_under_its_step_s_name() -> None:
    module = store_backfill()
    record_step = importlib.import_module(RECONCILIATION_MODULE).ReleasesRecordStep(
        country="us", outcome="unchanged"
    )

    documents = [
        module.document(outcome)
        for outcome in (
            record_step,
            BackfillOutcome(country="us", outcome="built", source_id="r"),
            ReconcileOutcome(country="us", kind="staging", outcome="built"),
            source_files_outcome(country="us", outcome="complete"),
        )
    ]

    assert [list(document) for document in documents] == [
        ["releases_record"],
        ["production_build"],
        ["staging_run"],
        ["source_files"],
    ]
    assert documents[1]["production_build"]["source_id"] == "r"
    assert documents[3]["source_files"]["outcome"] == "complete"
    for document in documents:
        assert json.loads(json.dumps(document)) == document
