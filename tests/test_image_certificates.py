"""CI fails if the built image holds a root of the compromised eMudhra CAs.

The fixture certificates are Debian ca-certificates 20230311's copies of
"emSign ECC Root CA - G3" and "AAA Certificate Services", which the compromised
intermediates chain to, and of "emSign ECC Root CA - C3", which they do not.
"""

import runpy
import subprocess
import sys
from pathlib import Path

import yaml

from tests.test_certificate_authorities import COMPROMISED_CHAIN_ROOTS

ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = ROOT / "tools" / "check-image-certificates.py"
FIXTURE_CERTIFICATES = ROOT / "tests" / "fixtures" / "certificates"
EMSIGN_G3 = FIXTURE_CERTIFICATES / "emSign_ECC_Root_CA_-_G3.pem"
COMODO_AAA = FIXTURE_CERTIFICATES / "Comodo_AAA_Services_root.pem"
EMSIGN_C3 = FIXTURE_CERTIFICATES / "emSign_ECC_Root_CA_-_C3.pem"
VALIDATION_IMAGE = "microcosm-collector:validation"


def run_check(*search_roots: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(CHECK_SCRIPT), *map(str, search_roots)],
        capture_output=True,
        check=False,
        text=True,
    )


def trust_store_with(store_root: Path, *certificates: Path) -> Path:
    trust_store = store_root / "etc" / "ssl" / "certs"
    trust_store.mkdir(parents=True)
    (trust_store / "ca-certificates.crt").write_text(
        "".join(certificate.read_text() for certificate in certificates)
    )
    return trust_store


def test_validation_checks_the_image_it_has_just_built() -> None:
    validation = yaml.load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    commands = [
        step["run"] for step in validation["jobs"]["validate"]["steps"] if "run" in step
    ]
    build = commands.index(f"docker build --tag {VALIDATION_IMAGE} .")

    assert commands[build + 1] == (
        f"docker run --rm -i {VALIDATION_IMAGE} python -I - / "
        "< tools/check-image-certificates.py"
    )


def test_check_names_every_file_holding_a_removed_root(tmp_path: Path) -> None:
    trust_store = trust_store_with(tmp_path, EMSIGN_C3, EMSIGN_G3, COMODO_AAA)
    # A link to the source certificate, as update-ca-certificates makes.
    (trust_store / EMSIGN_G3.name).symlink_to(EMSIGN_G3)

    result = run_check(tmp_path)

    assert result.returncode == 1
    assert result.stdout.splitlines()[-3:] == [
        f"{trust_store / 'ca-certificates.crt'} holds removed root "
        '"emSign ECC Root CA - G3"',
        f"{trust_store / 'ca-certificates.crt'} holds removed root "
        '"AAA Certificate Services"',
        f'{trust_store / EMSIGN_G3.name} holds removed root "emSign ECC Root CA - G3"',
    ]
    assert "3 copies of removed roots found." in result.stderr


def test_check_passes_when_only_unaffected_roots_remain(tmp_path: Path) -> None:
    trust_store = trust_store_with(tmp_path, EMSIGN_C3)
    # update-ca-certificates leaves no link to a removed root, but a stale one
    # whose target is gone holds no certificate.
    (trust_store / EMSIGN_G3.name).symlink_to(tmp_path / "removed.crt")

    result = run_check(tmp_path)

    assert result.returncode == 0
    assert result.stdout == (
        f"Certificates per directory:\n     1  {trust_store}\nNo removed root found.\n"
    )


def test_check_fails_when_it_finds_no_certificate(tmp_path: Path) -> None:
    result = run_check(tmp_path)

    assert result.returncode == 1
    assert f"Found no certificates under {tmp_path}." in result.stderr


def test_check_looks_for_the_roots_the_hugging_face_test_rejects() -> None:
    removed_roots = runpy.run_path(str(CHECK_SCRIPT))["REMOVED_ROOTS"]

    assert removed_roots == COMPROMISED_CHAIN_ROOTS
