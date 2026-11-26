"""Storing the original files a build the calibration tree store holds lacks.

The backfill's copy step takes every build from the store's own manifest,
finds its files on Hugging Face at its stored commit with the backfill's own
rules, and stores a copy of each one the store lacks under the stored build
ID, through the tree package's ``calibration-tree-source-copies.ts``. A copy of
one of the four files a tree is built from is stored only when the stored
tree's index records that file with the same SHA-256. These tests stand in for
Hugging Face and the tree package's Bun processes, so nothing reaches the
network or runs Bun.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from telemetry_collector.calibration_trees import tree_commands
from telemetry_collector.huggingface_read import (
    HuggingFaceReader,
    HuggingFaceReadSettings,
)
from tests.huggingface_fakes import FakeHuggingFace
from tests.test_calibration_tree_backfill import (
    BLOB_TOKEN,
    BUILD_MANIFEST,
    BUNDLE_COMMIT,
    DEMOGRAPHICS,
    DIAGNOSTICS,
    MINUTE,
    PARENT_BUILD_MANIFEST,
    READ_TOKEN,
    REFORM_VALIDATION,
    RELEASE_COMMIT,
    RELEASE_MANIFEST,
    RUN_ID,
    STAGING_COMMIT,
    UK_RELEASES,
    US_RELEASES,
    US_STAGING,
    US_TAG,
    US_VERSION_2_RESULT_FILES,
    ManualClock,
    StandInBun,
    backfill_module,
    hugging_face_with,
    release_entry,
    release_folder,
    schema_1_run,
    sha256,
    staging_entry,
    store_manifest,
    us_version_2_run,
)

SOURCE_COPIES_MODULE = "telemetry_collector.calibration_tree_source_copies"
COPY_COMMAND = "calibration-tree-source-copies.ts"
MANIFEST_READ = "calibration-tree-manifest-read.ts"
UK_TAG = "microcosm-uk-2024-25-national"
#: The name each original file's copy is stored under, by the tree package's
#: name for the file.
STORED_NAMES = {
    "calibrationDiagnostics": "calibration_diagnostics.json",
    "buildManifest": "build_manifest.json",
    "releaseManifest": "release_manifest.json",
    "demographics": "demographics.json",
    "parentBuildManifest": "parent_build_manifest.json",
    "reformValidation": "reform_validation.json",
}
RELEASE_CONTENTS = {
    "calibrationDiagnostics": DIAGNOSTICS,
    "buildManifest": BUILD_MANIFEST,
    "releaseManifest": RELEASE_MANIFEST,
    "demographics": DEMOGRAPHICS,
    "parentBuildManifest": PARENT_BUILD_MANIFEST,
    "reformValidation": REFORM_VALIDATION,
}
TREE_FILES = (
    "calibration_diagnostics.json",
    "build_manifest.json",
    "release_manifest.json",
    "demographics.json",
)
COPIED_FILES = ("parent_build_manifest.json", "reform_validation.json")


def copies_module() -> ModuleType:
    if importlib.util.find_spec(SOURCE_COPIES_MODULE) is None:
        pytest.fail(f"{SOURCE_COPIES_MODULE} does not exist yet.")
    return importlib.import_module(SOURCE_COPIES_MODULE)


def recorded(
    folder: str, contents: Mapping[str, bytes], files: Sequence[str]
) -> dict[str, Any]:
    """A stored tree's record of the files it was built from."""

    return {
        file: {
            "path": f"{folder}/{STORED_NAMES[file]}",
            "sha256": sha256(contents[file]),
        }
        for file in files
    }


#: A release tree built from all four of its folder's tree files.
RELEASE_RECORD = recorded(
    f"releases/{US_TAG}",
    RELEASE_CONTENTS,
    ["calibrationDiagnostics", "buildManifest", "releaseManifest", "demographics"],
)


@dataclass
class SourceCopyBun(StandInBun):
    """Also stands in for the package's source copies command.

    ``copies`` holds the store's copies by country and build ID, each by the
    name it is stored under. ``list`` answers with them, leaving out those in
    ``unlisted``, as if another writer stored them after the listing. ``store``
    keeps a copy already stored, as the command does, and names it when its
    bytes differ.
    """

    copies: dict[tuple[str, str], dict[str, bytes]] = field(default_factory=dict)
    unlisted: set[tuple[str, str, str]] = field(default_factory=set)
    #: Files the store command refuses, by the package's name.
    refused: set[str] = field(default_factory=set)
    #: Builds whose tree index the store does not hold.
    without_index: set[str] = field(default_factory=set)
    copy_exits: dict[str, int] = field(default_factory=dict)
    manifest_read_exit: int = 0
    seconds_per_store: float = 0.0

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        script = Path(argv[2]).name
        if script == MANIFEST_READ and self.manifest_read_exit != 0:
            return subprocess.CompletedProcess(
                argv, self.manifest_read_exit, "", "The manifest read failed."
            )
        if script == COPY_COMMAND:
            return self._copies(argv, env)
        return super().__call__(argv, env, timeout)

    def _copies(
        self, argv: Sequence[str], env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        action = argv[3]
        call: dict[str, Any] = {
            "step": f"copies {action}",
            "argv": list(argv),
            "env": dict(env),
        }
        self.calls.append(call)
        self.events.append(f"copies {action}")
        exit_code = self.copy_exits.get(action, 0)
        if exit_code != 0:
            return subprocess.CompletedProcess(
                argv, exit_code, "", f"The copies {action} failed."
            )
        if action == "sources":
            build = argv[5]
            if build in self.without_index:
                return subprocess.CompletedProcess(
                    argv, 2, "", f"The store holds no tree for build {build}."
                )
            answer = self.tree_sources.get(
                build,
                {
                    "calibrationDiagnostics": {"sha256": sha256(DIAGNOSTICS)},
                    "buildManifest": {"sha256": sha256(BUILD_MANIFEST)},
                    "releaseManifest": {"sha256": sha256(RELEASE_MANIFEST)},
                },
            )
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"sources": answer}), ""
            )
        if action == "list":
            country = argv[4]
            builds = {
                build: sorted(
                    name
                    for name in names
                    if (copy_country, build, name) not in self.unlisted
                )
                for (copy_country, build), names in self.copies.items()
                if copy_country == country
            }
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"builds": builds}), ""
            )
        assert action == "store"
        request = json.loads(Path(argv[4]).read_text())
        call["request"] = request
        call["contents"] = {
            file["file"]: Path(file["path"]).read_bytes() for file in request["files"]
        }
        if self.clock is not None:
            self.clock.now += self.seconds_per_store
        stored = self.copies.setdefault(
            (request["country"], request["buildArtifactId"]), {}
        )
        answer: dict[str, list[Any]] = {
            "uploaded": [],
            "kept": [],
            "conflicts": [],
            "refused": [],
        }
        for file in request["files"]:
            name = STORED_NAMES[file["file"]]
            content = call["contents"][file["file"]]
            if file["file"] in self.refused:
                answer["refused"].append(
                    {"file": file["file"], "reason": "Another version."}
                )
            elif name in stored:
                answer["kept" if stored[name] == content else "conflicts"].append(
                    file["file"]
                )
            else:
                stored[name] = content
                answer["uploaded"].append(file["file"])
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer), "")


@dataclass
class Copies:
    hugging_face: FakeHuggingFace
    bun: SourceCopyBun = field(default_factory=SourceCopyBun)
    clock: ManualClock = field(default_factory=ManualClock)

    def run(self, **options: Any) -> list[Any]:
        module = copies_module()

        @contextmanager
        def recorded_lock() -> Iterator[None]:
            self.bun.events.append("lock taken")
            yield
            self.bun.events.append("lock released")

        self.bun.clock = self.clock
        commands = tree_commands(
            blob_token=BLOB_TOKEN,
            manifest_lock=recorded_lock,
            bun="/usr/local/bin/bun",
            run_command=self.bun,
        )
        reader = HuggingFaceReader(
            HuggingFaceReadSettings(token=READ_TOKEN),
            client=self.hugging_face.client(),
        )
        options.setdefault("countries", ["us"])
        return list(
            module.store_missing_source_files(
                reader=reader, commands=commands, clock=self.clock, **options
            )
        )


def full_release_folder(directory: str = US_TAG) -> dict[str, bytes]:
    folder = f"releases/{directory}"
    return release_folder(directory) | {
        f"{folder}/parent_build_manifest.json": PARENT_BUILD_MANIFEST,
        f"{folder}/reform_validation.json": REFORM_VALIDATION,
    }


def release_copies(
    files: Mapping[str, bytes] | None = None,
    *,
    stored: Mapping[str, bytes] | None = None,
    tree_record: Mapping[str, Any] = RELEASE_RECORD,
) -> tuple[Copies, dict[str, Any]]:
    """A store holding one US release build, whose folder ``files`` holds."""

    entry = release_entry(US_TAG)
    copies = Copies(
        hugging_face=hugging_face_with(
            {(US_RELEASES, RELEASE_COMMIT): files or full_release_folder()}
        )
    )
    copies.bun.store = store_manifest("us", [entry])
    copies.bun.tree_sources[entry["buildArtifactId"]] = dict(tree_record)
    if stored is not None:
        copies.bun.copies[("us", entry["buildArtifactId"])] = dict(stored)
    return copies, entry


def stored_copies(names: Sequence[str]) -> dict[str, bytes]:
    by_name = {
        STORED_NAMES[file]: content for file, content in RELEASE_CONTENTS.items()
    }
    return {name: by_name[name] for name in names}


# What is stored -----------------------------------------------------------------


def test_a_stored_release_build_without_copies_gets_every_original_file() -> None:
    copies, entry = release_copies()

    [outcome] = copies.run()

    assert outcome.outcome == "copied"
    assert (outcome.country, outcome.kind, outcome.source_id) == (
        "us",
        "release",
        US_TAG,
    )
    assert outcome.build_artifact_id == entry["buildArtifactId"]
    assert (outcome.hf_repo, outcome.hf_commit) == (US_RELEASES, RELEASE_COMMIT)
    assert outcome.files == {name: "copied" for name in STORED_NAMES.values()}
    assert outcome.incomplete is False
    [store] = copies.bun.steps("copies store")
    assert store["request"]["country"] == "us"
    assert store["request"]["buildArtifactId"] == entry["buildArtifactId"]
    assert store["contents"] == RELEASE_CONTENTS
    for file in store["request"]["files"]:
        assert file["sha256"] == sha256(store["contents"][file["file"]])
    assert copies.bun.copies[("us", entry["buildArtifactId"])] == stored_copies(
        list(STORED_NAMES.values())
    )


def test_a_build_with_only_its_other_copies_gets_the_two_copied_files() -> None:
    copies, entry = release_copies(stored=stored_copies(TREE_FILES))

    [outcome] = copies.run()

    assert outcome.outcome == "copied"
    assert outcome.files == {
        **{name: "stored" for name in TREE_FILES},
        **{name: "copied" for name in COPIED_FILES},
    }
    [store] = copies.bun.steps("copies store")
    assert store["contents"] == {
        "parentBuildManifest": PARENT_BUILD_MANIFEST,
        "reformValidation": REFORM_VALIDATION,
    }
    # The tree's index is read for any missing copy, so that a build whose tree
    # is not stored is found before anything is downloaded.
    assert len(copies.bun.steps("copies sources")) == 1
    assert sorted(copies.hugging_face.downloaded_paths()) == [
        f"releases/{US_TAG}/{name}" for name in COPIED_FILES
    ]


def test_a_build_with_every_copy_is_complete_and_downloads_nothing() -> None:
    copies, _ = release_copies(stored=stored_copies(list(STORED_NAMES.values())))

    [outcome] = copies.run()

    assert outcome.outcome == "complete"
    assert outcome.files == {name: "stored" for name in STORED_NAMES.values()}
    assert copies.hugging_face.downloaded_paths() == []
    assert copies.bun.steps("copies store") == []
    assert copies.bun.steps("copies sources") == []


def test_a_tree_file_that_differs_from_the_tree_s_record_is_not_stored() -> None:
    record = {
        **RELEASE_RECORD,
        "calibrationDiagnostics": {
            "path": f"releases/{US_TAG}/calibration_diagnostics.json",
            "sha256": sha256(b'{"schema_version": 8, "targets": [1]}'),
        },
    }
    copies, entry = release_copies(tree_record=record)

    [outcome] = copies.run()

    assert outcome.outcome == "copied"
    assert outcome.files["calibration_diagnostics.json"] == "differs"
    assert outcome.files["build_manifest.json"] == "copied"
    [store] = copies.bun.steps("copies store")
    assert "calibrationDiagnostics" not in store["contents"]
    assert (
        "calibration_diagnostics.json"
        not in copies.bun.copies[("us", entry["buildArtifactId"])]
    )


def test_a_build_whose_only_missing_file_differs_is_not_copied() -> None:
    record = {
        **RELEASE_RECORD,
        "buildManifest": {
            "path": f"releases/{US_TAG}/build_manifest.json",
            "sha256": sha256(b'{"build": "another build"}'),
        },
    }
    stored = stored_copies(
        [name for name in STORED_NAMES.values() if name != "build_manifest.json"]
    )
    copies, _ = release_copies(stored=stored, tree_record=record)

    [outcome] = copies.run()

    assert outcome.outcome == "not_copied"
    assert outcome.files["build_manifest.json"] == "differs"
    assert outcome.incomplete is False
    assert copies.bun.steps("copies store") == []


#: The worker records a tree file's SHA-256 over its UTF-8 text, with a leading
#: byte-order mark dropped, so this file's record is DIAGNOSTICS's digest.
DIAGNOSTICS_WITH_MARK = b"\xef\xbb\xbf" + DIAGNOSTICS


def release_copies_with_marked_diagnostics() -> tuple[Copies, dict[str, Any]]:
    files = full_release_folder() | {
        f"releases/{US_TAG}/calibration_diagnostics.json": DIAGNOSTICS_WITH_MARK
    }
    stored = stored_copies(
        [
            name
            for name in STORED_NAMES.values()
            if name != "calibration_diagnostics.json"
        ]
    )
    return release_copies(files, stored=stored)


def test_a_tree_file_recorded_by_its_decoded_text_is_copied_as_its_bytes() -> None:
    copies, _ = release_copies_with_marked_diagnostics()

    [outcome] = copies.run()

    assert outcome.outcome == "copied"
    assert outcome.files["calibration_diagnostics.json"] == "copied"
    [store] = copies.bun.steps("copies store")
    assert store["contents"] == {"calibrationDiagnostics": DIAGNOSTICS_WITH_MARK}


def test_a_marked_tree_file_stored_with_lfs_is_copied_by_a_real_run() -> None:
    copies, _ = release_copies_with_marked_diagnostics()
    diagnostics = f"releases/{US_TAG}/calibration_diagnostics.json"
    copies.hugging_face.datasets[US_RELEASES].lfs_paths.add(diagnostics)

    [outcome] = copies.run()

    assert outcome.files["calibration_diagnostics.json"] == "copied"
    [store] = copies.bun.steps("copies store")
    assert store["contents"] == {"calibrationDiagnostics": DIAGNOSTICS_WITH_MARK}


def test_a_dry_run_compares_a_file_by_its_decoded_text_too() -> None:
    copies, _ = release_copies_with_marked_diagnostics()

    [outcome] = copies.run(dry_run=True)

    assert outcome.outcome == "would_copy"
    assert outcome.files["calibration_diagnostics.json"] == "would_copy"


def test_a_tree_file_the_tree_was_not_built_from_is_left_out() -> None:
    without_demographics = {
        file: record
        for file, record in RELEASE_RECORD.items()
        if file != "demographics"
    }
    copies, _ = release_copies(tree_record=without_demographics)

    [outcome] = copies.run()

    assert outcome.outcome == "copied"
    assert outcome.files["demographics.json"] == "not_in_tree"
    [store] = copies.bun.steps("copies store")
    assert "demographics" not in store["contents"]
    assert f"releases/{US_TAG}/demographics.json" not in (
        copies.hugging_face.downloaded_paths()
    )


def test_a_build_lacking_only_files_its_tree_was_not_built_from_is_complete() -> None:
    without_demographics = {
        file: record
        for file, record in RELEASE_RECORD.items()
        if file != "demographics"
    }
    stored = stored_copies(
        [name for name in STORED_NAMES.values() if name != "demographics.json"]
    )
    copies, _ = release_copies(stored=stored, tree_record=without_demographics)

    [outcome] = copies.run()

    assert outcome.outcome == "complete"
    assert outcome.files["demographics.json"] == "not_in_tree"
    assert copies.bun.steps("copies store") == []


def test_a_copy_another_writer_stored_meanwhile_is_kept_and_named_if_it_differs() -> (
    None
):
    copies, entry = release_copies(stored=stored_copies(TREE_FILES))
    build = entry["buildArtifactId"]
    copies.bun.copies[("us", build)] |= {
        "parent_build_manifest.json": b'{"build_id": "another parent"}',
        "reform_validation.json": REFORM_VALIDATION,
    }
    copies.bun.unlisted |= {
        ("us", build, "parent_build_manifest.json"),
        ("us", build, "reform_validation.json"),
    }

    [outcome] = copies.run()

    # Every file now has a copy, so nothing more is to be done for it.
    assert outcome.outcome == "complete"
    assert outcome.files["parent_build_manifest.json"] == "conflict"
    assert outcome.files["reform_validation.json"] == "kept"
    assert copies.bun.copies[("us", build)]["parent_build_manifest.json"] == (
        b'{"build_id": "another parent"}'
    )


def test_a_stored_staging_run_s_files_follow_the_staging_readers_rules() -> None:
    entry = staging_entry(RUN_ID)
    run_folder = schema_1_run(RUN_ID) | {
        f"runs/{RUN_ID}/reform_validation.json": REFORM_VALIDATION
    }
    copies = Copies(
        hugging_face=hugging_face_with({(US_STAGING, STAGING_COMMIT): run_folder})
    )
    copies.bun.store = store_manifest("us", [entry])

    [outcome] = copies.run()

    assert outcome.outcome == "copied"
    assert (outcome.kind, outcome.source_id) == ("staging", RUN_ID)
    assert outcome.files == {
        "calibration_diagnostics.json": "copied",
        "build_manifest.json": "copied",
        "release_manifest.json": "copied",
        "reform_validation.json": "copied",
    }
    [store] = copies.bun.steps("copies store")
    assert store["contents"] == {
        "calibrationDiagnostics": DIAGNOSTICS,
        "buildManifest": BUILD_MANIFEST,
        "releaseManifest": RELEASE_MANIFEST,
        "reformValidation": REFORM_VALIDATION,
    }


def test_a_stored_version_2_run_s_reform_validation_that_differs_from_its_declaration_is_not_copied() -> (
    None
):
    entry = staging_entry(RUN_ID)
    run_folder = us_version_2_run(
        RUN_ID,
        declared={
            **US_VERSION_2_RESULT_FILES,
            "reform_validation": b'{"reforms": ["another"]}',
        },
    )
    copies = Copies(
        hugging_face=hugging_face_with({(US_STAGING, STAGING_COMMIT): run_folder})
    )
    copies.bun.store = store_manifest("us", [entry])

    [outcome] = copies.run()

    assert outcome.files["reform_validation.json"] == "differs"
    assert outcome.files["build_manifest.json"] == "copied"
    stored = [step["contents"] for step in copies.bun.steps("copies store")]
    assert all("reformValidation" not in contents for contents in stored)


def test_a_stored_staged_bundle_gets_a_copy_of_its_diagnostics() -> None:
    entry = staging_entry(RUN_ID, repo=US_RELEASES, commit=BUNDLE_COMMIT)
    copies = Copies(
        hugging_face=hugging_face_with(
            {
                (US_RELEASES, BUNDLE_COMMIT): {
                    f"staged/{RUN_ID}/calibration_diagnostics.json": DIAGNOSTICS
                }
            }
        )
    )
    copies.bun.store = store_manifest("us", [entry])
    copies.bun.tree_sources[entry["buildArtifactId"]] = recorded(
        f"staged/{RUN_ID}", RELEASE_CONTENTS, ["calibrationDiagnostics"]
    )

    [outcome] = copies.run()

    assert outcome.outcome == "copied"
    assert outcome.files == {"calibration_diagnostics.json": "copied"}


def test_a_tree_file_over_the_size_limit_is_too_large_and_never_downloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backfill_module(), "MAX_SOURCE_FILE_BYTES", len(DIAGNOSTICS) - 1
    )
    stored = stored_copies(
        [
            name
            for name in STORED_NAMES.values()
            if name != "calibration_diagnostics.json"
        ]
    )
    copies, _ = release_copies(stored=stored)

    [outcome] = copies.run()

    assert outcome.outcome == "not_copied"
    assert outcome.files["calibration_diagnostics.json"] == "too_large"
    assert copies.hugging_face.downloaded_paths() == []


# Which builds -------------------------------------------------------------------


def test_builds_come_from_the_store_s_own_manifest_at_their_stored_commits() -> None:
    older_commit = "7" * 40
    entry = release_entry(US_TAG, commit=older_commit)
    copies = Copies(
        hugging_face=hugging_face_with(
            {(US_RELEASES, older_commit): full_release_folder()}
        )
    )
    copies.bun.store = store_manifest("us", [entry])
    copies.bun.tree_sources[entry["buildArtifactId"]] = dict(RELEASE_RECORD)

    [outcome] = copies.run()

    assert outcome.outcome == "copied"
    assert outcome.hf_commit == older_commit
    resolved = [
        request.url.path
        for request in copies.hugging_face.requests
        if "/resolve/" in request.url.path
    ]
    assert resolved
    assert all(f"/resolve/{older_commit}/" in path for path in resolved)


def test_kinds_and_countries_limit_the_builds() -> None:
    release = release_entry(US_TAG)
    staging = staging_entry(RUN_ID)
    uk_release = release_entry(UK_TAG, country="uk")
    copies = Copies(
        hugging_face=hugging_face_with(
            {
                (US_RELEASES, RELEASE_COMMIT): full_release_folder(),
                (US_STAGING, STAGING_COMMIT): schema_1_run(RUN_ID),
                (UK_RELEASES, RELEASE_COMMIT): full_release_folder(UK_TAG),
            }
        )
    )
    copies.bun.store = store_manifest("us", [release, staging])
    copies.bun.store["countries"]["uk"] = {
        "latestReleaseBuildArtifactId": None,
        "builds": [uk_release],
    }

    release_only = copies.run(countries=["us"], kinds=["release"])
    uk_only = copies.run(countries=["uk"], dry_run=True)

    assert [(outcome.kind, outcome.source_id) for outcome in release_only] == [
        ("release", US_TAG)
    ]
    assert [(outcome.country, outcome.source_id) for outcome in uk_only] == [
        ("uk", UK_TAG)
    ]


def test_a_stored_entry_that_fails_the_checks_is_invalid_and_nothing_is_read() -> None:
    entry = release_entry(US_TAG, repo="someone/else")
    copies = Copies(hugging_face=hugging_face_with({}))
    copies.bun.store = store_manifest("us", [entry])

    [outcome] = copies.run()

    assert outcome.outcome == "invalid"
    assert outcome.reason
    assert outcome.incomplete is False
    assert copies.hugging_face.requests == []


def test_a_build_whose_files_are_not_at_its_commit_is_not_found() -> None:
    files = {
        path: content
        for path, content in full_release_folder().items()
        if not path.endswith("calibration_diagnostics.json")
    }
    copies, _ = release_copies(files)

    [outcome] = copies.run()

    assert outcome.outcome == "not_found"
    assert outcome.incomplete is False
    assert copies.bun.steps("copies store") == []


def test_a_stored_commit_hugging_face_no_longer_holds_is_not_found() -> None:
    entry = release_entry(US_TAG, commit="8" * 40)
    copies = Copies(hugging_face=hugging_face_with({}))
    copies.bun.store = store_manifest("us", [entry])

    [outcome] = copies.run()

    assert outcome.outcome == "not_found"
    assert outcome.incomplete is False


# Dry runs -----------------------------------------------------------------------


def test_a_dry_run_stores_nothing_and_says_what_a_real_run_would_copy() -> None:
    copies, entry = release_copies(stored=stored_copies(["build_manifest.json"]))
    diagnostics = f"releases/{US_TAG}/calibration_diagnostics.json"
    copies.hugging_face.datasets[US_RELEASES].lfs_paths.add(diagnostics)

    [outcome] = copies.run(dry_run=True)

    assert outcome.outcome == "would_copy"
    assert outcome.files == {
        **{name: "would_copy" for name in STORED_NAMES.values()},
        "build_manifest.json": "stored",
    }
    assert copies.bun.steps("copies store") == []
    assert copies.bun.copies[("us", entry["buildArtifactId"])] == stored_copies(
        ["build_manifest.json"]
    )
    # Diagnostics stored with LFS are checked by their listed SHA-256.
    assert diagnostics not in copies.hugging_face.downloaded_paths()
    assert "lock taken" not in copies.bun.events


def test_a_dry_run_reports_a_tree_file_that_differs_from_the_tree_s_record() -> None:
    diagnostics = f"releases/{US_TAG}/calibration_diagnostics.json"
    record = {
        **RELEASE_RECORD,
        "calibrationDiagnostics": {"path": diagnostics, "sha256": "d" * 64},
        "releaseManifest": {
            "path": f"releases/{US_TAG}/release_manifest.json",
            "sha256": "e" * 64,
        },
    }
    copies, _ = release_copies(tree_record=record)
    copies.hugging_face.datasets[US_RELEASES].lfs_paths.add(diagnostics)

    [outcome] = copies.run(dry_run=True)

    assert outcome.outcome == "would_copy"
    assert outcome.files["calibration_diagnostics.json"] == "differs"
    assert outcome.files["release_manifest.json"] == "differs"
    assert outcome.files["build_manifest.json"] == "would_copy"
    assert diagnostics not in copies.hugging_face.downloaded_paths()


# Failures and limits -----------------------------------------------------------


def test_a_failed_read_is_an_error_and_the_next_country_goes_on() -> None:
    us_entry = release_entry(US_TAG)
    uk_entry = release_entry(UK_TAG, country="uk")
    copies = Copies(
        hugging_face=hugging_face_with(
            {
                (US_RELEASES, RELEASE_COMMIT): full_release_folder(),
                (UK_RELEASES, RELEASE_COMMIT): full_release_folder(UK_TAG),
            }
        )
    )
    copies.hugging_face.datasets[US_RELEASES].forced_status = 500
    copies.bun.store = store_manifest("us", [us_entry])
    copies.bun.store["countries"]["uk"] = {
        "latestReleaseBuildArtifactId": None,
        "builds": [uk_entry],
    }
    copies.bun.tree_sources[uk_entry["buildArtifactId"]] = recorded(
        f"releases/{UK_TAG}",
        RELEASE_CONTENTS,
        ["calibrationDiagnostics", "buildManifest", "releaseManifest", "demographics"],
    )

    outcomes = copies.run(countries=["us", "uk"])

    assert [(outcome.country, outcome.outcome) for outcome in outcomes] == [
        ("us", "error"),
        ("uk", "copied"),
    ]
    assert outcomes[0].incomplete is True
    assert outcomes[0].reason


def test_a_listing_of_copies_that_fails_is_an_error_for_the_country() -> None:
    copies, _ = release_copies()
    copies.bun.copy_exits["list"] = 3

    [outcome] = copies.run()

    assert (outcome.country, outcome.outcome, outcome.source_id) == (
        "us",
        "error",
        None,
    )
    assert outcome.incomplete is True
    assert copies.hugging_face.requests == []


def test_a_store_s_manifest_that_cannot_be_read_is_an_error_for_each_country() -> None:
    copies, _ = release_copies()
    copies.bun.manifest_read_exit = 3

    outcomes = copies.run(countries=["us", "uk"])

    assert [(outcome.country, outcome.outcome) for outcome in outcomes] == [
        ("us", "error"),
        ("uk", "error"),
    ]
    assert all(outcome.incomplete for outcome in outcomes)


def test_a_build_the_copy_command_refuses_is_not_copied_and_final() -> None:
    copies, _ = release_copies()
    copies.bun.copy_exits["store"] = 2

    [outcome] = copies.run()

    assert outcome.outcome == "not_copied"
    assert outcome.reason
    assert outcome.incomplete is False


def test_a_build_whose_tree_the_store_does_not_hold_is_not_copied_and_final() -> None:
    copies, entry = release_copies()
    copies.bun.without_index.add(entry["buildArtifactId"])

    [outcome] = copies.run()

    assert outcome.outcome == "not_copied"
    assert outcome.reason
    assert outcome.incomplete is False
    assert copies.bun.steps("copies store") == []


@pytest.mark.parametrize("dry_run", [False, True], ids=["real-run", "dry-run"])
def test_a_build_lacking_only_copied_files_without_a_stored_tree_is_not_copied(
    dry_run: bool,
) -> None:
    copies, entry = release_copies(stored=stored_copies(TREE_FILES))
    copies.bun.without_index.add(entry["buildArtifactId"])

    [outcome] = copies.run(dry_run=dry_run)

    assert outcome.outcome == "not_copied"
    assert outcome.incomplete is False
    assert copies.hugging_face.downloaded_paths() == []
    assert copies.bun.steps("copies store") == []


def test_a_file_the_copy_command_refuses_is_reported_as_differing() -> None:
    copies, _ = release_copies()
    copies.bun.refused.add("buildManifest")

    [outcome] = copies.run()

    assert outcome.outcome == "copied"
    assert outcome.files["build_manifest.json"] == "differs"
    assert outcome.files["release_manifest.json"] == "copied"


def test_the_tree_s_record_is_read_once_through_the_copy_command() -> None:
    copies, entry = release_copies()

    copies.run()

    [sources] = copies.bun.steps("copies sources")
    script = str(copies_module().COMMAND_SCRIPT)
    assert sources["argv"] == [
        "/usr/local/bin/bun",
        "run",
        script,
        "sources",
        "us",
        entry["buildArtifactId"],
    ]
    assert copies.bun.steps("run files tree-sources") == []


def test_a_copy_command_that_fails_otherwise_is_an_error() -> None:
    copies, _ = release_copies()
    copies.bun.copy_exits["store"] = 3

    [outcome] = copies.run()

    assert outcome.outcome == "error"
    assert outcome.incomplete is True


def test_no_build_s_copies_start_after_the_time_limit() -> None:
    second_tag = "microcosm-us-2025-20261101T090000Z-1a2b3c4d"
    first, second = release_entry(US_TAG), release_entry(second_tag)
    copies = Copies(
        hugging_face=hugging_face_with(
            {
                (US_RELEASES, RELEASE_COMMIT): full_release_folder()
                | full_release_folder(second_tag)
            }
        )
    )
    copies.bun.store = store_manifest("us", [first, second])
    for entry, tag in ((first, US_TAG), (second, second_tag)):
        copies.bun.tree_sources[entry["buildArtifactId"]] = recorded(
            f"releases/{tag}",
            RELEASE_CONTENTS,
            [
                "calibrationDiagnostics",
                "buildManifest",
                "releaseManifest",
                "demographics",
            ],
        )
    copies.bun.seconds_per_store = 41 * MINUTE

    outcomes = copies.run()

    assert [(outcome.source_id, outcome.outcome) for outcome in outcomes] == [
        (US_TAG, "copied"),
        (second_tag, "deferred"),
    ]
    assert outcomes[1].incomplete is True
    assert len(copies.bun.steps("copies store")) == 1
    assert not any(
        second_tag in path for path in copies.hugging_face.downloaded_paths()
    )


def test_the_time_limit_is_the_one_it_is_given() -> None:
    copies, _ = release_copies()
    copies.clock.now = 10 * MINUTE

    [outcome] = copies.run(time_limit_seconds=0)

    assert outcome.outcome == "deferred"
    assert outcome.incomplete is True
    # Past the limit, a build's files are not even looked for.
    assert copies.hugging_face.requests == []
    assert copies.bun.steps("copies sources") == []


def test_a_dry_run_has_no_time_limit() -> None:
    copies, _ = release_copies()

    [outcome] = copies.run(dry_run=True, time_limit_seconds=0)

    assert outcome.outcome == "would_copy"


def test_only_the_store_s_commands_get_the_blob_token() -> None:
    copies, _ = release_copies()

    copies.run()

    script = str(copies_module().COMMAND_SCRIPT)
    copy_calls = [
        call for call in copies.bun.calls if call["step"].startswith("copies")
    ]
    assert [call["step"] for call in copy_calls] == [
        "copies list",
        "copies sources",
        "copies store",
    ]
    assert copy_calls[0]["argv"] == ["/usr/local/bin/bun", "run", script, "list", "us"]
    for call in copy_calls:
        assert call["env"]["CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN"] == BLOB_TOKEN
        assert set(call["env"]) == {
            "PATH",
            "LANG",
            "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN",
        }
    for request in copies.hugging_face.requests:
        assert BLOB_TOKEN not in str(request.url) + str(request.headers)


def test_each_build_s_downloaded_files_are_removed_after_it() -> None:
    copies, _ = release_copies()

    copies.run()

    [store] = copies.bun.steps("copies store")
    paths = [Path(file["path"]) for file in store["request"]["files"]]
    assert paths
    assert not any(path.exists() for path in paths)
    assert not Path(store["argv"][4]).exists()


# Reporting ----------------------------------------------------------------------


def test_each_outcome_is_one_json_document() -> None:
    copies, _ = release_copies()

    [outcome] = copies.run()

    document = json.loads(json.dumps(outcome.to_json()))
    assert document["outcome"] == "copied"
    assert document["files"]["reform_validation.json"] == "copied"


def test_the_summary_counts_builds_and_files_and_lists_incomplete_builds() -> None:
    module = copies_module()
    outcome_class = module.SourceFilesOutcome
    outcomes = [
        outcome_class(
            country="us",
            outcome="copied",
            kind="release",
            source_id=US_TAG,
            files={
                "calibration_diagnostics.json": "copied",
                "demographics.json": "stored",
            },
        ),
        outcome_class(
            country="us",
            outcome="deferred",
            kind="staging",
            source_id=RUN_ID,
            build_artifact_id="b" * 64,
            incomplete=True,
        ),
        outcome_class(
            country="uk",
            outcome="complete",
            kind="release",
            source_id=UK_TAG,
            files={"calibration_diagnostics.json": "stored"},
        ),
    ]

    summary = module.summarize(outcomes)

    assert summary == {
        "outcomes": {"copied": 1, "deferred": 1, "complete": 1},
        "files": {"copied": 1, "stored": 2},
        "incomplete": True,
        "incomplete_builds": [
            {
                "country": "us",
                "kind": "staging",
                "source_id": RUN_ID,
                "build_artifact_id": "b" * 64,
            }
        ],
    }
