"""Adapter from compiler metadata and recorded evidence to Orrery.

Static export never reads population data. A supplied execution overlay is
projected as recorded; it is never verified against a content store here.

Copied from Microcosm's ``microcosm.graph.orrery`` without its command-line
entry point and its ``Graph`` input functions (see the package docstring).
"""

from __future__ import annotations

import hashlib
import json
import math

from .canonical import canonical_json
from .orrery_evidence import apply_contracts
from .presentation import presentation_id, validate_presentation
from .schema import validate_graph_schema

__all__ = [
    "orrery_document_from_schema",
    "orrery_json_from_schema",
]

_SAFE_INTEGER = 2**53 - 1
_MAX_INPUT_BYTES = 32 * 1024 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_MAX_ITEMS = 2_000_000
_MAX_NODES = 20_000
_MAX_EDGES = 100_000
_FIELD_KEYS = ("population", "entity", "column", "provider", "declared_in")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Orrery adapter: {message}.")


def _transport_json(value: object) -> object:
    """Detach JSON and tag numeric values JavaScript cannot preserve exactly."""

    items = 0
    charge = 0

    def walk(child: object, depth: int) -> object:
        nonlocal items, charge
        items += 1
        charge += 16
        _require(items <= _MAX_ITEMS and depth <= 64, "metadata complexity")
        kind = type(child)
        if child is None or kind is bool:
            result = child
        elif kind is str:
            _require(len(child) <= 128 * 1024, "string length")
            charge += len(child.encode("utf-8"))
            result = child
        elif kind is int:
            _require(child.bit_length() <= 1024, "integer size")
            charge += child.bit_length()
            result = (
                {"integer_literal": str(child)} if abs(child) > _SAFE_INTEGER else child
            )
        elif kind is float:
            _require(math.isfinite(child), "finite numbers required")
            result = (
                {"float_literal": repr(child)}
                if child.is_integer() and abs(child) > _SAFE_INTEGER
                else child
            )
        elif kind is list:
            result = [walk(item, depth + 1) for item in child]
        elif kind is dict:
            _require(all(type(key) is str for key in child), "string keys required")
            result = {
                walk(key, depth + 1): walk(item, depth + 1)
                for key, item in child.items()
            }
        else:
            raise ValueError("Orrery adapter: plain JSON values required.")
        _require(charge <= _MAX_OUTPUT_BYTES, "prospective metadata size")
        return result

    return walk(value, 0)


def _object(value: object, label: str) -> dict[str, object]:
    _require(type(value) is dict, f"{label} must be an object")
    return value  # type: ignore[return-value]


def _array(value: object, label: str) -> list[object]:
    _require(type(value) is list, f"{label} must be an array")
    return value  # type: ignore[return-value]


def _text(value: object, label: str) -> str:
    _require(type(value) is str and bool(value.strip()), f"{label} must be text")
    return value  # type: ignore[return-value]


def _index(values: object, key: str, label: str) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in _array(values, label):
        record = _object(item, f"{label} item")
        identity = _text(record.get(key), f"{label} {key}")
        _require(identity not in result, f"duplicate {label} {key}")
        result[identity] = record
    return result


def _field_id(field: dict[str, object]) -> str:
    return presentation_id(
        "field", *(_text(field.get(key), f"field {key}") for key in _FIELD_KEYS)
    )


def _revision(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def _bounded_json(value: object, limit: int) -> str:
    parts: list[str] = []
    size = 0
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    for part in encoder.iterencode(value):
        size += len(part.encode("utf-8"))
        _require(size <= limit, "serialized metadata size")
        parts.append(part)
    return "".join(parts)


def _owned_declarations(
    operations: dict[str, dict[str, object]],
) -> dict[tuple[str, str, str], dict[str, object]]:
    result: dict[tuple[str, str, str], dict[str, object]] = {}
    for node_id, operation in operations.items():
        for value in _array(operation.get("outputs"), f"operation {node_id} outputs"):
            owned = _object(value, f"operation {node_id} output")
            key = (
                node_id,
                _text(owned.get("entity"), "output entity"),
                _text(owned.get("column"), "output column"),
            )
            _require(key not in result, "duplicate Owned declaration")
            result[key] = {**owned, "rewrite": owned.get("rewrite", False)}
    return result


def orrery_document_from_schema(
    schema: object, *, title: str | None = None, execution: object | None = None
) -> dict[str, object]:
    """Transform compiler metadata into a complete Orrery document.

    The schema is recompiled before projection. Stable node and edge identities
    are derived from declared coordinates and relationships. Revisions are
    content digests, not runtime cache keys or verification claims.
    """

    document = validate_graph_schema(schema)
    graph = _object(document["graph"], "graph")
    compiled = _object(document["compiled"], "compiled")
    operations = _index(graph.get("nodes"), "id", "operations")
    sources = _index(graph.get("sources"), "name", "sources")
    versions = _object(compiled.get("versions"), "compiled versions")
    predecessors = _object(compiled.get("predecessors"), "compiled predecessors")
    declarations = _owned_declarations(operations)
    contract = validate_presentation(document)
    scope = (contract or {}).get("scope", {})
    country = _text(document["country"], "country")
    # Records carry their own declarations; the complete compiler schema is the
    # separately saved canonical file, bound here by digest rather than embedded.
    schema_summary = _object(
        _transport_json(
            {
                "protocol": document["protocol"],
                "country": country,
                "graph_sha256": document["graph_sha256"],
                "schema_sha256": hashlib.sha256(canonical_json(document)).hexdigest(),
                "compiled_order": list(_array(compiled.get("order"), "compiled order")),
                "counts": {
                    "operations": len(operations),
                    "sources": len(sources),
                    "fields": len(_array(document["fields"], "fields")),
                    "input_bindings": len(
                        _array(document["input_bindings"], "input bindings")
                    ),
                },
                "extensions": document["extensions"],
            }
        ),
        "schema summary",
    )

    nodes: dict[str, dict[str, object]] = {}
    edges: dict[str, dict[str, object]] = {}
    used = (
        len(_bounded_json(schema_summary, _MAX_OUTPUT_BYTES).encode("utf-8")) + 65_536
    )

    def presentation_record(record: dict[str, object]) -> dict[str, object]:
        nonlocal used
        transported = _object(_transport_json(record), "presentation record")
        transported["revision"] = _revision(transported)
        encoded = _bounded_json(transported, _MAX_OUTPUT_BYTES - used)
        used += len(encoded.encode("utf-8")) + 1
        return transported

    def add_node(node: dict[str, object]) -> None:
        identity = _text(node.get("id"), "presentation node id")
        _require(identity not in nodes, "duplicate presentation node")
        _require(len(nodes) < _MAX_NODES, "node limit")
        nodes[identity] = presentation_record(node)

    def add_edge(
        source: str,
        target: str,
        kind: str,
        category: str,
        data: dict[str, object] | None = None,
        label: str | None = None,
    ) -> None:
        facts = {} if data is None else data
        identity = presentation_id(
            "edge", kind, source, target, _bounded_json(facts, _MAX_OUTPUT_BYTES)
        )
        if identity in edges:
            return
        _require(len(edges) < _MAX_EDGES, "edge limit")
        record = {
            "id": identity,
            "source": source,
            "target": target,
            "kind": kind,
            "category": category,
            "data": facts,
        }
        if label is not None:
            record["label"] = label
        edges[identity] = presentation_record(record)

    for node_id, operation in operations.items():
        node: dict[str, object] = {
            "id": presentation_id("operation", node_id),
            "label": node_id,
            "kind": "operation",
            "data": {
                "declaration": operation,
                "population": versions[node_id],
            },
        }
        if operation.get("description"):
            node["description"] = operation["description"]
        add_node(node)
    for source_name, source in sources.items():
        node = {
            "id": presentation_id("source", source_name),
            "label": source_name,
            "kind": "source",
            "data": {"declaration": source},
        }
        if source.get("description"):
            node["description"] = source["description"]
        add_node(node)

    fields: dict[str, dict[str, object]] = {}
    visible: dict[tuple[str, str, str], str] = {}

    def add_field(field: dict[str, object], *, visible_field: bool) -> str:
        identity = _field_id(field)
        declared_in = _text(field.get("declared_in"), "field declared_in")
        entity = _text(field.get("entity"), "field entity")
        column = _text(field.get("column"), "field column")
        declaration = declarations[(declared_in, entity, column)]
        for prop in ("dtype", "rows", "ownership", "rewrite"):
            _require(
                field.get(prop) == declaration.get(prop),
                "field declaration disagreement",
            )
        if identity not in fields:
            fields[identity] = field
            provider = _text(field.get("provider"), "field provider")
            population = _text(field.get("population"), "field population")
            description = f"Population version {population}; provided by {provider}"
            if declared_in != provider:
                description += f"; declared in {declared_in}"
            add_node(
                {
                    "id": identity,
                    "label": f"{entity}.{column}",
                    "kind": "field",
                    # Containment under the provider keeps a folded operation's
                    # versioned fields folded with it; provenance stays an edge.
                    "parentId": presentation_id("operation", provider),
                    "description": description,
                    "data": {**field, "visible_in_schema": visible_field},
                }
            )
            add_edge(
                presentation_id("operation", provider),
                identity,
                "provided_field",
                "provenance",
            )
        else:
            _require(fields[identity] == field, "ambiguous field value")
        return identity

    for value in _array(document["fields"], "fields"):
        field = _object(value, "field")
        coordinate = (
            _text(field.get("population"), "field population"),
            _text(field.get("entity"), "field entity"),
            _text(field.get("column"), "field column"),
        )
        _require(coordinate not in visible, "duplicate visible field")
        visible[coordinate] = add_field(field, visible_field=True)

    for value in _array(document["input_bindings"], "input bindings"):
        binding = _object(value, "input binding")
        declared_in = _text(binding.get("declared_in"), "binding declared_in")
        entity = _text(binding.get("entity"), "binding entity")
        column = _text(binding.get("column"), "binding column")
        declaration = declarations[(declared_in, entity, column)]
        field = {
            key: binding[key]
            for key in ("population", "entity", "column", "provider", "declared_in")
        }
        field.update(
            {key: declaration[key] for key in ("dtype", "rows", "ownership", "rewrite")}
        )
        field_id = add_field(field, visible_field=False)
        add_edge(
            field_id,
            presentation_id("operation", _text(binding.get("node"), "binding node")),
            "declared_read",
            "dependency",
            {"read_kind": binding["kind"], "rows": binding["rows"]},
            label=f"{_text(binding['kind'], 'binding kind').replace('_', ' ')}"
            f" · rows {_text(binding['rows'], 'binding rows')}",
        )

    for node_id, operation in operations.items():
        target = presentation_id("operation", node_id)
        for parent in _array(predecessors[node_id], f"predecessors for {node_id}"):
            add_edge(
                presentation_id("operation", _text(parent, "predecessor")),
                target,
                "compiled_predecessor",
                "dependency",
            )
        base = operation.get("base")
        if base is not None:
            base_id = _text(base, "structural base")
            for (population, _entity, _column), field_id in visible.items():
                if population == base_id:
                    add_edge(
                        field_id,
                        target,
                        "structural_input",
                        "provenance",
                    )
        for source in _array(operation.get("sources"), f"sources for {node_id}"):
            add_edge(
                presentation_id("source", _text(source, "source reference")),
                target,
                "declared_source",
                "provenance",
            )
        for value in _array(
            operation.get("artifact_inputs", []), f"artifact inputs for {node_id}"
        ):
            binding = _object(value, "artifact input")
            add_edge(
                presentation_id(
                    "operation",
                    _text(binding.get("producer"), "artifact producer"),
                ),
                target,
                "artifact_input",
                "dependency",
                binding,
            )

    _require(
        all(
            edge["source"] in nodes and edge["target"] in nodes
            for edge in edges.values()
        ),
        "dangling presentation edge",
    )
    scope_id = scope.get("id") if type(scope) is dict else None
    scope_label = scope.get("label") if type(scope) is dict else None
    result: dict[str, object] = {
        "schemaVersion": "graph-explorer/v1",
        "id": presentation_id("microcosm", country, *([scope_id] if scope_id else [])),
        "title": _text(title, "title")
        if title is not None
        else f"{country.upper()} · {scope_label or scope_id}"
        if scope_id or scope_label
        else f"{country.upper()} graph schema",
        "revision": _revision(document),
        "description": (
            "Compiler-derived declaration metadata; no execution, verification, "
            "or release result."
        ),
        "nodes": [nodes[key] for key in sorted(nodes)],
        "edges": [edges[key] for key in sorted(edges)],
        "metadata": {
            "adapter": "microcosm.graph.orrery.v1",
            "scope": "complete_compiler_schema",
            "truncated": False,
            "evidence_scope": (
                "The embedded graph was recompiled; no source bytes, kernel "
                "execution, or authenticity check was performed."
            ),
            "revision_scope": (
                "Content digests of declarations and presentation records; not "
                "runtime cache keys."
            ),
            "edge_scope": (
                "Declared operation reads and compiler dependencies; not "
                "individual-output formulas or observed runtime access."
            ),
            "containment_scope": (
                "Groups contain operations and sources; a field is contained by "
                "the operation whose artifact provides it. Containment folds the "
                "canvas and asserts no value dependency."
            ),
            "schema_scope": (
                "Records carry their declarations; the complete compiler schema "
                "is the separately saved canonical graph.schema.json whose "
                "bytes have metadata.microcosm.schema_sha256."
            ),
            "microcosm": schema_summary,
            "missing_runtime_data": [
                "entity identifiers and memberships",
                "field values",
                "source bytes and preparation internals",
                "kernel execution and implicit runtime reads",
                "runtime receipts and release decisions",
            ],
        },
    }
    apply_contracts(result, document, execution)
    _require(len(result["nodes"]) <= _MAX_NODES, "node limit")
    transported_result = _object(_transport_json(result), "Orrery document")
    for node in transported_result["nodes"]:
        node["revision"] = _revision(
            {key: value for key, value in node.items() if key != "revision"}
        )
    _bounded_json(transported_result, _MAX_OUTPUT_BYTES)
    return transported_result


def orrery_json_from_schema(
    schema: object, *, title: str | None = None, execution: object | None = None
) -> str:
    """Return deterministic UTF-8-ready JSON for Orrery."""

    return (
        _bounded_json(
            orrery_document_from_schema(schema, title=title, execution=execution),
            _MAX_OUTPUT_BYTES,
        )
        + "\n"
    )
