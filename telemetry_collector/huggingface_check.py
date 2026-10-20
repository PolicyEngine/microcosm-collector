"""The collector's daily Hugging Face check, run as a Cloud Run job.

Cloud Scheduler starts the job once a day. The token check requests every
dataset in ``HUGGINGFACE_DATASETS`` with the collector's read token and
reports each one that answers with anything but HTTP 200, including a
redirect or no answer at all.

Each problem is printed as one JSON log entry with severity ``ERROR``, which
Cloud Logging reads from a job's standard output, followed by one summary
entry. The job then exits 1, so its execution fails and Cloud Monitoring
alerts a person. A missing or unusable token setting exits 2 before any
request. No entry includes the token.

Run it with ``python -m telemetry_collector.huggingface_check``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any, Final

import httpx

from telemetry_collector.huggingface_read import (
    HUGGINGFACE_DATASETS,
    HuggingFaceDataset,
    HuggingFaceReadConfigurationError,
    HuggingFaceReader,
    HuggingFaceReadError,
)

TOKEN_CHECK: Final = "token_access"
CONFIGURATION_CHECK: Final = "configuration"
PROBLEMS_FOUND_EXIT_CODE: Final = 1
CONFIGURATION_EXIT_CODE: Final = 2


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


def run_checks(
    reader: HuggingFaceReader,
    datasets: Sequence[HuggingFaceDataset] = HUGGINGFACE_DATASETS,
) -> list[Finding]:
    """Every check, for every dataset; an empty list means all passed."""

    findings, _ = check_token_access(reader, datasets)
    return findings


def _write(output: IO[str], entry: Mapping[str, Any]) -> None:
    output.write(json.dumps(entry, sort_keys=False) + "\n")
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
    _write(
        log,
        {
            "severity": "ERROR" if findings else "INFO",
            "message": (
                f"Hugging Face check found {len(findings)} problems."
                if findings
                else "Hugging Face check passed."
            ),
            "checked_datasets": [dataset.name for dataset in datasets],
            "problems": len(findings),
        },
    )
    return PROBLEMS_FOUND_EXIT_CODE if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
