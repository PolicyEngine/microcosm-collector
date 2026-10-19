"""The Orrery projection records which Microcosm files it was copied from."""

import hashlib
import json
from pathlib import Path

from telemetry_collector.orrery import projection
from telemetry_collector.orrery.projection import orrery_document_from_schema
from telemetry_collector.orrery.projection.provenance import (
    CHANGED_MODULES,
    MICROCOSM_COMMIT,
    MICROCOSM_REPO,
    MICROCOSM_SOURCE_DIRECTORY,
    ORRERY_ADAPTER,
    SOURCE_BLOBS,
)

PROJECTION_DIRECTORY = Path(projection.__file__).parent
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "orrery_projection"
COLLECTOR_MODULES = {"__init__.py", "provenance.py"}


def git_blob_id(source: Path) -> str:
    """The ID git gives a file's contents, as `git hash-object` prints it."""

    contents = source.read_bytes()
    return hashlib.sha1(
        b"blob %d\0" % len(contents) + contents, usedforsecurity=False
    ).hexdigest()


def test_provenance_names_the_microcosm_commit_and_directory_copied_from() -> None:
    assert MICROCOSM_REPO == "PolicyEngine/microcosm"
    assert len(MICROCOSM_COMMIT) == 40
    assert set(MICROCOSM_COMMIT) <= set("0123456789abcdef")
    assert MICROCOSM_SOURCE_DIRECTORY == "packages/microcosm-graph/src/microcosm/graph"


def test_every_module_is_either_copied_from_microcosm_or_the_collectors_own() -> None:
    present_modules = {source.name for source in PROJECTION_DIRECTORY.glob("*.py")}

    assert present_modules == set(SOURCE_BLOBS) | COLLECTOR_MODULES
    assert set(CHANGED_MODULES) <= set(SOURCE_BLOBS)


def test_unchanged_modules_have_their_microcosm_source_blob_ids() -> None:
    blob_ids = {
        module: git_blob_id(PROJECTION_DIRECTORY / module) for module in SOURCE_BLOBS
    }
    unchanged = {
        module: blob_id
        for module, blob_id in SOURCE_BLOBS.items()
        if module not in CHANGED_MODULES
    }

    assert {module: blob_ids[module] for module in unchanged} == unchanged
    assert all(blob_ids[module] != SOURCE_BLOBS[module] for module in CHANGED_MODULES)


def test_expected_outputs_were_saved_from_the_commit_the_code_was_copied_from() -> None:
    fixture_index = json.loads((FIXTURE_ROOT / "fixtures.json").read_text())

    assert fixture_index["microcosm_repo"] == MICROCOSM_REPO
    assert fixture_index["microcosm_commit"] == MICROCOSM_COMMIT


def test_projection_writes_microcosms_adapter_identifier() -> None:
    schema = json.loads(
        (FIXTURE_ROOT / "toy-declarations-only" / "graph.schema.json").read_bytes()
    )
    saved = json.loads(
        (FIXTURE_ROOT / "toy-declarations-only" / "graph.orrery.json").read_bytes()
    )

    document = orrery_document_from_schema(schema)

    assert ORRERY_ADAPTER == "microcosm.graph.orrery.v1"
    assert document["metadata"]["adapter"] == ORRERY_ADAPTER
    assert saved["metadata"]["adapter"] == ORRERY_ADAPTER
