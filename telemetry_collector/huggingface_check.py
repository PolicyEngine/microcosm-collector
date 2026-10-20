"""The collector's daily Hugging Face check, run as a Cloud Run job.

Cloud Scheduler starts the job once a day. It runs two checks:

1. **The token check** requests every dataset in ``HUGGINGFACE_DATASETS``
   with the collector's read token and reports each one that answers with
   anything but HTTP 200, including a redirect or no answer at all.
2. **The releases.json check**, for each dataset whose entry sets
   ``checks_releases_record``, confirms at the dataset's head commit that:

   - there is exactly one ``releases.json``, at the root, and no
     ``latest*.json`` pointer file anywhere;
   - the record passes the rules in the microcosm-emitter-interface package's
     ``microcosm_emitter_interface.releases`` module, the same code the
     emitter's release writer runs before every write: the record's own rules,
     and every listed build has its folder and a manifest naming it;
   - every file in each listed build's folder is in that build's manifest and
     still has the SHA-256 the manifest records. Files stored with LFS are
     compared by the SHA-256 Hugging Face records for their content, without
     a download; other files are downloaded and hashed.

   The rules that compare one write with the state before it (existing build
   entries change only ``withdrawn``; no file inside an existing build folder
   is added, changed or removed) need the earlier state, so only the release
   writer can apply them. Checking every file against its manifest finds a
   later change to any file except the manifest itself.

   The interface package is imported only when a dataset sets
   ``checks_releases_record``, and no dataset does yet: see the README's
   "Daily Hugging Face check" for what switching it on needs.

Each problem is printed as one JSON log entry with severity ``ERROR``, which
Cloud Logging reads from a job's standard output, followed by one summary
entry. The job then exits 1, so its execution fails and Cloud Monitoring
alerts a person. A missing or unusable token setting exits 2 before any
request. No entry includes the token.

Run it with ``python -m telemetry_collector.huggingface_check``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import IO, Any, Final

import httpx

from telemetry_collector.huggingface_read import (
    HUGGINGFACE_DATASETS,
    DatasetFile,
    HuggingFaceDataset,
    HuggingFaceReadConfigurationError,
    HuggingFaceReader,
    HuggingFaceReadError,
)

TOKEN_CHECK: Final = "token_access"
RELEASES_RECORD_CHECK: Final = "releases_record"
CONFIGURATION_CHECK: Final = "configuration"
RELEASE_RULES_MODULE: Final = "microcosm_emitter_interface.releases"
RELEASE_RULES_DISTRIBUTION: Final = "microcosm-emitter-interface"
PROBLEMS_FOUND_EXIT_CODE: Final = 1
CONFIGURATION_EXIT_CODE: Final = 2
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Finding:
    """One problem the check found, with a fixed ``code`` and no secrets."""

    dataset: str
    check: str
    code: str
    message: str

    def log_entry(self) -> dict[str, Any]:
        return {
            "severity": "ERROR",
            "message": self.message,
            "check": self.check,
            "dataset": self.dataset,
            "code": self.code,
        }


def _read_error_code(error: HuggingFaceReadError) -> str:
    if error.status_code is None:
        return "UNREACHABLE"
    if error.status_code == 200:
        return "UNUSABLE_RESPONSE"
    return f"HTTP_{error.status_code}"


# The token check --------------------------------------------------------------


def check_token_access(
    reader: HuggingFaceReader, datasets: Sequence[HuggingFaceDataset]
) -> tuple[list[Finding], dict[str, str]]:
    """Request each dataset once; return the problems and the head commits."""

    findings: list[Finding] = []
    head_commits: dict[str, str] = {}
    for dataset in datasets:
        try:
            head_commits[dataset.name] = reader.head_commit(dataset.name)
        except HuggingFaceReadError as error:
            findings.append(
                Finding(dataset.name, TOKEN_CHECK, _read_error_code(error), str(error))
            )
    return findings, head_commits


# The releases.json check ----------------------------------------------------------


class ReleaseRulesUnavailableError(RuntimeError):
    """The interface package that holds the releases.json rules is missing."""


def load_release_rules() -> ModuleType:
    """Import the releases.json rules from the interface package."""

    try:
        return importlib.import_module(RELEASE_RULES_MODULE)
    except ModuleNotFoundError as error:
        raise ReleaseRulesUnavailableError(
            f"The releases.json check needs the {RELEASE_RULES_DISTRIBUTION} "
            f"package, which provides {RELEASE_RULES_MODULE}, and it is not "
            "installed."
        ) from error


def manifest_file_digests(manifest: object) -> dict[str, str]:
    """Each file a build's ``release_manifest.json`` lists, with its SHA-256.

    The manifest lists its build's files as ``files``, each entry with the
    file's bare ``name`` and its ``sha256``: the fields of the interface
    package's ``ReleaseFile``. Microcosm's new manifest is not written yet
    and the interface package does not define this list; when it does, this
    function reads that definition instead.
    """

    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list):
        raise ValueError("The manifest has no file list.")
    digests: dict[str, str] = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("A file entry is not an object.")
        name = entry.get("name")
        sha256 = entry.get("sha256")
        if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
            raise ValueError("A file entry has no bare file name.")
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise ValueError(f"{name} has no lowercase hexadecimal SHA-256.")
        if name in digests:
            raise ValueError(f"{name} is listed twice.")
        digests[name] = sha256
    return digests


def check_releases_record(
    reader: HuggingFaceReader,
    dataset: str,
    revision: str,
    release_rules: ModuleType,
) -> list[Finding]:
    """Every releases.json rule for one dataset, read at ``revision``."""

    findings: list[Finding] = []

    def report(code: str, message: str) -> None:
        findings.append(Finding(dataset, RELEASES_RECORD_CHECK, code, message))

    try:
        _check_releases_record(reader, dataset, revision, release_rules, report)
    except HuggingFaceReadError as error:
        report(_read_error_code(error), str(error))
    return findings


def _check_releases_record(
    reader: HuggingFaceReader,
    dataset: str,
    revision: str,
    release_rules: ModuleType,
    report: Callable[[str, str], None],
) -> None:
    files = {listed.path: listed for listed in reader.list_files(dataset, revision)}
    record_path = release_rules.RELEASES_RECORD_PATH
    for path in sorted(files):
        if path != record_path and path.rsplit("/", 1)[-1] == record_path:
            report(
                "EXTRA_RECORD",
                f"{path} is a second {record_path}; only the one at the root "
                "may exist.",
            )
        if release_rules.is_pointer_file(path):
            report(
                "POINTER_FILE_PRESENT",
                f"{path} is a latest*.json pointer file; only {record_path} "
                "may say which build is current.",
            )
    if record_path not in files:
        report("RECORD_MISSING", f"{dataset} has no {record_path} at its root.")
        return

    manifests: dict[str, bytes] = {}

    def read_record_or_manifest(path: str) -> bytes:
        if path not in manifests:
            manifests[path] = reader.read_file(dataset, path, revision)
        return manifests[path]

    try:
        record = release_rules.ReleasesRecord.parse(
            read_record_or_manifest(record_path)
        )
    except release_rules.ReleaseRecordError as error:
        for violation in error.violations:
            report(violation.code, violation.message)
        return
    for violation in release_rules.check_repository(
        record, list(files), read_record_or_manifest
    ):
        report(violation.code, violation.message)
    for release, entry in sorted(record.releases.items()):
        for build in sorted(entry.builds):
            _check_build_files(
                reader,
                dataset,
                revision,
                files,
                release_rules.build_folder(release, build),
                release_rules.RELEASE_MANIFEST_NAME,
                read_record_or_manifest,
                report,
            )


def _check_build_files(
    reader: HuggingFaceReader,
    dataset: str,
    revision: str,
    files: Mapping[str, DatasetFile],
    folder: str,
    manifest_name: str,
    read_manifest: Callable[[str], bytes],
    report: Callable[[str, str], None],
) -> None:
    manifest_path = f"{folder}/{manifest_name}"
    if manifest_path not in files:
        # The interface's folder and manifest rule already reported this.
        return
    try:
        digests = manifest_file_digests(json.loads(read_manifest(manifest_path)))
    except ValueError as error:
        report(
            "MANIFEST_FILES_UNREADABLE",
            f"{manifest_path} does not list its build's files with their "
            f"SHA-256: {error}",
        )
        return
    for name, expected_sha256 in sorted(digests.items()):
        if name == manifest_name:
            continue
        path = f"{folder}/{name}"
        listed = files.get(path)
        if listed is None:
            report("FILE_MISSING", f"{path} is in {manifest_path} but not published.")
            continue
        published_sha256 = (
            listed.lfs_sha256
            or hashlib.sha256(reader.read_file(dataset, path, revision)).hexdigest()
        )
        if published_sha256 != expected_sha256:
            report(
                "SHA256_MISMATCH",
                f"{path} has SHA-256 {published_sha256}, but {manifest_path} "
                f"records {expected_sha256}.",
            )
    for path in sorted(files):
        name = path.removeprefix(f"{folder}/")
        if (
            path.startswith(f"{folder}/")
            and name != manifest_name
            and name not in digests
        ):
            report("FILE_NOT_IN_MANIFEST", f"{path} is not listed in {manifest_path}.")


# Running the checks ---------------------------------------------------------------


def run_checks(
    reader: HuggingFaceReader,
    datasets: Sequence[HuggingFaceDataset] = HUGGINGFACE_DATASETS,
    *,
    release_rules_loader: Callable[[], ModuleType] = load_release_rules,
) -> list[Finding]:
    """Every check, for every dataset; an empty list means all passed."""

    findings, head_commits = check_token_access(reader, datasets)
    record_datasets = [
        dataset for dataset in datasets if dataset.checks_releases_record
    ]
    if not record_datasets:
        return findings
    try:
        release_rules = release_rules_loader()
    except ReleaseRulesUnavailableError as error:
        findings.extend(
            Finding(
                dataset.name,
                RELEASES_RECORD_CHECK,
                "RELEASE_RULES_UNAVAILABLE",
                str(error),
            )
            for dataset in record_datasets
        )
        return findings
    for dataset in record_datasets:
        # The token check has already reported a dataset that did not answer.
        if dataset.name in head_commits:
            findings.extend(
                check_releases_record(
                    reader, dataset.name, head_commits[dataset.name], release_rules
                )
            )
    return findings


def _write(output: IO[str], entry: Mapping[str, Any]) -> None:
    output.write(json.dumps(entry) + "\n")
    output.flush()


def main(
    environment: Mapping[str, str] | None = None,
    *,
    client: httpx.Client | None = None,
    output: IO[str] | None = None,
) -> int:
    """Run every check and log the result; non-zero when anything failed."""

    log = output or sys.stdout
    datasets = HUGGINGFACE_DATASETS
    try:
        reader = HuggingFaceReader.from_environment(environment, client=client)
    except HuggingFaceReadConfigurationError as error:
        _write(
            log,
            {"severity": "ERROR", "message": str(error), "check": CONFIGURATION_CHECK},
        )
        return CONFIGURATION_EXIT_CODE
    with reader:
        findings = run_checks(reader, datasets)
    for finding in findings:
        _write(log, finding.log_entry())
    record_datasets = [
        dataset.name for dataset in datasets if dataset.checks_releases_record
    ]
    message = (
        f"Hugging Face check found {len(findings)} problems."
        if findings
        else "Hugging Face check passed."
    )
    if not record_datasets:
        message += " The releases.json check is switched off for every dataset."
    _write(
        log,
        {
            "severity": "ERROR" if findings else "INFO",
            "message": message,
            "checked_datasets": [dataset.name for dataset in datasets],
            "releases_record_checked": record_datasets,
            "problems": len(findings),
        },
    )
    return PROBLEMS_FOUND_EXIT_CODE if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
