"""The collector's HTTPS requests trust no root of the compromised eMudhra CAs.

On 2026-10-20 eight eMudhra (emSign) intermediate CA certificates were found to
have private keys that anyone can recover from the public certificates
(https://emudhra.we-have-a-problem.com). They chain to "emSign ECC Root CA - G3"
and, through Sectigo's cross-signatures, to "AAA Certificate Services". certifi
2026.10.19 is the first release without the emSign root.
"""

import tomllib
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIRST_CERTIFI_WITHOUT_EMSIGN_ECC_ROOT = (2026, 10, 19)
COMPROMISED_CHAIN_ROOTS = {"emSign ECC Root CA - G3", "AAA Certificate Services"}


def locked_version(package_name: str) -> tuple[int, ...]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    [version] = [
        package["version"]
        for package in lock["package"]
        if package["name"] == package_name
    ]
    return tuple(int(part) for part in version.split("."))


def common_names(certificate: dict) -> set[str]:
    return {
        value
        for relative_name in certificate["subject"]
        for attribute, value in relative_name
        if attribute == "commonName"
    }


def test_image_locks_a_certifi_without_the_emsign_ecc_root() -> None:
    # The Dockerfile installs exactly these versions with `uv sync --frozen`.
    assert locked_version("certifi") >= FIRST_CERTIFI_WITHOUT_EMSIGN_ECC_ROOT


def test_hugging_face_requests_trust_no_compromised_root(monkeypatch) -> None:
    # `httpx.get`, which HuggingFaceAuthenticator calls, verifies certificates
    # against this context: certifi's bundle unless SSL_CERT_FILE or
    # SSL_CERT_DIR is set, and neither the image nor the deployment sets them.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    trusted_roots = httpx.create_ssl_context().get_ca_certs()
    trusted_names = set().union(*(common_names(root) for root in trusted_roots))

    assert len(trusted_roots) > 100
    assert trusted_names & COMPROMISED_CHAIN_ROOTS == set()
