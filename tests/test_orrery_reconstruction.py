"""The collector checks Microcosm's reconstruction marker with Microcosm's own code.

Microcosm's reconstruction command rebuilds the graph schema of a UK build
made before microcosm#1144 and marks it ``microcosm.reconstruction``. The
collector's copy of ``reconstruction.py`` holds the marker's checks, unchanged
from Microcosm, and runs on the projection modules copied before it.
tools/generate-orrery-reconstruction-fixture.py saved a schema the command
rebuilt, and Microcosm's projection of it, under
fixtures/orrery_reconstruction.
"""

import ast
import gzip
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

from telemetry_collector.orrery import projection
from telemetry_collector.orrery.projection import (
    orrery_json_from_schema,
    provenance,
)
from telemetry_collector.orrery.projection.schema import validate_graph_schema

RECONSTRUCTION_MODULE = "telemetry_collector.orrery.projection.reconstruction"
PROJECTION_DIRECTORY = Path(projection.__file__).parent
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "orrery_reconstruction"
FIXTURE_INDEX = json.loads((FIXTURE_ROOT / "fixtures.json").read_text())
[CASE] = FIXTURE_INDEX["cases"]


def git_blob_id(source: Path) -> str:
    """The ID git gives a file's contents, as `git hash-object` prints it."""

    contents = source.read_bytes()
    return hashlib.sha1(
        b"blob %d\0" % len(contents) + contents, usedforsecurity=False
    ).hexdigest()


def fixture_bytes(stored_name: str) -> bytes:
    payload = (FIXTURE_ROOT / stored_name).read_bytes()
    return gzip.decompress(payload) if stored_name.endswith(".gz") else payload


def reconstruction() -> ModuleType:
    assert importlib.util.find_spec(RECONSTRUCTION_MODULE) is not None
    return importlib.import_module(RECONSTRUCTION_MODULE)


def test_the_marker_checks_are_microcosm_s_module_unchanged() -> None:
    copied_sources = getattr(provenance, "RECONSTRUCTION_SOURCE_BLOBS", {})
    copied_file = PROJECTION_DIRECTORY / "reconstruction.py"

    assert set(copied_sources) == {"reconstruction.py"}
    assert copied_file.is_file()
    assert git_blob_id(copied_file) == copied_sources["reconstruction.py"]


def relative_imports(module: str) -> set[str]:
    """The projection modules one module imports with ``from .x import``."""

    tree = ast.parse((PROJECTION_DIRECTORY / module).read_text())
    return {
        f"{statement.module}.py"
        for statement in ast.walk(tree)
        if isinstance(statement, ast.ImportFrom)
        and statement.level == 1
        and statement.module is not None
    }


def modules_run_by(module: str) -> set[str]:
    """Every projection module ``module`` imports, directly or through others."""

    found: set[str] = set()
    pending = [module]
    while pending:
        for imported in relative_imports(pending.pop()) - found:
            found.add(imported)
            pending.append(imported)
    return found - {module}


def test_the_modules_it_runs_are_the_copies_taken_from_the_earlier_commit() -> None:
    assert (PROJECTION_DIRECTORY / "reconstruction.py").is_file()
    imported_modules = modules_run_by("reconstruction.py")
    import_blobs = getattr(provenance, "RECONSTRUCTION_IMPORT_BLOBS", {})

    assert imported_modules == {
        "canonical.py",
        "decl.py",
        "expansion.py",
        "schema.py",
        "serialize.py",
    }
    # Their blob IDs at the later commit, recorded when the module was copied,
    # equal those at the commit the projection was copied from.
    assert set(import_blobs) == imported_modules
    assert {
        module: provenance.SOURCE_BLOBS[module] for module in imported_modules
    } == import_blobs


def test_the_fixture_was_saved_from_the_commit_the_checks_were_copied_from() -> None:
    assert FIXTURE_INDEX["microcosm_repo"] == provenance.MICROCOSM_REPO
    assert FIXTURE_INDEX["microcosm_commit"] == getattr(
        provenance, "RECONSTRUCTION_COMMIT", None
    )


def test_the_copied_checks_accept_the_marker_microcosm_s_command_wrote() -> None:
    schema = validate_graph_schema(json.loads(fixture_bytes(CASE["schema"])))

    marker = reconstruction().validate_reconstruction(schema)

    assert marker is not None
    assert marker["protocol"] == "microcosm.graph.reconstruction.v1"
    assert marker["basis"] == "declaration_only"
    assert marker["reconstruction_revision"] == {
        "git_commit": FIXTURE_INDEX["microcosm_commit"],
        "git_dirty": False,
    }


def test_the_copied_checks_find_no_marker_in_a_build_s_schema() -> None:
    build_schema = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "orrery_projection"
            / "toy-recorded-execution"
            / "graph.schema.json"
        ).read_bytes()
    )

    assert (
        reconstruction().validate_reconstruction(validate_graph_schema(build_schema))
        is None
    )


def test_the_collector_projects_the_reconstructed_schema_as_microcosm_does() -> None:
    schema = json.loads(fixture_bytes(CASE["schema"]))

    projected = orrery_json_from_schema(schema).encode("utf-8")

    assert projected == fixture_bytes(CASE["expected"])
