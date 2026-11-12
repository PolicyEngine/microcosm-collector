"""The artifact submission contract: request models, limits and identifiers.

`docs/artifact-submissions.md` describes the contract; this module enforces it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

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
# Each processing round of a submission gets at most this many worker claims
# and this many Cloud Tasks; recovery fails a job that would need more.
MAX_JOB_ATTEMPTS = 3
MAX_JOB_DISPATCHES = 3
# Recovery dispatches a queued job again after this long without a claim,
# release or dispatch.
UNCLAIMED_JOB_IDLE_SECONDS = 30 * 60
# The ``processed_by`` of the default handler, which only checks the bytes.
INTAKE_ONLY_HANDLER = "intake-only"
#: The owner of the submissions the collector makes itself for past builds
#: (``reproject-past-builds``). No Hugging Face user has this ID.
COLLECTOR_OWNER_HF_ID = "microcosm-collector"

SubmissionKind = Literal["calibration-diagnostics", "orrery-inputs", "releases-record"]
SubmissionStatus = Literal[
    "awaiting_upload", "queued", "processing", "succeeded", "rejected", "failed"
]
JobStatus = Literal["queued", "running", "succeeded", "rejected", "failed"]
FinalStatus = Literal["succeeded", "rejected", "failed"]
# The one definition of each status set; the tables' check constraints and the
# endpoints use these.
SUBMISSION_STATUSES: tuple[SubmissionStatus, ...] = get_args(SubmissionStatus)
JOB_STATUSES: tuple[JobStatus, ...] = get_args(JobStatus)
FINAL_STATUSES: frozenset[str] = frozenset(get_args(FinalStatus))

FILE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
# The emitter interface's safe_identifier rule, for builds, releases and
# Hugging Face folder segments.
BUILD_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
BUILD_IDENTIFIER_PATTERN = rf"^{BUILD_IDENTIFIER}$"
HUGGINGFACE_REPO_PART = r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
HUGGINGFACE_REPO_PATTERN = rf"^{HUGGINGFACE_REPO_PART}/{HUGGINGFACE_REPO_PART}$"
GIT_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
HUGGINGFACE_FOLDER_PATTERN = rf"^{BUILD_IDENTIFIER}(/{BUILD_IDENTIFIER})*$"
MAX_HUGGINGFACE_FOLDER_CHARACTERS = 512
#: An ``upstream`` file's name: ``upstream-<its SHA-256>.json``, as Microcosm
#: names each producing run's file it copies (``save_graph_schema``). Intake
#: doesn't check it; the Orrery graph handler requires it, so the service can
#: take a served upstream file's SHA-256 from its name.
UPSTREAM_FILE_NAME_PATTERN = r"^upstream-([0-9a-f]{64})\.json$"


def upstream_file_sha256(name: str) -> str | None:
    """The SHA-256 an upstream file's name gives, or ``None`` for a name not
    of the form ``upstream-<64 lowercase hex characters>.json``."""

    named = re.fullmatch(UPSTREAM_FILE_NAME_PATTERN, name)
    return None if named is None else named.group(1)


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
    # A release repo's releases.json after a change made a build current; the
    # record's writer accepts at most 4 MiB.
    "releases-record": {
        "releases_record": RoleRule(1, 1, 4 * MEBIBYTE),
    },
}


class HuggingFaceLocation(BaseModel):
    """Where the submitted bytes live on Hugging Face."""

    model_config = ConfigDict(extra="forbid")

    repo: str = Field(pattern=HUGGINGFACE_REPO_PATTERN)
    # The commit that added the folder, never a later commit, branch or tag.
    commit: str = Field(pattern=GIT_COMMIT_PATTERN)
    # Each segment starts with a letter or digit, so "." and ".." cannot occur.
    folder: str = Field(
        pattern=HUGGINGFACE_FOLDER_PATTERN,
        max_length=MAX_HUGGINGFACE_FOLDER_CHARACTERS,
    )


class SubmissionProvenance(BaseModel):
    """The build that produced a submission's files.

    Every field is required; ``release`` and ``huggingface`` may be null but
    not left out, so the hashed form always holds all five keys.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=SAFE_IDENTIFIER)
    build_id: str = Field(pattern=BUILD_IDENTIFIER_PATTERN)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    release: str | None = Field(pattern=BUILD_IDENTIFIER_PATTERN)
    huggingface: HuggingFaceLocation | None


class SubmittedFile(BaseModel):
    """One inventory entry: a file's name, role, exact size and SHA-256.

    ``size`` is the number the emitter's file entries call ``bytes``.
    """

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
        if self.kind == "releases-record":
            self._enforce_releases_record_provenance()
        return self

    def _enforce_releases_record_provenance(self) -> None:
        # The record's repo and commit, and the build the change made current.
        provenance = self.provenance
        if provenance.huggingface is None:
            raise ValueError("releases-record submissions need Hugging Face provenance")
        if provenance.release is None:
            raise ValueError("releases-record submissions need a release")
        build_folder = f"releases/{provenance.release}/{provenance.build_id}"
        if provenance.huggingface.folder != build_folder:
            raise ValueError(
                "a releases-record submission's folder must be "
                "releases/<release>/<build_id>, the folder of the build made current"
            )

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


def files_digest(files: Iterable[SubmittedFile | Mapping[str, Any]]) -> str:
    """SHA-256 of the files, sorted by name: a submission's ``files_sha256``.

    It differs from the emitter's ``inventory_sha256``, which also covers its
    inventory's version and publication ID.
    """

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


def derive_job_id(submission_id: str, job_type: str, processing_round: int) -> str:
    """The identifier of one processing round's job for a submission.

    Round 1 is the job completion records; each reprocessing adds one, so a
    stored submission can be processed again under a new job.
    """

    document = {
        "submission_id": submission_id,
        "job_type": job_type,
        "processing_round": processing_round,
    }
    return "job-" + _sha256_hex(document)[:32]


def intake_object_prefix(submission_id: str) -> str:
    """The intake bucket prefix that holds every file of a submission."""

    return f"submissions/{submission_id}/"


def intake_object_name(submission_id: str, file_name: str) -> str:
    """Where a submitted file is stored in the intake bucket."""

    return intake_object_prefix(submission_id) + file_name


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    """SHA-256 of a receipt, excluding its own ``receipt_sha256`` field."""

    return _sha256_hex(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
