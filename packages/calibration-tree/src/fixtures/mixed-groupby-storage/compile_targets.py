"""Compile this directory's added targets through Microcosm at 0ff293b.

Builds the fourth case of hierarchy-probe-targets.json and both cases of
listed-dimension-targets.json, as the review of 836fc66 built its witness
(work/null-pair/producer.py): load Microcosm's ledger_targets.py with its
microcosm imports replaced by plain namespaces, select the facts with the
catalog sum resolver (_resolve_reference_fact), and compile them with
target_spec_from_ledger_reference. It prints one JSON line per target, with
the fixture's fields and the selected fact keys.

    curl -fsSL -o ledger_targets.py https://raw.githubusercontent.com/PolicyEngine/microcosm/0ff293b1a335a29d46f1598f2039beffa242e632/packages/microcosm-build/src/microcosm/build/ledger_targets.py
    python3 -I compile_targets.py ledger_targets.py hierarchy-probe-targets.json
"""
import ast
from copy import deepcopy
import json
from pathlib import Path
import sys
import types

SOURCE = Path(sys.argv[1])
FIXTURE = Path(sys.argv[2])
tree = ast.parse(SOURCE.read_text(), filename=str(SOURCE))
external_names = []
for node in tree.body:
    if isinstance(node, ast.ImportFrom) and node.module.startswith("microcosm"):
        external_names.extend(alias.asname or alias.name for alias in node.names)
tree.body = [node for node in tree.body if not (
    isinstance(node, ast.ImportFrom) and node.module.startswith("microcosm"))]
module = types.ModuleType("ledger_targets_0ff293b")
sys.modules[module.__name__] = module
module.__dict__.update({name: types.SimpleNamespace for name in external_names})
module.LexicalFloat = type("LexicalFloat", (float,), {})
module.UK_GEOGRAPHY_ID_TO_LABEL = {}
exec(compile(tree, str(SOURCE), "exec"), module.__dict__)

seed = types.SimpleNamespace(provider=types.SimpleNamespace(id="provider", label="Publisher"),
    category=types.SimpleNamespace(id="balance", label="Balance", provider_id="provider"),
    target_label="Combined balance")
reference = module.LedgerTargetReference(name="combined-balance",
    ledger_selector={"source_name": "Publisher"}, value_operation="sum",
    expected_member_count=2, entity="person", measure="balance", period=2024, hierarchy=seed)

def to_json(value):
    return json.loads(json.dumps(value, default=lambda item: vars(item)))

def emit(case, input_facts):
    selected = module._resolve_reference_fact(reference, module._ledger_fact_index(input_facts))
    spec = module.target_spec_from_ledger_reference(selected, reference)
    hierarchy = to_json(vars(spec.hierarchy))
    print(json.dumps({
        "case": case,
        "facts": input_facts,
        "hierarchy_dimensions": hierarchy["dimensions"],
        "representative": spec.metadata["ledger_fact_key"],
        "dashboard_target": {"hierarchy": hierarchy, "metadata": spec.metadata, "name": spec.name},
        "selected_keys": [module._fact_key(fact) for fact in selected],
    }, sort_keys=True))

def rename(fact, key):
    fact["aggregate_fact_key"] = key
    fact["source_record_id"] = key
    fact["label"] = key

base = deepcopy(json.loads(FIXTURE.read_text())[0]["facts"])
assert [fact["aggregate_fact_key"] for fact in base] == ["a-explicit", "b-layout"]

# 1. The review's witness: the mixed-storage facts, a-explicit renamed z-explicit.
witness = deepcopy(base)
rename(witness[0], "z-explicit")
emit("ordinary a + layout a; disjoint sex cells; explicit fact renamed z-explicit", witness)

# 2. Two facts that share sex=female; one carries loan type A in its dimensions,
#    the other in its layout's group-by.
shared = deepcopy(base)
shared[1]["dimensions"] = {"sex": "female"}
emit("ordinary a + layout a; both female; layout fact is the representative", shared)
renamed = deepcopy(shared)
rename(renamed[0], "z-explicit")
emit("ordinary a + layout a; both female; explicit fact renamed z-explicit is the representative", renamed)
