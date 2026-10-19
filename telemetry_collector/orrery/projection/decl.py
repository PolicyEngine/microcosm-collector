"""Declarations: the only authority for what a build computes.

Pure, frozen data. No callables, no logic. Everything the executor, the
store, the identity regime, and the documentation know about a build is
derived from one :class:`Graph`. Two kinds of fields exist: normative fields
enter node keys; descriptive fields (:data:`DESCRIPTIVE_FIELDS`) never do.

The population model behind the declarations:

- A ``CREATE`` node turns declared sources into a population version (a
  ``Frame``) and declares every column it loads, so ownership is total from
  the first node. Every other node lives in exactly one population version,
  named by :attr:`Node.population`.
- A structural node (``FILTER``, ``EXPAND``, ``REWEIGHT``) transforms a
  base version into a new version and carries every column of its base;
  it therefore depends on every node of its base version, and a node in
  the new version that reads a carried column depends on the structural
  node, whose artifact is what it actually reads.
- A non-structural node reads declared slices of its version and owns the
  cells it declares. Its predecessors are exactly the owners of the columns
  it reads. Chained imputation is expressed by listing an earlier target's
  column as an input; there is no other ordering mechanism.
- Every column's dtype is declared by its owner, so a row mask's dtype is
  known at compile time: a mask that is not ``bool`` or ``boolean`` is a
  compile error (charter D4). Nulls inside a nullable mask are a run-time
  rejection by the executor.
- An ``EXPAND`` node copies rows: every new row names the base row it
  copies, so lineage is total. A node that declares ``entrants=True`` may
  also add rows that copy nothing (births not patterned on a parent,
  immigrant cohorts); the kernel materializes every carried column for
  such a row, the executor records them as entrants rather than copies,
  and the node cannot claim to conserve mass (amendment 11).
- Mass is accounted as weighted person mass per stratum. A graph may name
  a partition column (:attr:`Graph.mass_partition`, e.g. a period on a
  person-period population); the ledger then reports per stratum within
  each partition and ``conserve`` holds within each partition, so a row
  contributes mass only to the partitions it exists in (amendment 12).

This file is a frozen interface (see ``docs/graph-acceptance.md``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import StrEnum
from types import MappingProxyType

__all__ = [
    "ArtifactInput",
    "ArtifactOutput",
    "ArtifactType",
    "DESCRIPTIVE_FIELDS",
    "DTYPES",
    "GATE_OUTCOMES",
    "MASK_DTYPES",
    "MASS_POLICIES",
    "PARTITION_DTYPES",
    "ROWS_ALL",
    "WEIGHT_KINDS",
    "WEIGHT_UPDATE_MASS_POLICIES",
    "CompiledGraph",
    "Graph",
    "GraphError",
    "Node",
    "Owned",
    "Ownership",
    "Param",
    "Slice",
    "SourceRef",
    "StructuralDelta",
    "WeightTransition",
    "WeightUpdate",
    "compile_graph",
]

type Param = bool | int | float | str | None | tuple["Param", ...]

#: Fields that never enter a node key. Everything else on a declaration is
#: normative.
DESCRIPTIVE_FIELDS = frozenset({"description", "citation"})

#: Closed set of dtype tokens a node may own. The executor maps them to
#: pandas dtypes; ``boolean`` and ``Int64`` are the nullable kinds.
DTYPES = frozenset(
    {"bool", "boolean", "int32", "int64", "Int64", "float32", "float64", "string"}
)

#: The dtypes a row mask may have.
MASK_DTYPES = frozenset({"bool", "boolean"})

#: Row scope meaning "every row of the entity".
ROWS_ALL = "all"

#: Legal weight kinds, in transition order.
WEIGHT_KINDS = ("design", "importance", "calibrated")

#: Mass policies a weight transition or structural node may declare.
MASS_POLICIES = frozenset({"conserve", "free", "declared"})

#: Mass policies a same-kind :class:`WeightUpdate` may declare. ``free`` is
#: deliberately absent (amendment 26).
WEIGHT_UPDATE_MASS_POLICIES = frozenset({"conserve", "declared"})

#: The dtypes a mass-partition column may have.
PARTITION_DTYPES = frozenset({"int32", "int64", "string"})

#: The closed set of gate outcomes (charter F4). ``unreached`` is also the
#: outcome of a release whose required human decisions are absent.
GATE_OUTCOMES = ("pass", "fail", "evidence_absent", "not_applicable", "unreached")


class GraphError(ValueError):
    """A declaration violates a compile-time invariant."""


class Ownership(StrEnum):
    """What a node promises about the cells it owns."""

    PRODUCED = "produced"
    ABSENT = "absent"


class StructuralDelta(StrEnum):
    """How a node changes the row set of its population version."""

    NONE = "none"
    CREATE = "create"
    FILTER = "filter"
    EXPAND = "expand"
    REWEIGHT = "reweight"


def _check_param(name: str, value: object) -> None:
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GraphError(f"Parameter {name!r} is not finite: {value!r}.")
        return
    if isinstance(value, tuple):
        for index, item in enumerate(value):
            _check_param(f"{name}[{index}]", item)
        return
    raise GraphError(
        f"Parameter {name!r} has type {type(value).__name__}; parameters are "
        "bool, int, float, str, None, or tuples of those."
    )


def _nonempty(label: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise GraphError(f"{label} must be a non-empty string, got {value!r}.")


def _description(label: str, value: str) -> None:
    """Require an empty sentinel or text containing a non-whitespace character."""

    if not isinstance(value, str) or (value and not value.strip()):
        raise GraphError(
            f"{label} must be empty or contain non-whitespace text, got {value!r}."
        )


def _name(label: str, value: str) -> None:
    """An entity or column name: non-empty and free of ``.``.

    Receipts, keys, and evidence spell a coordinate ``entity.column``; a dot
    inside either part would make two coordinates collide (amendment 15).
    """
    _nonempty(label, value)
    if "." in value:
        raise GraphError(f"{label} may not contain '.', got {value!r}.")


@dataclass(frozen=True)
class SourceRef:
    """A named external input, identified by content at run time.

    Attributes:
        name: The name nodes refer to.
        codec: How bytes become a table (a codec registered with the store).
        description: Descriptive; never hashed.
    """

    name: str
    codec: str
    description: str = ""

    def __post_init__(self) -> None:
        _nonempty("SourceRef.name", self.name)
        _nonempty("SourceRef.codec", self.codec)
        _description("SourceRef.description", self.description)


@dataclass(frozen=True)
class ArtifactType:
    """A nominal, versioned contract over an opaque byte payload.

    The graph never parses the bytes: it carries the name and version a
    producer declared and refuses an edge whose consumer expected another
    pair. Validating the payload itself is the consumer's job (amendment
    19).

    Attributes:
        name: The contract's name, e.g. ``"qrf.forest"``.
        schema_version: A positive integer; a payload change bumps it.
    """

    name: str
    schema_version: int

    def __post_init__(self) -> None:
        _nonempty("ArtifactType.name", self.name)
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise GraphError("ArtifactType.schema_version must be a positive integer.")


@dataclass(frozen=True)
class ArtifactOutput:
    """A named, typed subset of a kernel's opaque byte outputs.

    The bytes still arrive in ``KernelResult.artifacts`` under ``name``; the
    declaration is what makes the output addressable by another node and
    required of the kernel. Undeclared diagnostic bytes remain legal.

    Attributes:
        name: The name the kernel returns the bytes under.
        type: The nominal contract the payload claims to satisfy.
    """

    name: str
    type: ArtifactType

    def __post_init__(self) -> None:
        _nonempty("ArtifactOutput.name", self.name)
        if not isinstance(self.type, ArtifactType):
            raise GraphError("ArtifactOutput.type must be an ArtifactType.")


@dataclass(frozen=True)
class ArtifactInput:
    """A declared artifact edge, with a consumer-local alias and exact type.

    The consumer reads the bytes under its own ``name``, so a node is not
    coupled to what the producer called them. ``compile_graph`` resolves the
    edge and refuses an unknown producer, an undeclared output, or a type
    the producer does not declare.

    Attributes:
        name: The alias the consumer reads in ``KernelContext.artifacts``.
        producer: The node id that declares the output.
        artifact: The producer's declared output name.
        type: The exact :class:`ArtifactType` the producer declares.
    """

    name: str
    producer: str
    artifact: str
    type: ArtifactType

    def __post_init__(self) -> None:
        for field_name in ("name", "producer", "artifact"):
            _nonempty(f"ArtifactInput.{field_name}", getattr(self, field_name))
        if not isinstance(self.type, ArtifactType):
            raise GraphError("ArtifactInput.type must be an ArtifactType.")


@dataclass(frozen=True)
class Slice:
    """What a node reads: columns of one entity, optionally under a row mask.

    Attributes:
        entity: The entity table.
        columns: The columns the kernel receives. Nothing else is visible.
        rows: :data:`ROWS_ALL`, or the name of a boolean column of the same
            entity that must also appear in the node's inputs.
    """

    entity: str
    columns: tuple[str, ...]
    rows: str = ROWS_ALL

    def __post_init__(self) -> None:
        _name("Slice.entity", self.entity)
        if not self.columns:
            raise GraphError(f"Slice on {self.entity!r} declares no columns.")
        if len(set(self.columns)) != len(self.columns):
            raise GraphError(f"Slice on {self.entity!r} repeats a column.")
        for column in self.columns:
            _name("Slice.columns[]", column)
        _name("Slice.rows", self.rows)


@dataclass(frozen=True)
class Owned:
    """What a node writes: one column of one entity, at declared positions.

    Attributes:
        entity: The entity table.
        column: The column this node owns.
        dtype: One of :data:`DTYPES`; the executor enforces it exactly.
        rows: :data:`ROWS_ALL`, or a boolean input column naming the owned
            positions. Positions outside the mask are never written.
        ownership: ``PRODUCED`` writes values; ``ABSENT`` asserts null.
        rewrite: The node replaces a column its population version carries
            from its base. The kernel receives the incumbent values under the
            same column name, the base's declared dtype must equal ``dtype``,
            and the node owns the column in its own version. A rewrite needs a
            version with a base, so a column loaded by a ``CREATE`` node is
            rewritten only after a structural node has opened a new version.
    """

    entity: str
    column: str
    dtype: str
    rows: str = ROWS_ALL
    ownership: Ownership = Ownership.PRODUCED
    rewrite: bool = False

    def __post_init__(self) -> None:
        _name("Owned.entity", self.entity)
        _name("Owned.column", self.column)
        if self.dtype not in DTYPES:
            raise GraphError(
                f"Owned {self.entity}.{self.column}: dtype {self.dtype!r} is not "
                f"one of {sorted(DTYPES)}."
            )
        _name("Owned.rows", self.rows)
        if not isinstance(self.ownership, Ownership):
            raise GraphError("Owned.ownership must be an Ownership value.")


@dataclass(frozen=True)
class WeightTransition:
    """A declared change of weight kind on one entity.

    Attributes:
        entity: The entity whose explicit weights change.
        to_kind: The resulting kind; must come later than the current kind
            in :data:`WEIGHT_KINDS` (design may move straight to calibrated;
            no transition moves backwards).
        mass: ``conserve`` (total and per-stratum mass unchanged), ``free``
            (any mass, recorded), or ``declared`` (the kernel's receipt
            states the target mass and the executor checks it).
    """

    entity: str
    to_kind: str
    mass: str = "conserve"

    def __post_init__(self) -> None:
        _name("WeightTransition.entity", self.entity)
        if self.to_kind not in WEIGHT_KINDS:
            raise GraphError(
                f"WeightTransition.to_kind {self.to_kind!r} is not one of "
                f"{WEIGHT_KINDS}."
            )
        if self.mass not in MASS_POLICIES:
            raise GraphError(
                f"WeightTransition.mass {self.mass!r} is not one of "
                f"{sorted(MASS_POLICIES)}."
            )


@dataclass(frozen=True)
class WeightUpdate:
    """A declared numerical replacement of weights that keeps their kind.

    :class:`WeightTransition` only ever moves a weight *kind* forward, so a
    stage that recomputes the numbers of weights it already holds — a
    sampling normalization is the case this was extracted for — cannot be
    declared at all. This is that declaration, and it is deliberately
    narrower than a transition (amendment 26):

    - The kind does not move. The executor checks the incumbent kind, the
      declared kind and the returned weights' kind are the same one.
    - Mass is ``conserve`` or ``declared``; ``free`` is not offered,
      because an update that may move mass arbitrarily and does not
      change kind records nothing a reader could check it against.
    - ``reason`` is required, non-empty, and normative: it enters the node
      key, so a node that replaces weights for a different stated purpose
      is a different node.
    - The kernel must bind the ordered entity axis its replacement values
      are positional against, through
      :func:`~microcosm.graph.weight_update.weight_update_receipt`. The
      executor recomputes that binding from the incumbent axis, on cold
      execution and on replay.

    An update replaces weight *values*; it does not re-anchor design
    ancestry, and it is not a way to redefine a calibration cap. A row's
    design anchor is the design weight it entered the population carrying:
    ``Population.design_weights`` is captured once, at ``CREATE``
    (``population.Population.from_frame``), and afterwards only carried by
    stable entity id (``population._carry_design_weights``), which ``patch``
    passes on explicitly so the re-derive-from-the-frame default is never
    taken. So a design-kind update leaves every existing row's anchor
    exactly where it was, a later ``EXPAND``'s copied rows still inherit
    the anchor of the row they copy rather than that row's current value,
    and ``max_weight_ratio`` with ``weight_anchor='design'`` keeps the
    denominator it was written against. A row admitted with no ancestor is
    anchored on the design weight the ``EXPAND`` installs for it, whatever
    that is — it has no earlier weight to be anchored on. Re-anchoring here
    instead would let an unrelated normalization silently widen every cap
    declared upstream of it by that normalization's factor; a stage that
    wants a cap against normalized weights states the ratio it means.

    Attributes:
        entity: The entity whose explicit weights are replaced.
        kind: The unchanged weight kind; one of :data:`WEIGHT_KINDS`.
        reason: Why the numbers are replaced. Normative, non-empty.
        mass: ``conserve`` or ``declared``.
    """

    entity: str
    kind: str
    reason: str
    mass: str = "declared"

    def __post_init__(self) -> None:
        _name("WeightUpdate.entity", self.entity)
        _nonempty("WeightUpdate.reason", self.reason)
        if self.kind not in WEIGHT_KINDS:
            raise GraphError(
                f"WeightUpdate.kind {self.kind!r} is not one of {WEIGHT_KINDS}."
            )
        if self.mass not in WEIGHT_UPDATE_MASS_POLICIES:
            raise GraphError(
                f"WeightUpdate.mass {self.mass!r} is not one of "
                f"{sorted(WEIGHT_UPDATE_MASS_POLICIES)}; an update that keeps "
                "its kind does not get unconstrained free mass."
            )

    @property
    def to_kind(self) -> str:
        """The unchanged kind, for shared structural-weight accounting.

        A property, not a field, so it stays out of the normative
        projection and a :class:`WeightUpdate` can never canonicalize to
        the same bytes as a :class:`WeightTransition`.
        """

        return self.kind


@dataclass(frozen=True)
class Node:
    """One unit of computation and cell ownership.

    Attributes:
        id: Unique within the graph.
        kernel: Kernel reference, e.g. ``"fit.qrf@1"``.
        artifact_inputs: Typed byte dependencies on other nodes. Each local
            alias names a declared producer output of exactly the expected
            nominal type and version; the producer becomes a predecessor
            without owning any cell of this node (amendment 19).
        artifact_outputs: Typed byte outputs the kernel must return in
            ``KernelResult.artifacts``. Other untyped diagnostic bytes
            remain legal and stay unaddressable.
        inputs: Slices the kernel receives. Their owners are this node's
            predecessors.
        outputs: Cells this node owns. A ``CREATE`` node declares every
            column it loads; other structural nodes declare none (they
            carry their base's columns).
        params: Normative parameters; pure data (see :data:`Param`).
        population: Id of the structural node whose row set this node lives
            in. May be omitted only when the graph has exactly one
            structural node.
        structural: The row-set change this node makes; ``NONE`` for
            ordinary nodes.
        base: For a structural node other than ``CREATE``: the population
            version it transforms.
        sources: Names of :class:`SourceRef` entries this node reads.
        weights: A declared :class:`WeightTransition` (the kind moves
            forward) or :class:`WeightUpdate` (the numbers are replaced
            and the kind does not move), if any.
        mass: Mass policy for structural nodes that change rows or weights.
        entrants: ``EXPAND`` nodes only: the kernel may add rows that copy
            no base row. Such a row has null lineage, the kernel supplies
            every carried column for it, and the executor records it as an
            entrant. Entrants add mass, so the node's mass policy cannot be
            ``conserve``.
        description: Descriptive; never hashed.
        citation: Descriptive; never hashed.
    """

    id: str
    kernel: str
    inputs: tuple[Slice, ...] = ()
    outputs: tuple[Owned, ...] = ()
    params: Mapping[str, Param] = field(default_factory=dict)
    population: str | None = None
    structural: StructuralDelta = StructuralDelta.NONE
    base: str | None = None
    sources: tuple[str, ...] = ()
    weights: WeightTransition | WeightUpdate | None = None
    mass: str = "conserve"
    description: str = ""
    citation: str = ""
    entrants: bool = False
    artifact_inputs: tuple[ArtifactInput, ...] = ()
    artifact_outputs: tuple[ArtifactOutput, ...] = ()

    def __post_init__(self) -> None:
        _nonempty("Node.id", self.id)
        _nonempty("Node.kernel", self.kernel)
        _description("Node.description", self.description)
        for name, kind in (
            ("artifact_inputs", ArtifactInput),
            ("artifact_outputs", ArtifactOutput),
        ):
            declarations = getattr(self, name)
            if not isinstance(declarations, tuple) or any(
                not isinstance(item, kind) for item in declarations
            ):
                raise GraphError(
                    f"Node {self.id!r}: {name} must be a tuple of {kind.__name__}."
                )
            if len({item.name for item in declarations}) != len(declarations):
                raise GraphError(f"Node {self.id!r}: duplicate names in {name}.")
        if not isinstance(self.structural, StructuralDelta):
            raise GraphError(f"Node {self.id!r}: structural must be a StructuralDelta.")
        if self.mass not in MASS_POLICIES:
            raise GraphError(f"Node {self.id!r}: mass {self.mass!r} is not legal.")
        for name in sorted(self.params):
            _nonempty("Node.params key", name)
            _check_param(name, self.params[name])
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        if len({(o.entity, o.column) for o in self.outputs}) != len(self.outputs):
            raise GraphError(f"Node {self.id!r} declares the same owned cell twice.")
        if len(set(self.sources)) != len(self.sources):
            raise GraphError(f"Node {self.id!r} repeats a source.")
        if self.structural is StructuralDelta.CREATE:
            if self.base is not None or self.population is not None:
                raise GraphError(
                    f"Node {self.id!r}: a CREATE node has no base or population."
                )
            if not self.sources:
                raise GraphError(f"Node {self.id!r}: a CREATE node needs sources.")
            if self.inputs:
                raise GraphError(f"Node {self.id!r}: a CREATE node has no inputs.")
            if not self.outputs:
                raise GraphError(
                    f"Node {self.id!r}: a CREATE node must declare every column "
                    "it loads, so ownership is total from the first node."
                )
        elif self.structural is not StructuralDelta.NONE:
            if self.base is None:
                raise GraphError(
                    f"Node {self.id!r}: a {self.structural.value} node needs a base."
                )
            if self.population is not None:
                raise GraphError(
                    f"Node {self.id!r}: a structural node declares base, not "
                    "population."
                )
            if self.outputs:
                raise GraphError(
                    f"Node {self.id!r}: a {self.structural.value} node carries its "
                    "base's columns and owns none of its own."
                )
        elif self.base is not None:
            raise GraphError(f"Node {self.id!r}: only structural nodes have a base.")
        if not isinstance(self.entrants, bool):
            raise GraphError(f"Node {self.id!r}: entrants must be a boolean.")
        if self.entrants and self.structural is not StructuralDelta.EXPAND:
            raise GraphError(
                f"Node {self.id!r}: only an EXPAND node may admit entrants."
            )
        if self.entrants and self.mass == "conserve":
            raise GraphError(
                f"Node {self.id!r}: entrants add mass, so an entrant-admitting "
                "node cannot declare mass='conserve'."
            )
        if self.weights is not None and not isinstance(
            self.weights, WeightTransition | WeightUpdate
        ):
            raise GraphError(
                f"Node {self.id!r}: weights must be a WeightTransition or a "
                "WeightUpdate, not a look-alike."
            )
        if self.weights is not None and self.structural is not StructuralDelta.REWEIGHT:
            raise GraphError(
                f"Node {self.id!r}: a weight transition changes the population "
                "every later node reads, so it is a REWEIGHT node with a base."
            )
        if self.structural is StructuralDelta.REWEIGHT:
            if self.weights is None:
                raise GraphError(
                    f"Node {self.id!r}: a REWEIGHT node declares its "
                    "WeightTransition or WeightUpdate."
                )
            if self.mass != self.weights.mass:
                # Reached by a WeightTransition and a WeightUpdate alike, so
                # the text names neither (amendment 26).
                raise GraphError(
                    f"Node {self.id!r}: mass policy {self.mass!r} disagrees with "
                    f"its declared weight change's {self.weights.mass!r}."
                )
        declared_inputs = {(s.entity, c) for s in self.inputs for c in s.columns}
        for s in self.inputs:
            if s.rows != ROWS_ALL and (s.entity, s.rows) not in declared_inputs:
                raise GraphError(
                    f"Node {self.id!r}: row mask {s.rows!r} on {s.entity!r} must be "
                    "one of the node's input columns."
                )
        for o in self.outputs:
            if o.rows != ROWS_ALL and (o.entity, o.rows) not in declared_inputs:
                raise GraphError(
                    f"Node {self.id!r}: owned-row mask {o.rows!r} on {o.entity!r} "
                    "must be one of the node's input columns."
                )
            if (o.entity, o.column) in declared_inputs and not o.rewrite:
                raise GraphError(
                    f"Node {self.id!r} both reads and owns {o.entity}.{o.column}; "
                    "a node cannot own a column it consumes unless the cell is "
                    "declared as a rewrite."
                )

    def normative(self) -> dict[str, object]:
        """The projection that enters the node key (descriptive fields dropped).

        The artifact declarations are normative but elided when empty, so a
        node that declares none projects exactly as it did before amendment
        19 and its key does not move.
        """
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name not in DESCRIPTIVE_FIELDS
            and not (
                f.name in {"artifact_inputs", "artifact_outputs"}
                and not getattr(self, f.name)
            )
        }


@dataclass(frozen=True)
class Graph:
    """A complete build declaration for one country.

    Attributes:
        country: Descriptive label carried into manifests; never hashed
            into node keys (a node's identity is its computation).
        sources: External inputs by name.
        nodes: Every node. Declaration order carries no meaning.
        mass_partition: ``(entity, column)`` of a column that partitions
            mass accounting, or ``None``. When set, every ``CREATE`` node
            declares the column with a dtype in :data:`PARTITION_DTYPES`,
            the executor's ledger reports per stratum within each partition
            value, and ``conserve`` holds within each partition. Normative:
            it enters the key of every structural node.
    """

    country: str
    sources: tuple[SourceRef, ...]
    nodes: tuple[Node, ...]
    mass_partition: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        _nonempty("Graph.country", self.country)
        if len({s.name for s in self.sources}) != len(self.sources):
            raise GraphError("Graph repeats a source name.")
        if len({n.id for n in self.nodes}) != len(self.nodes):
            raise GraphError("Graph repeats a node id.")
        if self.mass_partition is not None:
            if (
                not isinstance(self.mass_partition, tuple)
                or len(self.mass_partition) != 2
                or not all(isinstance(part, str) for part in self.mass_partition)
            ):
                raise GraphError(
                    "Graph.mass_partition must be an (entity, column) pair of strings."
                )
            _name("Graph.mass_partition entity", self.mass_partition[0])
            _name("Graph.mass_partition column", self.mass_partition[1])

    def normative(self) -> dict[str, object]:
        """The graph-level facts that enter every structural node's key."""
        return {"mass_partition": self.mass_partition}

    def node(self, node_id: str) -> Node:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise GraphError(f"Unknown node {node_id!r}.")


@dataclass(frozen=True)
class CompiledGraph:
    """A validated graph with its derived structure.

    Attributes:
        graph: The declaration.
        order: A canonical topological order (by depth, then id), so two
            declarations of the same nodes compile to the same order.
        owners: ``(version, entity, column)`` to the node that produces that
            column in that version: a ``CREATE`` node for the columns it
            loads, otherwise the owning node.
        predecessors: Node id to the sorted ids it depends on.
        versions: Node id to the population version it lives in (for a
            structural node, the version it creates: its own id).
    """

    graph: Graph
    order: tuple[str, ...]
    owners: Mapping[tuple[str, str, str], str]
    predecessors: Mapping[str, tuple[str, ...]]
    versions: Mapping[str, str]


def compile_graph(graph: Graph) -> CompiledGraph:
    """Validate a graph and derive ownership, predecessors, and order.

    Raises:
        GraphError: A cell with two owners or none (ownership is total and
            exclusive), an unknown source or population, a structural node
            whose base is not structural, a row mask whose declared dtype is
            not boolean, a mass-partition column that a ``CREATE`` node does
            not declare with a partition dtype, a cycle, or a graph with
            several structural nodes and a node that omits ``population``.
    """

    by_id = {node.id: node for node in graph.nodes}
    source_names = {s.name for s in graph.sources}
    structural = [n for n in graph.nodes if n.structural is not StructuralDelta.NONE]
    if not any(n.structural is StructuralDelta.CREATE for n in structural):
        raise GraphError("A graph needs at least one CREATE node.")
    default_version = structural[0].id if len(structural) == 1 else None

    versions: dict[str, str] = {}
    for node in graph.nodes:
        for name in node.sources:
            if name not in source_names:
                raise GraphError(f"Node {node.id!r} reads unknown source {name!r}.")
        if node.structural is not StructuralDelta.NONE:
            if node.base is not None:
                base = by_id.get(node.base)
                if base is None or base.structural is StructuralDelta.NONE:
                    raise GraphError(
                        f"Node {node.id!r}: base {node.base!r} is not a structural "
                        "node."
                    )
            versions[node.id] = node.id
            continue
        version = node.population or default_version
        if version is None:
            raise GraphError(
                f"Node {node.id!r} omits population, and the graph has "
                f"{len(structural)} structural nodes."
            )
        holder = by_id.get(version)
        if holder is None or holder.structural is StructuralDelta.NONE:
            raise GraphError(
                f"Node {node.id!r}: population {version!r} is not a structural node."
            )
        versions[node.id] = version

    owners: dict[tuple[str, str, str], str] = {}
    dtypes: dict[tuple[str, str, str], str] = {}
    for node in graph.nodes:
        for owned in node.outputs:
            key = (versions[node.id], owned.entity, owned.column)
            if owned.rewrite and by_id[key[0]].structural is StructuralDelta.CREATE:
                raise GraphError(
                    f"Node {node.id!r} rewrites {owned.entity}.{owned.column} inside "
                    f"the CREATE version {key[0]!r}; a rewrite needs a version with "
                    "a base to carry the incumbent from."
                )
            if key in owners:
                raise GraphError(
                    f"{owned.entity}.{owned.column} in version {key[0]!r} is owned "
                    f"by both {owners[key]!r} and {node.id!r}."
                )
            owners[key] = node.id
            dtypes[key] = owned.dtype

    if graph.mass_partition is not None:
        entity, column = graph.mass_partition
        for node in structural:
            if node.structural is not StructuralDelta.CREATE:
                continue
            dtype = dtypes.get((node.id, entity, column))
            if dtype is None:
                raise GraphError(
                    f"Graph.mass_partition names {entity}.{column}, which CREATE "
                    f"node {node.id!r} does not declare; partitions must exist "
                    "from the first version."
                )
            if dtype not in PARTITION_DTYPES:
                raise GraphError(
                    f"Graph.mass_partition {entity}.{column} is declared {dtype!r}; "
                    f"a partition column must be one of {sorted(PARTITION_DTYPES)}."
                )
        # A partition value is fixed when a row is created. Any later owner of
        # the column, rewrite or not, could move mass between partitions with
        # the total unchanged, which no mass policy can see; refuse it here.
        for (version, owner_entity, owner_column), owner_id in sorted(owners.items()):
            if (owner_entity, owner_column) != (entity, column):
                continue
            if by_id[owner_id].structural is not StructuralDelta.CREATE:
                raise GraphError(
                    f"Node {owner_id!r} owns mass partition {entity}.{column} in "
                    f"version {version!r}; a partition value is fixed by the CREATE "
                    "node that admits the row, and no later node may write or "
                    "rewrite it."
                )

    def declared_dtype(version: str, entity: str, column: str) -> str | None:
        """The owner-declared dtype of a column as visible in ``version``."""
        while True:
            dtype = dtypes.get((version, entity, column))
            if dtype is not None:
                return dtype
            holder = by_id[version]
            if holder.structural is StructuralDelta.CREATE:
                return None
            version = holder.base  # type: ignore[assignment]

    def reader_of(node_id: str, version: str, entity: str, column: str) -> str:
        """The node whose artifact a reader in ``version`` receives."""
        if declared_dtype(version, entity, column) is None:
            raise GraphError(
                f"Node {node_id!r} reads {entity}.{column}, which no node owns in "
                f"version {version!r} or its bases."
            )
        owner = owners.get((version, entity, column))
        return owner if owner is not None else version

    def check_mask(node_id: str, version: str, entity: str, mask: str) -> None:
        if mask == ROWS_ALL:
            return
        dtype = declared_dtype(version, entity, mask)
        if dtype is not None and dtype not in MASK_DTYPES:
            raise GraphError(
                f"Node {node_id!r}: row mask {entity}.{mask} is declared "
                f"{dtype!r}; a row mask must be one of {sorted(MASK_DTYPES)}."
            )

    members: dict[str, set[str]] = {}
    for node_id, version in versions.items():
        if by_id[node_id].structural is StructuralDelta.NONE:
            members.setdefault(version, set()).add(node_id)

    predecessors: dict[str, set[str]] = {n.id: set() for n in graph.nodes}
    for node in graph.nodes:
        if node.structural is StructuralDelta.CREATE:
            continue
        if node.structural is not StructuralDelta.NONE:
            base = node.base
            assert base is not None
            predecessors[node.id].add(base)
            predecessors[node.id].update(members.get(base, ()))
            for s in node.inputs:
                for column in s.columns:
                    predecessors[node.id].add(
                        reader_of(node.id, base, s.entity, column)
                    )
                check_mask(node.id, base, s.entity, s.rows)
            continue
        version = versions[node.id]
        predecessors[node.id].add(version)
        rewritten = {(o.entity, o.column) for o in node.outputs if o.rewrite}
        for s in node.inputs:
            for column in s.columns:
                if (s.entity, column) in rewritten:
                    continue  # the incumbent arrives through the rewrite itself
                source = reader_of(node.id, version, s.entity, column)
                if source == node.id:
                    raise GraphError(f"Node {node.id!r} depends on itself.")
                predecessors[node.id].add(source)
            check_mask(node.id, version, s.entity, s.rows)
        for o in node.outputs:
            check_mask(node.id, version, o.entity, o.rows)
            if not o.rewrite:
                continue
            holder = by_id[version]
            if holder.structural is StructuralDelta.CREATE:
                raise GraphError(
                    f"Node {node.id!r} rewrites {o.entity}.{o.column} inside the "
                    f"CREATE version {version!r}; a rewrite needs a version with a "
                    "base to carry the incumbent from."
                )
            base_dtype = declared_dtype(holder.base, o.entity, o.column)  # type: ignore[arg-type]
            if base_dtype is None:
                raise GraphError(
                    f"Node {node.id!r} rewrites {o.entity}.{o.column}, which no "
                    f"node defines below version {version!r}."
                )
            if base_dtype != o.dtype:
                raise GraphError(
                    f"Node {node.id!r} rewrites {o.entity}.{o.column} as {o.dtype!r} "
                    f"but the incumbent is declared {base_dtype!r}."
                )

    # Artifact edges cross population versions, without changing cell ownership
    # (amendment 19). They join the same predecessor sets, so the depth
    # computation below refuses an artifact cycle like any other.
    for node in graph.nodes:
        for binding in node.artifact_inputs:
            if binding.producer == node.id:
                raise GraphError(
                    f"Node {node.id!r} depends on itself through an artifact."
                )
            producer = by_id.get(binding.producer)
            if producer is None:
                raise GraphError(
                    f"Node {node.id!r}: unknown artifact producer {binding.producer!r}."
                )
            outputs = {output.name: output for output in producer.artifact_outputs}
            output = outputs.get(binding.artifact)
            if output is None:
                raise GraphError(
                    f"Node {node.id!r}: producer {producer.id!r} has no declared "
                    f"artifact {binding.artifact!r}."
                )
            if output.type != binding.type:
                raise GraphError(
                    f"Node {node.id!r}: artifact {binding.artifact!r} type does not "
                    "match its producer."
                )
            predecessors[node.id].add(producer.id)

    depth: dict[str, int] = {}

    def depth_of(node_id: str, trail: tuple[str, ...]) -> int:
        if node_id in depth:
            return depth[node_id]
        if node_id in trail:
            cycle = " -> ".join((*trail[trail.index(node_id) :], node_id))
            raise GraphError(f"Cycle: {cycle}.")
        preds = predecessors[node_id]
        value = (
            0 if not preds else 1 + max(depth_of(p, (*trail, node_id)) for p in preds)
        )
        depth[node_id] = value
        return value

    for node in graph.nodes:
        depth_of(node.id, ())
    order = tuple(sorted(by_id, key=lambda i: (depth[i], i)))
    return CompiledGraph(
        graph=graph,
        order=order,
        owners=MappingProxyType(owners),
        predecessors=MappingProxyType(
            {i: tuple(sorted(p)) for i, p in predecessors.items()}
        ),
        versions=MappingProxyType(versions),
    )
