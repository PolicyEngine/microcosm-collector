"""Fail if any certificate file under a directory holds a removed root.

CI pipes this script into the built image's own Python, so it reads the image's
files rather than the runner's:

    docker run --rm -i microcosm-collector:validation python -I - / \
        < tools/check-image-certificates.py

It reads every *.pem, *.crt and *.cer file and compares the SHA-256 fingerprint
of each PEM certificate in it with REMOVED_ROOTS. It needs only the standard
library.
"""

import base64
import hashlib
import os
import re
import sys

# SHA-256 fingerprints of the DER-encoded roots that the compromised eMudhra
# intermediates chain to: directly, and through Sectigo's cross-signatures.
# tests/test_certificate_authorities.py reads them from here.
REMOVED_ROOTS = {
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b": (
        "emSign ECC Root CA - G3"
    ),
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4": (
        "AAA Certificate Services"
    ),
}
PEM_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.DOTALL
)
# Kernel file systems that a running container mounts; they are not image files.
SKIPPED_DIRECTORIES = {"/proc", "/sys", "/dev"}


def main(search_root: str) -> None:
    certificate_count = 0
    removed_roots_found = 0
    for directory, subdirectories, file_names in os.walk(search_root):
        subdirectories[:] = [
            name
            for name in subdirectories
            if os.path.join(directory, name) not in SKIPPED_DIRECTORIES
        ]
        for file_name in file_names:
            path = os.path.join(directory, file_name)
            if not file_name.endswith((".pem", ".crt", ".cer")):
                continue
            # isfile is false for a link whose target is missing.
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as certificate_file:
                encoded_certificates = PEM_CERTIFICATE.findall(certificate_file.read())
            for encoded_certificate in encoded_certificates:
                certificate = base64.b64decode(encoded_certificate)
                fingerprint = hashlib.sha256(certificate).hexdigest()
                certificate_count += 1
                if fingerprint in REMOVED_ROOTS:
                    removed_roots_found += 1
                    print(f'{path} holds removed root "{REMOVED_ROOTS[fingerprint]}"')
    if not certificate_count:
        sys.exit(f"Found no certificates under {search_root}.")
    if removed_roots_found:
        sys.exit(f"{removed_roots_found} copies of removed roots found.")
    print(f"Checked {certificate_count} certificates; none is a removed root.")


if __name__ == "__main__":
    main(sys.argv[1])
