"""Fail if any certificate file under the given directories holds a removed root.

CI pipes this script into the built image's own Python after `docker build`, so
it reads the image's files, not the runner's:

    docker run --rm -i microcosm-collector:validation python -I - / \
        < tools/check-image-certificates.py

The deployment builds its own image, so `scripts/deploy.sh build-and-push-image`
runs this script the same way on that image, and pushes it only if it passes.

It reads every file named *.pem, *.crt or *.cer, following symbolic links to
files, and compares the SHA-256 fingerprint of each PEM certificate in it with
REMOVED_ROOTS. That covers Debian's trust store (/etc/ssl/certs and the
certificates it links to) and every certifi bundle, including pip's vendored
copy. It prints how many certificates each directory holds, then every file
that holds a removed root. It needs only the standard library.
"""

import base64
import hashlib
import os
import re
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

# SHA-256 fingerprints of the DER-encoded roots that the compromised eMudhra
# intermediates chain to: directly, and through Sectigo's cross-signatures.
# tests/test_certificate_authorities.py checks the Hugging Face connection for
# the same two.
REMOVED_ROOTS = {
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b": (
        "emSign ECC Root CA - G3"
    ),
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4": (
        "AAA Certificate Services"
    ),
}
CERTIFICATE_FILE_SUFFIXES = (".pem", ".crt", ".cer")
# Kernel file systems that a running container mounts; they are not image files.
SKIPPED_DIRECTORIES = {"/proc", "/sys", "/dev"}
PEM_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.DOTALL
)


def certificate_fingerprints(path: str) -> list[str]:
    contents = Path(path).read_bytes()
    return [
        hashlib.sha256(base64.b64decode(encoded_certificate)).hexdigest()
        for encoded_certificate in PEM_CERTIFICATE.findall(contents)
    ]


def certificate_files(search_root: str) -> Iterator[tuple[str, str]]:
    for directory, subdirectories, file_names in os.walk(search_root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if os.path.join(directory, name) not in SKIPPED_DIRECTORIES
        )
        for file_name in sorted(file_names):
            path = os.path.join(directory, file_name)
            # A link whose target is missing points at no certificate.
            if file_name.endswith(CERTIFICATE_FILE_SUFFIXES) and os.path.exists(path):
                yield directory, path


def main(search_roots: list[str]) -> None:
    if not search_roots:
        sys.exit("Usage: check-image-certificates.py DIRECTORY...")
    certificates_per_directory: Counter[str] = Counter()
    removed_roots_found = []
    for search_root in search_roots:
        for directory, path in certificate_files(search_root):
            fingerprints = certificate_fingerprints(path)
            certificates_per_directory[directory] += len(fingerprints)
            removed_roots_found.extend(
                (path, REMOVED_ROOTS[fingerprint])
                for fingerprint in fingerprints
                if fingerprint in REMOVED_ROOTS
            )
    if not sum(certificates_per_directory.values()):
        sys.exit(f"Found no certificates under {', '.join(search_roots)}.")
    print("Certificates per directory:")
    for directory, certificate_count in sorted(certificates_per_directory.items()):
        if certificate_count:
            print(f"{certificate_count:>6}  {directory}")
    for path, root_name in removed_roots_found:
        print(f'{path} holds removed root "{root_name}"')
    if removed_roots_found:
        sys.exit(f"{len(removed_roots_found)} copies of removed roots found.")
    print("No removed root found.")


if __name__ == "__main__":
    main(sys.argv[1:])
