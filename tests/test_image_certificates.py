"""tools/check-image-certificates.py fails on a certificate store with a removed root.

The fixture certificates are Debian ca-certificates 20230311's copies of
"emSign ECC Root CA - G3" and "AAA Certificate Services", which the compromised
eMudhra intermediates chain to, and of "emSign ECC Root CA - C3", which they do
not.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = ROOT / "tools" / "check-image-certificates.py"
FIXTURE_CERTIFICATES = ROOT / "tests" / "fixtures" / "certificates"
UNAFFECTED_ROOT = FIXTURE_CERTIFICATES / "emSign_ECC_Root_CA_-_C3.pem"


def run_check(search_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(CHECK_SCRIPT), str(search_root)],
        capture_output=True,
        check=False,
        text=True,
    )


def write_certificate_store(search_root: Path, *certificates: Path) -> None:
    store = search_root / "etc" / "ssl" / "certs"
    store.mkdir(parents=True)
    (store / "ca-certificates.crt").write_text(
        "".join(certificate.read_text() for certificate in certificates)
    )


@pytest.mark.parametrize(
    ("removed_root_file", "removed_root_name"),
    [
        ("emSign_ECC_Root_CA_-_G3.pem", "emSign ECC Root CA - G3"),
        ("Comodo_AAA_Services_root.pem", "AAA Certificate Services"),
    ],
)
def test_check_fails_on_a_store_holding_a_removed_root(
    tmp_path: Path, removed_root_file: str, removed_root_name: str
) -> None:
    write_certificate_store(
        tmp_path, UNAFFECTED_ROOT, FIXTURE_CERTIFICATES / removed_root_file
    )

    result = run_check(tmp_path)

    assert result.returncode == 1
    assert removed_root_name in result.stdout


def test_check_passes_on_a_store_holding_only_an_unaffected_root(
    tmp_path: Path,
) -> None:
    write_certificate_store(tmp_path, UNAFFECTED_ROOT)

    result = run_check(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_check_fails_when_it_finds_no_certificates(tmp_path: Path) -> None:
    result = run_check(tmp_path)

    assert result.returncode == 1
