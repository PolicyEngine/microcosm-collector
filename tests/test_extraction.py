"""The extraction from calibration-diagnostics keeps history and can be repeated."""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXTRACTION_SCRIPT = ROOT / "tools" / "extract-from-calibration-diagnostics.sh"
# Where the collector keeps the record, relative to its checkout root.
CHECKOUT_RECORD = Path("tools") / "extraction-source.env"
INCREMENTAL_BATCH_COMMITS = [
    (
        "Apply event batches incrementally",
        {"telemetry-service/storage.py": "incremental = True\n"},
    ),
    (
        "Test incremental batch application",
        {"telemetry-service/test_storage.py": "def test_batches(): pass\n"},
    ),
    (
        "Document incremental batch application",
        {"telemetry-service/notes.md": "Batches apply incrementally.\n"},
    ),
]
BOUNDED_LISTING_COMMITS = [
    ("Bound the run listing", {"telemetry-service/app.py": "bounded = True\n"}),
]


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


def commit_subjects(work_tree: Path, revision_range: str) -> list[str]:
    return run_git(work_tree, "log", "--format=%s", revision_range).splitlines()


def merge_source_pull_request(
    source: Path,
    pull_request_number: int,
    collector_commits: list[tuple[str, dict[str, str]]],
    *,
    main_moves_meanwhile: bool,
) -> list[str]:
    """Merge a pull request with a merge commit, as calibration-diagnostics does.

    When main moves on meanwhile (here, outside telemetry-service/), the filter
    prunes the merge commit; when the branch is up to date, the extraction keeps
    it. calibration-diagnostics' history has both. Returns every commit subject
    the pull request adds to the source.
    """

    branch_name = f"pull-request-{pull_request_number}"
    run_git(source, "switch", "--quiet", "--create", branch_name)
    for message, files in collector_commits:
        commit_files(source, message, files)
    run_git(source, "switch", "--quiet", "main")
    if main_moves_meanwhile:
        commit_files(
            source,
            f"Change only the dashboard while #{pull_request_number} is open",
            {"frontend/page.ts": f"// changed while #{pull_request_number} is open\n"},
        )
    merge_message = (
        f"Merge pull request #{pull_request_number} from PolicyEngine/{branch_name}"
    )
    run_git(
        source, "merge", "--quiet", "--no-ff", "--message", merge_message, branch_name
    )
    return [message for message, _ in collector_commits] + [merge_message]


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


@pytest.fixture
def merged_source_checkout(tmp_path: Path) -> Path:
    """A calibration-diagnostics look-alike whose collector changes arrive through
    pull requests merged with merge commits, as its main branch does."""

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
    merge_source_pull_request(
        source,
        201,
        [("Change the collector", {"telemetry-service/app.py": "second = True\n"})],
        main_moves_meanwhile=True,
    )
    return source


def run_extraction(
    output_dir: Path,
    record_file: Path,
    source: Path,
    *,
    working_directory: Path,
    source_ref: str = "main",
) -> subprocess.CompletedProcess[str]:
    environment = isolated_git_environment()
    environment["EXTRACTION_SOURCE_URL"] = str(source)
    environment["EXTRACTION_SOURCE_REF"] = source_ref
    # Keep git from finding a repo above the test's own directories.
    environment["GIT_CEILING_DIRECTORIES"] = str(working_directory.parent)
    return subprocess.run(
        ["bash", str(EXTRACTION_SCRIPT), str(output_dir), str(record_file)],
        cwd=working_directory,
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


def listed_new_commit_subjects(extraction_stdout: str) -> list[str]:
    _, _, listing = extraction_stdout.partition("New since the last extraction:\n")
    return [line.split(" ", 1)[1] for line in listing.splitlines() if line]


def start_collector_checkout(tmp_path: Path, source: Path) -> Path:
    """The collector as it began: the first extraction's history, then a
    collector-only commit that adds the record."""

    initial_output = tmp_path / "initial-extraction"
    initial_record = tmp_path / "initial-record.env"
    result = run_extraction(
        initial_output, initial_record, source, working_directory=tmp_path
    )
    assert result.returncode == 0, result.stderr
    collector = tmp_path / "microcosm-collector"
    run_git(tmp_path, "clone", "--quiet", str(initial_output), str(collector))
    commit_files(
        collector,
        "Record the extraction from calibration-diagnostics",
        {
            str(CHECKOUT_RECORD): initial_record.read_text(),
            "CHANGELOG.md": "## [0.2.0]\n",
        },
    )
    return collector


def extract_into_collector(
    collector: Path, source: Path, output_name: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the extraction from the collector checkout, as the README does."""

    output_dir = collector.parent / output_name
    result = run_extraction(
        output_dir, collector / CHECKOUT_RECORD, source, working_directory=collector
    )
    return result, output_dir


def merge_extraction_and_record(collector: Path, extraction_output: Path) -> None:
    run_git(collector, "fetch", "--quiet", str(extraction_output), "main")
    run_git(
        collector,
        "merge",
        "--quiet",
        "--no-ff",
        "--message",
        "Merge the telemetry-service extraction",
        "FETCH_HEAD",
    )
    run_git(collector, "add", str(CHECKOUT_RECORD))
    run_git(collector, "commit", "--quiet", "--message", "Record the extraction")


def land_on_main(collector: Path, branch_name: str, merge_method: str) -> None:
    """Land a pull request branch on main as GitHub's merge methods do."""

    run_git(collector, "switch", "--quiet", "main")
    if merge_method == "merge":
        run_git(
            collector,
            "merge",
            "--quiet",
            "--no-ff",
            "--message",
            f"Merge pull request from PolicyEngine/{branch_name}",
            branch_name,
        )
    elif merge_method == "squash":
        run_git(collector, "merge", "--quiet", "--squash", branch_name)
        run_git(collector, "commit", "--quiet", "--message", f"Squashed {branch_name}")
    elif merge_method == "rebase":
        run_git(collector, "rebase", "--quiet", "main", branch_name)
        run_git(collector, "switch", "--quiet", "main")
        run_git(collector, "merge", "--quiet", "--ff-only", branch_name)
    else:
        raise ValueError(f"Unknown merge method: {merge_method}")


def carry_over_pull_request(
    collector: Path,
    source: Path,
    pull_request_number: int,
    collector_commits: list[tuple[str, dict[str, str]]],
    merge_method: str,
) -> list[str]:
    """Merge an up-to-date source pull request, carry it over as the README
    describes and land the carry-over pull request with the given merge method.
    Returns the source commit subjects carried over."""

    carried_over_subjects = merge_source_pull_request(
        source, pull_request_number, collector_commits, main_moves_meanwhile=False
    )
    branch_name = f"carry-over-{pull_request_number}"
    run_git(collector, "switch", "--quiet", "--create", branch_name)
    result, extraction_output = extract_into_collector(
        collector, source, f"{branch_name}-extraction"
    )
    assert result.returncode == 0, result.stderr
    merge_extraction_and_record(collector, extraction_output)
    land_on_main(collector, branch_name, merge_method)
    return carried_over_subjects


def test_extraction_moves_the_service_to_the_root_with_its_history(
    tmp_path: Path, source_checkout: Path
) -> None:
    output_dir = tmp_path / "extracted"
    record_file = tmp_path / "record.env"

    result = run_extraction(
        output_dir, record_file, source_checkout, working_directory=tmp_path
    )

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

    result = run_extraction(
        output_dir, record_file, source_checkout, working_directory=tmp_path
    )

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
    collector = start_collector_checkout(tmp_path, source_checkout)
    first_extracted_commit = recorded_values(collector / CHECKOUT_RECORD)[
        "extracted_commit"
    ]
    commit_files(
        source_checkout,
        "Apply event batches incrementally",
        {"telemetry-service/storage.py": "incremental = True\n"},
    )

    result, second_output = extract_into_collector(
        collector, source_checkout, "second-extraction"
    )

    assert result.returncode == 0, result.stderr
    second_extracted_commit = recorded_values(collector / CHECKOUT_RECORD)[
        "extracted_commit"
    ]
    assert run_git(second_output, "rev-parse", "main~1") == first_extracted_commit
    assert second_extracted_commit == run_git(second_output, "rev-parse", "main")
    assert "Apply event batches incrementally" in result.stdout


def test_rerunning_after_merge_commit_carry_overs_adds_only_new_source_commits(
    tmp_path: Path, merged_source_checkout: Path
) -> None:
    collector = start_collector_checkout(tmp_path, merged_source_checkout)
    carried_over_subjects = carry_over_pull_request(
        collector, merged_source_checkout, 219, INCREMENTAL_BATCH_COMMITS, "merge"
    )
    # The carried-over history includes the source pull request's merge commit.
    assert carried_over_subjects[-1] in commit_subjects(collector, "main")
    merge_source_pull_request(
        merged_source_checkout, 230, BOUNDED_LISTING_COMMITS, main_moves_meanwhile=True
    )
    run_git(collector, "switch", "--quiet", "--create", "carry-over-230")

    result, extraction_output = extract_into_collector(
        collector, merged_source_checkout, "carry-over-230-extraction"
    )

    assert result.returncode == 0, result.stderr
    run_git(collector, "fetch", "--quiet", str(extraction_output), "main")
    commits_the_merge_adds = commit_subjects(collector, "HEAD..FETCH_HEAD")
    assert sorted(commits_the_merge_adds) == sorted(
        listed_new_commit_subjects(result.stdout)
    )
    assert "Bound the run listing" in commits_the_merge_adds
    assert not set(commits_the_merge_adds) & set(carried_over_subjects)


@pytest.mark.parametrize("merge_method", ["squash", "rebase"])
def test_extraction_refuses_a_record_whose_carry_over_skipped_a_merge_commit(
    tmp_path: Path, merged_source_checkout: Path, merge_method: str
) -> None:
    collector = start_collector_checkout(tmp_path, merged_source_checkout)
    carry_over_pull_request(
        collector, merged_source_checkout, 219, INCREMENTAL_BATCH_COMMITS, merge_method
    )
    # main has the carried-over changes and record, but not the extracted commits.
    assert run_git(collector, "show", "main:storage.py") == "incremental = True"
    recorded_before = (collector / CHECKOUT_RECORD).read_text()
    rerun_output = tmp_path / "rerun-extraction"

    result = run_extraction(
        rerun_output,
        collector / CHECKOUT_RECORD,
        merged_source_checkout,
        working_directory=collector,
    )

    assert result.returncode == 1
    assert "is not in the history of HEAD" in result.stderr
    assert "with a merge commit" in result.stderr
    assert (collector / CHECKOUT_RECORD).read_text() == recorded_before
    assert not rerun_output.exists()


def test_extraction_refuses_a_record_whose_commit_the_checkout_lacks(
    tmp_path: Path, merged_source_checkout: Path
) -> None:
    collector = start_collector_checkout(tmp_path, merged_source_checkout)
    record_file = collector / CHECKOUT_RECORD
    recorded_commit = recorded_values(record_file)["extracted_commit"]
    unknown_commit = "f" * 40
    record_file.write_text(
        record_file.read_text().replace(recorded_commit, unknown_commit)
    )
    rerun_output = tmp_path / "rerun-extraction"

    result = run_extraction(
        rerun_output, record_file, merged_source_checkout, working_directory=collector
    )

    assert result.returncode == 1
    assert (
        f"The recorded extraction {unknown_commit} is not in the history of HEAD"
        in (result.stderr)
    )
    assert not rerun_output.exists()


def test_extraction_with_a_record_refuses_to_run_outside_a_git_checkout(
    tmp_path: Path, source_checkout: Path
) -> None:
    record_file = tmp_path / "record.env"
    assert (
        run_extraction(
            tmp_path / "first", record_file, source_checkout, working_directory=tmp_path
        ).returncode
        == 0
    )
    outside_any_checkout = tmp_path / "outside"
    outside_any_checkout.mkdir()

    result = run_extraction(
        tmp_path / "second",
        record_file,
        source_checkout,
        working_directory=outside_any_checkout,
    )

    assert result.returncode == 1
    assert "is not in a git work tree" in result.stderr
    assert not (tmp_path / "second").exists()


def test_carrying_over_after_restoring_a_squashed_carry_over_adds_only_new_commits(
    tmp_path: Path, merged_source_checkout: Path
) -> None:
    collector = start_collector_checkout(tmp_path, merged_source_checkout)
    carried_over_subjects = carry_over_pull_request(
        collector, merged_source_checkout, 219, INCREMENTAL_BATCH_COMMITS, "squash"
    )
    merge_source_pull_request(
        merged_source_checkout, 230, BOUNDED_LISTING_COMMITS, main_moves_meanwhile=True
    )
    # The README's recovery: merge the squashed pull request's head without
    # changing any file, then carry over as usual on the same branch.
    run_git(collector, "switch", "--quiet", "--create", "carry-over-230")
    run_git(
        collector,
        "merge",
        "--quiet",
        "--strategy",
        "ours",
        "--message",
        "Restore the squashed carry-over's commits to the history",
        "carry-over-219",
    )
    assert run_git(collector, "rev-parse", "HEAD^{tree}") == run_git(
        collector, "rev-parse", "main^{tree}"
    )

    result, extraction_output = extract_into_collector(
        collector, merged_source_checkout, "carry-over-230-extraction"
    )

    assert result.returncode == 0, result.stderr
    run_git(collector, "fetch", "--quiet", str(extraction_output), "main")
    commits_the_merge_adds = commit_subjects(collector, "HEAD..FETCH_HEAD")
    assert "Bound the run listing" in commits_the_merge_adds
    assert not set(commits_the_merge_adds) & set(carried_over_subjects)


def test_extraction_refuses_a_history_without_the_recorded_commit(
    tmp_path: Path, source_checkout: Path
) -> None:
    collector = start_collector_checkout(tmp_path, source_checkout)
    recorded_before = (collector / CHECKOUT_RECORD).read_text()
    run_git(source_checkout, "commit", "--quiet", "--amend", "--message", "Rewritten")

    result, _ = extract_into_collector(
        collector, source_checkout, "rewritten-extraction"
    )

    assert result.returncode == 1
    assert "is not part of the new history" in result.stderr
    assert (collector / CHECKOUT_RECORD).read_text() == recorded_before


def test_extraction_refuses_an_existing_output_directory(
    tmp_path: Path, source_checkout: Path
) -> None:
    output_dir = tmp_path / "extracted"
    output_dir.mkdir()

    result = run_extraction(
        output_dir, tmp_path / "record.env", source_checkout, working_directory=tmp_path
    )

    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert not (tmp_path / "record.env").exists()


def test_extraction_refuses_a_source_without_the_service_directory(
    tmp_path: Path, source_checkout: Path
) -> None:
    run_git(source_checkout, "rm", "-r", "--quiet", "telemetry-service")
    run_git(source_checkout, "commit", "--quiet", "--message", "Remove the collector")

    result = run_extraction(
        tmp_path / "extracted",
        tmp_path / "record.env",
        source_checkout,
        working_directory=tmp_path,
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
