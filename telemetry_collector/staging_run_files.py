"""Copies of the staging run files the dashboard reads.

The ``reconcile-calibration-trees`` command keeps a private copy of these files
for each staging run it covers. The tree package's ``staging-run-files-command.ts``, run
as its own Bun process with the Blob token, stores them at
``staging-run-files/<country>/`` beside one index per country, which records
each run's files, what they say and the state of its folder they came from.

A run's folder is compared by a fingerprint of every file in it, by path and
git blob ID, both taken from the file listing the command already reads, so an
unchanged run costs no Hugging Face request.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from telemetry_collector.calibration_trees import (
    MANIFEST_READ_TIMEOUT_SECONDS,
    PACKAGE_DIRECTORY,
    PUBLICATION_TIMEOUT_SECONDS,
    CalibrationTreeCommandError,
    TreeCommands,
    shortened_message,
)
from telemetry_collector.huggingface_read import DatasetFile

COMMAND_SCRIPT: Final = PACKAGE_DIRECTORY / "src" / "staging-run-files-command.ts"
#: The files of a run folder the dashboard reads and no tree build stores:
#: the package's STAGING_RUN_FILE_NAMES.
COPIED_RUN_FILES: Final = (
    "run_manifest.json",
    "progress.json",
    "events.ndjson",
    "calibration_progress.json",
    "reform_validation.json",
    "artifacts/staged_dataset.json",
)
#: How many runs are stored between writes of the index, so that an execution
#: that stops keeps what it copied.
RUN_FILE_BATCH_SIZE: Final = 10
_RUN_FILE: Final = re.compile(r"runs/([A-Za-z0-9][A-Za-z0-9._-]*)/(.+)")


@dataclass(frozen=True)
class RunFolder:
    """The files listed under one ``runs/<id>/`` folder, by their path inside it."""

    run_id: str
    files: Mapping[str, DatasetFile]

    @property
    def copied_files(self) -> dict[str, DatasetFile]:
        return {
            name: self.files[name] for name in COPIED_RUN_FILES if name in self.files
        }

    @property
    def fingerprint(self) -> str:
        """A digest that changes whenever any file in the folder changes."""

        digest = hashlib.sha256()
        for name in sorted(self.files):
            digest.update(f"{name}\0{self.files[name].blob_id}\n".encode())
        return digest.hexdigest()


def run_folders(listed: Iterable[DatasetFile]) -> dict[str, RunFolder]:
    """Each run folder in a staging repo's file listing, by run ID."""

    files: defaultdict[str, dict[str, DatasetFile]] = defaultdict(dict)
    for dataset_file in listed:
        match = _RUN_FILE.fullmatch(dataset_file.path)
        if match:
            files[match[1]][match[2]] = dataset_file
    return {
        run_id: RunFolder(run_id, folder_files)
        for run_id, folder_files in files.items()
    }


#: The command's exit when the store holds no index for the tree asked about.
EXIT_NO_TREE: Final = 2


class StoredTreeMissingError(CalibrationTreeCommandError):
    """The store's manifest lists a tree whose index the store does not hold."""


def _answer(commands: TreeCommands, arguments: list[str], timeout: float) -> Any:
    completed = commands.run_command(
        [commands.bun, "run", str(COMMAND_SCRIPT), *arguments],
        commands.publication_environment,
        timeout,
    )
    if completed.returncode == EXIT_NO_TREE and arguments[0] == "tree-sources":
        raise StoredTreeMissingError(shortened_message(completed.stderr))
    if completed.returncode != 0:
        raise CalibrationTreeCommandError(
            f"The staging run files {arguments[0]} exited with "
            f"{completed.returncode}: {shortened_message(completed.stderr)}"
        )
    lines = completed.stdout.strip().splitlines()
    try:
        answer = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as error:
        raise CalibrationTreeCommandError(
            f"The staging run files {arguments[0]} printed no JSON answer."
        ) from error
    if not isinstance(answer, dict):
        raise CalibrationTreeCommandError(
            f"The staging run files {arguments[0]} printed no JSON answer."
        )
    return answer


def read_copied_runs(commands: TreeCommands, country: str) -> dict[str, dict[str, Any]]:
    """The records of the country's copied runs, by run ID."""

    runs = _answer(commands, ["read", country], MANIFEST_READ_TIMEOUT_SECONDS).get(
        "runs"
    )
    if not isinstance(runs, dict):
        raise CalibrationTreeCommandError("The staging run files index has no runs.")
    return runs


def read_tree_sources(
    commands: TreeCommands, country: str, build_artifact_id: str
) -> dict[str, Any]:
    """The source files a stored tree records, by the tree's role names.

    Raises ``StoredTreeMissingError`` when the store holds no index for it.
    """

    answer: dict[str, Any] = _answer(
        commands,
        ["tree-sources", country, build_artifact_id],
        MANIFEST_READ_TIMEOUT_SECONDS,
    )
    return answer


def publish(
    commands: TreeCommands, request: Mapping[str, Any], directory: Path
) -> dict[str, dict[str, int]]:
    """Store ``request``'s files and update the index: each run's file counts.

    The request is written under ``directory``, which holds its downloaded files.
    """

    request_path = directory / "publication.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    runs = _answer(
        commands, ["publish", str(request_path)], PUBLICATION_TIMEOUT_SECONDS
    ).get("runs")
    if not isinstance(runs, dict):
        raise CalibrationTreeCommandError(
            "The staging run files publish reported no runs."
        )
    return runs
