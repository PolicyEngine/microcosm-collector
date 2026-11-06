"""Orrery graph documents are validated with one pinned @axiom-foundation/orrery.

The validator under orrery-validator runs under Node and accepts exactly what
microcosm-runs' publication accepts for a graph file: strict UTF-8 JSON,
Orrery's parseGraphDocument, and microcosm-runs' checkSources. These tests run
the real validator (CI installs it with `npm ci` first) on the documents the
collector's projection writes, and on documents each check refuses.
"""

import gzip
import importlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_DIRECTORY = ROOT / "orrery-validator"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "orrery_projection"
FIXTURE_CASES = {
    case["name"]: case
    for case in json.loads((FIXTURE_ROOT / "fixtures.json").read_text())["cases"]
}
ORRERY_PACKAGE = "@axiom-foundation/orrery"
ORRERY_VERSION = "0.6.2"
NODE_VERSION = "24.21.0"
VALIDATION_MODULE = "telemetry_collector.orrery.validation"
CHECK_IMAGE_SCRIPT = ROOT / "tools" / "check-worker-image.sh"


def validation() -> ModuleType:
    assert importlib.util.find_spec(VALIDATION_MODULE) is not None
    return importlib.import_module(VALIDATION_MODULE)


def projected_document(case_name: str) -> dict:
    stored_name = FIXTURE_CASES[case_name]["expected"]
    payload = (FIXTURE_ROOT / stored_name).read_bytes()
    if stored_name.endswith(".gz"):
        payload = gzip.decompress(payload)
    return json.loads(payload)


def linked_names(document: object) -> set[str]:
    """What each non-https `url` in a document names, before any fragment."""

    names: set[str] = set()
    pending = [document]
    while pending:
        current = pending.pop()
        children = (
            current.items()
            if isinstance(current, dict)
            else enumerate(current)
            if isinstance(current, list)
            else []
        )
        for key, item in children:
            if (
                key == "url"
                and isinstance(item, str)
                and not item.startswith("https://")
            ):
                names.add(item.split("#", 1)[0])
            if isinstance(item, (dict, list)):
                pending.append(item)
    return names


def replace_first_url(document: object, url: str) -> bool:
    """Set the first `url` found in a document to ``url``."""

    pending = [document]
    while pending:
        current = pending.pop(0)
        children = (
            list(current.items())
            if isinstance(current, dict)
            else list(enumerate(current))
            if isinstance(current, list)
            else []
        )
        for key, item in children:
            if key == "url" and isinstance(item, str):
                current[key] = url
                return True
            if isinstance(item, (dict, list)):
                pending.append(item)
    return False


def write_document(tmp_path: Path, document: object) -> Path:
    document_path = tmp_path / "graph.orrery.json"
    document_path.write_text(json.dumps(document), encoding="utf-8")
    return document_path


def test_the_validator_pins_one_orrery_version_with_a_lockfile() -> None:
    manifest_path = VALIDATOR_DIRECTORY / "package.json"
    lock_path = VALIDATOR_DIRECTORY / "package-lock.json"
    assert manifest_path.is_file()
    assert lock_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    lock = json.loads(lock_path.read_text())

    assert manifest["private"] is True
    assert manifest["dependencies"] == {ORRERY_PACKAGE: ORRERY_VERSION}
    assert "devDependencies" not in manifest
    assert lock["packages"][""]["dependencies"] == {ORRERY_PACKAGE: ORRERY_VERSION}
    locked = lock["packages"][f"node_modules/{ORRERY_PACKAGE}"]
    assert locked["version"] == ORRERY_VERSION
    assert locked["integrity"].startswith("sha512-")


@pytest.mark.parametrize("case_name", sorted(FIXTURE_CASES))
def test_documents_the_projection_writes_are_accepted(
    case_name: str, tmp_path: Path
) -> None:
    validation_module = validation()
    document = projected_document(case_name)

    result = validation_module.validate_orrery_document(
        write_document(tmp_path, document), linked_names(document)
    )

    assert result.orrery_version == ORRERY_VERSION


def test_a_link_to_a_file_not_published_with_the_document_is_refused(
    tmp_path: Path,
) -> None:
    validation_module = validation()
    document = projected_document("toy-recorded-execution")
    names = linked_names(document)
    missing = sorted(names)[0]

    with pytest.raises(validation_module.OrreryDocumentRefusedError) as refusal:
        validation_module.validate_orrery_document(
            write_document(tmp_path, document), names - {missing}
        )

    assert refusal.value.code == "invalid_source"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.org/source",
        "https://user@example.org/source",
        "https://",
        "http://example.org/source",
    ],
)
def test_links_that_are_not_plain_https_or_a_published_file_are_refused(
    url: str, tmp_path: Path
) -> None:
    validation_module = validation()
    document = projected_document("toy-declarations-only")
    assert replace_first_url(document, url)

    with pytest.raises(validation_module.OrreryDocumentRefusedError) as refusal:
        validation_module.validate_orrery_document(
            write_document(tmp_path, document), linked_names(document) - {url}
        )

    assert refusal.value.code == "invalid_source"


def test_a_link_to_a_published_file_may_carry_a_fragment(tmp_path: Path) -> None:
    validation_module = validation()
    document = projected_document("toy-declarations-only")
    assert replace_first_url(document, "graph.schema.json#/nodes/0")

    result = validation_module.validate_orrery_document(
        write_document(tmp_path, document), {"graph.schema.json"}
    )

    assert result.orrery_version == ORRERY_VERSION


def test_a_document_orrery_refuses_is_refused_with_orrery_s_issues(
    tmp_path: Path,
) -> None:
    validation_module = validation()
    document = projected_document("toy-declarations-only")
    document.pop("nodes")

    with pytest.raises(validation_module.OrreryDocumentRefusedError) as refusal:
        validation_module.validate_orrery_document(
            write_document(tmp_path, document), linked_names(document)
        )

    assert refusal.value.code == "invalid_graph"
    assert refusal.value.issues
    assert all(set(issue) == {"path", "message"} for issue in refusal.value.issues)


@pytest.mark.parametrize(
    "payload", [b"\xff\xfe{}", b'{"nodes": [', b"\xef\xbb\xbf{}\xc3"]
)
def test_bytes_that_are_not_utf8_json_are_refused(
    payload: bytes, tmp_path: Path
) -> None:
    validation_module = validation()
    document_path = tmp_path / "graph.orrery.json"
    document_path.write_bytes(payload)

    with pytest.raises(validation_module.OrreryDocumentRefusedError) as refusal:
        validation_module.validate_orrery_document(document_path, set())

    assert refusal.value.code == "invalid_json"


def test_a_missing_document_is_a_validator_error_not_a_refusal(
    tmp_path: Path,
) -> None:
    validation_module = validation()

    with pytest.raises(validation_module.OrreryValidatorError):
        validation_module.validate_orrery_document(tmp_path / "missing.json", set())


def test_a_missing_node_is_a_validator_error(tmp_path: Path) -> None:
    validation_module = validation()
    document = projected_document("toy-declarations-only")

    with pytest.raises(validation_module.OrreryValidatorError):
        validation_module.validate_orrery_document(
            write_document(tmp_path, document),
            linked_names(document),
            node=str(tmp_path / "no-such-node"),
        )


def test_a_validator_that_runs_too_long_is_stopped(tmp_path: Path) -> None:
    validation_module = validation()
    stalled_validator = tmp_path / "stalled.mjs"
    stalled_validator.write_text("setInterval(() => {}, 1000);\n")

    with pytest.raises(validation_module.OrreryValidatorError):
        validation_module.validate_orrery_document(
            write_document(tmp_path, {}),
            set(),
            validator=stalled_validator,
            timeout_seconds=1,
        )


def test_the_command_line_prints_the_result_and_exits_with_its_code(
    tmp_path: Path,
) -> None:
    validation()
    document = projected_document("toy-declarations-only")
    document_path = write_document(tmp_path, document)
    command = ["uv", "run", "--no-sync", "python", "-m", VALIDATION_MODULE]

    accepted = subprocess.run(
        [*command, str(document_path), *sorted(linked_names(document))],
        capture_output=True,
        check=False,
        cwd=ROOT,
        text=True,
    )
    document.pop("nodes")
    refused = subprocess.run(
        [*command, str(write_document(tmp_path, document))],
        capture_output=True,
        check=False,
        cwd=ROOT,
        text=True,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout) == {
        "valid": True,
        "orrery_version": ORRERY_VERSION,
    }
    assert refused.returncode == 2, refused.stderr
    assert json.loads(refused.stdout)["code"] == "invalid_graph"


def test_the_image_holds_pinned_node_and_the_installed_validator() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    validation_module = validation()

    # The same Node release as the npm publishing workflow, by digest.
    assert f"FROM node:{NODE_VERSION}-trixie-slim@sha256:" in dockerfile, (
        "Node must come from the pinned official image"
    )
    assert "COPY --from=orrery-validator /usr/local/bin/node /usr/local/bin/node" in (
        dockerfile
    )
    assert "RUN npm ci --omit=dev --omit=peer --ignore-scripts" in dockerfile
    assert (
        "COPY --from=orrery-validator /orrery-validator ./orrery-validator"
        in dockerfile
    )
    # The image's validator is where the Python module looks for it.
    assert validation_module.VALIDATOR_SCRIPT.relative_to(ROOT) == Path(
        "orrery-validator/validate-orrery-document.mjs"
    )
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "**/node_modules" in dockerignore


def test_ci_installs_the_validator_before_the_tests_and_checks_the_image() -> None:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["validate"]["steps"]
    runs = [step.get("run") for step in steps]
    node = [
        step for step in steps if step.get("uses", "").startswith("actions/setup-node@")
    ]

    assert len(node) == 1
    assert node[0]["uses"].split("@")[1] == "249970729cb0ef3589644e2896645e5dc5ba9c38"
    assert node[0]["with"]["node-version"] == NODE_VERSION
    install = "npm ci --prefix orrery-validator --omit=dev --omit=peer --ignore-scripts"
    assert install in runs
    assert runs.index(install) < runs.index("uv run pytest -q")
    image_check = "bash tools/check-worker-image.sh microcosm-collector:validation"
    assert image_check in runs
    assert runs.index("docker build --tag microcosm-collector:validation .") < (
        runs.index(image_check)
    )


def run_image_check(
    tmp_path: Path, *, accepted_exit: int, refused_exit: int
) -> subprocess.CompletedProcess[str]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    calls = tmp_path / "docker-calls"
    fake_docker = bin_directory / "docker"
    # The refused document is the one whose name has "refused" in it.
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        'if [[ "$*" == *refused* ]]; then\n'
        f"  exit {refused_exit}\n"
        "fi\n"
        f"exit {accepted_exit}\n"
    )
    fake_docker.chmod(0o755)
    return subprocess.run(
        ["bash", str(CHECK_IMAGE_SCRIPT), "microcosm-collector:validation"],
        cwd=ROOT,
        env={"PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        check=False,
        text=True,
    )


def test_the_image_check_passes_when_the_image_accepts_and_refuses_as_expected(
    tmp_path: Path,
) -> None:
    assert CHECK_IMAGE_SCRIPT.is_file()

    result = run_image_check(tmp_path, accepted_exit=0, refused_exit=2)

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "docker-calls").read_text().splitlines()
    assert len(calls) == 2
    assert all(call.startswith("run --rm ") for call in calls)
    assert all("microcosm-collector:validation" in call for call in calls)
    assert all(f"-m {VALIDATION_MODULE}" in call for call in calls)


@pytest.mark.parametrize(
    ("accepted_exit", "refused_exit"), [(2, 2), (0, 0), (1, 2), (0, 1)]
)
def test_the_image_check_fails_otherwise(
    accepted_exit: int, refused_exit: int, tmp_path: Path
) -> None:
    assert CHECK_IMAGE_SCRIPT.is_file()

    result = run_image_check(
        tmp_path, accepted_exit=accepted_exit, refused_exit=refused_exit
    )

    assert result.returncode != 0
