"""The README's setup steps name the same token, datasets and scope as the code."""

from pathlib import Path

from telemetry_collector.huggingface_read import (
    GENERIC_TOKEN_VARIABLES,
    HUGGINGFACE_DATASETS,
    TOKEN_VARIABLE,
)

ROOT = Path(__file__).resolve().parents[1]
SECTION_HEADING = "## Hugging Face read access"


def readme_section() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert SECTION_HEADING in readme
    section = readme.split(SECTION_HEADING, 1)[1]
    return section.split("\n## ", 1)[0]


def dataset_rows(section: str) -> dict[str, list[str]]:
    rows = {}
    for row in section.splitlines():
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if row.startswith("| `policyengine/"):
            rows[cells[0].strip("`")] = cells
    return rows


def test_the_readme_names_the_token_variable_and_the_refused_generic_ones() -> None:
    section = readme_section()

    assert f"`{TOKEN_VARIABLE}`" in section
    for generic_variable in GENERIC_TOKEN_VARIABLES:
        assert f"`{generic_variable}`" in section
    assert "Secret Manager" in section


def test_the_readme_lists_every_dataset_with_its_token_scope() -> None:
    rows = dataset_rows(readme_section())

    assert set(rows) == {dataset.name for dataset in HUGGINGFACE_DATASETS}
    for dataset in HUGGINGFACE_DATASETS:
        in_scope = rows[dataset.name][-1]
        assert in_scope.startswith("no" if dataset.public else "yes"), dataset.name


def test_the_readme_mounts_the_token_without_replacing_other_mounts() -> None:
    section = readme_section()

    assert f"--update-secrets {TOKEN_VARIABLE}=" in section
    assert "--set-secrets" in section
