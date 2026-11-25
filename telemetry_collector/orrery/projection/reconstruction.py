"""Declaration-only graph schemas rebuilt after a build from its recorded files.

A build that recorded its graph declaration but no run binding cannot have its
execution verified, yet its declared graph is still exact. A reconstructed
schema projects that recorded declaration alone and carries a marker in its
producer extensions: the marker says the schema was rebuilt afterwards and
binds every input file by digest. It names two code revisions: the one that
declared the graph (``source_revision``) and the one that performed the
reconstruction (``reconstruction_revision``). Only the declaration's bytes come
from the build. The schema's ``compiled``, ``fields`` and ``input_bindings``
sections, and any Orrery projection of it, are computed by the reconstructing
code. Because the marker lives in ``graph.schema.json``, any later projection
of the saved schema keeps it, and Orrery documents show it under
``metadata.microcosm.extensions``. Nothing here presents an operation as
executed; no receipt, cache or gate state is attached.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from .decl import compile_graph
from .schema import _plain_json, graph_schema
from .serialize import graph_from_json

__all__ = [
    "DECLARATION_ONLY",
    "DECLARATION_ROLE",
    "GIT_COMMIT_PATTERN",
    "INPUT_VERIFICATIONS",
    "RECONSTRUCTION_EXTENSION",
    "RECONSTRUCTION_PROTOCOL",
    "REVISION_ORIGINS",
    "code_revision",
    "reconstructed_schema",
    "reconstruction_marker",
    "validate_reconstruction",
]

RECONSTRUCTION_EXTENSION = "microcosm.reconstruction"
RECONSTRUCTION_PROTOCOL = "microcosm.graph.reconstruction.v1"
DECLARATION_ONLY = "declaration_only"
DECLARATION_ROLE = "declaration"
# ``recorded``: the build wrote the revision itself. ``caller_asserted``: the
# build recorded none, and whoever ran the reconstruction supplied it.
REVISION_ORIGINS = frozenset({"recorded", "caller_asserted"})
# How an input's bytes compare with the digests the build itself recorded:
# - ``recorded_digest_matched``: every recorded digest matched.
# - ``fallback_digest_matched``: the build marked one recorded digest as taken
#   before it rewrote the file. That digest was skipped, and another digest the
#   build recorded matched.
# - ``only_stale_digest_recorded``: every digest the build recorded for the file
#   is marked as taken before the rewrite, so nothing it recorded checks the
#   current bytes.
# - ``no_recorded_digest``: the build recorded none.
# In the last two cases the file is bound only by the digest listed here.
INPUT_VERIFICATIONS = frozenset(
    {
        "recorded_digest_matched",
        "fallback_digest_matched",
        "only_stale_digest_recorded",
        "no_recorded_digest",
    }
)
RECONSTRUCTION_SCOPE = (
    "Rebuilt after the build from its recorded graph declaration, the only part "
    "taken from the build: the compiled sections and this projection come from "
    "the code at reconstruction_revision. Operations carry no execution, cache "
    "or gate state; nothing here was observed running."
)
_MARKER_FIELDS = frozenset(
    {
        "protocol",
        "reconstructed",
        "basis",
        "source_revision",
        "reconstruction_revision",
        "inputs",
        "scope",
    }
)
_REVISION_FIELDS = frozenset({"git_commit", "git_dirty", "origin"})
_RECONSTRUCTION_REVISION_FIELDS = frozenset({"git_commit", "git_dirty"})
_INPUT_FIELDS = frozenset({"name", "role", "sha256", "bytes", "verification"})
# A full lowercase SHA-1 or SHA-256 git object name; abbreviations are refused.
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Graph reconstruction: {message}.")


def _text(value: object, label: str) -> str:
    _require(type(value) is str and bool(value.strip()), f"{label} must be text")
    return value  # type: ignore[return-value]


def _file_name(value: object) -> str:
    name = _text(value, "input name")
    _require(
        name not in {".", ".."}
        and not any(separator in name for separator in ("/", "\\"))
        and not any(ord(character) < 32 for character in name),
        "input names must be plain file names",
    )
    return name


def _validated_marker(value: object) -> dict[str, object]:
    marker = _plain_json(value)
    _require(type(marker) is dict, "marker must be an object")
    assert isinstance(marker, dict)
    _require(set(marker) == _MARKER_FIELDS, "marker fields")
    _require(marker["protocol"] == RECONSTRUCTION_PROTOCOL, "marker protocol")
    _require(marker["reconstructed"] is True, "marker must say reconstructed")
    _require(marker["basis"] == DECLARATION_ONLY, "marker basis")
    _text(marker["scope"], "marker scope")
    revision = marker["source_revision"]
    _require(
        type(revision) is dict and set(revision) == _REVISION_FIELDS,
        "source revision fields",
    )
    _require(
        type(revision["git_commit"]) is str
        and GIT_COMMIT_PATTERN.fullmatch(revision["git_commit"]) is not None,
        "source revision must be a full lowercase git commit",
    )
    _require(
        type(revision["git_dirty"]) in (bool, type(None)),
        "source revision dirty flag must be true, false or null",
    )
    _require(revision["origin"] in REVISION_ORIGINS, "source revision origin")
    reconstruction = marker["reconstruction_revision"]
    _require(
        type(reconstruction) is dict
        and set(reconstruction) == _RECONSTRUCTION_REVISION_FIELDS,
        "reconstruction revision fields",
    )
    commit = reconstruction["git_commit"]
    _require(
        commit is None
        or (type(commit) is str and GIT_COMMIT_PATTERN.fullmatch(commit) is not None),
        "reconstruction revision must be a full lowercase git commit or null",
    )
    _require(
        type(reconstruction["git_dirty"]) in (bool, type(None))
        and (commit is not None or reconstruction["git_dirty"] is None),
        "reconstruction revision dirty flag must be true, false or, without a "
        "commit, null",
    )
    inputs = marker["inputs"]
    _require(type(inputs) is list and bool(inputs), "marker inputs")
    names = []
    for item in inputs:
        _require(type(item) is dict and set(item) == _INPUT_FIELDS, "input fields")
        names.append(_file_name(item["name"]))
        _text(item["role"], "input role")
        _require(
            type(item["sha256"]) is str
            and _SHA256.fullmatch(item["sha256"]) is not None,
            "input digests must be lowercase SHA-256",
        )
        _require(
            type(item["bytes"]) is int and item["bytes"] > 0,
            "input sizes must be positive integers",
        )
        _require(
            item["verification"] in INPUT_VERIFICATIONS, "input verification state"
        )
    _require(len(set(names)) == len(names), "duplicate input name")
    _require(names == sorted(names), "inputs must be listed in name order")
    _require(
        sum(item["role"] == DECLARATION_ROLE for item in inputs) == 1,
        "exactly one input must be the declaration",
    )
    return marker


def _declaration_input(marker: Mapping[str, object]) -> Mapping[str, object]:
    inputs = marker["inputs"]
    assert isinstance(inputs, list)
    return next(item for item in inputs if item["role"] == DECLARATION_ROLE)


def _git_environment() -> dict[str, str] | None:
    """This process's environment without the variables that choose git's repository.

    The names removed are the installed git's own list of variables local to
    one repository (``git rev-parse --local-env-vars``): ``GIT_DIR``,
    ``GIT_WORK_TREE``, ``GIT_INDEX_FILE``, ``GIT_COMMON_DIR``,
    ``GIT_OBJECT_DIRECTORY``, the ``git -c`` carriers ``GIT_CONFIG_PARAMETERS``
    and ``GIT_CONFIG_COUNT``, and the rest. ``None`` when git cannot list them.
    ``microcosm.frame.adapters.axiom`` strips the same list for its checkout
    check; this module does not import that adapter, so that it stays light.
    """

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    local = frozenset(result.stdout.split())
    return {name: value for name, value in os.environ.items() if name not in local}


def code_revision(path: str | Path) -> dict[str, object]:
    """The git commit and working-tree state of the checkout that tracks ``path``.

    Pass the file of the code performing the reconstruction. git runs in that
    file's directory, not the current one, and the file must be tracked there:
    an installed copy that happens to sit inside some checkout (a virtual
    environment within a repository, say) identifies no revision. Both values
    are ``None`` when no checkout tracks the file, so an unknown revision is
    never recorded as a clean one. git runs without the caller's
    repository-selecting variables (``GIT_DIR``, ``GIT_WORK_TREE`` and the rest),
    so an outer git command, hook or shell cannot point it at another checkout.
    """

    location = Path(path).resolve()
    unknown: dict[str, object] = {"git_commit": None, "git_dirty": None}
    environment = _git_environment()
    if environment is None:
        return unknown

    def git(*arguments: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(location.parent), *arguments],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        except OSError:
            return None
        return result.stdout if result.returncode == 0 else None

    if git("ls-files", "--error-unmatch", "--", location.name) is None:
        return unknown
    head = git("rev-parse", "HEAD")
    commit = None if head is None else head.strip()
    if commit is None or GIT_COMMIT_PATTERN.fullmatch(commit) is None:
        return unknown
    changes = git("status", "--porcelain", "--untracked-files=no")
    return {
        "git_commit": commit,
        "git_dirty": None if changes is None else bool(changes.strip()),
    }


def reconstruction_marker(
    *,
    git_commit: str,
    git_dirty: bool | None,
    revision_origin: str,
    reconstruction_revision: Mapping[str, object],
    inputs: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Return a validated marker naming both revisions and every input read.

    ``git_commit``, ``git_dirty`` and ``revision_origin`` describe the code that
    declared the graph. ``reconstruction_revision`` is :func:`code_revision` of
    the code performing the reconstruction, which computes every section of the
    schema except the declaration itself. Each input is a mapping with ``name``
    (a plain file name), ``role``, ``sha256``, ``bytes`` and ``verification``;
    exactly one has role ``declaration``. Inputs are listed in name order.
    """

    ordered = sorted(
        (dict(item) for item in inputs), key=lambda item: str(item.get("name"))
    )
    return _validated_marker(
        {
            "protocol": RECONSTRUCTION_PROTOCOL,
            "reconstructed": True,
            "basis": DECLARATION_ONLY,
            "source_revision": {
                "git_commit": git_commit,
                "git_dirty": git_dirty,
                "origin": revision_origin,
            },
            "reconstruction_revision": dict(reconstruction_revision),
            "inputs": ordered,
            "scope": RECONSTRUCTION_SCOPE,
        }
    )


def validate_reconstruction(schema: Mapping[str, object]) -> dict[str, object] | None:
    """Return a schema's validated reconstruction marker, or ``None`` if absent.

    The marker must name the declaration embedded in the schema: its
    declaration input's digest equals the schema's ``graph_sha256``.
    """

    extensions = schema["extensions"]
    _require(isinstance(extensions, Mapping), "schema extensions must be an object")
    assert isinstance(extensions, Mapping)
    if RECONSTRUCTION_EXTENSION not in extensions:
        return None
    marker = _validated_marker(extensions[RECONSTRUCTION_EXTENSION])
    _require(
        _declaration_input(marker)["sha256"] == schema["graph_sha256"],
        "marker does not name this declaration",
    )
    return marker


def reconstructed_schema(
    declaration: bytes, marker: Mapping[str, object]
) -> dict[str, object]:
    """Project recorded declaration bytes into a marked compiler schema.

    ``declaration`` is the exact ``graph.json`` a build wrote. The marker must
    name those bytes, and the current serializer must reproduce them exactly;
    otherwise the projection would describe a different declaration from the
    one that was recorded, and the declaration is refused.
    """

    _require(type(declaration) is bytes, "declaration must be bytes")
    checked = _validated_marker(marker)
    named = _declaration_input(checked)
    declaration_sha256 = hashlib.sha256(declaration).hexdigest()
    _require(
        named["sha256"] == declaration_sha256 and named["bytes"] == len(declaration),
        "marker does not name this declaration",
    )
    try:
        text = declaration.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            "Graph reconstruction: declaration must be UTF-8 JSON."
        ) from error
    try:
        declared = graph_from_json(text)
    except TypeError as error:
        # The serializer reports JSON of the wrong shape (an array where the
        # graph object belongs, say) as a TypeError; recorded files are data.
        raise ValueError(
            f"Graph reconstruction: the recorded declaration is not a graph ({error})."
        ) from error
    schema = graph_schema(
        compile_graph(declared),
        extensions={RECONSTRUCTION_EXTENSION: checked},
    )
    _require(
        schema["graph_sha256"] == declaration_sha256,
        "the declaration does not reproduce its recorded bytes through the "
        "current serializer",
    )
    return schema
