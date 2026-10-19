"""Canonical, compiler-derived metadata for graph presentation adapters.

The schema contains declarations and compile-time relationships only. It does
not contain Frame values, source bytes, kernel results, cache state, or runtime
receipts. ``input_bindings`` describe the declared fields an operation can
read; they do not claim that a kernel actually read every supplied value.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

from .canonical import canonical_json
from .decl import (
    ROWS_ALL,
    CompiledGraph,
    GraphError,
    Node,
    Owned,
    StructuralDelta,
    compile_graph,
)
from .expansion import declared_expand_cells, materialized_expand_coordinates
from .serialize import graph_from_json, graph_to_json

__all__ = ["graph_schema", "validate_graph_schema"]

PROTOCOL = "microcosm.graph.schema.v1"
_ROOT_KEYS = {
    "protocol",
    "country",
    "graph_sha256",
    "graph",
    "compiled",
    "fields",
    "input_bindings",
    "extensions",
}
_MAX_ITEMS = 2_000_000
_MAX_BYTES = 32 * 1024 * 1024
_MAX_DEPTH = 64
# Operation parameters can retain serialized gate contracts larger than 64 KiB.
# Keep a scalar bound as well as the total metadata and serialized byte bounds.
_MAX_STRING = 128 * 1024


def _plain_json(value: object) -> object:
    """Return a bounded detached JSON value without invoking custom objects."""

    items = 0
    charge = 0

    def walk(child: object, depth: int) -> object:
        nonlocal items, charge
        items += 1
        charge += 16
        if items > _MAX_ITEMS or depth > _MAX_DEPTH:
            raise ValueError("Graph schema exceeds the metadata complexity limit.")
        kind = type(child)
        if child is None or kind is bool:
            result = child
        elif kind is str:
            if len(child) > _MAX_STRING:
                raise ValueError("Graph schema string exceeds the length limit.")
            charge += len(child.encode("utf-8"))
            result = child
        elif kind is int:
            if child.bit_length() > 1024:
                raise ValueError("Graph schema integer exceeds the size limit.")
            charge += child.bit_length()
            result = child
        elif kind is float:
            if not math.isfinite(child):
                raise ValueError("Graph schema requires finite numbers.")
            result = child
        elif kind is list:
            result = [walk(item, depth + 1) for item in child]
        elif kind is dict:
            if not all(type(key) is str for key in child):
                raise ValueError("Graph schema requires string object keys.")
            result = {
                walk(key, depth + 1): walk(item, depth + 1)
                for key, item in child.items()
            }
        else:
            raise ValueError("Graph schema accepts plain JSON values only.")
        if charge > _MAX_BYTES:
            raise ValueError("Graph schema exceeds the metadata size limit.")
        return result

    copied = walk(value, 0)
    if len(canonical_json(copied)) > _MAX_BYTES:
        raise ValueError("Graph schema exceeds the serialized size limit.")
    return copied


def _compiled_payload(compiled: CompiledGraph) -> dict[str, object]:
    return {
        "order": list(compiled.order),
        "versions": dict(sorted(compiled.versions.items())),
        "predecessors": {
            node_id: list(compiled.predecessors[node_id])
            for node_id in sorted(compiled.predecessors)
        },
        "owners": [
            [*coordinate, owner]
            for coordinate, owner in sorted(compiled.owners.items())
        ],
    }


def _require_compiler_result(compiled: CompiledGraph) -> CompiledGraph:
    if not isinstance(compiled, CompiledGraph):
        raise TypeError(
            f"graph_schema expects CompiledGraph, got {type(compiled).__name__}"
        )
    expected = compile_graph(compiled.graph)
    if _compiled_payload(compiled) != _compiled_payload(expected):
        raise GraphError("CompiledGraph metadata disagrees with compile_graph output.")
    return expected


def _owned(node: Node, entity: str, column: str) -> Owned:
    matches = [
        owned
        for owned in node.outputs
        if owned.entity == entity and owned.column == column
    ]
    if len(matches) != 1:
        raise GraphError(
            f"Compiler owner {node.id!r} has no unique declaration for "
            f"{entity}.{column}."
        )
    return matches[0]


def _declaration(
    compiled: CompiledGraph,
    by_id: Mapping[str, Node],
    version: str,
    entity: str,
    column: str,
    *,
    exclude_owner: str | None = None,
) -> tuple[str, Owned]:
    """Find the nearest declaration visible in a population version."""

    current = version
    while True:
        owner = compiled.owners.get((current, entity, column))
        if owner is not None and owner != exclude_owner:
            return owner, _owned(by_id[owner], entity, column)
        holder = by_id[current]
        if holder.structural is StructuralDelta.CREATE:
            raise GraphError(
                f"Compiler metadata has no declaration for {entity}.{column} "
                f"in population {version!r}."
            )
        assert holder.base is not None
        current = holder.base


def _field(
    population: str,
    provider: str,
    declared_in: str,
    owned: Owned,
) -> dict[str, object]:
    return {
        "population": population,
        "entity": owned.entity,
        "column": owned.column,
        "dtype": owned.dtype,
        "provider": provider,
        "declared_in": declared_in,
        "rows": owned.rows,
        "ownership": owned.ownership.value,
        "rewrite": owned.rewrite,
    }


def _fields(
    compiled: CompiledGraph, by_id: Mapping[str, Node]
) -> list[dict[str, object]]:
    coordinates: dict[str, set[tuple[str, str]]] = {}
    fields: list[dict[str, object]] = []
    for population in compiled.order:
        holder = by_id[population]
        if holder.structural is StructuralDelta.NONE:
            continue
        visible = (
            set()
            if holder.structural is StructuralDelta.CREATE
            else set(coordinates[holder.base])  # type: ignore[index]
        )
        visible.update(
            (entity, column)
            for version, entity, column in compiled.owners
            if version == population
        )
        coordinates[population] = visible
        for entity, column in sorted(visible):
            owner = compiled.owners.get((population, entity, column))
            declared_in, owned = _declaration(
                compiled, by_id, population, entity, column
            )
            fields.append(
                _field(
                    population,
                    population if owner is None else owner,
                    declared_in,
                    owned,
                )
            )
    return fields


def _binding(
    compiled: CompiledGraph,
    by_id: Mapping[str, Node],
    node: Node,
    population: str,
    entity: str,
    column: str,
    kind: str,
    rows: str,
) -> dict[str, str]:
    owner = compiled.owners.get((population, entity, column))
    exclude_owner = node.id if owner == node.id else None
    declared_in, _ = _declaration(
        compiled,
        by_id,
        population,
        entity,
        column,
        exclude_owner=exclude_owner,
    )
    return {
        "node": node.id,
        "population": population,
        "entity": entity,
        "column": column,
        "provider": population if exclude_owner is not None else owner or population,
        "declared_in": declared_in,
        "kind": kind,
        "rows": rows,
    }


def _input_bindings(
    compiled: CompiledGraph, by_id: Mapping[str, Node]
) -> list[dict[str, str]]:
    bindings: list[dict[str, str]] = []
    for node_id in compiled.order:
        node = by_id[node_id]
        if node.structural is StructuralDelta.CREATE:
            continue
        population = (
            compiled.versions[node.id]
            if node.structural is StructuralDelta.NONE
            else node.base
        )
        assert population is not None
        for slice_ in node.inputs:
            for column in slice_.columns:
                bindings.append(
                    _binding(
                        compiled,
                        by_id,
                        node,
                        population,
                        slice_.entity,
                        column,
                        "slice",
                        slice_.rows,
                    )
                )
            if slice_.rows != ROWS_ALL:
                bindings.append(
                    _binding(
                        compiled,
                        by_id,
                        node,
                        population,
                        slice_.entity,
                        slice_.rows,
                        "slice_mask",
                        ROWS_ALL,
                    )
                )
        for owned in node.outputs:
            if owned.rewrite:
                bindings.append(
                    _binding(
                        compiled,
                        by_id,
                        node,
                        population,
                        owned.entity,
                        owned.column,
                        "rewrite_incumbent",
                        owned.rows,
                    )
                )
            if owned.rows != ROWS_ALL:
                bindings.append(
                    _binding(
                        compiled,
                        by_id,
                        node,
                        population,
                        owned.entity,
                        owned.rows,
                        "output_mask",
                        ROWS_ALL,
                    )
                )
        expanded_dtypes: dict[tuple[str, str], str] = {}
        if "materialized_expand_outputs" in node.params:
            holder = by_id[population]
            if holder.structural is not StructuralDelta.EXPAND:
                raise GraphError(
                    f"Node {node.id!r} uses materialized_expand_outputs on "
                    f"population {population!r}, which is not an EXPAND version."
                )
            expanded_dtypes = {
                (entity, column): dtype
                for entity, column, dtype in declared_expand_cells(holder)
            }
        for entity, column in sorted(materialized_expand_coordinates(node)):
            if compiled.owners.get((population, entity, column)) != node.id:
                raise GraphError(
                    f"Node {node.id!r} does not uniquely own materialized EXPAND "
                    f"output {entity}.{column} in population {population!r}."
                )
            owned = _owned(node, entity, column)
            expanded_dtype = expanded_dtypes.get((entity, column))
            if expanded_dtype is None:
                raise GraphError(
                    f"EXPAND node {population!r} does not declare materialized "
                    f"output {entity}.{column} claimed by node {node.id!r}."
                )
            if expanded_dtype != owned.dtype:
                raise GraphError(
                    f"EXPAND node {population!r} declares materialized output "
                    f"{entity}.{column} as {expanded_dtype!r}, but claimant "
                    f"{node.id!r} owns it as {owned.dtype!r}."
                )
            bindings.append(
                {
                    "node": node.id,
                    "population": population,
                    "entity": entity,
                    "column": column,
                    "provider": population,
                    "declared_in": node.id,
                    "kind": "materialized_expand_output",
                    "rows": owned.rows,
                }
            )
    return bindings


def graph_schema(
    compiled: CompiledGraph,
    *,
    extensions: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Project a validated ``CompiledGraph`` into portable static metadata.

    ``input_bindings`` and field providers are derived from the same ownership
    tables used by execution. No Frame or kernel is loaded, and no runtime value
    is included.
    """

    compiled = _require_compiler_result(compiled)
    extension_value: object = {} if extensions is None else dict(extensions)
    detached_extensions = _plain_json(extension_value)
    if type(detached_extensions) is not dict:
        raise ValueError("Graph schema extensions must be an object.")
    graph_text = graph_to_json(compiled.graph)
    graph_value = json.loads(graph_text)
    by_id = {node.id: node for node in compiled.graph.nodes}
    result = {
        "protocol": PROTOCOL,
        "country": compiled.graph.country,
        "graph_sha256": hashlib.sha256(graph_text.encode("utf-8")).hexdigest(),
        "graph": graph_value,
        "compiled": _compiled_payload(compiled),
        "fields": _fields(compiled, by_id),
        "input_bindings": _input_bindings(compiled, by_id),
        "extensions": detached_extensions,
    }
    return _plain_json(result)  # type: ignore[return-value]


def validate_graph_schema(value: object) -> dict[str, object]:
    """Recompile and return a detached canonical schema or reject it.

    Only ``extensions`` is producer-defined. Every other field must equal the
    result derived from the embedded declaration by :func:`compile_graph`.
    """

    document = _plain_json(value)
    if type(document) is not dict:
        raise ValueError("Graph schema must be an object.")
    if set(document) != _ROOT_KEYS:
        raise ValueError("Graph schema root fields do not match the protocol.")
    if document.get("protocol") != PROTOCOL:
        raise ValueError("Graph schema protocol is unsupported.")
    graph_value = document.get("graph")
    if type(graph_value) is not dict:
        raise ValueError("Graph schema graph must be an object.")
    extensions = document.get("extensions")
    if type(extensions) is not dict:
        raise ValueError("Graph schema extensions must be an object.")
    restored = graph_from_json(canonical_json(graph_value).decode("utf-8"))
    expected = graph_schema(compile_graph(restored), extensions=extensions)
    if canonical_json(document) != canonical_json(expected):
        raise ValueError("Graph schema core metadata disagrees with its graph.")
    return expected
