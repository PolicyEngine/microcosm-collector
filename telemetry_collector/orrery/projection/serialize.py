"""Canonical JSON serialization for frozen graph declarations."""

from __future__ import annotations

import json
from collections.abc import Mapping

from .canonical import canonical_json
from .decl import (
    ArtifactInput,
    ArtifactOutput,
    ArtifactType,
    Graph,
    Node,
    Owned,
    Ownership,
    Param,
    Slice,
    SourceRef,
    StructuralDelta,
    WeightTransition,
    WeightUpdate,
)

__all__ = ["graph_from_json", "graph_to_json"]


def graph_to_json(graph: Graph) -> str:
    """Serialize ``graph`` losslessly as canonical declaration JSON."""

    if not isinstance(graph, Graph):
        raise TypeError(f"graph_to_json expects Graph, got {type(graph).__name__}")
    payload = {
        "country": graph.country,
        "sources": [
            {
                "name": source.name,
                "codec": source.codec,
                "description": source.description,
            }
            for source in graph.sources
        ],
        "nodes": [_node_payload(node) for node in graph.nodes],
        # Amendment 12: present only when declared, so a declaration written
        # before the amendment serializes byte for byte as it did.
        **(
            {}
            if graph.mass_partition is None
            else {"mass_partition": list(graph.mass_partition)}
        ),
    }
    return canonical_json(payload).decode("utf-8")


def graph_from_json(text: str) -> Graph:
    """Restore a :class:`Graph` from :func:`graph_to_json` output."""

    if not isinstance(text, str):
        raise TypeError(f"graph_from_json expects str, got {type(text).__name__}")
    try:
        raw = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as error:
        raise ValueError("graph JSON is not valid JSON") from error
    root = _mapping(raw, "graph")
    fields = {"country", "sources", "nodes"}
    if "mass_partition" in root:
        fields.add("mass_partition")
    _exact_fields(root, fields, "graph")
    sources_raw = _array(root["sources"], "graph.sources")
    nodes_raw = _array(root["nodes"], "graph.nodes")
    return Graph(
        country=_string(root["country"], "graph.country"),
        sources=tuple(
            _source_from_payload(value, index)
            for index, value in enumerate(sources_raw)
        ),
        nodes=tuple(
            _node_from_payload(value, index) for index, value in enumerate(nodes_raw)
        ),
        mass_partition=_partition_from_payload(
            root.get("mass_partition"), "graph.mass_partition"
        ),
    )


def _partition_from_payload(value: object, label: str) -> tuple[str, str] | None:
    if value is None:
        return None
    parts = _array(value, label)
    if len(parts) != 2:
        raise TypeError(f"{label} must be an [entity, column] pair")
    return (_string(parts[0], f"{label}[0]"), _string(parts[1], f"{label}[1]"))


def _node_payload(node: Node) -> dict[str, object]:
    return {
        # Elided when empty, so a graph declaring no artifact edges
        # serializes exactly as it did before amendment 19.
        **(
            {
                "artifact_inputs": [
                    {
                        "name": item.name,
                        "producer": item.producer,
                        "artifact": item.artifact,
                        "type": _artifact_type_payload(item.type),
                    }
                    for item in node.artifact_inputs
                ]
            }
            if node.artifact_inputs
            else {}
        ),
        **(
            {
                "artifact_outputs": [
                    {
                        "name": item.name,
                        "type": _artifact_type_payload(item.type),
                    }
                    for item in node.artifact_outputs
                ]
            }
            if node.artifact_outputs
            else {}
        ),
        "id": node.id,
        "kernel": node.kernel,
        "inputs": [
            {
                "entity": slice_.entity,
                "columns": list(slice_.columns),
                "rows": slice_.rows,
            }
            for slice_ in node.inputs
        ],
        "outputs": [
            {
                "entity": owned.entity,
                "column": owned.column,
                "dtype": owned.dtype,
                "rows": owned.rows,
                "ownership": owned.ownership.value,
                **({"rewrite": True} if owned.rewrite else {}),
            }
            for owned in node.outputs
        ],
        "params": dict(node.params),
        "population": node.population,
        "structural": node.structural.value,
        "base": node.base,
        "sources": list(node.sources),
        "weights": _weights_payload(node.weights),
        "mass": node.mass,
        **({"entrants": True} if node.entrants else {}),
        "description": node.description,
        "citation": node.citation,
    }


def _source_from_payload(value: object, index: int) -> SourceRef:
    label = f"graph.sources[{index}]"
    payload = _mapping(value, label)
    _exact_fields(payload, {"name", "codec", "description"}, label)
    return SourceRef(
        name=_string(payload["name"], f"{label}.name"),
        codec=_string(payload["codec"], f"{label}.codec"),
        description=_string(payload["description"], f"{label}.description"),
    )


def _node_from_payload(value: object, index: int) -> Node:
    label = f"graph.nodes[{index}]"
    payload = _mapping(value, label)
    fields = {
        "id",
        "kernel",
        "inputs",
        "outputs",
        "params",
        "population",
        "structural",
        "base",
        "sources",
        "weights",
        "mass",
        "description",
        "citation",
    }
    fields.update(
        name for name in ("artifact_inputs", "artifact_outputs") if name in payload
    )
    if "entrants" in payload:
        fields.add("entrants")
    _exact_fields(payload, fields, label)
    entrants = payload.get("entrants", False)
    if not isinstance(entrants, bool):
        raise TypeError(f"{label}.entrants must be a boolean")
    inputs = _array(payload["inputs"], f"{label}.inputs")
    outputs = _array(payload["outputs"], f"{label}.outputs")
    sources = _array(payload["sources"], f"{label}.sources")
    params = _mapping(payload["params"], f"{label}.params")
    population = _optional_string(payload["population"], f"{label}.population")
    base = _optional_string(payload["base"], f"{label}.base")
    return Node(
        artifact_inputs=tuple(
            _artifact_from_payload(value, input_=True)
            for value in _array(
                payload.get("artifact_inputs", []), f"{label}.artifact_inputs"
            )
        ),
        artifact_outputs=tuple(
            _artifact_from_payload(value, input_=False)
            for value in _array(
                payload.get("artifact_outputs", []), f"{label}.artifact_outputs"
            )
        ),
        id=_string(payload["id"], f"{label}.id"),
        kernel=_string(payload["kernel"], f"{label}.kernel"),
        inputs=tuple(
            _slice_from_payload(child, f"{label}.inputs[{child_index}]")
            for child_index, child in enumerate(inputs)
        ),
        outputs=tuple(
            _owned_from_payload(child, f"{label}.outputs[{child_index}]")
            for child_index, child in enumerate(outputs)
        ),
        params={
            _string(name, f"{label}.params key"): _param_from_json(
                child, f"{label}.params[{name!r}]"
            )
            for name, child in params.items()
        },
        population=population,
        structural=StructuralDelta(
            _string(payload["structural"], f"{label}.structural")
        ),
        base=base,
        sources=tuple(
            _string(name, f"{label}.sources[{source_index}]")
            for source_index, name in enumerate(sources)
        ),
        weights=_weights_from_payload(payload["weights"], f"{label}.weights"),
        mass=_string(payload["mass"], f"{label}.mass"),
        entrants=entrants,
        description=_string(payload["description"], f"{label}.description"),
        citation=_string(payload["citation"], f"{label}.citation"),
    )


def _slice_from_payload(value: object, label: str) -> Slice:
    payload = _mapping(value, label)
    _exact_fields(payload, {"entity", "columns", "rows"}, label)
    columns = _array(payload["columns"], f"{label}.columns")
    return Slice(
        entity=_string(payload["entity"], f"{label}.entity"),
        columns=tuple(
            _string(column, f"{label}.columns[{index}]")
            for index, column in enumerate(columns)
        ),
        rows=_string(payload["rows"], f"{label}.rows"),
    )


def _owned_from_payload(value: object, label: str) -> Owned:
    payload = _mapping(value, label)
    fields = {"entity", "column", "dtype", "rows", "ownership"}
    if "rewrite" in payload:
        fields.add("rewrite")
    _exact_fields(payload, fields, label)
    rewrite = payload.get("rewrite", False)
    if not isinstance(rewrite, bool):
        raise TypeError(f"{label}.rewrite must be a boolean")
    return Owned(
        entity=_string(payload["entity"], f"{label}.entity"),
        column=_string(payload["column"], f"{label}.column"),
        dtype=_string(payload["dtype"], f"{label}.dtype"),
        rows=_string(payload["rows"], f"{label}.rows"),
        ownership=Ownership(_string(payload["ownership"], f"{label}.ownership")),
        rewrite=rewrite,
    )


def _weights_payload(
    weights: WeightTransition | WeightUpdate | None,
) -> dict[str, object] | None:
    """Discriminate the two weight declarations by their own field names.

    ``WeightUpdate`` exposes ``to_kind`` as a property, so projecting it
    the way a transition is projected would round-trip it back as a
    transition and silently change what the node means. The transition
    payload is byte-for-byte what it was before amendment 26, so every
    declaration serialized before it restores unchanged.
    """

    if weights is None:
        return None
    if isinstance(weights, WeightUpdate):
        return {
            "entity": weights.entity,
            "kind": weights.kind,
            "reason": weights.reason,
            "mass": weights.mass,
        }
    return {
        "entity": weights.entity,
        "to_kind": weights.to_kind,
        "mass": weights.mass,
    }


def _weights_from_payload(
    value: object, label: str
) -> WeightTransition | WeightUpdate | None:
    if value is None:
        return None
    payload = _mapping(value, label)
    if "kind" in payload:
        _exact_fields(payload, {"entity", "kind", "reason", "mass"}, label)
        return WeightUpdate(
            entity=_string(payload["entity"], f"{label}.entity"),
            kind=_string(payload["kind"], f"{label}.kind"),
            reason=_string(payload["reason"], f"{label}.reason"),
            mass=_string(payload["mass"], f"{label}.mass"),
        )
    _exact_fields(payload, {"entity", "to_kind", "mass"}, label)
    return WeightTransition(
        entity=_string(payload["entity"], f"{label}.entity"),
        to_kind=_string(payload["to_kind"], f"{label}.to_kind"),
        mass=_string(payload["mass"], f"{label}.mass"),
    )


def _param_from_json(value: object, label: str) -> Param:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, list):
        return tuple(
            _param_from_json(child, f"{label}[{index}]")
            for index, child in enumerate(value)
        )
    raise TypeError(f"{label} is not a legal graph parameter")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(name, str) for name in value
    ):
        raise TypeError(f"{label} must be a JSON object with string keys")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be a JSON array")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _exact_fields(
    payload: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(payload) != expected:
        raise ValueError(
            f"{label} fields are {sorted(payload)!r}, expected {sorted(expected)!r}"
        )


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"graph JSON contains non-finite constant {value}")


def _artifact_type_payload(type_: ArtifactType) -> dict[str, object]:
    return {"name": type_.name, "schema_version": type_.schema_version}


def _artifact_from_payload(
    value: object, *, input_: bool
) -> ArtifactInput | ArtifactOutput:
    raw = _mapping(value, "artifact declaration")
    fields = {"name", "type"} | ({"producer", "artifact"} if input_ else set())
    _exact_fields(raw, fields, "artifact declaration")
    type_raw = _mapping(raw["type"], "artifact type")
    _exact_fields(type_raw, {"name", "schema_version"}, "artifact type")
    type_ = ArtifactType(
        _string(type_raw["name"], "artifact type.name"), type_raw["schema_version"]
    )
    name = _string(raw["name"], "artifact name")
    if input_:
        return ArtifactInput(
            name,
            _string(raw["producer"], "artifact producer"),
            _string(raw["artifact"], "artifact output"),
            type_,
        )
    return ArtifactOutput(name, type_)
