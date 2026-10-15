"""The extraction from calibration-diagnostics keeps history and can be repeated."""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXTRACTION_SCRIPT = ROOT / "tools" / "extract-from-calibration-diagnostics.sh"


def isolated_git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", ""),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Collector Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_AUTHOR_DATE": "2026-10-01T12:00:00Z",
        "GIT_COMMITTER_NAME": "Collector Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.invalid",
        "GIT_COMMITTER_DATE": "2026-10-01T12:00:00Z",
    }


def run_git(work_tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(work_tree), *arguments],
        env=isolated_git_environment(),
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def commit_files(source: Path, message: str, files: dict[str, str]) -> str:
    for relative_path, content in files.items():
        file_path = source / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        run_git(source, "add", relative_path)
    run_git(source, "commit", "--quiet", "--message", message)
    return run_git(source, "rev-parse", "HEAD")


@pytest.fixture
def source_checkout(tmp_path: Path) -> Path:
    """A small calibration-diagnostics look-alike with interleaved history."""

    source = tmp_path / "calibration-diagnostics"
    source.mkdir()
    run_git(source, "init", "--quiet", "--initial-branch=main")
    commit_files(
        source,
        "Add the collector and the dashboard",
        {
            "telemetry-service/app.py": "first = True\n",
            "frontend/page.ts": "export {};\n",
        },
    )
    commit_files(source, "Change only the dashboard", {"frontend/page.ts": "// 2\n"})
    commit_files(
        source, "Change the collector", {"telemetry-service/app.py": "second = True\n"}
    )
    return source


def run_extraction(
    output_dir: Path, record_file: Path, source: Path, source_ref: str = "main"
) -> subprocess.CompletedProcess[str]:
    environment = isolated_git_environment()
    environment["EXTRACTION_SOURCE_URL"] = str(source)
    environment["EXTRACTION_SOURCE_REF"] = source_ref
    return subprocess.run(
        ["bash", str(EXTRACTION_SCRIPT), str(output_dir), str(record_file)],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )


def recorded_values(record_file: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in record_file.read_text().splitlines()
        if line and not line.startswith("#")
    )


def test_extraction_moves_the_service_to_the_root_with_its_history(
    tmp_path: Path, source_checkout: Path
) -> None:
    output_dir = tmp_path / "extracted"
    record_file = tmp_path / "record.env"

    result = run_extraction(output_dir, record_file, source_checkout)

    assert result.returncode == 0, result.stderr
    assert run_git(output_dir, "ls-tree", "--name-only", "main") == "app.py"
    assert run_git(output_dir, "log", "--format=%s|%an|%ae", "main").splitlines() == [
        "Change the collector|Collector Author|author@example.invalid",
        "Add the collector and the dashboard|Collector Author|author@example.invalid",
    ]
    assert run_git(output_dir, "show", "main:app.py") == "second = True"


def test_extraction_records_the_source_and_extracted_commits(
    tmp_path: Path, source_checkout: Path
) -> None:
    output_dir = tmp_path / "extracted"
    record_file = tmp_path / "record.env"

    result = run_extraction(output_dir, record_file, source_checkout)

    assert result.returncode == 0, result.stderr
    record = recorded_values(record_file)
    assert record["source_url"] == str(source_checkout)
    assert record["source_ref"] == "main"
    assert record["source_commit"] == run_git(source_checkout, "rev-parse", "HEAD")
    assert record["source_directory"] == "telemetry-service"
    assert record["extracted_commit"] == run_git(output_dir, "rev-parse", "main")


def test_rerunning_the_extraction_extends_the_recorded_history(
    tmp_path: Path, source_checkout: Path
) -> None:
    record_file = tmp_path / "record.env"
    first_output = tmp_path / "first"
    assert run_extraction(first_output, record_file, source_checkout).returncode == 0
    first_extracted_commit = recorded_values(record_file)["extracted_commit"]
    commit_files(
        source_checkout,
        "Apply event batches incrementally",
        {"telemetry-service/storage.py": "incremental = True\n"},
    )

    second_output = tmp_path / "second"
    result = run_extraction(second_output, record_file, source_checkout)

    assert result.returncode == 0, result.stderr
    second_extracted_commit = recorded_values(record_file)["extracted_commit"]
    assert run_git(second_output, "rev-parse", "main~1") == first_extracted_commit
    assert second_extracted_commit == run_git(second_output, "rev-parse", "main")
    assert "Apply event batches incrementally" in result.stdout


def test_extraction_refuses_a_history_without_the_recorded_commit(
    tmp_path: Path, source_checkout: Path
) -> None:
    record_file = tmp_path / "record.env"
    assert (
        run_extraction(tmp_path / "first", record_file, source_checkout).returncode == 0
    )
    recorded_before = record_file.read_text()
    run_git(source_checkout, "commit", "--quiet", "--amend", "--message", "Rewritten")

    result = run_extraction(tmp_path / "second", record_file, source_checkout)

    assert result.returncode == 1
    assert "is not part of the new history" in result.stderr
    assert record_file.read_text() == recorded_before


def test_extraction_refuses_an_existing_output_directory(
    tmp_path: Path, source_checkout: Path
) -> None:
    output_dir = tmp_path / "extracted"
    output_dir.mkdir()

    result = run_extraction(output_dir, tmp_path / "record.env", source_checkout)

    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert not (tmp_path / "record.env").exists()


def test_extraction_refuses_a_source_without_the_service_directory(
    tmp_path: Path, source_checkout: Path
) -> None:
    run_git(source_checkout, "rm", "-r", "--quiet", "telemetry-service")
    run_git(source_checkout, "commit", "--quiet", "--message", "Remove the collector")

    result = run_extraction(
        tmp_path / "extracted", tmp_path / "record.env", source_checkout
    )

    assert result.returncode == 1
    assert "has no telemetry-service/ directory" in result.stderr


def test_committed_record_names_the_source_and_extracted_commits() -> None:
    record = recorded_values(ROOT / "tools" / "extraction-source.env")

    assert record["source_url"] == (
        "https://github.com/PolicyEngine/calibration-diagnostics.git"
    )
    assert record["source_directory"] == "telemetry-service"
    for commit_key in ("source_commit", "extracted_commit"):
        assert re.fullmatch(r"[0-9a-f]{40}", record[commit_key]), commit_key
