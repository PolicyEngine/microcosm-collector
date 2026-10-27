"""The collector's HTTPS requests trust no root of the compromised eMudhra CAs.

On 2026-10-20 eight eMudhra (emSign) intermediate CA certificates were found to
have private keys that anyone can recover from the public certificates
(https://emudhra.we-have-a-problem.com). They chain to "emSign ECC Root CA - G3"
and, through Sectigo's cross-signatures, to "AAA Certificate Services".

certifi 2026.10.18 removed all four emSign roots, and 2026.10.19 restored the
three that the compromised intermediates do not chain to. Advisory
GHSA-6q48-6c7w-9gw2 names 2026.10.19 as the first fixed version
(https://github.com/advisories/GHSA-6q48-6c7w-9gw2).
"""

import hashlib
import runpy
import ssl
import tomllib
from pathlib import Path

import httpcore
import pytest

from telemetry_collector.auth import (
    HuggingFaceAuthenticationUnavailableError,
    HuggingFaceAuthenticator,
)

ROOT = Path(__file__).resolve().parents[1]
# The first fixed version in advisory GHSA-6q48-6c7w-9gw2, whose affected range
# is ">= 2019.03.09, < 2026.10.19". certifi 2026.10.18 already lacks "emSign ECC
# Root CA - G3", but that range still includes it.
FIRST_PATCHED_CERTIFI = (2026, 10, 19)
# SHA-256 fingerprints of the DER-encoded root certificates, as both macOS's
# system roots and Debian's ca-certificates 20230311 report them. certifi
# 2026.7.22 holds the same emSign certificate. CI checks the built image's
# certificate files for the same roots with this script.
COMPROMISED_CHAIN_ROOTS = runpy.run_path(
    str(ROOT / "tools" / "check-image-certificates.py")
)["REMOVED_ROOTS"]
# These change which roots httpx trusts or which host it connects to. Neither
# the image nor scripts/deploy.sh sets any of them.
VARIABLES_UNSET_IN_DEPLOYMENT = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


class StreamThatStopsBeforeHandshake(httpcore.NetworkStream):
    """A TCP stream that records the TLS settings it is given, then fails."""

    def __init__(self, handshakes: list[tuple[ssl.SSLContext, str | None]]) -> None:
        self._handshakes = handshakes

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self._handshakes.append((ssl_context, server_hostname))
        raise httpcore.ConnectError("The test stops before the TLS handshake.")

    def close(self) -> None:
        pass


def locked_version(package_name: str) -> tuple[int, ...]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    [version] = [
        package["version"]
        for package in lock["package"]
        if package["name"] == package_name
    ]
    return tuple(int(part) for part in version.split("."))


def test_image_locks_a_patched_certifi() -> None:
    # The Dockerfile installs exactly these versions with `uv sync --frozen`.
    assert locked_version("certifi") >= FIRST_PATCHED_CERTIFI


def test_hugging_face_check_trusts_no_compromised_root(monkeypatch) -> None:
    # Run the authenticator's own request, replacing only the TCP connection, and
    # keep the SSL context its connection hands to the TLS handshake.
    for variable in VARIABLES_UNSET_IN_DEPLOYMENT:
        monkeypatch.delenv(variable, raising=False)
    connections: list[tuple[str, int]] = []
    handshakes: list[tuple[ssl.SSLContext, str | None]] = []

    def open_connection(backend, host, port, **connection_options):
        connections.append((host, port))
        return StreamThatStopsBeforeHandshake(handshakes)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", open_connection)

    with pytest.raises(HuggingFaceAuthenticationUnavailableError):
        HuggingFaceAuthenticator().authenticate("hf-test-token", "policyengine")

    [(ssl_context, server_hostname)] = handshakes
    # get_ca_certs lists the roots loaded from files; roots in a hashed
    # certificate directory would load only during a handshake.
    trusted_fingerprints = {
        hashlib.sha256(certificate).hexdigest()
        for certificate in ssl_context.get_ca_certs(binary_form=True)
    }
    trusted_compromised_roots = {
        name
        for fingerprint, name in COMPROMISED_CHAIN_ROOTS.items()
        if fingerprint in trusted_fingerprints
    }

    assert connections == [("huggingface.co", 443)]
    assert server_hostname == "huggingface.co"
    assert ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert ssl_context.check_hostname
    assert len(trusted_fingerprints) > 100
    assert trusted_compromised_roots == set()
