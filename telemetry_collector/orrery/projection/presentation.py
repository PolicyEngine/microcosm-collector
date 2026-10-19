"""Country-neutral, producer-supplied graph presentation contract."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from .schema import _plain_json

PRESENTATION_EXTENSION = "microcosm.presentation"
PRESENTATION_PROTOCOL = "microcosm.graph.presentation.v1"


def presentation_id(*parts: str) -> str:
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Graph evidence: {message}.")


def text(value: object, label: str) -> str:
    require(type(value) is str and bool(value.strip()), f"{label} must be text")
    return value


def digest(value: object) -> str:
    require(
        type(value) is str
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value),
        "expected a lowercase SHA-256 digest",
    )
    return value


def source_reference(value: object) -> dict:
    require(type(value) is dict, "source reference must be an object")
    require(set(value) <= {"label", "url", "sha256"}, "unknown source reference field")
    text(value.get("label"), "reference label")
    if "url" in value:
        url = text(value["url"], "reference URL")
        parsed = urlsplit(url)
        require(
            parsed.scheme in {"", "http", "https"}
            and (bool(parsed.netloc) if parsed.scheme else not parsed.netloc)
            and not url.startswith("//")
            and "\\" not in url
            and not any(ord(c) < 32 for c in url),
            "unsupported reference URL",
        )
    if "sha256" in value:
        digest(value["sha256"])
    return value


def validate_presentation(schema: dict) -> dict | None:
    """Validate containment and references without changing compiler facts."""
    value = schema["extensions"].get(PRESENTATION_EXTENSION)
    if value is None:
        return None
    value = _plain_json(value)
    require(type(value) is dict, "presentation must be an object")
    require(value.get("protocol") == PRESENTATION_PROTOCOL, "presentation protocol")
    require(
        set(value) <= {"protocol", "scope", "groups", "operations", "sources"},
        "unknown presentation field",
    )
    operations = {node["id"] for node in schema["graph"]["nodes"]}
    sources = {source["name"] for source in schema["graph"]["sources"]}
    groups = value.get("groups", [])
    require(type(groups) is list, "groups must be an array")
    by_id = {}
    assigned = set()
    for group in groups:
        require(type(group) is dict, "group must be an object")
        require(
            set(group)
            <= {"id", "label", "description", "parent", "operations", "sources"},
            "unknown group field",
        )
        identity = text(group.get("id"), "group id")
        require(identity not in by_id, "duplicate group id")
        text(group.get("label"), "group label")
        if "description" in group:
            text(group["description"], "group description")
        by_id[identity] = group
        for kind, available in (("operations", operations), ("sources", sources)):
            members = group.get(kind, [])
            require(type(members) is list, "group members must be an array")
            for member in members:
                require(
                    type(member) is str and member in available, "unknown group member"
                )
                coordinate = (kind, member)
                require(coordinate not in assigned, "duplicate group membership")
                assigned.add(coordinate)
    for identity in by_id:
        seen = set()
        current = identity
        while current is not None:
            require(current in by_id, "unknown group parent")
            require(current not in seen, "cyclic group containment")
            seen.add(current)
            current = by_id[current].get("parent")
    for kind, available in (("operations", operations), ("sources", sources)):
        records = value.get(kind, {})
        require(type(records) is dict, "presentation records must be an object")
        for identity, record in records.items():
            require(identity in available, "unknown presentation subject")
            require(type(record) is dict, "presentation record must be an object")
            require(
                set(record) <= {"label", "description", "composite", "references"},
                "unknown presentation record field",
            )
            for field in ("label", "description"):
                if field in record:
                    text(record[field], field)
            if "composite" in record:
                composite = record["composite"]
                require(type(composite) is dict, "composite must be an object")
                unit = composite.get("execution_unit")
                require(
                    unit is None or unit in {"single", "composite"},
                    "composite execution_unit",
                )
                members = composite.get("operations", [])
                require(
                    type(members) is list
                    and all(
                        type(item) is dict and type(item.get("kind")) is str
                        for item in members
                    ),
                    "composite operations",
                )
                for field in ("stage", "node", "kernel", "coupling", "randomness"):
                    if composite.get(field) is not None:
                        text(composite[field], f"composite {field}")
            refs = record.get("references", [])
            require(type(refs) is list, "references must be an array")
            for ref in refs:
                source_reference(ref)
    scope = value.get("scope", {})
    require(type(scope) is dict, "scope must be an object")
    require(
        set(scope) <= {"id", "label", "description", "boundaries"},
        "unknown scope field",
    )
    for field in ("id", "label", "description"):
        if field in scope:
            text(scope[field], f"scope {field}")
    boundaries = scope.get("boundaries", [])
    require(type(boundaries) is list, "boundaries must be an array")
    seen = set()
    for boundary in boundaries:
        require(type(boundary) is dict, "boundary must be an object")
        require(
            set(boundary) <= {"operation", "kind", "upstream", "missing"},
            "unknown boundary field",
        )
        node = boundary.get("operation")
        require(type(node) is str and node in operations, "unknown boundary operation")
        require(node not in seen, "duplicate scope boundary")
        seen.add(node)
        text(boundary.get("kind"), "boundary kind")
        refs = boundary.get("upstream", [])
        require(type(refs) is list, "upstream references must be an array")
        for ref in refs:
            source_reference(ref)
            require(
                "sha256" in ref and "url" in ref,
                "upstream reference needs URL and digest",
            )
        require(
            bool(refs) or "missing" in boundary,
            "boundary needs evidence or a missing reason",
        )
        if "missing" in boundary:
            text(boundary["missing"], "missing upstream reason")
    return value
