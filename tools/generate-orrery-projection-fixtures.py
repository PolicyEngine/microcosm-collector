"""Write the Orrery projection's expected outputs by running Microcosm's copy.

The collector's copy of the projection (telemetry_collector/orrery/projection)
must produce the same bytes as Microcosm's. This script runs Microcosm's
projection on a set of compiler schemas and execution overlays and saves each
input with the Orrery JSON Microcosm produced from it. It also records, for
the node, edge and output-size limits, the smallest limit at which Microcosm
still projects one of those inputs, and the error it raises one below it.
tests/test_orrery_projection.py projects the same inputs with the collector's
copy and requires identical bytes and errors.

Run it from the root of a PolicyEngine/microcosm checkout with no uncommitted
changes to tracked files, using that checkout's Python environment, which
includes microcosm-graph, microcosm-build and their test helpers:

    cd ../microcosm
    .venv/bin/python ../microcosm-collector/tools/generate-orrery-projection-fixtures.py \
        ../microcosm-collector/tests/fixtures/orrery_projection

The inputs are synthetic fixtures from Microcosm's own tests and the UK graph
declarations, which hold no build data. Their execution overlays come from
running those fixtures, not from a real build.
"""

import argparse
import contextlib
import copy
import gzip
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

FIXTURE_INDEX = "fixtures.json"
#: Files at least this large are stored gzip-compressed.
COMPRESSION_THRESHOLD_BYTES = 1_000_000
#: The limits whose boundaries are recorded, and the input they are found on.
LIMIT_CONSTANTS = ("_MAX_NODES", "_MAX_EDGES", "_MAX_OUTPUT_BYTES")
LIMIT_CASE = "toy-recorded-execution"
ORRERY_CONSTANTS = (
    "_SAFE_INTEGER",
    "_MAX_OUTPUT_BYTES",
    "_MAX_ITEMS",
    "_MAX_NODES",
    "_MAX_EDGES",
)
SCHEMA_CONSTANTS = ("_MAX_ITEMS", "_MAX_BYTES", "_MAX_DEPTH", "_MAX_STRING")


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


def stored_name(file_name: str, payload: bytes) -> str:
    return (
        f"{file_name}.gz" if len(payload) >= COMPRESSION_THRESHOLD_BYTES else file_name
    )


def write_fixture(fixture_root: Path, relative_name: str, payload: bytes) -> str:
    """Write ``payload`` under ``fixture_root`` and return its stored name."""

    name = stored_name(relative_name, payload)
    target = fixture_root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if name.endswith(".gz"):
        target.write_bytes(gzip.compress(payload, compresslevel=9, mtime=0))
    else:
        target.write_bytes(payload)
    return name


@contextlib.contextmanager
def limit_set_to(module, constant: str, value: int) -> Iterator[None]:
    original = getattr(module, constant)
    setattr(module, constant, value)
    try:
        yield
    finally:
        setattr(module, constant, original)


def smallest_accepted_limit(
    module, constant: str, project: Callable[[], str]
) -> tuple[int, str]:
    """Binary-search the smallest value of ``constant`` that still projects."""

    accepted = getattr(module, constant)
    refused = 0
    while accepted - refused > 1:
        candidate = (accepted + refused) // 2
        with limit_set_to(module, constant, candidate):
            try:
                project()
            except ValueError:
                refused = candidate
            else:
                accepted = candidate
    with limit_set_to(module, constant, accepted - 1):
        try:
            project()
        except ValueError as error:
            refusal = str(error)
        else:
            raise RuntimeError(f"{constant} boundary search did not converge")
    return accepted, refusal


class FixtureInputs:
    """The schemas and overlays the expected outputs are generated from."""

    def __init__(self, microcosm_root: Path, work_directory: Path) -> None:
        sys.path.insert(0, str(microcosm_root))
        self.work_directory = work_directory
        self.toy = load_module_from_file(
            "_toy", microcosm_root / "packages/microcosm-graph/tests/_toy.py"
        )
        self.uk_calibration = load_module_from_file(
            "uk_full_calibration_graph_fixture",
            microcosm_root
            / "packages/microcosm-build/tests/engine_free/uk"
            / "test_uk_full_calibration_graph.py",
        )

    def toy_presentation(self, compiled) -> dict:
        from microcosm.graph.presentation import PRESENTATION_PROTOCOL

        processing = [node_id for node_id in compiled.order if node_id != "gate_tax"]
        return {
            "protocol": PRESENTATION_PROTOCOL,
            "scope": {
                "id": "fixture",
                "label": "Synthetic country",
                "description": "Microcosm's toy country, run twice.",
                "boundaries": [
                    {
                        "operation": "survey",
                        "kind": "checkpoint",
                        "missing": "The fixture has no recorded producing graph.",
                    }
                ],
            },
            "groups": [
                {"id": "sources", "label": "Sources", "sources": ["survey"]},
                {
                    "id": "work",
                    "label": "Processing",
                    "description": "Every toy operation except its check.",
                    "operations": processing,
                },
                {
                    "id": "checks",
                    "label": "Checks",
                    "parent": "work",
                    "operations": ["gate_tax"],
                },
            ],
            "operations": {
                "survey": {"composite": {"execution_unit": "single"}},
                "gate_tax": {
                    "label": "Tax threshold check",
                    "description": "Fails when simulated tax leaves its band.",
                },
            },
            "sources": {
                "survey": {
                    "references": [
                        {
                            "label": "Source definition",
                            "url": "https://example.org/survey",
                        }
                    ]
                }
            },
        }

    def toy_recorded_execution(self) -> tuple[dict, dict]:
        """Schema and overlay of a computing phase and its replay, as UK saves them."""

        from microcosm.graph import (
            collect_execution_evidence,
            graph_schema,
            load_run_evidence,
            save_run_evidence,
        )
        from microcosm.graph.presentation import PRESENTATION_EXTENSION

        root = self.work_directory / "toy-recorded-execution"
        computed = self.toy.run_toy(self.toy.full_graph(), root)
        replayed = self.toy.run_toy(
            computed.compiled.graph,
            root,
            sources=computed.sources,
            registry=computed.registry,
            store=computed.store,
        )
        evidence_directory = root / "evidence"
        for run, phase in ((computed, "numerical"), (replayed, "final")):
            index_path = save_run_evidence(
                run.compiled,
                run.manifest,
                store=computed.store,
                directory=evidence_directory,
                attempt_id="attempt",
                phase=phase,
            )
        schema = graph_schema(
            computed.compiled,
            extensions={
                PRESENTATION_EXTENSION: self.toy_presentation(computed.compiled)
            },
        )
        runs = load_run_evidence(
            index_path, store=computed.store, reference_base=evidence_directory
        )
        overlay = collect_execution_evidence(schema, runs=runs, store=computed.store)
        return schema, overlay

    def toy_failed_check(self) -> tuple[dict, dict]:
        """A single phase whose threshold check fails, with no presentation."""

        from microcosm.graph import (
            collect_execution_evidence,
            graph_schema,
            record_run_binding,
        )

        root = self.work_directory / "toy-failed-check"
        run = self.toy.run_toy(self.toy.full_graph(gate_low=1e20, gate_high=1e21), root)
        schema = graph_schema(run.compiled)
        binding = record_run_binding(
            run.compiled, run.manifest, attempt_id="failed", phase="checks"
        )
        overlay = collect_execution_evidence(schema, runs=[binding], store=run.store)
        if overlay["phases"][0]["operations"]["gate_tax"]["gate"] != "fail":
            raise RuntimeError("the toy threshold check was expected to fail")
        return schema, overlay

    def declaration_features(self) -> dict:
        """Rewrites, expansion, artifact inputs and numbers JavaScript cannot hold."""

        from microcosm.graph import graph_schema
        from test_support.microcosm_graph.schema import compiled_graph

        return graph_schema(
            compiled_graph(),
            extensions={
                "fixture.numbers": {
                    "beyond_safe_integer": 2**53,
                    "large_integer": 2**80,
                    "negative_large_integer": -(2**60),
                    "integral_float": 2.0**60,
                    "large_float": 1e100,
                    "fraction": 0.1,
                    "safe_integer": 2**53 - 1,
                }
            },
        )

    def uk_calibration_summaries(self) -> tuple[dict, dict]:
        """UK calibration nodes on a three-household fixture, with summaries.

        UK's own summary providers describe each calibration artifact, except
        that the preflight report's provider raises, as a provider can during
        a build; the overlay then records that failure for the artifact.
        """

        from microcosm.build.uk_runtime.graph_terminal import FULL_GATE_REPORT_TYPE
        from microcosm.build.uk_runtime.orrery_contract import UK_SUMMARY_PROVIDERS
        from microcosm.graph import (
            ContentStore,
            collect_execution_evidence,
            graph_schema,
            load_run_evidence,
            run_graph,
            save_run_evidence,
        )

        root = self.work_directory / "uk-calibration-summaries"
        root.mkdir(parents=True)
        compiled, _endpoints = self.uk_calibration.compiled(2)
        fixture_source = root / "fixture"
        fixture_source.write_bytes(b"fixture")
        store = ContentStore(root / "store")
        manifest = run_graph(
            compiled,
            sources={"fixture": fixture_source},
            store=store,
            kernels=self.uk_calibration.registry(),
        )

        def failing_provider(_context):
            raise RuntimeError("the fixture's preflight summary provider failed")

        providers = dict(UK_SUMMARY_PROVIDERS)
        providers[
            (FULL_GATE_REPORT_TYPE.name, FULL_GATE_REPORT_TYPE.schema_version)
        ] = failing_provider
        evidence_directory = root / "evidence"
        index_path = save_run_evidence(
            compiled,
            manifest,
            store=store,
            directory=evidence_directory,
            attempt_id="fixture",
            phase="numerical",
            artifact_summaries=providers,
        )
        schema = graph_schema(compiled)
        runs = load_run_evidence(
            index_path, store=store, reference_base=evidence_directory
        )
        overlay = collect_execution_evidence(schema, runs=runs, store=store)
        statuses = {
            name: status
            for operation in overlay["phases"][0]["operations"].values()
            for name, status in operation.get("artifact_summary_status", {}).items()
        }
        if statuses.get("preflight") != "provider_failed" or "present" not in set(
            statuses.values()
        ):
            raise RuntimeError(f"unexpected summary statuses: {statuses}")
        return schema, overlay

    def uk_full_dense_declarations(self) -> dict:
        """The complete UK dense graph's declarations, before any build runs."""

        from microcosm.build.country_spec import load_country_spec
        from microcosm.build.uk_runtime.graph import (
            uk_spine_endpoint,
            uk_spine_graph,
        )
        from microcosm.build.uk_runtime.graph_build import (
            UKFullBuildConfig,
            uk_full_graph,
        )
        from microcosm.build.uk_runtime.graph_calibration import (
            UKGraphCalibrationConfig,
        )
        from microcosm.build.uk_runtime.graph_evidence import add_uk_spine_gate_nodes
        from microcosm.build.uk_runtime.graph_terminal import (
            append_uk_full_gate_nodes,
        )
        from microcosm.build.uk_runtime.orrery_contract import uk_graph_presentation
        from microcosm.graph import compile_graph, graph_schema
        from microcosm.graph.presentation import PRESENTATION_EXTENSION

        spine = add_uk_spine_gate_nodes(
            uk_spine_graph(source_mode="split"),
            spec=load_country_spec("uk"),
            engine_identity="declaration-only",
        )
        full = uk_full_graph(
            UKFullBuildConfig(
                calibration_year=2025,
                geography_assignment="legacy",
                calibration=UKGraphCalibrationConfig(dataset_households=None),
            ),
            spine=spine,
        )
        graph = append_uk_full_gate_nodes(
            full.graph,
            calibration=full.calibration,
            spine_stage_names=uk_spine_endpoint(spine).stage_names,
            engine_identity="declaration-only",
            review_date="2026-10-08",
        )
        return graph_schema(
            compile_graph(graph),
            extensions={
                PRESENTATION_EXTENSION: uk_graph_presentation(graph, scope="full_dense")
            },
        )


def cases(inputs: FixtureInputs) -> list[dict]:
    recorded_schema, recorded_overlay = inputs.toy_recorded_execution()
    failed_schema, failed_overlay = inputs.toy_failed_check()
    uk_calibration_schema, uk_calibration_overlay = inputs.uk_calibration_summaries()
    return [
        {
            "name": "toy-recorded-execution",
            "description": (
                "Microcosm's toy country with grouped presentation, a scope "
                "boundary and source references; the overlay holds a computing "
                "phase and its replay, loaded from saved run evidence."
            ),
            "schema": recorded_schema,
            "execution": recorded_overlay,
            "title": None,
        },
        {
            "name": "toy-declarations-only",
            "description": "The same schema projected without an execution overlay.",
            "schema": copy.deepcopy(recorded_schema),
            "execution": None,
            "title": None,
        },
        {
            "name": "toy-failed-check",
            "description": (
                "One phase whose threshold check fails; no presentation contract."
            ),
            "schema": failed_schema,
            "execution": failed_overlay,
            "title": None,
        },
        {
            "name": "declaration-features-titled",
            "description": (
                "Rewritten, expanded and reweighted populations, an artifact "
                "input, numbers outside JavaScript's safe range and a title."
            ),
            "schema": inputs.declaration_features(),
            "execution": None,
            "title": "Declaration features",
        },
        {
            "name": "uk-calibration-summaries",
            "description": (
                "UK calibration nodes on a three-household fixture, with UK's "
                "artifact summaries and one failed summary provider."
            ),
            "schema": uk_calibration_schema,
            "execution": uk_calibration_overlay,
            "title": None,
        },
        {
            "name": "uk-full-dense-declarations",
            "description": (
                "The complete UK dense graph's declarations and presentation "
                "contract, with no execution overlay."
            ),
            "schema": inputs.uk_full_dense_declarations(),
            "execution": None,
            "title": None,
        },
    ]


def generate(microcosm_root: Path, fixture_root: Path) -> None:
    from microcosm.graph import orrery, schema
    from microcosm.graph.canonical import canonical_json
    from microcosm.graph.execution_overlay import (
        execution_overlay_bytes,
        load_execution_overlay,
    )

    if run_git(microcosm_root, "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("Commit or discard the Microcosm checkout's changes first.")
    commit = run_git(microcosm_root, "rev-parse", "HEAD")
    if fixture_root.exists():
        # Replace only a directory this script wrote, never another one.
        if not (fixture_root / FIXTURE_INDEX).is_file():
            raise SystemExit(f"{fixture_root} exists and holds no {FIXTURE_INDEX}.")
        shutil.rmtree(fixture_root)
    fixture_root.mkdir(parents=True)
    index: dict = {
        "microcosm_repository": "PolicyEngine/microcosm",
        "microcosm_commit": commit,
        "cases": [],
    }
    with tempfile.TemporaryDirectory() as work_directory:
        inputs = FixtureInputs(microcosm_root, Path(work_directory))
        for case in cases(inputs):
            name = case["name"]
            schema_bytes = canonical_json(case["schema"])
            loaded_schema = json.loads(schema_bytes)
            expected = orrery.orrery_json_from_schema(
                case["schema"], title=case["title"], execution=case["execution"]
            ).encode("utf-8")
            entry = {
                "name": name,
                "description": case["description"],
                "title": case["title"],
                "schema": write_fixture(
                    fixture_root, f"{name}/graph.schema.json", schema_bytes
                ),
                "execution": None,
                "expected": write_fixture(
                    fixture_root, f"{name}/graph.orrery.json", expected
                ),
            }
            loaded_overlay = None
            if case["execution"] is not None:
                entry["execution"] = write_fixture(
                    fixture_root,
                    f"{name}/graph.execution.json",
                    execution_overlay_bytes(case["execution"]),
                )
                loaded_overlay = load_execution_overlay(
                    fixture_root / entry["execution"]
                )
            # The collector reads the saved files, so they must reproduce the
            # bytes Microcosm projects from its in-memory values.
            reloaded = orrery.orrery_json_from_schema(
                loaded_schema, title=case["title"], execution=loaded_overlay
            ).encode("utf-8")
            if reloaded != expected:
                raise RuntimeError(f"{name}: saved inputs do not reproduce the output")
            index["cases"].append(entry)
            if name == LIMIT_CASE:
                boundaries = []
                for constant in LIMIT_CONSTANTS:
                    accepted, refusal = smallest_accepted_limit(
                        orrery,
                        constant,
                        lambda case=case: orrery.orrery_json_from_schema(
                            case["schema"],
                            title=case["title"],
                            execution=case["execution"],
                        ),
                    )
                    boundaries.append(
                        {
                            "constant": constant,
                            "smallest_accepted": accepted,
                            "refusal_one_below": refusal,
                        }
                    )
                index["limits"] = {
                    "case": name,
                    "orrery_constants": {
                        constant: getattr(orrery, constant)
                        for constant in ORRERY_CONSTANTS
                    },
                    "schema_constants": {
                        constant: getattr(schema, constant)
                        for constant in SCHEMA_CONSTANTS
                    },
                    "boundaries": boundaries,
                }
    (fixture_root / FIXTURE_INDEX).write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "fixture_root",
        type=Path,
        help="the collector's tests/fixtures/orrery_projection directory",
    )
    arguments = parser.parse_args()
    generate(Path.cwd().resolve(), arguments.fixture_root.resolve())


if __name__ == "__main__":
    main()
