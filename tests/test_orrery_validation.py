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


@pytest.mark.parametrize(
    "url",
    [
        "https://user:s3cret@example.org/source",
        "http://user:s3cret@example.org/source",
        "https://user:s3cret@exa mple.org/source",
        "HTTPS://user:s3cret@example.org/source",
    ],
)
def test_a_refusal_never_repeats_a_link_s_value(url: str, tmp_path: Path) -> None:
    validation_module = validation()
    document = projected_document("toy-declarations-only")
    assert replace_first_url(document, url)

    with pytest.raises(validation_module.OrreryDocumentRefusedError) as refusal:
        validation_module.validate_orrery_document(
            write_document(tmp_path, document), linked_names(document) - {url}
        )

    assert refusal.value.code == "invalid_source"
    assert "s3cret" not in str(refusal.value)
    assert "s3cret" not in json.dumps(refusal.value.issues)
    # The refusal says where the link is, not what it holds.
    assert refusal.value.issues
    assert refusal.value.issues[0]["path"].endswith(".url")


def test_a_refusal_reports_the_orrery_version(tmp_path: Path) -> None:
    validation_module = validation()
    document = projected_document("toy-declarations-only")
    document.pop("nodes")

    with pytest.raises(validation_module.OrreryDocumentRefusedError) as refusal:
        validation_module.validate_orrery_document(
            write_document(tmp_path, document), linked_names(document)
        )

    assert getattr(refusal.value, "orrery_version", None) == ORRERY_VERSION


def test_a_refusal_keeps_at_most_100_short_issues(tmp_path: Path) -> None:
    validation_module = validation()
    document = projected_document("toy-declarations-only")
    # Orrery reports each repeated node identity, with the identity in the
    # message.
    repeated = {**document["nodes"][0], "id": "n" * 5000}
    document["nodes"] = [repeated] * 150
    document["edges"] = []

    with pytest.raises(validation_module.OrreryDocumentRefusedError) as refusal:
        validation_module.validate_orrery_document(
            write_document(tmp_path, document), linked_names(document)
        )

    assert refusal.value.code == "invalid_graph"
    assert len(refusal.value.issues) == 100
    assert all(
        len(issue["path"]) <= 500 and len(issue["message"]) <= 500
        for issue in refusal.value.issues
    )


def duplicate_identity_issues(tmp_path: Path, identity: str) -> list[dict[str, str]]:
    """Orrery's issues for a document whose nodes all repeat ``identity``."""

    validation_module = validation()
    document = projected_document("toy-declarations-only")
    document["nodes"] = [{**document["nodes"][0], "id": identity}] * 3
    document["edges"] = []

    with pytest.raises(validation_module.OrreryDocumentRefusedError) as refusal:
        validation_module.validate_orrery_document(
            write_document(tmp_path, document), linked_names(document)
        )

    assert refusal.value.code == "invalid_graph"
    return refusal.value.issues


def encodes_as_utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@pytest.mark.parametrize(
    "identity",
    # Orrery's message puts 19 characters before the identity, so cutting at
    # 499 UTF-16 code units lands inside a pair for the first identity. Both
    # are over 500 characters long whichever way they are counted.
    ["x" + "\U0001f600" * 600, "\U0001f600" * 600],
    ids=["identity-at-an-odd-offset", "identity-at-an-even-offset"],
)
def test_a_shortened_issue_keeps_whole_characters(
    identity: str, tmp_path: Path
) -> None:
    issues = duplicate_identity_issues(tmp_path, identity)

    duplicates = [
        issue for issue in issues if issue["message"].startswith("Duplicate identity ")
    ]
    assert duplicates
    # At most 500 characters, counted as Python counts them, and never half
    # of a UTF-16 surrogate pair, which UTF-8 cannot encode.
    shortened = f"Duplicate identity {identity}"[:499] + "…"
    assert all(issue["message"] == shortened for issue in duplicates)
    assert all(
        encodes_as_utf8(issue["path"]) and encodes_as_utf8(issue["message"])
        for issue in issues
    )


def test_an_unpaired_surrogate_in_a_document_is_replaced_in_its_issues(
    tmp_path: Path,
) -> None:
    # Strict UTF-8 bytes can still escape one in JSON ("\ud83d"), and Orrery
    # repeats identities in its messages and paths.
    issues = duplicate_identity_issues(tmp_path, "a\ud83db")

    assert all(
        encodes_as_utf8(issue["path"]) and encodes_as_utf8(issue["message"])
        for issue in issues
    )
    assert "Duplicate identity a�b" in [issue["message"] for issue in issues]


def test_a_document_with_a_byte_order_mark_is_accepted(tmp_path: Path) -> None:
    validation_module = validation()
    document = projected_document("toy-declarations-only")
    document_path = tmp_path / "graph.orrery.json"
    # microcosm-runs' decoder drops a leading byte-order mark too.
    document_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(document).encode())

    result = validation_module.validate_orrery_document(
        document_path, linked_names(document)
    )

    assert result.orrery_version == ORRERY_VERSION


def test_published_names_are_a_collection_of_names(tmp_path: Path) -> None:
    validation_module = validation()
    document_path = write_document(tmp_path, projected_document("toy-failed-check"))

    with pytest.raises(TypeError):
        validation_module.validate_orrery_document(document_path, "graph.schema.json")
    with pytest.raises(validation_module.OrreryValidatorError):
        validation_module.validate_orrery_document(document_path, {"graph\x00.json"})


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


# macOS adds this to every process's environment.
MACOS_PROCESS_VARIABLES = {"__CF_USER_TEXT_ENCODING"}


def test_node_gets_only_path_and_a_utf8_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validation_module = validation()
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://collector:s3cret@database.invalid/collector"
    )
    monkeypatch.setenv("LANG", "en_US.ISO8859-1")
    environment_reporter = tmp_path / "report-environment.mjs"
    # Answers "valid", with Node's environment in place of the Orrery version.
    environment_reporter.write_text(
        "console.log(JSON.stringify("
        "{ valid: true, orrery_version: JSON.stringify(process.env) }));\n"
    )

    result = validation_module.validate_orrery_document(
        write_document(tmp_path, {}), set(), validator=environment_reporter
    )

    node_environment = json.loads(result.orrery_version)
    assert set(node_environment) - MACOS_PROCESS_VARIABLES == {"PATH", "LANG"}
    assert node_environment["PATH"] == os.environ["PATH"]
    assert node_environment["LANG"] == "C.UTF-8"


def test_the_answer_is_read_as_utf8_whatever_the_worker_s_locale(
    tmp_path: Path,
) -> None:
    validation()
    document = projected_document("toy-declarations-only")
    document["nodes"] = [{**document["nodes"][0], "id": "\U0001f600"}] * 2
    document["edges"] = []
    # The C locale, without Python's UTF-8 mode, reads pipes as ASCII.
    ascii_locale = {
        **os.environ,
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONUTF8": "0",
    }

    refused = subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            VALIDATION_MODULE,
            str(write_document(tmp_path, document)),
        ],
        capture_output=True,
        check=False,
        cwd=ROOT,
        env=ascii_locale,
        text=True,
    )

    assert refused.returncode == 2, refused.stderr
    issues = json.loads(refused.stdout)["issues"]
    assert "Duplicate identity \U0001f600" in [issue["message"] for issue in issues]


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
    refusal = json.loads(refused.stdout)
    assert refusal["code"] == "invalid_graph"
    assert refusal.get("orrery_version") == ORRERY_VERSION


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
