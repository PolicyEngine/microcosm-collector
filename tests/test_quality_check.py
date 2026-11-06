"""CI checks style and the syntax of every shell script through one script."""

import os
import shutil
import subprocess
from pathlib import Path

import yaml

from tests.test_deploy_script import install_command_stub

ROOT = Path(__file__).resolve().parents[1]
QUALITY_SCRIPT = ROOT / "tools" / "check-quality.sh"


def quality_tree(tree_root: Path, broken_script: str | None = None) -> Path:
    """A copy of the quality script beside scripts, with Ruff stubbed out."""

    (tree_root / "tools").mkdir()
    (tree_root / "scripts").mkdir()
    shutil.copy(QUALITY_SCRIPT, tree_root / "tools" / "check-quality.sh")
    (tree_root / "tools" / "release.sh").write_text("echo release\n")
    (tree_root / "scripts" / "deploy.sh").write_text("echo deploy\n")
    if broken_script:
        (tree_root / broken_script).write_text("if then fi\n")
    stub_directory = tree_root / "stubs"
    stub_directory.mkdir()
    install_command_stub(stub_directory, "uv", 'printf "%s\\n" "$*" >> "$COMMAND_LOG"')
    return tree_root


def run_quality_check(tree_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "tools/check-quality.sh"],
        cwd=tree_root,
        env={
            "PATH": f"{tree_root / 'stubs'}:{os.environ['PATH']}",
            "COMMAND_LOG": str(tree_root / "commands.log"),
        },
        capture_output=True,
        check=False,
        text=True,
    )


def validation_commands() -> list[str]:
    validation = yaml.load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    return [
        step["run"] for step in validation["jobs"]["validate"]["steps"] if "run" in step
    ]


def readme_section_commands(heading: str) -> list[str]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]
    code_block = section.split("```bash\n", 1)[1].split("```", 1)[0]
    return [line for line in code_block.splitlines() if line]


def test_validation_checks_quality_through_one_script() -> None:
    commands = validation_commands()

    assert "bash tools/check-quality.sh" in commands
    assert not [command for command in commands if "ruff" in command]


def test_readme_test_commands_are_ones_ci_runs() -> None:
    readme_commands = readme_section_commands("Tests")

    assert "bash tools/check-quality.sh" in readme_commands
    assert set(readme_commands) <= set(validation_commands())


def test_quality_check_runs_ruff_and_accepts_valid_scripts(tmp_path: Path) -> None:
    tree_root = quality_tree(tmp_path)

    result = run_quality_check(tree_root)

    assert result.returncode == 0, result.stderr
    assert (tree_root / "commands.log").read_text().splitlines() == [
        "run --no-sync ruff check .",
        "run --no-sync ruff format --check .",
    ]


def test_quality_check_rejects_a_release_or_deploy_script_that_cannot_parse(
    tmp_path: Path,
) -> None:
    for broken_script in ("tools/update-version.sh", "scripts/check-secrets.sh"):
        tree_directory = tmp_path / broken_script.replace("/", "-")
        tree_directory.mkdir()
        tree_root = quality_tree(tree_directory, broken_script)

        result = run_quality_check(tree_root)

        assert result.returncode != 0, broken_script
        assert broken_script.split("/")[-1] in result.stderr
