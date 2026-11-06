"""Validate Orrery graph documents with the pinned ``@axiom-foundation/orrery``.

``orrery-validator/validate-orrery-document.mjs`` runs under Node with the one
Orrery version its lockfile pins, and accepts exactly what microcosm-runs'
publication accepts for a graph file: strict UTF-8 JSON, Orrery's
``parseGraphDocument``, and microcosm-runs' ``checkSources`` (every ``url`` is
an ``https://`` URL without a user name or password, or names a file
published with the document). The image the worker job runs holds Node and
the installed validator at the same path relative to this package as the
repo does.

Run ``python -m telemetry_collector.orrery.validation DOCUMENT [NAME ...]`` to
validate a document by hand. It prints the validator's answer and exits 0
when the document is valid, 2 when it is refused and 1 when the validator
could not run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALIDATOR_DIRECTORY = Path(__file__).resolve().parents[2] / "orrery-validator"
VALIDATOR_SCRIPT = VALIDATOR_DIRECTORY / "validate-orrery-document.mjs"
# A 64 MiB document validates in seconds; this only stops a stalled process.
VALIDATION_TIMEOUT_SECONDS = 600
EXIT_VALID = 0
EXIT_VALIDATOR_ERROR = 1
EXIT_REFUSED = 2
MAX_ERROR_CHARACTERS = 2000


@dataclass(frozen=True)
class OrreryValidation:
    """A valid document, and the Orrery version that validated it."""

    orrery_version: str


class OrreryDocumentRefusedError(Exception):
    """The document is not one microcosm-runs would publish as a graph file."""

    def __init__(self, code: str, message: str, issues: list[dict[str, str]]) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.issues = issues


class OrreryValidatorError(RuntimeError):
    """The validator could not give an answer about the document."""


def _answer(stdout: str) -> dict[str, Any]:
    try:
        answer = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise OrreryValidatorError(
            "The Orrery validator printed no JSON answer."
        ) from error
    if not isinstance(answer, dict) or not isinstance(
        answer.get("orrery_version"), str
    ):
        raise OrreryValidatorError("The Orrery validator's answer has no version.")
    return answer


def validate_orrery_document(
    document: Path,
    published_file_names: Iterable[str],
    *,
    node: str = "node",
    validator: Path = VALIDATOR_SCRIPT,
    timeout_seconds: float = VALIDATION_TIMEOUT_SECONDS,
) -> OrreryValidation:
    """Validate ``document``, whose links may name ``published_file_names``.

    Raises ``OrreryDocumentRefusedError`` for a document the checks refuse and
    ``OrreryValidatorError`` when the validator could not run.
    """

    command = [node, str(validator), str(document), *sorted(published_file_names)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise OrreryValidatorError(
            f"The Orrery validator did not finish within {timeout_seconds} seconds."
        ) from error
    except OSError as error:
        raise OrreryValidatorError(
            f"The Orrery validator could not start: {error}"
        ) from error
    if completed.returncode == EXIT_VALID:
        answer = _answer(completed.stdout)
        if answer.get("valid") is not True:
            raise OrreryValidatorError("The Orrery validator's answer contradicts it.")
        return OrreryValidation(orrery_version=answer["orrery_version"])
    if completed.returncode == EXIT_REFUSED:
        answer = _answer(completed.stdout)
        if answer.get("valid") is not False or not isinstance(answer.get("code"), str):
            raise OrreryValidatorError("The Orrery validator's answer contradicts it.")
        raise OrreryDocumentRefusedError(
            answer["code"],
            str(answer.get("message", "")),
            list(answer.get("issues", [])),
        )
    raise OrreryValidatorError(
        f"The Orrery validator exited with {completed.returncode}: "
        f"{completed.stderr.strip()[:MAX_ERROR_CHARACTERS]}"
    )


def main(argv: list[str]) -> int:
    """Validate a document from the command line; return the exit code."""

    if not argv:
        print(
            "Usage: python -m telemetry_collector.orrery.validation "
            "DOCUMENT [PUBLISHED_FILE_NAME ...]",
            file=sys.stderr,
        )
        return EXIT_VALIDATOR_ERROR
    document, *names = argv
    try:
        result = validate_orrery_document(Path(document), names)
    except OrreryDocumentRefusedError as refusal:
        print(
            json.dumps(
                {
                    "valid": False,
                    "code": refusal.code,
                    "message": refusal.message,
                    "issues": refusal.issues,
                }
            )
        )
        return EXIT_REFUSED
    except OrreryValidatorError as error:
        print(str(error), file=sys.stderr)
        return EXIT_VALIDATOR_ERROR
    print(json.dumps({"valid": True, "orrery_version": result.orrery_version}))
    return EXIT_VALID


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
