"""Shared validation for the graph's EXPAND runtime conventions.

EXPAND kernels encode structural cell overlays and following ownership claims
in normative ``Node.params`` entries.  Those conventions remain outside the
frozen declaration interface while this module gives population handling,
execution, and static presentation schemas one interpretation of them.
"""

from __future__ import annotations

from .decl import DTYPES, GraphError, Node

__all__ = ["declared_expand_cells", "materialized_expand_coordinates"]


def materialized_expand_coordinates(
    node: Node,
) -> frozenset[tuple[str, str]]:
    """Return the carried EXPAND cells an ordinary node claims as outputs.

    ``materialized_expand_outputs`` is a reserved node parameter used when an
    EXPAND kernel physically installs a new column before a following ordinary
    node gives that column an ownership declaration. The claim node therefore
    receives the existing values as inputs even though the coordinate is not a
    ``Slice`` or rewrite.
    """

    raw_materialized = node.params.get("materialized_expand_outputs", ())
    if not isinstance(raw_materialized, tuple) or any(
        not isinstance(value, str) or "." not in value for value in raw_materialized
    ):
        raise GraphError(
            f"Node {node.id!r} params['materialized_expand_outputs'] must be a "
            "tuple of 'entity.column' strings."
        )
    materialized: set[tuple[str, str]] = set()
    owned_by_coordinate = {
        (output.entity, output.column): output for output in node.outputs
    }
    for value in raw_materialized:
        entity, column = value.split(".", 1)
        coordinate = (entity, column)
        output = owned_by_coordinate.get(coordinate)
        if output is None or output.rewrite:
            raise GraphError(
                f"Node {node.id!r} materialized EXPAND output {value!r} must be "
                "one of its non-rewrite owned cells."
            )
        materialized.add(coordinate)
    if len(materialized) != len(raw_materialized):
        raise GraphError(f"Node {node.id!r} repeats a materialized EXPAND output.")
    return frozenset(materialized)


def declared_expand_cells(node: Node) -> tuple[tuple[str, str, str], ...]:
    """Return the normative ``(entity, column, dtype)`` EXPAND overlays."""

    raw = node.params.get("expand_cells")
    if not isinstance(raw, tuple):
        raise GraphError(f"EXPAND node {node.id!r} needs tuple params['expand_cells'].")
    cells: list[tuple[str, str, str]] = []
    for item in raw:
        if (
            not isinstance(item, tuple)
            or len(item) != 3
            or any(not isinstance(part, str) or not part for part in item)
        ):
            raise GraphError(
                f"EXPAND node {node.id!r} has malformed expand_cells entry {item!r}."
            )
        entity, column, dtype = item
        if "." in entity or "." in column:
            raise GraphError(
                f"EXPAND node {node.id!r} params['expand_cells'] entity and "
                f"column names must be dot-free; got {entity!r}, {column!r}."
            )
        if dtype not in DTYPES:
            raise GraphError(f"Unknown graph dtype token {dtype!r}.")
        cells.append((entity, column, dtype))
    if len({(entity, column) for entity, column, _ in cells}) != len(cells):
        raise GraphError(f"EXPAND node {node.id!r} repeats an expanded cell.")
    return tuple(cells)
