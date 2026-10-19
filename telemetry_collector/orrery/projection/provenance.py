"""The Microcosm commit and files this package's modules were copied from.

Each copied module keeps its Microcosm file name. ``SOURCE_BLOBS`` holds the
git blob ID of each source file at ``MICROCOSM_COMMIT``, so a test can confirm
that an unchanged module still has its source's exact contents, and a later
copy can be compared with ``git rev-parse <commit>:<directory>/<module>``. The
README's "Orrery projection" section describes how to copy a later version.
"""

MICROCOSM_REPOSITORY = "PolicyEngine/microcosm"
#: The head of PolicyEngine/microcosm#1295 when the modules were copied.
MICROCOSM_COMMIT = "8d159a8c5653ad63affb1a5d2e39034f877db2ff"
MICROCOSM_SOURCE_DIRECTORY = "packages/microcosm-graph/src/microcosm/graph"

#: Written at ``metadata.adapter`` in every projected document. It is
#: Microcosm's identifier for this projection, kept unchanged in the copy.
ORRERY_ADAPTER = "microcosm.graph.orrery.v1"

#: Each copied module and its source file's git blob ID at MICROCOSM_COMMIT.
SOURCE_BLOBS = {
    "canonical.py": "a9e80411af4ad317c94d2eacffe0fa0160a8bad3",
    "decl.py": "5d2bf5cfed4d7081dbaf9c61b74c717ee8c6dc28",
    "execution_overlay.py": "124cebf91e66e0fe73d44363feeb561a0d3604ce",
    "expansion.py": "cb613916d48d5f0ddaabcf69ccaf3e7f8b46b9ed",
    "orrery.py": "f68b859f0718baba3f39d0e7881b7796824b4655",
    "orrery_evidence.py": "dee1b6597ebe2517f817a297f6f44142af0758fe",
    "presentation.py": "911a7a8674c947a336498bb86fa6e45589cba064",
    "schema.py": "c71004dffc6fc80ee76f9c99edb6865010e2d831",
    "serialize.py": "501d6e37da426c964579b7ac5a918ec19ef2b1a0",
}

#: The copied modules whose contents differ from their source, and how.
CHANGED_MODULES = {
    "orrery.py": (
        "Removes main and its helpers (_read_json, _unique_json_object, "
        "_invalid_json_constant and _rebase_references), because the command's "
        "overlay option reads Microcosm's content store; removes "
        "orrery_document, orrery_json and _compiled, which compile a Graph "
        "object before projecting it; and drops the imports only they used. "
        "The module docstring says so. orrery_document_from_schema, "
        "orrery_json_from_schema and the helpers they call are unchanged."
    ),
}
