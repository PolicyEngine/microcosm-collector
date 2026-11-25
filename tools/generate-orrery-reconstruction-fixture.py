"""Write the reconstructed graph schema the Orrery graph handler is tested with.

Microcosm's reconstruction command
(``python -m microcosm.build.uk_runtime.orrery_reconstruction``) rebuilds the
graph schema of a UK build made before microcosm#1144, which recorded its
graph declaration but no execution overlay, and marks the schema as
reconstructed (``microcosm.reconstruction`` in its extensions). The collector
accepts such a schema as an ``orrery-inputs`` submission with no overlay.

This script runs the command's function, ``reconstruct_uk_publication``, on
Microcosm's own replica of such a build's output directory
(``write_pre_1144_build`` in Microcosm's
``test_uk_orrery_reconstruction.py``), holding the maintained UK dense graph
declaration, as that test module's
``test_maintained_uk_dense_declaration_reconstructs_within_orrery_limits``
builds it. It saves the ``graph.schema.json`` the command wrote, and the
document Microcosm's projection makes from that schema with no title and no
overlay, which is what the handler must publish. It checks that the saved
document differs from the command's own ``graph.orrery.json`` only in its
title. ``tests/test_orrery_graphs.py`` submits the saved schema and requires
the saved document; ``tests/test_orrery_reconstruction.py`` checks the
schema's marker with the collector's copy of ``reconstruction.py``, that the
index names the commit that copy came from, and that the collector's
projection reproduces the saved document.

Run it from the root of a PolicyEngine/microcosm checkout with no uncommitted
changes to tracked files, using that checkout's Python environment, at a
commit whose reconstruction command still writes ``graph.orrery.json`` (such
as ``8c51b6eff7b23e1ff1509cd926fa750ae945a647``):

    cd ../microcosm
    uv sync --all-packages --locked
    .venv/bin/python ../microcosm-collector/tools/generate-orrery-reconstruction-fixture.py \
        ../microcosm-collector/tests/fixtures/orrery_reconstruction

The replica holds no build data: its declaration comes from Microcosm's UK
graph builders, and its other files are the test module's stand-ins. It is
written to relative paths inside a new temporary directory, so the digests
the marker records do not depend on where that directory is, and running the
script again at the same commit writes the same files.
"""

import argparse
import gzip
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FIXTURE_INDEX = "fixtures.json"
CASE = "uk-dense-pre-1144"
#: Files at least this large are stored gzip-compressed.
COMPRESSION_THRESHOLD_BYTES = 1_000_000
TEST_MODULE = (
    "packages/microcosm-build/tests/engine_free/uk/test_uk_orrery_reconstruction.py"
)


def run_git(microcosm_root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(microcosm_root), *arguments],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def load_module_from_file(module_name: str, source_file: Path):
    specification = importlib.util.spec_from_file_location(module_name, source_file)
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


def write_fixture(fixture_root: Path, relative_name: str, payload: bytes) -> str:
    """Write ``payload`` under ``fixture_root`` and return its stored name."""

    name = (
        f"{relative_name}.gz"
        if len(payload) >= COMPRESSION_THRESHOLD_BYTES
        else relative_name
    )
    target = fixture_root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if name.endswith(".gz"):
        # mtime=0 and no file name, so the same bytes always compress alike.
        with (
            target.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream,
        ):
            stream.write(payload)
    else:
        target.write_bytes(payload)
    return name


def maintained_uk_dense_declaration():
    """The UK dense graph declaration, as Microcosm's reconstruction test
    ``test_maintained_uk_dense_declaration_reconstructs_within_orrery_limits``
    builds it."""

    from microcosm.build.country_spec import load_country_spec
    from microcosm.build.uk_runtime.graph import uk_spine_endpoint, uk_spine_graph
    from microcosm.build.uk_runtime.graph_build import (
        UKFullBuildConfig,
        uk_full_graph,
    )
    from microcosm.build.uk_runtime.graph_calibration import UKGraphCalibrationConfig
    from microcosm.build.uk_runtime.graph_evidence import add_uk_spine_gate_nodes
    from microcosm.build.uk_runtime.graph_terminal import append_uk_full_gate_nodes

    spine = add_uk_spine_gate_nodes(
        uk_spine_graph(source_mode="split"),
        spec=load_country_spec("uk"),
        engine_identity="declaration-only",
    )
    full = uk_full_graph(
        UKFullBuildConfig(
            calibration_year=2025,
            geography_assignment="legacy",
            calibration=UKGraphCalibrationConfig(),
        ),
        spine=spine,
    )
    return append_uk_full_gate_nodes(
        full.graph,
        calibration=full.calibration,
        spine_stage_names=uk_spine_endpoint(spine).stage_names,
        engine_identity="declaration-only",
        review_date="2026-10-08",
    )


def generate(microcosm_root: Path, fixture_root: Path) -> None:
    if run_git(microcosm_root, "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("Commit or discard the Microcosm checkout's changes first.")
    commit = run_git(microcosm_root, "rev-parse", "HEAD")
    sys.path.insert(0, str(microcosm_root))

    from microcosm.build.uk_runtime.orrery_reconstruction import (
        GRAPH_NAME,
        SCHEMA_NAME,
        reconstruct_uk_publication,
    )
    from microcosm.graph.orrery import orrery_json_from_schema
    from microcosm.graph.reconstruction import validate_reconstruction
    from microcosm.graph.schema import validate_graph_schema

    replica = load_module_from_file(
        "uk_orrery_reconstruction_replica", microcosm_root / TEST_MODULE
    )
    if fixture_root.exists():
        # Replace only a directory this script wrote, never another one.
        if not (fixture_root / FIXTURE_INDEX).is_file():
            raise SystemExit(f"{fixture_root} exists and holds no {FIXTURE_INDEX}.")
        shutil.rmtree(fixture_root)
    fixture_root = fixture_root.resolve()
    declaration = maintained_uk_dense_declaration()
    starting_directory = Path.cwd()
    with tempfile.TemporaryDirectory() as work_directory:
        os.chdir(work_directory)
        try:
            build = replica.write_pre_1144_build(Path("build"), graph=declaration)
            report = reconstruct_uk_publication(build, Path("publication"))
            schema_bytes = (Path("publication") / SCHEMA_NAME).read_bytes()
            command_document = json.loads(
                (Path("publication") / GRAPH_NAME).read_bytes()
            )
        finally:
            os.chdir(starting_directory)
    schema = json.loads(schema_bytes)
    marker = validate_reconstruction(validate_graph_schema(schema))
    if marker is None:
        raise RuntimeError("The reconstructed schema carries no marker.")
    if marker["reconstruction_revision"] != {"git_commit": commit, "git_dirty": False}:
        raise RuntimeError(
            "The marker does not name this clean checkout as the code that "
            f"reconstructed the schema: {marker['reconstruction_revision']}."
        )
    expected = orrery_json_from_schema(schema).encode("utf-8")
    untitled_command_document = {**command_document, "title": None}
    if {**json.loads(expected), "title": None} != untitled_command_document:
        raise RuntimeError(
            "Microcosm's projection of the saved schema differs from the "
            "command's document in more than its title."
        )
    fixture_root.mkdir(parents=True)
    index = {
        "microcosm_repo": "PolicyEngine/microcosm",
        "microcosm_commit": commit,
        "cases": [
            {
                "name": CASE,
                "description": (
                    "The schema Microcosm's reconstruction command rebuilds from "
                    "a replica of a UK dense build made before microcosm#1144, "
                    "holding the maintained UK dense graph declaration, and "
                    "Microcosm's projection of it with no title and no overlay."
                ),
                "schema": write_fixture(
                    fixture_root, f"{CASE}/graph.schema.json", schema_bytes
                ),
                "expected": write_fixture(
                    fixture_root, f"{CASE}/graph.orrery.json", expected
                ),
                "command_title": command_document["title"],
                "operations": report["declaration"]["operations"],
            }
        ],
    }
    (fixture_root / FIXTURE_INDEX).write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fixture_root", type=Path)
    arguments = parser.parse_args()
    generate(Path.cwd(), arguments.fixture_root)


if __name__ == "__main__":
    main()
