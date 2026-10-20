"""The collector's Orrery projection writes the bytes Microcosm's projection writes.

tools/generate-orrery-projection-fixtures.py ran Microcosm's projection on each
saved compiler schema and execution overlay under fixtures/orrery_projection,
and saved the Orrery JSON it produced. These tests project the same files with
the collector's copy and require identical bytes, identical refusals at the
node, edge and output-size limits, and the same overlay file reader.
"""

import ast
import gzip
import json
import os
import sys
from pathlib import Path

import pytest

from telemetry_collector.orrery import projection
from telemetry_collector.orrery.projection import (
    execution_overlay,
    orrery,
    orrery_json_from_schema,
)
from telemetry_collector.orrery.projection import schema as schema_module

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "orrery_projection"
FIXTURE_INDEX = json.loads((FIXTURE_ROOT / "fixtures.json").read_text())
CASES = {case["name"]: case for case in FIXTURE_INDEX["cases"]}
LIMITS = FIXTURE_INDEX["limits"]
PROJECTION_DIRECTORY = Path(projection.__file__).parent


def fixture_bytes(stored_name: str) -> bytes:
    payload = (FIXTURE_ROOT / stored_name).read_bytes()
    return gzip.decompress(payload) if stored_name.endswith(".gz") else payload


def overlay_file(stored_name: str, working_directory: Path) -> Path:
    """A plain overlay file for the reader, decompressing a stored one if needed."""

    if not stored_name.endswith(".gz"):
        return FIXTURE_ROOT / stored_name
    decompressed = working_directory / Path(stored_name).with_suffix("").name
    decompressed.write_bytes(fixture_bytes(stored_name))
    return decompressed


def project_case(case: dict, working_directory: Path) -> bytes:
    """Project a case's saved files as the collector will receive them."""

    schema = json.loads(fixture_bytes(case["schema"]))
    overlay = (
        None
        if case["execution"] is None
        else execution_overlay.load_execution_overlay(
            overlay_file(case["execution"], working_directory)
        )
    )
    return orrery_json_from_schema(
        schema, title=case["title"], execution=overlay
    ).encode("utf-8")


def first_difference(actual: bytes, expected: bytes) -> str:
    """Describe where two outputs first differ, without printing either whole."""

    offset = next(
        (
            index
            for index, (actual_byte, expected_byte) in enumerate(
                zip(actual, expected, strict=False)
            )
            if actual_byte != expected_byte
        ),
        min(len(actual), len(expected)),
    )
    window = slice(max(offset - 80, 0), offset + 80)
    return (
        f"{len(actual)} bytes projected, {len(expected)} expected; first "
        f"difference at byte {offset}:\nprojected {actual[window]!r}\n"
        f"expected  {expected[window]!r}"
    )


def test_fixtures_cover_overlays_their_absence_titles_and_a_complete_uk_graph() -> None:
    with_overlay = [case for case in CASES.values() if case["execution"] is not None]
    without_overlay = [case for case in CASES.values() if case["execution"] is None]

    assert len(with_overlay) >= 3
    assert len(without_overlay) >= 3
    assert any(case["title"] is not None for case in CASES.values())
    assert "uk-full-dense-declarations" in CASES


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_projection_reproduces_microcosm_bytes_for_each_saved_input(
    case_name: str, tmp_path: Path
) -> None:
    case = CASES[case_name]
    expected = fixture_bytes(case["expected"])

    projected = project_case(case, tmp_path)

    assert projected == expected, first_difference(projected, expected)


@pytest.mark.parametrize(
    "boundary", LIMITS["boundaries"], ids=lambda boundary: boundary["constant"]
)
def test_projection_accepts_and_refuses_at_microcosm_limit_boundaries(
    boundary: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = CASES[LIMITS["case"]]
    expected = fixture_bytes(case["expected"])

    monkeypatch.setattr(orrery, boundary["constant"], boundary["smallest_accepted"])
    projected = project_case(case, tmp_path)
    monkeypatch.setattr(orrery, boundary["constant"], boundary["smallest_accepted"] - 1)
    with pytest.raises(ValueError) as refusal:
        project_case(case, tmp_path)

    assert projected == expected, first_difference(projected, expected)
    assert str(refusal.value) == boundary["refusal_one_below"]


def test_projection_limits_default_to_microcosm_values() -> None:
    orrery_constants = {
        constant: getattr(orrery, constant) for constant in LIMITS["orrery_constants"]
    }
    schema_constants = {
        constant: getattr(schema_module, constant)
        for constant in LIMITS["schema_constants"]
    }

    assert orrery_constants == LIMITS["orrery_constants"]
    assert schema_constants == LIMITS["schema_constants"]
    assert orrery_constants["_MAX_NODES"] == 20_000
    assert orrery_constants["_MAX_EDGES"] == 100_000
    assert orrery_constants["_MAX_OUTPUT_BYTES"] == 64 * 1024 * 1024


def test_projection_modules_import_only_the_standard_library_and_each_other() -> None:
    sibling_modules = {source.stem for source in PROJECTION_DIRECTORY.glob("*.py")}
    outside_imports = []
    for source in sorted(PROJECTION_DIRECTORY.glob("*.py")):
        for statement in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(statement, ast.Import):
                imported = [alias.name for alias in statement.names]
            elif isinstance(statement, ast.ImportFrom) and statement.level == 0:
                imported = [statement.module or ""]
            elif isinstance(statement, ast.ImportFrom):
                if statement.level == 1 and statement.module in sibling_modules:
                    continue
                imported = [f"{'.' * statement.level}{statement.module or ''}"]
            else:
                continue
            outside_imports += [
                f"{source.name}: {name}"
                for name in imported
                if name.split(".")[0] not in sys.stdlib_module_names
            ]

    assert outside_imports == []
    assert {"store", "evidence", "executor"}.isdisjoint(sibling_modules)


@pytest.mark.parametrize(
    "case_name", sorted(name for name, case in CASES.items() if case["execution"])
)
def test_saved_overlay_files_read_back_to_their_canonical_bytes(
    case_name: str, tmp_path: Path
) -> None:
    stored_name = CASES[case_name]["execution"]

    loaded = execution_overlay.load_execution_overlay(
        overlay_file(stored_name, tmp_path)
    )

    assert loaded["protocol"] == execution_overlay.EXECUTION_PROTOCOL
    assert execution_overlay.execution_overlay_bytes(loaded) == fixture_bytes(
        stored_name
    )


OVERLAY_PROTOCOL_FIELD = b'"protocol":"microcosm.graph.execution-evidence.v1"'


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b'{"protocol":"microcosm.graph.execution-input.v1"}', "evidence.v1 object"),
        (b'["microcosm.graph.execution-evidence.v1"]', "evidence.v1 object"),
        (
            b"{" + OVERLAY_PROTOCOL_FIELD + b',"history":"a","history":"b"}',
            "duplicate",
        ),
        (b"{" + OVERLAY_PROTOCOL_FIELD + b',"history":NaN}', "finite JSON numbers"),
        (b"{" + OVERLAY_PROTOCOL_FIELD + b',"history":1e400}', "finite numbers"),
        (b"{" + OVERLAY_PROTOCOL_FIELD.replace(b"evidence", b"\xff") + b"}", "utf-8"),
    ],
    ids=[
        "another protocol",
        "not an object",
        "duplicate key",
        "NaN literal",
        "overflowing number",
        "not UTF-8",
    ],
)
def test_overlay_reader_refuses_malformed_files(
    payload: bytes, message: str, tmp_path: Path
) -> None:
    overlay_path = tmp_path / "graph.execution.json"
    overlay_path.write_bytes(payload)

    with pytest.raises(ValueError, match=message):
        execution_overlay.load_execution_overlay(overlay_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO")
def test_overlay_reader_refuses_a_fifo_without_waiting_for_a_writer(
    tmp_path: Path,
) -> None:
    overlay_path = tmp_path / "graph.execution.json"
    os.mkfifo(overlay_path)

    with pytest.raises(ValueError, match="regular file"):
        execution_overlay.load_execution_overlay(overlay_path)


def test_overlay_reader_refuses_a_file_over_the_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay_path = FIXTURE_ROOT / CASES["toy-recorded-execution"]["execution"]
    monkeypatch.setattr(
        execution_overlay, "_MAX_OVERLAY_BYTES", overlay_path.stat().st_size - 1
    )

    with pytest.raises(ValueError, match="byte limit"):
        execution_overlay.load_execution_overlay(overlay_path)
