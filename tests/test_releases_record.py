"""The parser of ``releases.json`` that lists every build it records.

It follows the rules of the tree package's ``release-record.ts`` and of the
tree handler's ``current_default_build``: these tests check that it refuses
exactly the records ``current_default_build`` refuses, and names the same
current default build for every record both accept.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from types import ModuleType
from typing import Any

import pytest

from telemetry_collector.calibration_trees import (
    ReleasesRecordRefusedError,
    current_default_build,
)

RELEASES_RECORD_MODULE = "telemetry_collector.releases_record"
NATIONAL = "microcosm-uk-2024-25-national"
LOCAL = "microcosm-uk-2024-25-local"
FIRST_BUILD = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"
SECOND_BUILD = "uk-frs-calibration-attempt-20261101T120000Z-0a1b2c3d"


def releases_record_module() -> ModuleType:
    if importlib.util.find_spec(RELEASES_RECORD_MODULE) is None:
        pytest.fail(f"{RELEASES_RECORD_MODULE} does not exist yet.")
    return importlib.import_module(RELEASES_RECORD_MODULE)


def parse(content: bytes) -> Any:
    return releases_record_module().parse_releases_record(content)


def refused_error() -> type[Exception]:
    error = getattr(releases_record_module(), "ReleasesRecordRefusedError", None)
    if error is None:
        pytest.fail(f"{RELEASES_RECORD_MODULE} has no ReleasesRecordRefusedError.")
    return error


def record_document() -> dict[str, Any]:
    """Two releases: the national one, current at its second build, with its
    first build withdrawn; and a local one without a current build."""

    return {
        "schema_version": 1,
        "default": NATIONAL,
        "releases": {
            NATIONAL: {
                "role": "national",
                "current": SECOND_BUILD,
                "builds": {
                    SECOND_BUILD: {
                        "published_at": "2026-11-01T12:00:00Z",
                        "withdrawn": False,
                    },
                    FIRST_BUILD: {
                        "published_at": "2026-10-19T08:33:23+00:00",
                        "withdrawn": True,
                    },
                },
            },
            LOCAL: {
                "role": "local_area",
                "current": None,
                "builds": {
                    FIRST_BUILD: {
                        "published_at": "2026-10-20T09:00:00Z",
                        "withdrawn": False,
                    }
                },
            },
        },
    }


def encoded(document: dict[str, Any]) -> bytes:
    return json.dumps(document).encode()


def test_every_build_is_listed_by_release_then_build_withdrawn_ones_included() -> None:
    record = parse(encoded(record_document()))

    assert [
        (
            build.release,
            build.build,
            build.build_id,
            build.published_at,
            build.withdrawn,
            build.current,
            build.in_default_release,
        )
        for build in record.builds
    ] == [
        (
            LOCAL,
            FIRST_BUILD,
            f"{LOCAL}/{FIRST_BUILD}",
            "2026-10-20T09:00:00Z",
            False,
            False,
            False,
        ),
        (
            NATIONAL,
            FIRST_BUILD,
            f"{NATIONAL}/{FIRST_BUILD}",
            "2026-10-19T08:33:23+00:00",
            True,
            False,
            True,
        ),
        (
            NATIONAL,
            SECOND_BUILD,
            f"{NATIONAL}/{SECOND_BUILD}",
            "2026-11-01T12:00:00Z",
            False,
            True,
            True,
        ),
    ]
    assert record.default == NATIONAL


def test_the_current_default_build_is_the_default_release_s_current_build() -> None:
    record = parse(encoded(record_document()))

    current = record.current_default_build

    assert current is not None
    assert current.build_id == f"{NATIONAL}/{SECOND_BUILD}"
    assert [build.build_id for build in record.builds if build.current_default] == [
        current.build_id
    ]


@pytest.mark.parametrize(
    "change",
    ["no-default", "no-current"],
)
def test_a_record_without_a_current_default_build_names_none(change: str) -> None:
    document = record_document()
    if change == "no-default":
        document["default"] = None
    else:
        document["releases"][NATIONAL]["current"] = None

    record = parse(encoded(document))

    assert record.current_default_build is None
    assert not any(build.current_default for build in record.builds)
    assert len(record.builds) == 3


def test_published_at_is_kept_as_the_record_writes_it() -> None:
    document = record_document()
    document["releases"][LOCAL]["builds"][FIRST_BUILD]["published_at"] = "not a time"

    record = parse(encoded(document))

    assert record.builds[0].published_at == "not a time"


def _changed(change: str) -> bytes:
    """A record changed in one way that ``release-record.ts`` refuses or accepts."""

    document = record_document()
    national = document["releases"][NATIONAL]
    if change == "unlisted-current":
        national["current"] = "uk-frs-calibration-attempt-unlisted"
    elif change == "unlisted-default":
        document["default"] = "microcosm-uk-unlisted"
    elif change == "withdrawn-current":
        national["builds"][SECOND_BUILD]["withdrawn"] = True
    elif change == "schema-2":
        document["schema_version"] = 2
    elif change == "boolean-schema-version":
        document["schema_version"] = True
    elif change == "float-schema-version":
        # JSON.parse reads 1.0 as 1, so the tree package accepts it.
        return encoded(record_document()).replace(
            b'"schema_version": 1', b'"schema_version": 1.0'
        )
    elif change == "extra-key":
        document["notes"] = "extra"
    elif change == "missing-key":
        del document["default"]
    elif change == "releases-not-an-object":
        document["releases"] = []
    elif change == "release-without-role":
        del national["role"]
    elif change == "invalid-role":
        national["role"] = "National"
    elif change == "current-not-a-string":
        national["current"] = 7
    elif change == "builds-not-an-object":
        national["builds"] = []
    elif change == "unsafe-release-name":
        document["releases"]["../escape"] = document["releases"].pop(LOCAL)
    elif change == "unsafe-build-name":
        national["builds"]["a/b"] = national["builds"].pop(FIRST_BUILD)
    elif change == "build-with-extra-key":
        national["builds"][FIRST_BUILD]["note"] = "x"
    elif change == "empty-published-at":
        national["builds"][FIRST_BUILD]["published_at"] = ""
    elif change == "withdrawn-not-a-boolean":
        national["builds"][FIRST_BUILD]["withdrawn"] = 0
    elif change == "default-not-a-string":
        document["default"] = 3
    elif change == "not-json":
        return b"not json"
    elif change == "nan":
        return encoded(record_document()).replace(
            b'"schema_version": 1', b'"schema_version": NaN'
        )
    elif change == "invalid-utf-8":
        return encoded(record_document()).replace(b'"national"', b'"nation\xffal"')
    elif change == "byte-order-mark":
        return b"\xef\xbb\xbf" + encoded(record_document())
    elif change == "over-4-mib":
        return encoded(record_document()) + b" " * (4 * 1024 * 1024)
    elif change == "not-an-object":
        return b"[]"
    else:
        raise AssertionError(change)
    return encoded(document)


RECORD_CHANGES = [
    "unlisted-current",
    "unlisted-default",
    "withdrawn-current",
    "schema-2",
    "boolean-schema-version",
    "float-schema-version",
    "extra-key",
    "missing-key",
    "releases-not-an-object",
    "release-without-role",
    "invalid-role",
    "current-not-a-string",
    "builds-not-an-object",
    "unsafe-release-name",
    "unsafe-build-name",
    "build-with-extra-key",
    "empty-published-at",
    "withdrawn-not-a-boolean",
    "default-not-a-string",
    "not-json",
    "nan",
    "invalid-utf-8",
    "byte-order-mark",
    "over-4-mib",
    "not-an-object",
]


@pytest.mark.parametrize("change", RECORD_CHANGES)
def test_the_parser_refuses_exactly_the_records_current_default_build_refuses(
    change: str,
) -> None:
    content = _changed(change)
    error = refused_error()
    try:
        expected_current = current_default_build(content)
    except ReleasesRecordRefusedError:
        with pytest.raises(error):
            parse(content)
        return

    record = parse(content)

    current = record.current_default_build
    assert (None if current is None else current.build_id) == expected_current


def test_the_refusal_names_the_record_without_quoting_it_whole() -> None:
    with pytest.raises(refused_error()) as raised:
        parse(_changed("unlisted-current"))

    assert "releases.json" in str(raised.value)
    assert "is not listed" in str(raised.value)
