"""The releases.json check applies the interface package's rules to a dataset.

These tests need ``microcosm_emitter_interface.releases``, from the
microcosm-emitter-interface package, which is not published yet. Until it is a
dependency they are skipped. To run them against a checkout of
PolicyEngine/microcosm-emitter::

    uv run --with ../microcosm-emitter/packages/microcosm-emitter-interface \\
      pytest tests/test_releases_record_check.py
"""

import hashlib
import json
from dataclasses import replace

import httpx
import pytest

from telemetry_collector.huggingface_check import (
    RELEASE_RULES_MODULE,
    RELEASES_RECORD_CHECK,
    check_releases_record,
    run_checks,
)
from telemetry_collector.huggingface_read import (
    HUGGINGFACE_DATASETS,
    HuggingFaceReader,
    HuggingFaceReadSettings,
)
from tests.huggingface_fakes import CONTENT_HOST, FakeHuggingFace

release_rules = pytest.importorskip(
    RELEASE_RULES_MODULE,
    reason="microcosm-emitter-interface is not a dependency until it is published",
)

EXAMPLE_TOKEN = "hf_test_collector_read_token"
UK_RELEASES = "policyengine/populace-uk-private"
HEAD = "c" * 40
NATIONAL = "microcosm-uk-2024-25-national"
FIRST_BUILD = "uk-frs-calibration-attempt-20261014T214443Z-d12ff9b3"
SECOND_BUILD = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"
DATASET_FILE = "microcosm_uk_2024_25.h5"
DIAGNOSTICS_FILE = "calibration_diagnostics.json"


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def folder(build: str) -> str:
    return f"releases/{NATIONAL}/{build}"


def build_files(build: str, *, dataset_bytes: bytes) -> dict[str, bytes]:
    contents = {DATASET_FILE: dataset_bytes, DIAGNOSTICS_FILE: b'{"targets": []}'}
    roles = {DATASET_FILE: "dataset", DIAGNOSTICS_FILE: "calibration_diagnostics"}
    manifest = {
        "release": NATIONAL,
        "build": build,
        "files": [
            {"name": name, "role": roles[name], "sha256": digest(content)}
            for name, content in contents.items()
        ],
    }
    files = {f"{folder(build)}/{name}": content for name, content in contents.items()}
    files[f"{folder(build)}/release_manifest.json"] = json.dumps(manifest).encode()
    return files


def record_document() -> dict:
    record = release_rules.ReleasesRecord.empty()
    for build, published_at in (
        (FIRST_BUILD, "2026-10-14T21:44:43+00:00"),
        (SECOND_BUILD, "2026-10-19T08:33:23+00:00"),
    ):
        record = release_rules.add_build(
            record,
            release=NATIONAL,
            build=build,
            release_role="national_default",
            published_at=published_at,
            make_current=True,
        )
    return record.to_document()


@pytest.fixture
def hub() -> FakeHuggingFace:
    hub = FakeHuggingFace(token=EXAMPLE_TOKEN)
    files = {
        "README.md": b"# UK releases",
        "releases.json": json.dumps(record_document()).encode(),
        **build_files(FIRST_BUILD, dataset_bytes=b"first rows"),
        **build_files(SECOND_BUILD, dataset_bytes=b"second rows"),
    }
    hub.add_dataset(
        UK_RELEASES,
        files,
        lfs_paths={path for path in files if path.endswith(".h5")},
        head_commit=HEAD,
    )
    return hub


def uk_files(hub: FakeHuggingFace) -> dict[str, bytes]:
    return hub.datasets[UK_RELEASES].files


def write_record(hub: FakeHuggingFace, document: dict) -> None:
    uk_files(hub)["releases.json"] = json.dumps(document).encode()


def findings_for(hub: FakeHuggingFace):
    reader = HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN), client=hub.client()
    )
    return check_releases_record(reader, UK_RELEASES, HEAD, release_rules)


def codes(hub: FakeHuggingFace) -> list[str]:
    return sorted(finding.code for finding in findings_for(hub))


# A dataset that follows the rules -------------------------------------------


def test_a_dataset_that_follows_every_rule_has_no_findings(
    hub: FakeHuggingFace,
) -> None:
    assert findings_for(hub) == []


def test_every_read_is_pinned_to_the_head_commit(hub: FakeHuggingFace) -> None:
    findings_for(hub)

    huggingface_paths = [
        request.url.path
        for request in hub.requests
        if request.url.host == "huggingface.co"
    ]
    assert huggingface_paths
    assert all(f"/{HEAD}" in path for path in huggingface_paths)


def test_lfs_files_are_compared_by_their_recorded_sha256_without_a_download(
    hub: FakeHuggingFace,
) -> None:
    findings_for(hub)

    assert hub.requests_to(httpx.URL(CONTENT_HOST).host) == []
    assert not any(request.url.path.endswith(".h5") for request in hub.requests)


# One releases.json and no pointer files ----------------------------------------


def test_a_missing_releases_json_is_reported(hub: FakeHuggingFace) -> None:
    del uk_files(hub)["releases.json"]

    assert codes(hub) == ["RECORD_MISSING"]


def test_a_second_releases_json_is_reported(hub: FakeHuggingFace) -> None:
    uk_files(hub)[f"releases/{NATIONAL}/releases.json"] = b"{}"

    (finding,) = findings_for(hub)
    assert finding.code == "EXTRA_RECORD"
    assert f"releases/{NATIONAL}/releases.json" in finding.message
    assert finding.check == RELEASES_RECORD_CHECK
    assert finding.dataset == UK_RELEASES


def test_every_pointer_file_is_reported_wherever_it_is(hub: FakeHuggingFace) -> None:
    for pointer_path in (
        "latest.json",
        "latest-national.json",
        f"releases/{NATIONAL}/latest.json",
    ):
        uk_files(hub)[pointer_path] = b"{}"

    findings = findings_for(hub)

    assert [finding.code for finding in findings] == ["POINTER_FILE_PRESENT"] * 3
    assert all("latest" in finding.message for finding in findings)


# The record's rules, from the interface package ------------------------------


def test_a_record_that_is_not_valid_is_reported_with_the_rules_code(
    hub: FakeHuggingFace,
) -> None:
    write_record(hub, {"schema_version": 1, "releases": {}})

    assert codes(hub) == ["INVALID_RECORD"]


def test_a_current_build_that_is_not_listed_is_reported(hub: FakeHuggingFace) -> None:
    document = record_document()
    document["releases"][NATIONAL]["current"] = "uk-frs-calibration-attempt-unlisted"
    write_record(hub, document)

    assert codes(hub) == ["CURRENT_NOT_LISTED"]


def test_a_withdrawn_current_build_is_reported(hub: FakeHuggingFace) -> None:
    document = record_document()
    document["releases"][NATIONAL]["builds"][SECOND_BUILD]["withdrawn"] = True
    write_record(hub, document)

    assert codes(hub) == ["CURRENT_WITHDRAWN"]


def test_a_listed_build_without_its_folder_is_reported(hub: FakeHuggingFace) -> None:
    for path in [
        path for path in uk_files(hub) if path.startswith(folder(FIRST_BUILD))
    ]:
        del uk_files(hub)[path]

    assert codes(hub) == ["BUILD_FOLDER_MISSING"]


def test_a_manifest_naming_another_build_is_reported(hub: FakeHuggingFace) -> None:
    manifest_path = f"{folder(FIRST_BUILD)}/release_manifest.json"
    manifest = json.loads(uk_files(hub)[manifest_path])
    manifest["build"] = SECOND_BUILD
    uk_files(hub)[manifest_path] = json.dumps(manifest).encode()

    assert codes(hub) == ["MANIFEST_MISMATCH"]


# Published files against their manifests -------------------------------------


def test_an_lfs_file_whose_sha256_differs_from_its_manifest_is_reported(
    hub: FakeHuggingFace,
) -> None:
    dataset_path = f"{folder(FIRST_BUILD)}/{DATASET_FILE}"
    hub.datasets[UK_RELEASES].reported_lfs_sha256[dataset_path] = digest(b"replaced")

    (finding,) = findings_for(hub)
    assert finding.code == "SHA256_MISMATCH"
    assert dataset_path in finding.message


def test_a_changed_small_file_is_downloaded_hashed_and_reported(
    hub: FakeHuggingFace,
) -> None:
    diagnostics_path = f"{folder(SECOND_BUILD)}/{DIAGNOSTICS_FILE}"
    uk_files(hub)[diagnostics_path] = b'{"targets": ["changed"]}'

    (finding,) = findings_for(hub)
    assert finding.code == "SHA256_MISMATCH"
    assert diagnostics_path in finding.message


def test_a_file_the_manifest_lists_but_the_folder_lacks_is_reported(
    hub: FakeHuggingFace,
) -> None:
    del uk_files(hub)[f"{folder(SECOND_BUILD)}/{DIAGNOSTICS_FILE}"]

    assert codes(hub) == ["FILE_MISSING"]


def test_a_file_in_a_build_folder_its_manifest_does_not_list_is_reported(
    hub: FakeHuggingFace,
) -> None:
    uk_files(hub)[f"{folder(FIRST_BUILD)}/added_later.json"] = b"{}"

    (finding,) = findings_for(hub)
    assert finding.code == "FILE_NOT_IN_MANIFEST"
    assert "added_later.json" in finding.message


def test_a_manifest_without_a_usable_file_list_is_reported(
    hub: FakeHuggingFace,
) -> None:
    manifest_path = f"{folder(FIRST_BUILD)}/release_manifest.json"
    uk_files(hub)[manifest_path] = json.dumps(
        {"release": NATIONAL, "build": FIRST_BUILD}
    ).encode()

    assert codes(hub) == ["MANIFEST_FILES_UNREADABLE"]


def test_a_manifest_nested_too_deeply_to_decode_is_reported(
    hub: FakeHuggingFace,
) -> None:
    nesting_depth = 200_000
    manifest_path = f"{folder(FIRST_BUILD)}/release_manifest.json"
    uk_files(hub)[manifest_path] = b"[" * nesting_depth + b"]" * nesting_depth

    assert codes(hub) == ["MANIFEST_FILES_UNREADABLE", "MANIFEST_MISMATCH"]


def test_a_read_that_fails_during_the_check_is_reported(hub: FakeHuggingFace) -> None:
    answer_like_hugging_face = hub.handle

    def fail_manifest_reads(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("release_manifest.json"):
            hub.requests.append(request)
            return httpx.Response(500)
        return answer_like_hugging_face(request)

    reader = HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN),
        client=httpx.Client(transport=httpx.MockTransport(fail_manifest_reads)),
    )

    findings = check_releases_record(reader, UK_RELEASES, HEAD, release_rules)

    assert [finding.code for finding in findings] == ["HTTP_500"]


# Running the check for switched-on datasets only -----------------------------


def test_only_switched_on_datasets_get_the_releases_json_check(
    hub: FakeHuggingFace,
) -> None:
    uk_files(hub)["latest-national.json"] = b"{}"
    staging = hub.add_dataset(
        "policyengine/populace-uk-staging", {"latest.json": b"{}"}
    )
    reader = HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN), client=hub.client()
    )
    uk_releases = replace(HUGGINGFACE_DATASETS[1], checks_releases_record=True)
    uk_staging = HUGGINGFACE_DATASETS[4]

    findings = run_checks(reader, [uk_releases, uk_staging])

    assert staging.files == {"latest.json": b"{}"}
    assert [(finding.dataset, finding.code) for finding in findings] == [
        (UK_RELEASES, "POINTER_FILE_PRESENT")
    ]
