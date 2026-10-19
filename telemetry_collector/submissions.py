"""The artifact submission contract: request models, limits and identifiers.

`docs/artifact-submissions.md` describes the contract; this module enforces it.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

from telemetry_collector.models import SAFE_IDENTIFIER

CONTRACT_VERSION = 1
MEBIBYTE = 1024 * 1024
GIBIBYTE = 1024 * MEBIBYTE
MAX_FILES_PER_SUBMISSION = 256
MAX_SUBMISSION_BYTES = 8 * GIBIBYTE
UPLOAD_URL_LIFETIME_SECONDS = 30 * 60
MAX_JOB_ATTEMPTS = 3

SubmissionKind = Literal["calibration-diagnostics", "orrery-inputs"]
FILE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
HUGGINGFACE_REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
GIT_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
# Relative path segments; the validator also refuses "." and ".." segments.
REPO_FOLDER_PATTERN = r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$"


@dataclass(frozen=True)
class RoleRule:
    """How many files of one role a submission may hold, and how large each is."""

    minimum_count: int
    maximum_count: int | None
    maximum_file_bytes: int


ROLE_RULES: dict[str, dict[str, RoleRule]] = {
    "calibration-diagnostics": {
        "calibration_diagnostics": RoleRule(1, 1, GIBIBYTE),
        "build_manifest": RoleRule(0, 1, 64 * MEBIBYTE),
        "release_manifest": RoleRule(0, 1, 64 * MEBIBYTE),
        "demographics": RoleRule(0, 1, 64 * MEBIBYTE),
    },
    "orrery-inputs": {
        "schema": RoleRule(1, 1, 64 * MEBIBYTE),
        "execution_overlay": RoleRule(1, 1, 32 * MEBIBYTE),
        "upstream": RoleRule(0, None, 64 * MEBIBYTE),
        "evidence": RoleRule(0, None, 2 * GIBIBYTE),
    },
}


class HuggingFaceLocation(BaseModel):
    """Where the submitted bytes live on Hugging Face."""

    model_config = ConfigDict(extra="forbid")

    repo: str = Field(pattern=HUGGINGFACE_REPO_PATTERN, max_length=200)
    commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    folder: str = Field(pattern=REPO_FOLDER_PATTERN, max_length=512)

    @model_validator(mode="after")
    def refuse_relative_segments(self) -> HuggingFaceLocation:
        if any(segment in {".", ".."} for segment in self.folder.split("/")):
            raise ValueError("folder must not contain '.' or '..' segments")
        return self


class SubmissionProvenance(BaseModel):
    """The build that produced a submission's files."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=SAFE_IDENTIFIER)
    build_id: str = Field(pattern=SAFE_IDENTIFIER)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    release: str | None = Field(pattern=SAFE_IDENTIFIER)
    huggingface: HuggingFaceLocation | None


class SubmittedFile(BaseModel):
    """One inventory entry: a file's name, role, exact size and SHA-256."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=FILE_NAME_PATTERN)
    role: str = Field(min_length=1, max_length=64)
    size: StrictInt = Field(ge=1)
    sha256: str = Field(pattern=SHA256_PATTERN)


class SubmissionRequest(BaseModel):
    """The body of ``POST /v1/submissions``."""

    model_config = ConfigDict(extra="forbid")

    contract_version: StrictInt = Field(ge=CONTRACT_VERSION, le=CONTRACT_VERSION)
    kind: SubmissionKind
    licensed_data: StrictBool
    provenance: SubmissionProvenance
    files: list[SubmittedFile] = Field(
        min_length=1, max_length=MAX_FILES_PER_SUBMISSION
    )

    @model_validator(mode="after")
    def enforce_kind_rules(self) -> SubmissionRequest:
        names = Counter(file.name for file in self.files)
        if any(count > 1 for count in names.values()):
            raise ValueError("file names must be unique within a submission")
        rules = ROLE_RULES[self.kind]
        for file in self.files:
            rule = rules.get(file.role)
            if rule is None:
                raise ValueError(f"{file.role!r} is not a role of {self.kind}")
            if file.size > rule.maximum_file_bytes:
                raise ValueError(
                    f"{file.name} exceeds the {file.role} limit of "
                    f"{rule.maximum_file_bytes} bytes"
                )
        role_counts = Counter(file.role for file in self.files)
        for role, rule in rules.items():
            count = role_counts[role]
            if count < rule.minimum_count or (
                rule.maximum_count is not None and count > rule.maximum_count
            ):
                expected = (
                    f"exactly {rule.minimum_count}"
                    if rule.minimum_count == rule.maximum_count
                    else f"at most {rule.maximum_count}"
                )
                raise ValueError(
                    f"{self.kind} needs {expected} {role} file(s), not {count}"
                )
        if sum(file.size for file in self.files) > MAX_SUBMISSION_BYTES:
            raise ValueError("a submission's files may total at most 8 GiB")
        if self.kind == "calibration-diagnostics" and (
            self.provenance.huggingface is None
        ):
            raise ValueError(
                "calibration-diagnostics submissions need Hugging Face provenance"
            )
        return self

    def sorted_files(self) -> list[dict[str, Any]]:
        """The inventory as plain values, sorted by file name."""

        return sorted(
            (file.model_dump(mode="json") for file in self.files),
            key=lambda file: file["name"],
        )


def canonical_json(value: Any) -> bytes:
    """UTF-8 JSON with sorted keys, no whitespace and escaped non-ASCII text."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def _sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _file_values(files: Iterable[SubmittedFile | Mapping[str, Any]]) -> list[dict]:
    values = [
        file.model_dump(mode="json")
        if isinstance(file, SubmittedFile)
        else {key: file[key] for key in ("name", "role", "size", "sha256")}
        for file in files
    ]
    return sorted(values, key=lambda file: file["name"])


def inventory_digest(files: Iterable[SubmittedFile | Mapping[str, Any]]) -> str:
    """SHA-256 of the inventory, sorted by file name."""

    return _sha256_hex(_file_values(files))


def derive_submission_id(request: SubmissionRequest) -> str:
    """The submission's identifier, the same for every identical request."""

    document = {
        "contract_version": request.contract_version,
        "kind": request.kind,
        "licensed_data": request.licensed_data,
        "provenance": request.provenance.model_dump(mode="json"),
        "files": request.sorted_files(),
    }
    return "sub-" + _sha256_hex(document)[:32]


def derive_job_id(submission_id: str, job_type: str) -> str:
    """The identifier of the job that processes a submission."""

    return (
        "job-"
        + _sha256_hex({"submission_id": submission_id, "job_type": job_type})[:32]
    )


def intake_object_name(submission_id: str, file_name: str) -> str:
    """Where a submitted file is stored in the intake bucket."""

    return f"submissions/{submission_id}/{file_name}"


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    """SHA-256 of a receipt, excluding its own ``receipt_sha256`` field."""

    return _sha256_hex(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
