"""The daily check requests every dataset the collector reads with its token."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from telemetry_collector.huggingface_check import (
    CONFIGURATION_CHECK,
    RELEASE_RULES_MODULE,
    RELEASES_RECORD_CHECK,
    TOKEN_CHECK,
    Finding,
    ReleaseRulesUnavailableError,
    check_token_access,
    load_release_rules,
    main,
    manifest_file_digests,
    run_all_checks,
    run_checks,
)
from telemetry_collector.huggingface_read import (
    HUGGINGFACE_DATASETS,
    TOKEN_VARIABLE,
    HuggingFaceReader,
    HuggingFaceReadSettings,
)
from tests.huggingface_fakes import FakeHuggingFace

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_TOKEN = "hf_test_collector_read_token"
UK_RELEASES = "policyengine/populace-uk-private"


def hub_with_every_dataset() -> FakeHuggingFace:
    hub = FakeHuggingFace(token=EXAMPLE_TOKEN)
    for dataset in HUGGINGFACE_DATASETS:
        hub.add_dataset(dataset.name, private=not dataset.public)
    return hub


def run_main(
    hub: FakeHuggingFace, environment: dict[str, str] | None = None
) -> tuple[int, list[dict]]:
    output = io.StringIO()
    exit_code = main(
        {TOKEN_VARIABLE: EXAMPLE_TOKEN} if environment is None else environment,
        client=hub.client(),
        output=output,
    )
    return exit_code, [json.loads(entry) for entry in output.getvalue().splitlines()]


def problems(log_entries: list[dict]) -> list[dict]:
    return [entry for entry in log_entries if "code" in entry]


def test_every_dataset_answering_200_passes_the_token_check() -> None:
    hub = hub_with_every_dataset()

    exit_code, log_entries = run_main(hub)

    assert exit_code == 0
    assert problems(log_entries) == []
    (summary,) = log_entries
    assert summary["severity"] == "INFO"
    assert summary["checked_datasets"] == [
        dataset.name for dataset in HUGGINGFACE_DATASETS
    ]
    assert sorted(request.url.path for request in hub.requests) == sorted(
        f"/api/datasets/{dataset.name}" for dataset in HUGGINGFACE_DATASETS
    )
    assert all(
        request.headers["Authorization"] == f"Bearer {EXAMPLE_TOKEN}"
        for request in hub.requests
    )


@pytest.mark.parametrize("status_code", [302, 401, 403, 404, 429, 500, 503])
def test_a_dataset_answering_anything_but_200_fails_the_check(
    status_code: int,
) -> None:
    hub = hub_with_every_dataset()
    hub.datasets[UK_RELEASES].forced_status = status_code

    exit_code, log_entries = run_main(hub)

    assert exit_code == 1
    (problem,) = problems(log_entries)
    assert problem == {
        "severity": "ERROR",
        "message": problem["message"],
        "check": TOKEN_CHECK,
        "dataset": UK_RELEASES,
        "code": f"HTTP_{status_code}",
    }
    assert f"HTTP {status_code}" in problem["message"]
    summary = log_entries[-1]
    assert summary["severity"] == "ERROR"
    assert summary["problems"] == 1
    assert len(hub.requests) == len(HUGGINGFACE_DATASETS)


def test_an_unreachable_hugging_face_fails_the_check_for_every_dataset() -> None:
    def refuse_connection(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    output = io.StringIO()
    exit_code = main(
        {TOKEN_VARIABLE: EXAMPLE_TOKEN},
        client=httpx.Client(transport=httpx.MockTransport(refuse_connection)),
        output=output,
    )

    log_entries = [json.loads(entry) for entry in output.getvalue().splitlines()]
    assert exit_code == 1
    assert [(entry["dataset"], entry["code"]) for entry in problems(log_entries)] == [
        (dataset.name, "UNREACHABLE") for dataset in HUGGINGFACE_DATASETS
    ]


def test_a_200_without_a_head_commit_fails_the_check() -> None:
    reader = HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"private": True})
            )
        ),
    )

    findings, head_commits = check_token_access(reader, HUGGINGFACE_DATASETS[:1])

    assert findings == [
        Finding(
            dataset=HUGGINGFACE_DATASETS[0].name,
            check=TOKEN_CHECK,
            code="UNUSABLE_RESPONSE",
            message=findings[0].message,
        )
    ]
    assert head_commits == {}


def test_the_head_commit_of_each_dataset_that_answered_is_returned() -> None:
    hub = hub_with_every_dataset()
    hub.datasets[UK_RELEASES].head_commit = "e" * 40
    hub.datasets["policyengine/populace-us-staging"].forced_status = 404
    reader = HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN), client=hub.client()
    )

    findings, head_commits = check_token_access(reader, HUGGINGFACE_DATASETS)

    assert [finding.dataset for finding in findings] == [
        "policyengine/populace-us-staging"
    ]
    assert head_commits[UK_RELEASES] == "e" * 40
    assert set(head_commits) == {dataset.name for dataset in HUGGINGFACE_DATASETS} - {
        "policyengine/populace-us-staging"
    }


@pytest.mark.parametrize(
    "environment",
    [{}, {TOKEN_VARIABLE: EXAMPLE_TOKEN, "HF_TOKEN": "hf_test_generic_token"}],
    ids=["missing token", "generic token"],
)
def test_an_unusable_token_setting_fails_the_job_before_any_request(
    environment: dict[str, str],
) -> None:
    hub = hub_with_every_dataset()

    exit_code, log_entries = run_main(hub, environment)

    assert exit_code == 2
    (entry,) = log_entries
    assert entry["severity"] == "ERROR"
    assert entry["check"] == CONFIGURATION_CHECK
    assert hub.requests == []
    assert "hf_test_generic_token" not in json.dumps(entry)


def test_no_log_entry_contains_the_token() -> None:
    hub = hub_with_every_dataset()
    for dataset in hub.datasets.values():
        dataset.forced_status = 401

    exit_code, log_entries = run_main(hub)

    assert exit_code == 1
    assert EXAMPLE_TOKEN not in json.dumps(log_entries)


def test_every_log_entry_has_a_severity_and_a_message() -> None:
    hub = hub_with_every_dataset()
    hub.datasets[UK_RELEASES].forced_status = 403

    _, log_entries = run_main(hub)

    assert log_entries
    for entry in log_entries:
        assert entry["severity"] in {"INFO", "ERROR"}
        assert isinstance(entry["message"], str) and entry["message"]


def test_the_check_runs_as_a_module_and_exits_nonzero_without_a_token() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "telemetry_collector.huggingface_check"],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 2, completed.stderr
    (entry,) = [json.loads(entry) for entry in completed.stdout.splitlines()]
    assert TOKEN_VARIABLE in entry["message"]


# The releases.json check stays off until the interface package is published --


def reader_for(hub: FakeHuggingFace) -> HuggingFaceReader:
    return HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN), client=hub.client()
    )


def release_rules_must_not_load():
    raise AssertionError("The releases.json rules were loaded.")


def test_no_dataset_checks_releases_json_unless_the_interface_is_a_dependency() -> None:
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    interface_declared = any(
        dependency.startswith("microcosm-emitter-interface")
        for dependency in dependencies
    )
    switched_on = [
        dataset.name
        for dataset in HUGGINGFACE_DATASETS
        if dataset.checks_releases_record
    ]

    assert interface_declared or switched_on == []


def test_with_every_dataset_switched_off_the_rules_are_never_loaded() -> None:
    hub = hub_with_every_dataset()

    findings = run_checks(
        reader_for(hub),
        HUGGINGFACE_DATASETS,
        release_rules_loader=release_rules_must_not_load,
    )

    assert findings == []
    assert all(request.url.path.count("/tree/") == 0 for request in hub.requests)


def test_the_summary_says_the_releases_json_check_is_switched_off() -> None:
    _, log_entries = run_main(hub_with_every_dataset())

    summary = log_entries[-1]
    assert summary["releases_record_checked"] == []
    assert summary["releases_record_completed"] == []
    assert "releases.json check is switched off" in summary["message"]


class StandInRecordError(Exception):
    violations: tuple = ()


def stand_in_release_rules(folder_check=None) -> SimpleNamespace:
    """Rules that find nothing wrong with a releases.json listing no releases.

    The real rules come from the interface package, which CI doesn't install;
    these let a releases.json check run to its end without it. ``folder_check``
    stands in for the interface's check of every listed build's folder.
    """

    release_rules = SimpleNamespace(
        RELEASES_RECORD_PATH="releases.json",
        RELEASE_MANIFEST_NAME="release_manifest.json",
        ReleaseRecordError=StandInRecordError,
        ReleasesRecord=SimpleNamespace(
            parse=lambda content: SimpleNamespace(releases={})
        ),
        is_pointer_file=lambda path: path.rsplit("/", 1)[-1].startswith("latest"),
        build_folder=lambda release, build: f"releases/{release}/{build}",
    )
    release_rules.check_repository = folder_check or (lambda record, files, read: [])
    return release_rules


def switched_on_hub(files: dict[str, bytes] | None) -> tuple[FakeHuggingFace, object]:
    hub = FakeHuggingFace(token=EXAMPLE_TOKEN)
    dataset = replace(HUGGINGFACE_DATASETS[1], checks_releases_record=True)
    hub.add_dataset(dataset.name, files, private=not dataset.public)
    return hub, dataset


def test_a_releases_json_check_that_ran_to_its_end_is_listed_as_completed() -> None:
    hub, dataset = switched_on_hub({"releases.json": b"{}"})

    outcome = run_all_checks(
        reader_for(hub), [dataset], release_rules_loader=stand_in_release_rules
    )

    assert outcome.findings == []
    assert outcome.releases_record_completed == (dataset.name,)


def test_a_releases_json_check_stopped_by_a_read_error_is_not_completed() -> None:
    hub, dataset = switched_on_hub({"releases.json": b"{}"})

    def reads_a_missing_manifest(record, files, read):
        read("releases/missing/build/release_manifest.json")
        return []

    outcome = run_all_checks(
        reader_for(hub),
        [dataset],
        release_rules_loader=lambda: stand_in_release_rules(reads_a_missing_manifest),
    )

    assert [finding.code for finding in outcome.findings] == ["HTTP_404"]
    assert outcome.releases_record_completed == ()


def test_a_missing_releases_json_is_not_a_completed_check() -> None:
    hub, dataset = switched_on_hub({})

    outcome = run_all_checks(
        reader_for(hub), [dataset], release_rules_loader=stand_in_release_rules
    )

    assert [finding.code for finding in outcome.findings] == ["RECORD_MISSING"]
    assert outcome.releases_record_completed == ()


def test_a_dataset_whose_token_check_failed_has_no_completed_releases_json_check() -> (
    None
):
    hub, dataset = switched_on_hub({"releases.json": b"{}"})
    hub.datasets[dataset.name].forced_status = 503

    outcome = run_all_checks(
        reader_for(hub), [dataset], release_rules_loader=stand_in_release_rules
    )

    assert [(finding.check, finding.code) for finding in outcome.findings] == [
        (TOKEN_CHECK, "HTTP_503")
    ]
    assert outcome.releases_record_completed == ()


def test_without_the_interface_package_no_releases_json_check_completes() -> None:
    hub, dataset = switched_on_hub({"releases.json": b"{}"})

    def interface_missing():
        raise ReleaseRulesUnavailableError("microcosm-emitter-interface is missing.")

    outcome = run_all_checks(
        reader_for(hub), [dataset], release_rules_loader=interface_missing
    )

    assert outcome.releases_record_completed == ()


def test_a_dataset_switched_on_without_the_interface_package_fails_the_check() -> None:
    hub = hub_with_every_dataset()
    switched_on = replace(HUGGINGFACE_DATASETS[1], checks_releases_record=True)

    def interface_missing():
        raise ReleaseRulesUnavailableError("microcosm-emitter-interface is missing.")

    findings = run_checks(
        reader_for(hub),
        [HUGGINGFACE_DATASETS[0], switched_on],
        release_rules_loader=interface_missing,
    )

    assert findings == [
        Finding(
            dataset=switched_on.name,
            check=RELEASES_RECORD_CHECK,
            code="RELEASE_RULES_UNAVAILABLE",
            message="microcosm-emitter-interface is missing.",
        )
    ]


def test_a_dataset_whose_token_check_failed_gets_no_releases_json_check() -> None:
    hub = hub_with_every_dataset()
    switched_on = replace(HUGGINGFACE_DATASETS[1], checks_releases_record=True)
    hub.datasets[switched_on.name].forced_status = 404

    findings = run_checks(
        reader_for(hub), [switched_on], release_rules_loader=lambda: object()
    )

    assert [(finding.check, finding.code) for finding in findings] == [
        (TOKEN_CHECK, "HTTP_404")
    ]


def test_loading_the_rules_names_the_package_when_it_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, RELEASE_RULES_MODULE, None)

    with pytest.raises(ReleaseRulesUnavailableError) as raised:
        load_release_rules()

    assert "microcosm-emitter-interface" in str(raised.value)


def test_importing_the_check_does_not_import_the_interface_package() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, telemetry_collector.huggingface_check; "
            "print(any(name.startswith('microcosm_emitter_interface') "
            "for name in sys.modules))",
        ],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert completed.stdout.strip() == "False"


# The build manifest's file list ------------------------------------------------


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def artifact(path: str, sha256: str, **fields: str) -> dict[str, str]:
    """One entry of a release manifest's ``artifacts`` map."""

    return {
        "kind": "microdata",
        "role": "dataset",
        "path": path,
        "repo_id": "policyengine/populace-uk-private",
        "sha256": sha256,
        **fields,
    }


def test_a_manifest_lists_each_file_in_its_artifacts_map_with_its_sha256() -> None:
    # The keys name the artifacts; each entry's path is the file's bare name in
    # the build folder (decided 2026-10-29: today's map, without revisions).
    manifest = {
        "release": "microcosm-uk-2024-25-national",
        "build": "build-1",
        "artifacts": {
            "microcosm_uk_2024_25": artifact(
                "microcosm_uk_2024_25.h5", digest(b"rows")
            ),
            "calibration_diagnostics": artifact(
                "calibration_diagnostics.json",
                digest(b"{}"),
                kind="diagnostics",
                role="calibration_diagnostics",
            ),
        },
    }

    assert manifest_file_digests(manifest) == {
        "microcosm_uk_2024_25.h5": digest(b"rows"),
        "calibration_diagnostics.json": digest(b"{}"),
    }


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {"release": "r", "build": "b"},
        {"files": [{"name": "a.h5", "sha256": "0" * 64}]},
        {"artifacts": [artifact("a.h5", "0" * 64)]},
        {"artifacts": {}},
        {"artifacts": {"a": "a.h5"}},
        {"artifacts": {"a": {"path": "a.h5"}}},
        {"artifacts": {"a": artifact("a.h5", "ABC")}},
        {"artifacts": {"a": {"sha256": "0" * 64}}},
        {"artifacts": {"a": artifact("nested/a.h5", "0" * 64)}},
        {"artifacts": {"a": artifact("releases/r/b/a.h5", "0" * 64)}},
        {"artifacts": {"a": artifact("a\\b.h5", "0" * 64)}},
        {"artifacts": {"a": artifact("", "0" * 64)}},
        {"artifacts": {"a": artifact("..", "0" * 64)}},
        {
            "artifacts": {
                "a": artifact("a.h5", "0" * 64),
                "b": artifact("a.h5", "1" * 64),
            }
        },
    ],
    ids=[
        "not an object",
        "no artifacts map",
        "only the files list assumed before the decision",
        "artifacts not a map",
        "artifacts map empty",
        "entry not an object",
        "no sha256",
        "sha256 not lowercase hexadecimal",
        "no path",
        "path in a subfolder",
        "path including the build folder",
        "path with a backslash",
        "empty path",
        "path leaving the folder",
        "path listed twice",
    ],
)
def test_a_manifest_without_a_usable_artifacts_map_is_refused(
    manifest: object,
) -> None:
    with pytest.raises(ValueError):
        manifest_file_digests(manifest)
