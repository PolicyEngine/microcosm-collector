"""Backfilling the collector's calibration tree store from production's manifest.

The ``backfill-calibration-trees`` command reads production's public tree
manifest for each country, validates each entry before using any of its
values in a Hugging Face path, compares it with the collector's own store,
and builds each missing entry from its Hugging Face files with the tree
package's Bun worker and publication command, as a submission's tree is
built. These tests stand in for production's manifest, for Hugging Face and
for the three Bun processes (the store's manifest read, the worker and the
publication command), so nothing reaches the network or runs Bun.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from telemetry_collector.huggingface_read import (
    HuggingFaceReader,
    HuggingFaceReadSettings,
)
from tests.huggingface_fakes import FakeHuggingFace

BACKFILL_MODULE = "telemetry_collector.calibration_tree_backfill"
TREES_MODULE = "telemetry_collector.calibration_trees"
PRODUCTION_URL = "https://dashboard.example.test/api/microcosm/tree-manifest"
READ_TOKEN = "hf_collector_read_token"
BLOB_TOKEN = "vercel_blob_rw_test_token"
US_RELEASES = "policyengine/populace-us"
US_STAGING = "policyengine/populace-us-staging"
UK_RELEASES = "policyengine/populace-uk-private"
UK_STAGING = "policyengine/populace-uk-staging"
BE_RELEASES = "policyengine/populace-be-private"
RELEASE_REPOS = {"us": US_RELEASES, "uk": UK_RELEASES, "be": BE_RELEASES}
STAGING_REPOS = {"us": US_STAGING, "uk": UK_STAGING}
#: The commit each test's builds are pinned to, never a dataset's head.
RELEASE_COMMIT = "1" * 40
STAGING_COMMIT = "2" * 40
BUNDLE_COMMIT = "3" * 40
CREATED_AT = "2026-10-20T09:00:00.000Z"
LATER = "2026-11-01T00:00:00.000Z"
EARLIER = "2026-09-01T00:00:00.000Z"
#: A tree built from a submission records the submission's accepted_at.
SUBMISSION_ACCEPTED_AT = "2026-10-21T08:30:00.000Z"
#: The commit a submission's tree was built from, not production's entry's.
SUBMISSION_COMMIT = "5" * 40
US_RELEASE = "microcosm-us-2025"
US_TAG = f"{US_RELEASE}-20261020T090000Z-0a1b2c3d"
RUN_ID = "us-calibration-attempt-20261019T083323Z-6cba5d71"
DIAGNOSTICS = b'{"schema_version": 8, "targets": []}'
BUILD_MANIFEST = b'{"build": "us-calibration-attempt-1"}'
RELEASE_MANIFEST = b'{"release": "microcosm-us-2025"}'
DEMOGRAPHICS = b'{"households": 1}'
MINUTE = 60.0
#: The staged_dataset record's key for the repo its bundle was uploaded to:
#: Microcosm's own field name, built in two parts as the collector's
#: vocabulary test builds the word.
RECORD_REPO_KEY = "repo" + "sitory"


def backfill_module() -> ModuleType:
    if importlib.util.find_spec(BACKFILL_MODULE) is None:
        pytest.fail(f"{BACKFILL_MODULE} does not exist yet.")
    return importlib.import_module(BACKFILL_MODULE)


def trees_function(name: str) -> Callable[..., Any]:
    function = getattr(importlib.import_module(TREES_MODULE), name, None)
    if function is None:
        pytest.fail(f"{TREES_MODULE} has no {name}.")
    return function


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def build_id(country: str, kind: str, source_id: str) -> str:
    """Production's build ID for a source; the stand-in worker gives the same."""

    return sha256(f"{country}/{kind}/{source_id}".encode())


# Production's manifest ------------------------------------------------------


def release_entry(
    release_id: str,
    *,
    country: str = "us",
    commit: str = RELEASE_COMMIT,
    created_at: str | None = CREATED_AT,
    release_directory: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "buildArtifactId": build_id(country, "release", release_id),
        "kind": "release",
        "sourceId": release_id,
        "label": release_id,
        "releaseId": release_id,
        "stagingRunId": None,
        "hfRepo": repo or RELEASE_REPOS[country],
        "hfCommitSha": commit,
        "treeSchemaVersion": 6,
        "indexSha256": "e" * 64,
        "indexBytes": 1000,
        "createdAt": created_at,
        "updatedAt": created_at,
    }
    if release_directory is not None:
        entry["releaseDirectory"] = release_directory
    return entry


def staging_entry(
    run_id: str,
    *,
    country: str = "us",
    commit: str = STAGING_COMMIT,
    repo: str | None = None,
    label: str = US_RELEASE,
    created_at: str | None = CREATED_AT,
) -> dict[str, Any]:
    return {
        "buildArtifactId": build_id(country, "staging", run_id),
        "kind": "staging",
        "sourceId": run_id,
        "label": label,
        "releaseId": None,
        "stagingRunId": run_id,
        "hfRepo": repo or STAGING_REPOS[country],
        "hfCommitSha": commit,
        "treeSchemaVersion": 6,
        "indexSha256": "e" * 64,
        "indexBytes": 1000,
        "createdAt": created_at,
        "updatedAt": created_at,
    }


def production_manifest(
    country: str, builds: Sequence[dict[str, Any]], latest: str | None = None
) -> dict[str, Any]:
    return {
        "schemaVersion": 6,
        "country": country,
        "latestReleaseBuildArtifactId": latest,
        "builds": list(builds),
    }


@dataclass
class Production:
    """Stands in for the dashboard's public tree manifest, one answer per country.

    A country in ``chunked_bodies`` is answered in those chunks, each of which
    takes ``seconds_per_chunk`` of ``clock`` to arrive.
    """

    manifests: dict[str, Any] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)
    bodies: dict[str, bytes] = field(default_factory=dict)
    chunked_bodies: dict[str, list[bytes]] = field(default_factory=dict)
    clock: ManualClock | None = None
    seconds_per_chunk: float = 0.0
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path != "/api/microcosm/tree-manifest":
            return httpx.Response(404, json={"error": "not found"})
        country = request.url.params.get("country", "")
        if country in self.statuses:
            return httpx.Response(
                self.statuses[country],
                json={"error": "unavailable"},
                headers={"Location": "https://elsewhere.example.test/manifest"},
            )
        if country in self.bodies:
            return httpx.Response(200, content=self.bodies[country])
        if country in self.chunked_bodies:
            return httpx.Response(
                200, content=self._arriving(self.chunked_bodies[country])
            )
        return httpx.Response(
            200, json=self.manifests.get(country, production_manifest(country, []))
        )

    def _arriving(self, chunks: Sequence[bytes]) -> Iterator[bytes]:
        for chunk in chunks:
            if self.clock is not None:
                self.clock.now += self.seconds_per_chunk
            yield chunk

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


# The collector's store and the Bun processes ---------------------------------


def store_manifest(
    country: str = "us",
    builds: Sequence[dict[str, Any]] = (),
    latest: str | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": 6,
        "countries": {
            country: {"latestReleaseBuildArtifactId": latest, "builds": list(builds)}
        },
    }


def published_instant(entry: Mapping[str, Any]) -> float:
    """When a build was published, as the publication orders builds; undated first."""

    published = entry.get("createdAt") or entry.get("updatedAt")
    return (
        float("-inf")
        if published is None
        else datetime.fromisoformat(published).timestamp()
    )


@dataclass
class ManualClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class StandInBun:
    """Stands in for the store's manifest read, the tree worker and the publication.

    The worker gives each source production's build ID unless
    ``built_build_ids`` names another, and records the bytes of every file it
    is given. The publication answers ``madeLatest`` as it was asked, as the
    real one does: only when the build is at least as new as ``store``'s
    latest, and unless the source is in ``latest_not_moved``.
    """

    store: dict[str, Any] = field(default_factory=lambda: store_manifest())
    built_build_ids: dict[str, str] = field(default_factory=dict)
    worker_exits: dict[str, int] = field(default_factory=dict)
    publication_exits: dict[str, int] = field(default_factory=dict)
    #: (step, source ID) pairs whose process runs out of time.
    timed_out: set[tuple[str, str]] = field(default_factory=set)
    latest_not_moved: set[str] = field(default_factory=set)
    clock: ManualClock | None = None
    seconds_per_build: float = 0.0
    events: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        script = Path(argv[2]).name
        if script == "calibration-tree-manifest-read.ts":
            self.events.append("manifest read")
            self.calls.append(
                {"step": "manifest read", "argv": list(argv), "env": dict(env)}
            )
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.store), "")
        if script == "calibration-tree-worker.ts":
            return self._build(argv, env, timeout)
        if script == "calibration-tree-publication.ts":
            return self._publish(argv, env, timeout)
        raise AssertionError(f"Unexpected Bun script {script}.")

    def _build(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        request = json.loads(Path(argv[3]).read_text())
        source = (
            request["releaseId"] if request["kind"] == "release" else request["runId"]
        )
        self.events.append(f"build {source}")
        inputs = {
            role: Path(artifact["file"]).read_bytes()
            for role, artifact in request["artifacts"].items()
            if artifact is not None
        }
        self.calls.append(
            {
                "step": "build",
                "source": source,
                "argv": list(argv),
                "env": dict(env),
                "request": request,
                "inputs": inputs,
            }
        )
        if self.clock is not None:
            self.clock.now += self.seconds_per_build
        if ("build", source) in self.timed_out:
            raise subprocess.TimeoutExpired(list(argv), timeout)
        exit_code = self.worker_exits.get(source, 0)
        answer = {
            "country": request["country"],
            "buildArtifactId": self.built_build_ids.get(
                source, build_id(request["country"], request["kind"], source)
            ),
            "files": [],
            "manifestEntry": {
                "kind": request["kind"],
                "sourceId": source,
                "createdAt": request["createdAt"],
            },
        }
        return subprocess.CompletedProcess(
            argv,
            exit_code,
            json.dumps(answer) if exit_code == 0 else "",
            "" if exit_code == 0 else f"The worker stopped on {source}.",
        )

    def _publish(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        publication = json.loads(Path(argv[3]).read_text())
        worker = publication["worker"]
        source = worker["manifestEntry"]["sourceId"]
        self.events.append(f"publication {source}")
        self.calls.append(
            {
                "step": "publication",
                "source": source,
                "argv": list(argv),
                "env": dict(env),
                "publication": publication,
            }
        )
        if ("publication", source) in self.timed_out:
            raise subprocess.TimeoutExpired(list(argv), timeout)
        exit_code = self.publication_exits.get(source, 0)
        answer = {
            "buildArtifactId": worker["buildArtifactId"],
            "kind": worker["manifestEntry"]["kind"],
            "uploadedParts": 7,
            "keptParts": 0,
            "madeLatest": publication["makeLatest"]
            and source not in self.latest_not_moved
            and self._at_least_as_new_as_latest(worker),
        }
        return subprocess.CompletedProcess(
            argv,
            exit_code,
            json.dumps(answer) if exit_code == 0 else "",
            "" if exit_code == 0 else f"The publication stopped on {source}.",
        )

    def _at_least_as_new_as_latest(self, worker: Mapping[str, Any]) -> bool:
        """The publication's rule: the latest moves only to a build at least as new."""

        country = self.store["countries"].get(worker["country"], {})
        latest_build = country.get("latestReleaseBuildArtifactId")
        latest = next(
            (
                build
                for build in country.get("builds", [])
                if build["buildArtifactId"] == latest_build
            ),
            None,
        )
        if latest is None or latest_build == worker["buildArtifactId"]:
            return True
        return published_instant(worker["manifestEntry"]) >= published_instant(latest)

    def steps(self, step: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["step"] == step]

    def build_of(self, source: str) -> dict[str, Any]:
        [call] = [call for call in self.steps("build") if call["source"] == source]
        return call

    def publication_of(self, source: str) -> dict[str, Any]:
        [call] = [
            call for call in self.steps("publication") if call["source"] == source
        ]
        return call


# Hugging Face ---------------------------------------------------------------


def hugging_face_with(
    files_by_commit: Mapping[tuple[str, str], Mapping[str, bytes]],
) -> FakeHuggingFace:
    """Every registered dataset, each holding the given files at the given commits."""

    hugging_face = FakeHuggingFace(token=READ_TOKEN)
    for name in [*RELEASE_REPOS.values(), *STAGING_REPOS.values()]:
        hugging_face.add_dataset(name)
    for (dataset, commit), files in files_by_commit.items():
        hugging_face.datasets[dataset].revisions.setdefault(commit, {}).update(files)
    return hugging_face


def release_folder(directory: str, *, optional: bool = True) -> dict[str, bytes]:
    folder = f"releases/{directory}"
    files = {f"{folder}/calibration_diagnostics.json": DIAGNOSTICS}
    if optional:
        files |= {
            f"{folder}/build_manifest.json": BUILD_MANIFEST,
            f"{folder}/release_manifest.json": RELEASE_MANIFEST,
            f"{folder}/demographics.json": DEMOGRAPHICS,
        }
    return files


def schema_1_run(run_id: str) -> dict[str, bytes]:
    folder = f"runs/{run_id}"
    return {
        f"{folder}/run_manifest.json": json.dumps(
            {"schema_version": 1, "run_id": run_id}
        ).encode(),
        f"{folder}/progress.json": json.dumps(
            {"schema_version": 1, "run_id": run_id, "status": "passed"}
        ).encode(),
        f"{folder}/calibration_diagnostics.json": DIAGNOSTICS,
        f"{folder}/build_manifest.json": BUILD_MANIFEST,
        f"{folder}/release_manifest.json": RELEASE_MANIFEST,
    }


def declared_artifact(name: str, content: bytes) -> dict[str, Any]:
    return {
        "logical_name": name,
        "artifact_kind": name,
        "contract_relative_path": f"artifacts/{name}.json",
        "media_type": "application/json",
        "sha256": sha256(content),
        "classification": "aggregate",
    }


def structured_run(
    run_id: str,
    artifacts: Mapping[str, bytes],
    *,
    declared: Mapping[str, bytes] | None = None,
) -> dict[str, bytes]:
    """A schema-2 run whose manifest declares ``declared`` (``artifacts`` unless given)."""

    folder = f"runs/{run_id}"
    files = {
        f"{folder}/run_manifest.json": json.dumps(
            {
                "schema_name": "microcosm.staging.run-manifest",
                "schema_version": 2,
                "run_id": run_id,
                "artifacts": [
                    declared_artifact(name, content)
                    for name, content in (declared or artifacts).items()
                ],
            }
        ).encode(),
        f"{folder}/progress.json": json.dumps(
            {
                "schema_name": "microcosm.staging.progress",
                "schema_version": 2,
                "run_id": run_id,
            }
        ).encode(),
    }
    for name, content in artifacts.items():
        files[f"{folder}/artifacts/{name}.json"] = content
    return files


@dataclass
class Backfill:
    production: Production
    hugging_face: FakeHuggingFace
    bun: StandInBun = field(default_factory=StandInBun)
    clock: ManualClock = field(default_factory=ManualClock)

    def run(self, **options: Any) -> list[Any]:
        module = backfill_module()
        tree_commands = trees_function("tree_commands")

        @contextmanager
        def recorded_lock() -> Iterator[None]:
            self.bun.events.append("lock taken")
            yield
            self.bun.events.append("lock released")

        commands = tree_commands(
            blob_token=BLOB_TOKEN,
            manifest_lock=recorded_lock,
            bun="/usr/local/bin/bun",
            run_command=self.bun,
            size_limits=options.pop("size_limits", None),
        )
        reader = HuggingFaceReader(
            HuggingFaceReadSettings(token=READ_TOKEN),
            client=self.hugging_face.client(),
        )
        options.setdefault("countries", ["us"])
        return list(
            module.backfill_calibration_trees(
                production_manifest_url=PRODUCTION_URL,
                reader=reader,
                production_client=self.production.client(),
                commands=commands,
                clock=self.clock,
                **options,
            )
        )


def us_backfill(
    builds: Sequence[dict[str, Any]],
    *,
    latest: str | None = None,
    files: Mapping[tuple[str, str], Mapping[str, bytes]] | None = None,
) -> Backfill:
    return Backfill(
        production=Production({"us": production_manifest("us", builds, latest)}),
        hugging_face=hugging_face_with(files or {}),
    )


def outcome_of(outcomes: Sequence[Any], source_id: str) -> Any:
    [outcome] = [outcome for outcome in outcomes if outcome.source_id == source_id]
    return outcome


def summary_of(outcomes: Sequence[Any], countries: Sequence[str] = ("us",)) -> Any:
    return backfill_module().summarize(outcomes, countries)


# Reading production's manifest ------------------------------------------------


def test_production_s_manifest_is_read_for_each_country_without_a_credential() -> None:
    backfill = Backfill(production=Production(), hugging_face=hugging_face_with({}))

    outcomes = backfill.run(countries=["us", "uk", "be"], dry_run=True)

    assert outcomes == []
    assert [
        request.url.params["country"] for request in backfill.production.requests
    ] == [
        "us",
        "uk",
        "be",
    ]
    for request in backfill.production.requests:
        assert request.method == "GET"
        assert str(request.url).split("?", 1)[0] == PRODUCTION_URL
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        assert READ_TOKEN not in str(request.url) + str(request.headers)
        assert BLOB_TOKEN not in str(request.url) + str(request.headers)
    assert all(
        request.url.host == "huggingface.co"
        for request in backfill.hugging_face.requests
    )


@pytest.mark.parametrize(
    "refused_answer",
    [
        {**production_manifest("us", []), "schemaVersion": 5},
        production_manifest("uk", []),
        production_manifest("us", [release_entry(US_TAG)], latest="f" * 64),
        production_manifest(
            "us",
            [staging_entry(RUN_ID)],
            latest=build_id("us", "staging", RUN_ID),
        ),
        {"countries": {}},
    ],
    ids=[
        "schema-version",
        "another-country",
        "latest-not-listed",
        "latest-not-a-release",
        "not-a-country-manifest",
    ],
)
def test_a_refused_answer_is_an_error_for_that_country_only(
    refused_answer: dict[str, Any],
) -> None:
    uk_build = release_entry("microcosm-uk-2024-25-national", country="uk")
    backfill = Backfill(
        production=Production(
            {
                "us": refused_answer,
                "uk": production_manifest("uk", [uk_build]),
            }
        ),
        hugging_face=hugging_face_with({}),
    )
    backfill.bun.store = store_manifest("uk", [uk_build])

    outcomes = backfill.run(countries=["us", "uk"])

    [us_outcome] = [outcome for outcome in outcomes if outcome.country == "us"]
    assert us_outcome.outcome == "error"
    assert us_outcome.incomplete is True
    assert us_outcome.reason
    assert outcome_of(outcomes, "microcosm-uk-2024-25-national").outcome == (
        "already_stored"
    )
    assert summary_of(outcomes, ["us", "uk"])["incomplete"] is True


@pytest.mark.parametrize(
    ("answer", "status_code", "expected_reason"),
    [
        (None, 500, "HTTP 500"),
        (None, 302, "HTTP 302"),
        # A manifest that is valid JSON, padded past the size limit.
        (
            json.dumps(production_manifest("us", [])).encode() + b" " * 2048,
            None,
            "larger than",
        ),
        (b"not json", None, "JSON"),
    ],
    ids=["http-500", "redirect", "over-the-size-limit", "not-json"],
)
def test_an_unusable_answer_is_an_error_for_that_country_only(
    monkeypatch: pytest.MonkeyPatch,
    answer: bytes | None,
    status_code: int | None,
    expected_reason: str,
) -> None:
    monkeypatch.setattr(backfill_module(), "MAX_PRODUCTION_MANIFEST_BYTES", 1024)
    uk_build = release_entry("microcosm-uk-2024-25-national", country="uk")
    production = Production({"uk": production_manifest("uk", [uk_build])})
    if status_code is not None:
        production.statuses["us"] = status_code
    if answer is not None:
        production.bodies["us"] = answer
    backfill = Backfill(production=production, hugging_face=hugging_face_with({}))
    backfill.bun.store = store_manifest("uk", [uk_build])

    outcomes = backfill.run(countries=["us", "uk"])

    assert [(outcome.country, outcome.outcome) for outcome in outcomes] == [
        ("us", "error"),
        ("uk", "already_stored"),
    ]
    assert expected_reason in outcomes[0].reason
    # A redirect is reported, never followed.
    assert all(
        request.url.host == "dashboard.example.test" for request in production.requests
    )


def test_an_answer_that_takes_too_long_to_arrive_is_an_error_for_that_country_only() -> (
    None
):
    uk_build = release_entry("microcosm-uk-2024-25-national", country="uk")
    production = Production({"uk": production_manifest("uk", [uk_build])})
    us_answer = json.dumps(production_manifest("us", [])).encode()
    # Each chunk arrives well within the client's timeout for one read, but
    # the whole answer takes far longer than any limit on the read.
    production.chunked_bodies["us"] = [
        us_answer[offset : offset + 16] for offset in range(0, len(us_answer), 16)
    ]
    production.seconds_per_chunk = 5 * MINUTE
    backfill = Backfill(production=production, hugging_face=hugging_face_with({}))
    production.clock = backfill.clock
    backfill.bun.store = store_manifest("uk", [uk_build])

    outcomes = backfill.run(countries=["us", "uk"])

    assert [(outcome.country, outcome.outcome) for outcome in outcomes] == [
        ("us", "error"),
        ("uk", "already_stored"),
    ]
    assert "took longer than" in outcomes[0].reason
    assert outcomes[0].incomplete is True


@pytest.mark.parametrize(
    "invalid_entry",
    [
        release_entry("microcosm-us-2024", repo="someone/populace-us"),
        release_entry("microcosm-us-2024", repo=UK_RELEASES),
        release_entry("microcosm-us-2024", repo=US_STAGING),
        release_entry("microcosm-us-2024", commit="ABCDEF" * 6 + "ABCD"),
        release_entry("microcosm-us-2024", commit="1" * 39),
        release_entry("microcosm-us-2024", release_directory=f"../{US_RELEASE}"),
        release_entry("microcosm-us-2024", release_directory=f"{US_RELEASE}/../.."),
        release_entry("../microcosm-us-2024"),
        release_entry("microcosm-us-2024", created_at="2026-10-20T09:00:00"),
        staging_entry("latest"),
        staging_entry("../runs"),
        staging_entry(RUN_ID, repo="policyengine/populace-us-private"),
        {**staging_entry(RUN_ID), "label": ""},
        {**release_entry("microcosm-us-2024"), "kind": "comparison"},
        {**release_entry("microcosm-us-2024"), "stagingRunId": RUN_ID},
        {**release_entry("microcosm-us-2024"), "buildArtifactId": "not-a-build-id"},
    ],
    ids=[
        "another-owner",
        "another-country-s-repo",
        "release-from-the-staging-repo",
        "uppercase-commit",
        "short-commit",
        "directory-leaves-releases",
        "directory-with-dot-dot",
        "release-id-with-dot-dot",
        "time-without-offset",
        "staging-run-latest",
        "staging-run-with-dot-dot",
        "unregistered-staging-repo",
        "empty-label",
        "unknown-kind",
        "release-with-a-staging-run",
        "malformed-build-id",
    ],
)
def test_each_invalid_entry_is_refused_and_the_rest_of_the_country_continues(
    invalid_entry: dict[str, Any],
) -> None:
    valid = release_entry(US_RELEASE)
    backfill = us_backfill(
        [invalid_entry, valid],
        files={(US_RELEASES, RELEASE_COMMIT): release_folder(US_RELEASE)},
    )

    outcomes = backfill.run(dry_run=True)

    assert [outcome.outcome for outcome in outcomes] == ["invalid", "would_build"]
    assert outcomes[0].reason
    assert outcomes[0].incomplete is False
    # No value of the invalid entry reaches a Hugging Face path.
    for request in backfill.hugging_face.requests:
        assert ".." not in request.url.raw_path.decode()
        assert "microcosm-us-2024/" not in request.url.raw_path.decode()
        assert request.url.path.split("/")[3:5] != ["someone", "populace-us"]


def test_belgium_has_no_staging_repo_so_a_staging_entry_naming_one_is_invalid() -> None:
    be_staging = staging_entry(
        "be-calibration-attempt-1",
        country="be",
        repo="policyengine/populace-be-staging",
    )
    backfill = Backfill(
        production=Production({"be": production_manifest("be", [be_staging])}),
        hugging_face=hugging_face_with({}),
    )

    [outcome] = backfill.run(countries=["be"], dry_run=True)

    assert outcome.outcome == "invalid"
    assert backfill.hugging_face.requests == []


# Planning a release build -----------------------------------------------------


def test_a_release_build_is_read_from_its_release_directory_at_its_commit() -> None:
    tag = release_entry(US_TAG, release_directory=US_RELEASE)
    backfill = us_backfill(
        [tag], files={(US_RELEASES, RELEASE_COMMIT): release_folder(US_RELEASE)}
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "built"
    request = backfill.bun.build_of(US_TAG)["request"]
    assert {key: value for key, value in request.items() if key != "artifacts"} == {
        "kind": "release",
        "country": "us",
        "releaseId": US_TAG,
        "releaseDirectory": US_RELEASE,
        "hfRepo": US_RELEASES,
        "hfCommitSha": RELEASE_COMMIT,
        "createdAt": CREATED_AT,
    }
    assert {
        role: artifact["path"] for role, artifact in request["artifacts"].items()
    } == {
        "calibrationDiagnostics": f"releases/{US_RELEASE}/calibration_diagnostics.json",
        "buildManifest": f"releases/{US_RELEASE}/build_manifest.json",
        "releaseManifest": f"releases/{US_RELEASE}/release_manifest.json",
        "demographics": f"releases/{US_RELEASE}/demographics.json",
    }
    assert backfill.bun.build_of(US_TAG)["inputs"] == {
        "calibrationDiagnostics": DIAGNOSTICS,
        "buildManifest": BUILD_MANIFEST,
        "releaseManifest": RELEASE_MANIFEST,
        "demographics": DEMOGRAPHICS,
    }
    # Every read is pinned to the entry's commit.
    assert all(
        f"/{RELEASE_COMMIT}/" in request.url.path
        for request in backfill.hugging_face.requests
    )


def test_optional_files_a_release_folder_lacks_are_null_and_a_null_time_is_kept() -> (
    None
):
    build = release_entry(US_RELEASE, created_at=None)
    backfill = us_backfill(
        [build],
        files={
            (US_RELEASES, RELEASE_COMMIT): release_folder(US_RELEASE, optional=False)
        },
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "built"
    request = backfill.bun.build_of(US_RELEASE)["request"]
    assert request["createdAt"] is None
    assert request["releaseDirectory"] == US_RELEASE
    assert request["artifacts"]["buildManifest"] is None
    assert request["artifacts"]["releaseManifest"] is None
    assert request["artifacts"]["demographics"] is None
    assert backfill.hugging_face.downloaded_paths() == [
        f"releases/{US_RELEASE}/calibration_diagnostics.json"
    ]


def test_the_request_is_the_one_worker_request_makes_for_the_same_plan() -> None:
    tag = release_entry(US_TAG, release_directory=US_RELEASE)
    backfill = us_backfill(
        [tag], files={(US_RELEASES, RELEASE_COMMIT): release_folder(US_RELEASE)}
    )

    backfill.run()

    request = backfill.bun.build_of(US_TAG)["request"]
    trees = importlib.import_module(TREES_MODULE)
    folder = f"releases/{US_RELEASE}"
    plan = trees.TreeBuildPlan(
        kind="release",
        country="us",
        folder=folder,
        hf_repo=US_RELEASES,
        hf_commit=RELEASE_COMMIT,
        created_at=CREATED_AT,
        run_id=None,
        release_id=US_TAG,
        label=None,
        make_latest=False,
        paths={
            "calibration_diagnostics": f"{folder}/calibration_diagnostics.json",
            "build_manifest": f"{folder}/build_manifest.json",
            "release_manifest": f"{folder}/release_manifest.json",
            "demographics": f"{folder}/demographics.json",
        },
        release_directory=US_RELEASE,
    )
    roles = {
        "calibrationDiagnostics": "calibration_diagnostics",
        "buildManifest": "build_manifest",
        "releaseManifest": "release_manifest",
        "demographics": "demographics",
    }
    files = {
        roles[name]: Path(artifact["file"])
        for name, artifact in request["artifacts"].items()
    }

    assert request == trees.worker_request(plan, files)


# Planning a staging build -----------------------------------------------------


def test_a_schema_1_run_s_diagnostics_are_read_from_its_run_folder() -> None:
    run = staging_entry(RUN_ID)
    backfill = us_backfill(
        [run], files={(US_STAGING, STAGING_COMMIT): schema_1_run(RUN_ID)}
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "built"
    request = backfill.bun.build_of(RUN_ID)["request"]
    assert {key: value for key, value in request.items() if key != "artifacts"} == {
        "kind": "staging",
        "country": "us",
        "runId": RUN_ID,
        "label": US_RELEASE,
        "hfRepo": US_STAGING,
        "hfCommitSha": STAGING_COMMIT,
        "createdAt": CREATED_AT,
    }
    folder = f"runs/{RUN_ID}"
    assert {
        role: artifact and artifact["path"]
        for role, artifact in request["artifacts"].items()
    } == {
        "calibrationDiagnostics": f"{folder}/calibration_diagnostics.json",
        "buildManifest": f"{folder}/build_manifest.json",
        "releaseManifest": f"{folder}/release_manifest.json",
        "demographics": None,
    }


def test_a_structured_run_s_diagnostics_are_read_from_their_declared_path() -> None:
    run = staging_entry(RUN_ID)
    backfill = us_backfill(
        [run],
        files={
            (US_STAGING, STAGING_COMMIT): structured_run(
                RUN_ID, {"calibration_diagnostics": DIAGNOSTICS}
            )
        },
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "built"
    build = backfill.bun.build_of(RUN_ID)
    assert build["request"]["artifacts"]["calibrationDiagnostics"]["path"] == (
        f"runs/{RUN_ID}/artifacts/calibration_diagnostics.json"
    )
    assert build["inputs"]["calibrationDiagnostics"] == DIAGNOSTICS


def test_structured_diagnostics_that_differ_from_their_declared_digest_are_unbuildable() -> (
    None
):
    run = staging_entry(RUN_ID)
    backfill = us_backfill(
        [run],
        files={
            (US_STAGING, STAGING_COMMIT): structured_run(
                RUN_ID,
                {"calibration_diagnostics": DIAGNOSTICS},
                declared={"calibration_diagnostics": b"other diagnostics"},
            )
        },
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "unbuildable"
    assert "digest" in outcome.reason
    assert outcome.incomplete is False
    assert backfill.bun.steps("build") == []


def test_a_staged_bundle_recorded_in_the_release_repo_is_read_from_its_folder() -> None:
    bundle = staging_entry(RUN_ID, repo=US_RELEASES, commit=BUNDLE_COMMIT)
    backfill = us_backfill(
        [bundle],
        files={
            (US_RELEASES, BUNDLE_COMMIT): {
                f"staged/{RUN_ID}/microcosm_us_2025.diagnostics.json": DIAGNOSTICS,
                f"staged/{RUN_ID}/microcosm_us_2025.h5": b"rows",
            }
        },
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "built"
    request = backfill.bun.build_of(RUN_ID)["request"]
    assert request["hfRepo"] == US_RELEASES
    assert request["hfCommitSha"] == BUNDLE_COMMIT
    assert request["artifacts"] == {
        "calibrationDiagnostics": {
            "path": f"staged/{RUN_ID}/microcosm_us_2025.diagnostics.json",
            "file": request["artifacts"]["calibrationDiagnostics"]["file"],
        },
        "buildManifest": None,
        "releaseManifest": None,
        "demographics": None,
    }
    assert backfill.hugging_face.downloaded_paths() == [
        f"staged/{RUN_ID}/microcosm_us_2025.diagnostics.json"
    ]


def test_a_staged_bundle_a_run_s_receipt_names_is_read_from_the_release_repo() -> None:
    diagnostics_name = "microcosm_us_2025.diagnostics.json"
    receipt = json.dumps(
        {
            "status": "uploaded",
            "run_id": RUN_ID,
            RECORD_REPO_KEY: US_RELEASES,
            "prefix": f"staged/{RUN_ID}",
            "revision": BUNDLE_COMMIT,
            "files": {
                diagnostics_name: {"sha256": sha256(DIAGNOSTICS)},
                "microcosm_us_2025.h5": {"sha256": sha256(b"rows")},
            },
        }
    ).encode()
    run = staging_entry(RUN_ID)
    backfill = us_backfill(
        [run],
        files={
            (US_STAGING, STAGING_COMMIT): structured_run(
                RUN_ID, {"staged_dataset": receipt}
            ),
            (US_RELEASES, BUNDLE_COMMIT): {
                f"staged/{RUN_ID}/{diagnostics_name}": DIAGNOSTICS,
                f"staged/{RUN_ID}/microcosm_us_2025.h5": b"rows",
            },
        },
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "built"
    build = backfill.bun.build_of(RUN_ID)
    # The entry's own repo and commit, as production recorded them.
    assert build["request"]["hfRepo"] == US_STAGING
    assert build["request"]["hfCommitSha"] == STAGING_COMMIT
    assert build["request"]["artifacts"]["calibrationDiagnostics"]["path"] == (
        f"staged/{RUN_ID}/{diagnostics_name}"
    )
    assert build["inputs"] == {"calibrationDiagnostics": DIAGNOSTICS}
    [diagnostics_request] = [
        request
        for request in backfill.hugging_face.requests
        if request.url.path.endswith(diagnostics_name)
    ]
    assert diagnostics_request.url.path == (
        f"/datasets/{US_RELEASES}/resolve/{BUNDLE_COMMIT}/staged/{RUN_ID}/{diagnostics_name}"
    )


@pytest.mark.parametrize(
    "receipt_change",
    [
        {"run_id": "another-run"},
        {RECORD_REPO_KEY: UK_RELEASES},
        {"prefix": "staged/another-run"},
    ],
    ids=["another-run", "another-repo", "another-prefix"],
)
def test_a_receipt_naming_another_run_repo_or_prefix_is_unbuildable(
    receipt_change: dict[str, str],
) -> None:
    receipt = json.dumps(
        {
            "status": "uploaded",
            "run_id": RUN_ID,
            RECORD_REPO_KEY: US_RELEASES,
            "prefix": f"staged/{RUN_ID}",
            "revision": BUNDLE_COMMIT,
            "files": {"calibration_diagnostics.json": {"sha256": sha256(DIAGNOSTICS)}},
            **receipt_change,
        }
    ).encode()
    backfill = us_backfill(
        [staging_entry(RUN_ID)],
        files={
            (US_STAGING, STAGING_COMMIT): structured_run(
                RUN_ID, {"staged_dataset": receipt}
            ),
            (US_RELEASES, BUNDLE_COMMIT): {
                f"staged/{RUN_ID}/calibration_diagnostics.json": DIAGNOSTICS
            },
        },
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "unbuildable"
    assert all(
        UK_RELEASES not in request.url.path
        for request in backfill.hugging_face.requests
    )


# The latest build -------------------------------------------------------------


def test_only_production_s_latest_is_made_latest_and_each_country_s_goes_first() -> (
    None
):
    older = release_entry("microcosm-us-2024")
    latest = release_entry(US_RELEASE)
    staged = staging_entry(RUN_ID)
    uk_latest = release_entry("microcosm-uk-2024-25-national", country="uk")
    uk_other = release_entry("microcosm-uk-2024-25-dense", country="uk")
    backfill = Backfill(
        production=Production(
            {
                "us": production_manifest(
                    "us", [older, staged, latest], latest["buildArtifactId"]
                ),
                "uk": production_manifest(
                    "uk", [uk_other, uk_latest], uk_latest["buildArtifactId"]
                ),
            }
        ),
        hugging_face=hugging_face_with(
            {
                (US_RELEASES, RELEASE_COMMIT): {
                    **release_folder("microcosm-us-2024"),
                    **release_folder(US_RELEASE),
                },
                (US_STAGING, STAGING_COMMIT): schema_1_run(RUN_ID),
                (UK_RELEASES, RELEASE_COMMIT): {
                    **release_folder("microcosm-uk-2024-25-national"),
                    **release_folder("microcosm-uk-2024-25-dense"),
                },
            }
        ),
    )

    outcomes = backfill.run(countries=["us", "uk"])

    built_in_order = [call["source"] for call in backfill.bun.steps("build")]
    assert built_in_order[:2] == [US_RELEASE, "microcosm-uk-2024-25-national"]
    assert sorted(built_in_order[2:]) == sorted(
        ["microcosm-us-2024", RUN_ID, "microcosm-uk-2024-25-dense"]
    )
    make_latest = {
        call["source"]: call["publication"]["makeLatest"]
        for call in backfill.bun.steps("publication")
    }
    assert make_latest == {
        US_RELEASE: True,
        "microcosm-uk-2024-25-national": True,
        "microcosm-us-2024": False,
        RUN_ID: False,
        "microcosm-uk-2024-25-dense": False,
    }
    assert outcome_of(outcomes, US_RELEASE).make_latest is True
    assert outcome_of(outcomes, US_RELEASE).made_latest is True
    summary = summary_of(outcomes, ["us", "uk"])
    assert summary["latest"] == {
        "us": {"build": latest["buildArtifactId"], "made_latest": True},
        "uk": {"build": uk_latest["buildArtifactId"], "made_latest": True},
    }
    assert summary["incomplete"] is False


def production_s_latest_stored_as(
    stored_changes: Mapping[str, Any],
    *,
    store_latest: dict[str, Any] | None,
    files_commit: str = RELEASE_COMMIT,
) -> tuple[Backfill, dict[str, Any]]:
    """Production's US latest, which the store holds with ``stored_changes``.

    The store's latest is ``store_latest``, or the stored build itself when it
    is ``None``; its files are at ``files_commit``.
    """

    latest = release_entry(US_RELEASE, created_at=CREATED_AT)
    stored = {**latest, **stored_changes}
    backfill = us_backfill(
        [latest],
        latest=latest["buildArtifactId"],
        files={(US_RELEASES, files_commit): release_folder(US_RELEASE)},
    )
    store_latest = store_latest or stored
    backfill.bun.store = store_manifest(
        "us",
        [stored] if store_latest is stored else [store_latest, stored],
        latest=store_latest["buildArtifactId"],
    )
    return backfill, stored


def test_a_stored_latest_that_is_not_the_store_s_latest_is_published_again() -> None:
    older_store_latest = release_entry("microcosm-us-2024", created_at=EARLIER)
    backfill, stored = production_s_latest_stored_as(
        {}, store_latest=older_store_latest
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "built"
    request = backfill.bun.build_of(US_RELEASE)["request"]
    assert request["createdAt"] == stored["createdAt"]
    publication = backfill.bun.publication_of(US_RELEASE)["publication"]
    assert publication["makeLatest"] is True
    assert outcome.made_latest is True
    assert summary_of([outcome])["incomplete"] is False


def test_a_stored_latest_built_from_a_submission_is_published_again_as_stored() -> None:
    # A tree built from a submission records the submission's accepted_at,
    # not production's createdAt.
    backfill, stored = production_s_latest_stored_as(
        {"createdAt": SUBMISSION_ACCEPTED_AT, "updatedAt": SUBMISSION_ACCEPTED_AT},
        store_latest=release_entry("microcosm-us-2024", created_at=EARLIER),
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "built"
    assert outcome.made_latest is True
    request = backfill.bun.build_of(US_RELEASE)["request"]
    # Exactly as stored, so the publication reproduces the stored index.
    assert request["createdAt"] == SUBMISSION_ACCEPTED_AT
    assert request["hfRepo"] == stored["hfRepo"]
    assert request["hfCommitSha"] == stored["hfCommitSha"]
    assert backfill.bun.publication_of(US_RELEASE)["publication"]["makeLatest"]
    summary = summary_of([outcome])
    assert summary["latest"]["us"]["made_latest"] is True
    assert summary["incomplete"] is False


def test_a_stored_latest_from_another_commit_is_published_again_from_that_commit() -> (
    None
):
    backfill, stored = production_s_latest_stored_as(
        {"hfCommitSha": SUBMISSION_COMMIT},
        store_latest=release_entry("microcosm-us-2024", created_at=EARLIER),
        files_commit=SUBMISSION_COMMIT,
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "built"
    assert outcome.made_latest is True
    request = backfill.bun.build_of(US_RELEASE)["request"]
    assert request["hfCommitSha"] == SUBMISSION_COMMIT
    assert request["createdAt"] == stored["createdAt"]
    # Every file is read at the stored entry's commit, not production's.
    assert backfill.hugging_face.requests
    assert all(
        f"/{SUBMISSION_COMMIT}/" in request.url.path
        for request in backfill.hugging_face.requests
    )
    assert summary_of([outcome])["incomplete"] is False


def test_a_stored_latest_whose_entry_fails_the_checks_is_not_published_again() -> None:
    backfill, _ = production_s_latest_stored_as(
        {"hfRepo": US_STAGING},
        store_latest=release_entry("microcosm-us-2024", created_at=EARLIER),
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "invalid"
    assert outcome.made_latest is False
    assert backfill.hugging_face.requests == []
    assert backfill.bun.steps("build") == []
    assert summary_of([outcome])["incomplete"] is True


def test_a_stored_latest_older_than_the_store_s_latest_is_not_made_the_latest() -> None:
    newer_store_latest = release_entry("microcosm-us-2026", created_at=LATER)
    backfill, _ = production_s_latest_stored_as({}, store_latest=newer_store_latest)

    [outcome] = backfill.run()

    # Published again, but the publication keeps the newer latest.
    assert backfill.bun.publication_of(US_RELEASE)["publication"]["makeLatest"]
    assert outcome.outcome == "latest_not_moved"
    assert outcome.made_latest is False
    assert summary_of([outcome])["incomplete"] is True


def test_a_stored_latest_that_is_already_the_store_s_latest_is_left_alone() -> None:
    latest = release_entry(US_RELEASE)
    backfill = us_backfill([latest], latest=latest["buildArtifactId"])
    backfill.bun.store = store_manifest(
        "us", [latest], latest=latest["buildArtifactId"]
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "already_stored"
    assert outcome.made_latest is True
    assert summary_of([outcome])["latest"]["us"]["made_latest"] is True
    assert backfill.bun.steps("build") == []


def test_a_stored_latest_with_another_time_that_is_the_store_s_latest_needs_no_work() -> (
    None
):
    backfill, _ = production_s_latest_stored_as(
        {"createdAt": SUBMISSION_ACCEPTED_AT, "updatedAt": SUBMISSION_ACCEPTED_AT},
        store_latest=None,
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "mismatch"
    # From the store's manifest: the store's latest is already correct.
    assert outcome.made_latest is True
    assert backfill.hugging_face.requests == []
    assert backfill.bun.events == ["manifest read"]
    summary = summary_of([outcome])
    assert summary["latest"]["us"]["made_latest"] is True
    assert summary["incomplete"] is False


def test_a_latest_the_publication_does_not_move_is_reported_and_incomplete() -> None:
    latest = release_entry(US_RELEASE)
    backfill = us_backfill(
        [latest],
        latest=latest["buildArtifactId"],
        files={(US_RELEASES, RELEASE_COMMIT): release_folder(US_RELEASE)},
    )
    backfill.bun.latest_not_moved.add(US_RELEASE)

    [outcome] = backfill.run()

    assert outcome.outcome == "latest_not_moved"
    assert outcome.made_latest is False
    summary = summary_of([outcome])
    assert summary["latest"]["us"] == {
        "build": latest["buildArtifactId"],
        "made_latest": False,
    }
    assert summary["incomplete"] is True


# Comparing with the collector's store -----------------------------------------


def test_builds_stored_already_or_stored_differently_download_nothing() -> None:
    same = release_entry("microcosm-us-2023")
    other_commit = release_entry("microcosm-us-2024")
    other_time = staging_entry(RUN_ID)
    backfill = us_backfill([same, other_commit, other_time])
    backfill.bun.store = store_manifest(
        "us",
        [
            same,
            {**other_commit, "hfCommitSha": "9" * 40},
            {**other_time, "createdAt": LATER, "updatedAt": LATER},
        ],
    )

    outcomes = backfill.run()

    assert [(outcome.source_id, outcome.outcome) for outcome in outcomes] == [
        ("microcosm-us-2023", "already_stored"),
        ("microcosm-us-2024", "mismatch"),
        (RUN_ID, "mismatch"),
    ]
    assert (
        outcome_of(outcomes, "microcosm-us-2023").build_artifact_id
        == (same["buildArtifactId"])
    )
    assert all(outcome.incomplete is False for outcome in outcomes)
    # None of them is the store's latest, as its manifest says.
    assert all(outcome.made_latest is False for outcome in outcomes)
    assert backfill.hugging_face.requests == []
    assert backfill.bun.events == ["manifest read"]


def test_a_stored_time_written_another_way_is_the_same_instant() -> None:
    build = release_entry(US_RELEASE, created_at="2026-10-20T09:00:00.000Z")
    backfill = us_backfill([build])
    backfill.bun.store = store_manifest(
        "us", [{**build, "createdAt": "2026-10-20T10:00:00+01:00"}]
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "already_stored"


@pytest.mark.parametrize(
    ("production_time", "stored_time", "expected"),
    [
        ("2026-10-20T09:00:00.123456Z", "2026-10-20T09:00:00.123Z", "already_stored"),
        # Truncated as toISOString truncates, not rounded.
        ("2026-10-20T09:00:00.123999Z", "2026-10-20T09:00:00.123Z", "already_stored"),
        (
            "2026-10-20T10:00:00.123456+01:00",
            "2026-10-20T09:00:00.123Z",
            "already_stored",
        ),
        ("2026-10-20T09:00:00.123456Z", "2026-10-20T09:00:00.124Z", "mismatch"),
    ],
    ids=["microseconds", "not-rounded", "with-an-offset", "another-millisecond"],
)
def test_times_are_compared_to_the_millisecond_the_worker_stores(
    production_time: str, stored_time: str, expected: str
) -> None:
    # A staging run's time is copied from its run as written, while the
    # worker stores createdAt as toISOString writes it, to the millisecond.
    run = staging_entry(RUN_ID, created_at=production_time)
    backfill = us_backfill([run])
    backfill.bun.store = store_manifest(
        "us", [{**run, "createdAt": stored_time, "updatedAt": stored_time}]
    )

    [outcome] = backfill.run()

    assert outcome.outcome == expected
    assert backfill.hugging_face.requests == []


# The dry run -------------------------------------------------------------------


def test_a_dry_run_downloads_no_diagnostics_runs_no_worker_and_takes_no_lock() -> None:
    latest = release_entry(US_RELEASE)
    run = staging_entry(RUN_ID)
    backfill = us_backfill(
        [run, latest],
        latest=latest["buildArtifactId"],
        files={
            (US_RELEASES, RELEASE_COMMIT): release_folder(US_RELEASE),
            (US_STAGING, STAGING_COMMIT): schema_1_run(RUN_ID),
        },
    )

    outcomes = backfill.run(dry_run=True)

    assert [(outcome.source_id, outcome.outcome) for outcome in outcomes] == [
        (US_RELEASE, "would_build"),
        (RUN_ID, "would_build"),
    ]
    assert outcome_of(outcomes, US_RELEASE).make_latest is True
    assert outcome_of(outcomes, US_RELEASE).hf_commit == RELEASE_COMMIT
    assert outcome_of(outcomes, US_RELEASE).created_at == CREATED_AT
    assert backfill.bun.events == ["manifest read"]
    assert not any(
        path.endswith("diagnostics.json")
        for path in backfill.hugging_face.downloaded_paths()
    )
    summary = summary_of(outcomes)
    assert summary["latest"]["us"] == {
        "build": latest["buildArtifactId"],
        "made_latest": None,
    }
    assert summary["incomplete"] is False


# Building ------------------------------------------------------------------------


def two_release_builds() -> Backfill:
    return us_backfill(
        [release_entry("microcosm-us-2024"), release_entry(US_RELEASE)],
        files={
            (US_RELEASES, RELEASE_COMMIT): {
                **release_folder("microcosm-us-2024"),
                **release_folder(US_RELEASE),
            }
        },
    )


def test_the_lock_is_held_only_around_each_publication() -> None:
    backfill = two_release_builds()

    backfill.run()

    assert backfill.bun.events == [
        "manifest read",
        "build microcosm-us-2024",
        "lock taken",
        "publication microcosm-us-2024",
        "lock released",
        f"build {US_RELEASE}",
        "lock taken",
        f"publication {US_RELEASE}",
        "lock released",
    ]


def test_only_the_manifest_read_and_the_publication_get_the_blob_token() -> None:
    backfill = two_release_builds()

    backfill.run(size_limits={"CALIBRATION_TREE_MAX_GZIP_BYTES": "1000"})

    for call in backfill.bun.steps("build"):
        assert set(call["env"]) == {"PATH", "LANG", "CALIBRATION_TREE_MAX_GZIP_BYTES"}
        assert call["argv"][:2] == ["/usr/local/bin/bun", "run"]
    for call in [
        *backfill.bun.steps("manifest read"),
        *backfill.bun.steps("publication"),
    ]:
        assert call["env"]["CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN"] == BLOB_TOKEN
    for call in backfill.bun.calls:
        assert READ_TOKEN not in json.dumps(call["env"]) + json.dumps(call["argv"])
    for call in backfill.bun.steps("build"):
        assert BLOB_TOKEN not in json.dumps(call["env"]) + json.dumps(call["argv"])


def test_each_build_s_files_are_removed_after_it() -> None:
    backfill = two_release_builds()

    backfill.run()

    request_files = [Path(call["argv"][3]) for call in backfill.bun.steps("build")]
    assert len(request_files) == 2
    assert request_files[0].parent != request_files[1].parent
    assert not any(path.parent.exists() for path in request_files)


def test_worker_and_publication_refusals_are_final_and_the_next_build_runs() -> None:
    backfill = us_backfill(
        [
            release_entry("microcosm-us-2023"),
            release_entry("microcosm-us-2024"),
            release_entry(US_RELEASE),
        ],
        files={
            (US_RELEASES, RELEASE_COMMIT): {
                **release_folder("microcosm-us-2023"),
                **release_folder("microcosm-us-2024"),
                **release_folder(US_RELEASE),
            }
        },
    )
    backfill.bun.worker_exits["microcosm-us-2023"] = 2
    backfill.bun.publication_exits["microcosm-us-2024"] = 2

    outcomes = backfill.run()

    assert [(outcome.source_id, outcome.outcome) for outcome in outcomes] == [
        ("microcosm-us-2023", "unbuildable"),
        ("microcosm-us-2024", "refused"),
        (US_RELEASE, "built"),
    ]
    assert "The worker stopped" in outcome_of(outcomes, "microcosm-us-2023").reason
    assert all(outcome.incomplete is False for outcome in outcomes)
    assert summary_of(outcomes)["incomplete"] is False


@pytest.mark.parametrize(
    "failure",
    [
        ("worker exit", 1),
        ("worker exit", 3),
        ("publication exit", 1),
        ("publication exit", 3),
        ("build timeout", None),
        ("publication timeout", None),
        ("hugging face", None),
        ("download differs", None),
    ],
    ids=[
        "worker-1",
        "worker-3",
        "publication-1",
        "publication-3",
        "worker-timeout",
        "publication-timeout",
        "hugging-face-answer",
        "download-differs-from-listing",
    ],
)
def test_other_failures_are_errors_and_the_next_build_still_runs(
    failure: tuple[str, int | None],
) -> None:
    kind, exit_code = failure
    failing = "microcosm-us-2024"
    files = {
        (US_RELEASES, RELEASE_COMMIT): {
            **release_folder(failing),
            **release_folder(US_RELEASE),
        }
    }
    backfill = us_backfill(
        [release_entry(failing), release_entry(US_RELEASE)], files=files
    )
    if kind == "worker exit":
        backfill.bun.worker_exits[failing] = exit_code
    elif kind == "publication exit":
        backfill.bun.publication_exits[failing] = exit_code
    elif kind == "build timeout":
        backfill.bun.timed_out.add(("build", failing))
    elif kind == "publication timeout":
        backfill.bun.timed_out.add(("publication", failing))
    elif kind == "hugging face":
        # Its folder is pinned to a commit Hugging Face doesn't have.
        backfill = us_backfill(
            [release_entry(failing, commit="4" * 40), release_entry(US_RELEASE)],
            files=files,
        )
    else:
        stored = backfill.hugging_face.datasets[US_RELEASES]
        diagnostics = f"releases/{failing}/calibration_diagnostics.json"
        stored.lfs_paths.add(diagnostics)
        stored.reported_lfs_sha256[diagnostics] = "0" * 64

    outcomes = backfill.run()

    assert [(outcome.source_id, outcome.outcome) for outcome in outcomes] == [
        (failing, "error"),
        (US_RELEASE, "built"),
    ]
    assert outcome_of(outcomes, failing).incomplete is True
    assert outcome_of(outcomes, failing).reason
    assert summary_of(outcomes)["incomplete"] is True


# Limits ------------------------------------------------------------------------


def test_a_file_over_the_size_limit_is_too_large_and_never_downloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backfill_module(), "MAX_SOURCE_FILE_BYTES", len(DIAGNOSTICS) - 1
    )
    backfill = us_backfill(
        [release_entry(US_RELEASE)],
        files={(US_RELEASES, RELEASE_COMMIT): release_folder(US_RELEASE)},
    )

    [outcome] = backfill.run()

    assert outcome.outcome == "too_large"
    assert outcome.incomplete is False
    assert backfill.hugging_face.downloaded_paths() == []
    assert backfill.bun.steps("build") == []


def test_no_build_starts_after_the_time_limit() -> None:
    backfill = us_backfill(
        [
            release_entry("microcosm-us-2023"),
            release_entry("microcosm-us-2024"),
            release_entry(US_RELEASE),
        ],
        files={
            (US_RELEASES, RELEASE_COMMIT): {
                **release_folder("microcosm-us-2023"),
                **release_folder("microcosm-us-2024"),
                **release_folder(US_RELEASE),
            }
        },
    )
    backfill.bun.clock = backfill.clock
    backfill.bun.seconds_per_build = 25 * MINUTE

    outcomes = backfill.run(time_limit_seconds=40 * MINUTE)

    assert [(outcome.source_id, outcome.outcome) for outcome in outcomes] == [
        ("microcosm-us-2023", "built"),
        ("microcosm-us-2024", "built"),
        (US_RELEASE, "deferred"),
    ]
    assert outcome_of(outcomes, US_RELEASE).incomplete is True
    assert [call["source"] for call in backfill.bun.steps("build")] == [
        "microcosm-us-2023",
        "microcosm-us-2024",
    ]
    assert summary_of(outcomes)["incomplete"] is True


def test_the_command_s_time_limit_is_about_40_minutes() -> None:
    assert backfill_module().BUILD_TIME_LIMIT_SECONDS == 40 * MINUTE


# Parity with production ----------------------------------------------------------


def test_each_built_entry_says_whether_its_build_id_matches_production_s() -> None:
    backfill = us_backfill(
        [
            release_entry("microcosm-us-2024"),
            release_entry(US_RELEASE),
            staging_entry(RUN_ID),
        ],
        files={
            (US_RELEASES, RELEASE_COMMIT): {
                **release_folder("microcosm-us-2024"),
                **release_folder(US_RELEASE),
            },
            (US_STAGING, STAGING_COMMIT): schema_1_run(RUN_ID),
        },
    )
    # The worker hashes 4 of a staging build's files, the publisher 7, so a
    # staging build's ID differs from production's.
    backfill.bun.built_build_ids[US_RELEASE] = "d" * 64
    backfill.bun.built_build_ids[RUN_ID] = "c" * 64

    outcomes = backfill.run()

    assert outcome_of(outcomes, "microcosm-us-2024").matches_production is True
    assert outcome_of(outcomes, US_RELEASE).matches_production is False
    assert outcome_of(outcomes, US_RELEASE).build_artifact_id == "d" * 64
    assert outcome_of(outcomes, RUN_ID).matches_production is False
    summary = summary_of(outcomes)
    assert summary["release_builds_differing_from_production"] == 1
    assert summary["outcomes"] == {"built": 3}


def test_each_outcome_is_one_json_document() -> None:
    backfill = us_backfill(
        [release_entry(US_RELEASE)],
        files={(US_RELEASES, RELEASE_COMMIT): release_folder(US_RELEASE)},
    )

    [outcome] = backfill.run()

    document = outcome.to_json()
    assert json.loads(json.dumps(document)) == document
    assert {
        "country": "us",
        "kind": "release",
        "source_id": US_RELEASE,
        "outcome": "built",
        "hf_repo": US_RELEASES,
        "hf_commit": RELEASE_COMMIT,
        "created_at": CREATED_AT,
        "make_latest": False,
        "production_build_artifact_id": build_id("us", "release", US_RELEASE),
        "build_artifact_id": build_id("us", "release", US_RELEASE),
        "matches_production": True,
        "incomplete": False,
    }.items() <= document.items()
