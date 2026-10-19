from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from telemetry_collector.submissions import (
    MAX_FILES_PER_SUBMISSION,
    SubmissionRequest,
    canonical_json,
    derive_job_id,
    derive_submission_id,
    intake_object_name,
    inventory_digest,
    receipt_digest,
)

DIAGNOSTICS_SHA256 = "a" * 64
MANIFEST_SHA256 = "b" * 64


def calibration_submission(**changes: Any) -> dict[str, Any]:
    submission: dict[str, Any] = {
        "contract_version": 1,
        "kind": "calibration-diagnostics",
        "licensed_data": True,
        "provenance": {
            "run_id": "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71",
            "build_id": "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71",
            "country_code": "UK",
            "release": None,
            "huggingface": {
                "repo": "policyengine/populace-uk-staging",
                "commit": "0123456789abcdef0123456789abcdef01234567",
                "folder": "staged/uk-frs-calibration-attempt-20261019T083323Z-6cba5d71",
            },
        },
        "files": [
            {
                "name": "calibration_diagnostics.json",
                "role": "calibration_diagnostics",
                "size": 2048,
                "sha256": DIAGNOSTICS_SHA256,
            },
            {
                "name": "build_manifest.json",
                "role": "build_manifest",
                "size": 512,
                "sha256": MANIFEST_SHA256,
            },
        ],
    }
    submission.update(changes)
    return submission


def orrery_submission(**changes: Any) -> dict[str, Any]:
    submission: dict[str, Any] = {
        "contract_version": 1,
        "kind": "orrery-inputs",
        "licensed_data": True,
        "provenance": {
            "run_id": "uk-run-1",
            "build_id": "uk-frs-calibration-attempt-1",
            "country_code": "UK",
            "release": None,
            "huggingface": None,
        },
        "files": [
            {
                "name": "graph.schema.json",
                "role": "schema",
                "size": 100,
                "sha256": "c" * 64,
            },
            {
                "name": "graph.execution.json",
                "role": "execution_overlay",
                "size": 200,
                "sha256": "d" * 64,
            },
            {
                "name": "upstream-" + "e" * 64 + ".json",
                "role": "upstream",
                "size": 300,
                "sha256": "e" * 64,
            },
        ],
    }
    submission.update(changes)
    return submission


def test_accepts_calibration_diagnostics_with_optional_manifest() -> None:
    request = SubmissionRequest.model_validate(calibration_submission())

    assert request.kind == "calibration-diagnostics"
    assert [file.role for file in request.files] == [
        "calibration_diagnostics",
        "build_manifest",
    ]


def test_accepts_orrery_inputs_without_huggingface_provenance() -> None:
    request = SubmissionRequest.model_validate(orrery_submission())

    assert request.provenance.huggingface is None


def test_submission_id_ignores_file_order() -> None:
    submission = calibration_submission()
    reordered = deepcopy(submission)
    reordered["files"].reverse()

    first = derive_submission_id(SubmissionRequest.model_validate(submission))
    second = derive_submission_id(SubmissionRequest.model_validate(reordered))

    assert first == second
    assert first.startswith("sub-")
    assert len(first) == len("sub-") + 32


def test_submission_id_differs_between_staging_and_release_of_same_bytes() -> None:
    staging = calibration_submission()
    release = deepcopy(staging)
    release["provenance"]["release"] = "microcosm-uk-2024-25-national"

    assert derive_submission_id(
        SubmissionRequest.model_validate(staging)
    ) != derive_submission_id(SubmissionRequest.model_validate(release))


def test_submission_id_hashes_the_documented_canonical_fields() -> None:
    request = SubmissionRequest.model_validate(calibration_submission())
    expected_document = {
        "contract_version": 1,
        "kind": "calibration-diagnostics",
        "licensed_data": True,
        "provenance": calibration_submission()["provenance"],
        "files": sorted(calibration_submission()["files"], key=lambda f: f["name"]),
    }
    expected_digest = hashlib.sha256(
        json.dumps(
            expected_document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()

    assert derive_submission_id(request) == "sub-" + expected_digest[:32]


def test_inventory_digest_hashes_files_sorted_by_name() -> None:
    request = SubmissionRequest.model_validate(calibration_submission())
    sorted_files = sorted(calibration_submission()["files"], key=lambda f: f["name"])

    assert (
        inventory_digest(request.files)
        == hashlib.sha256(canonical_json(sorted_files)).hexdigest()
    )


def test_canonical_json_sorts_keys_and_escapes_non_ascii() -> None:
    assert canonical_json({"b": 1, "a": "é"}) == b'{"a":"\\u00e9","b":1}'


def test_job_id_is_derived_from_submission_and_job_type() -> None:
    first = derive_job_id("sub-1", "calibration-diagnostics")

    assert first == derive_job_id("sub-1", "calibration-diagnostics")
    assert first != derive_job_id("sub-2", "calibration-diagnostics")
    assert first.startswith("job-")
    assert len(first) == len("job-") + 32


def test_intake_object_name_places_files_under_their_submission() -> None:
    assert (
        intake_object_name("sub-1", "calibration_diagnostics.json")
        == "submissions/sub-1/calibration_diagnostics.json"
    )


def test_receipt_digest_excludes_its_own_field() -> None:
    receipt = {"submission_id": "sub-1", "status": "succeeded"}

    assert receipt_digest({**receipt, "receipt_sha256": "ignored"}) == (
        hashlib.sha256(canonical_json(receipt)).hexdigest()
    )


def test_refuses_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SubmissionRequest.model_validate(calibration_submission(destination="x"))


def test_refuses_a_role_from_another_kind() -> None:
    submission = calibration_submission()
    submission["files"][1]["role"] = "schema"

    with pytest.raises(ValidationError, match="not a role of"):
        SubmissionRequest.model_validate(submission)


def test_requires_exactly_one_calibration_diagnostics_file() -> None:
    submission = calibration_submission()
    submission["files"] = submission["files"][1:]

    with pytest.raises(ValidationError, match="calibration_diagnostics"):
        SubmissionRequest.model_validate(submission)


def test_requires_one_schema_and_one_execution_overlay() -> None:
    submission = orrery_submission()
    submission["files"] = [
        file for file in submission["files"] if file["role"] != "execution_overlay"
    ]

    with pytest.raises(ValidationError, match="execution_overlay"):
        SubmissionRequest.model_validate(submission)


def test_refuses_duplicate_file_names() -> None:
    submission = calibration_submission()
    submission["files"][1]["name"] = "calibration_diagnostics.json"

    with pytest.raises(ValidationError, match="unique"):
        SubmissionRequest.model_validate(submission)


def test_refuses_a_file_over_its_role_limit() -> None:
    submission = orrery_submission()
    submission["files"][1]["size"] = 32 * 1024 * 1024 + 1

    with pytest.raises(ValidationError, match="limit"):
        SubmissionRequest.model_validate(submission)


def test_refuses_a_submission_over_the_total_limit() -> None:
    submission = orrery_submission()
    submission["files"].extend(
        {
            "name": f"evidence-{index}.json",
            "role": "evidence",
            "size": 2 * 1024 * 1024 * 1024,
            "sha256": "f" * 64,
        }
        for index in range(4)
    )

    with pytest.raises(ValidationError, match="8 GiB"):
        SubmissionRequest.model_validate(submission)


def test_refuses_more_than_the_file_count_limit() -> None:
    submission = orrery_submission()
    submission["files"].extend(
        {
            "name": f"upstream-{index:064x}.json",
            "role": "upstream",
            "size": 1,
            "sha256": "f" * 64,
        }
        for index in range(MAX_FILES_PER_SUBMISSION)
    )

    with pytest.raises(ValidationError):
        SubmissionRequest.model_validate(submission)


def test_requires_huggingface_provenance_for_calibration_diagnostics() -> None:
    submission = calibration_submission()
    submission["provenance"]["huggingface"] = None

    with pytest.raises(ValidationError, match="Hugging Face provenance"):
        SubmissionRequest.model_validate(submission)


@pytest.mark.parametrize("licensed_data", ["true", 1, None])
def test_licensed_data_must_be_a_boolean(licensed_data: object) -> None:
    with pytest.raises(ValidationError):
        SubmissionRequest.model_validate(
            calibration_submission(licensed_data=licensed_data)
        )


def test_licensed_data_has_no_default() -> None:
    submission = calibration_submission()
    del submission["licensed_data"]

    with pytest.raises(ValidationError):
        SubmissionRequest.model_validate(submission)


@pytest.mark.parametrize(
    "file_name", ["../calibration.json", "nested/file.json", ".hidden", ""]
)
def test_refuses_file_names_that_are_not_flat(file_name: str) -> None:
    submission = calibration_submission()
    submission["files"][1]["name"] = file_name

    with pytest.raises(ValidationError):
        SubmissionRequest.model_validate(submission)


@pytest.mark.parametrize("folder", ["staged/../releases", "/staged/run", "staged//run"])
def test_refuses_huggingface_folders_outside_the_repo(folder: str) -> None:
    submission = calibration_submission()
    submission["provenance"]["huggingface"]["folder"] = folder

    with pytest.raises(ValidationError):
        SubmissionRequest.model_validate(submission)


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64])
def test_refuses_digests_that_are_not_lowercase_sha256(digest: str) -> None:
    submission = calibration_submission()
    submission["files"][0]["sha256"] = digest

    with pytest.raises(ValidationError):
        SubmissionRequest.model_validate(submission)


@pytest.mark.parametrize("size", [0, -1, "12", 1.5, True])
def test_file_size_must_be_a_positive_integer(size: object) -> None:
    submission = calibration_submission()
    submission["files"][0]["size"] = size

    with pytest.raises(ValidationError):
        SubmissionRequest.model_validate(submission)


@pytest.mark.parametrize("contract_version", [2, True, 1.0, "1"])
def test_contract_version_must_be_the_integer_one(contract_version: object) -> None:
    with pytest.raises(ValidationError):
        SubmissionRequest.model_validate(
            calibration_submission(contract_version=contract_version)
        )
