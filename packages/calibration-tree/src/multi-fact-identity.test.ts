import { describe, expect, test } from "bun:test";

import edFsaFamily from "./fixtures/ed-fsa-family/ed-fsa-family-schema-8.json";
import hierarchyProbeTargets from "./fixtures/mixed-groupby-storage/hierarchy-probe-targets.json";
import {
  buildCalibration,
  latestMicrocosmTargetDiagnosticsPage,
  latestMicrocosmTargetInvestigation,
} from "./latest-artifact";

// Microcosm writes ledger_member_fact_count and ledger_member_fact_keys on
// every target it builds from several facts, ratio operands included
// (_multi_fact_reference_metadata, ledger_targets.py:1318-1341 at 0ff293b),
// and takes the target's single-fact fields from one representative fact
// (:1085-1090). Its identity is the representative alone for a ratio-scaled
// cell, the base cells for a rolled-forward one, and every fact for a linear
// combination (identity_facts, :1018-1035). These rows are FSA rows of the
// fixture carrying the metadata Microcosm writes for each operation; each is
// renamed, because a UK CGT cell's name is not its record id.
type JsonObject = Record<string, unknown>;
type Row = Record<string, unknown>;

const fixture = edFsaFamily as JsonObject;
const fsaRows = fixture.targets as JsonObject[];

const ALABAMA_BALANCE = "ed_fsa.portfolio_by_location.2026_03_31.al.dollars_outstanding";
const PARENT_PLUS =
  "ed_fsa.portfolio_by_loan_type.fy2026_q2.dollars_outstanding.parent_plus.dollars_outstanding";
const EXCLUDING_PARENT_PLUS =
  "ed_fsa.portfolio_by_loan_type.fy2026_q2.dollars_outstanding.excluding_parent_plus.dollars_outstanding";

// Facts a ratio translates its base by, a second base cell and an allocated
// residual's total, named here.
const NUMERATOR = "ledger.aggregate_fact.v2:numerator_for_this_test";
const DENOMINATOR = "ledger.aggregate_fact.v2:denominator_for_this_test";
const SECOND_BASE_CELL = "ledger.aggregate_fact.v2:second_base_cell_for_this_test";
const TOTAL = "ledger.aggregate_fact.v2:total_for_this_test";

// The fields that name one fact, and the group-by value and universe
// constraint, as the dashboard shows them.
const IDENTITY_FIELDS = [
  "source_record_id",
  "fact_key",
  "semantic_fact_key",
  "aggregate_fact_key",
  "legacy_fact_key",
  "layout_groupby_value_id",
  "universe_constraint_set_key",
  "universe_constraint_count",
] as const;

function fsaRow(targetName: string): JsonObject {
  const row = fsaRows.find((candidate) => candidate.target_name === targetName);
  if (!row) throw new Error(`No FSA target is ${targetName}.`);
  return structuredClone(row);
}

// A copy of an FSA row under a new name, with the metadata Microcosm writes
// for a multi-fact operation.
function multiFactRow(
  targetName: string,
  name: string,
  operation: string,
  members: string[],
  metadata: Record<string, string> = {},
): JsonObject {
  const row = fsaRow(targetName);
  row.target_name = name;
  row.name = `${name}@2024`;
  (row.hierarchy as { target: { id: string } }).target.id = name;
  const keys = JSON.stringify(members);
  Object.assign(row.metadata as JsonObject, {
    ledger_value_operation: operation,
    ledger_member_fact_count: String(members.length),
    ledger_member_aggregate_fact_keys: keys,
    ledger_member_fact_keys: keys,
    ...metadata,
  });
  return row;
}

// The review's probe targets, as rows of a release.
const PROBE_TARGETS = hierarchyProbeTargets as { case: string; dashboard_target: JsonObject }[];

function probeRow(probe: (typeof PROBE_TARGETS)[number]): JsonObject {
  return {
    ...structuredClone(probe.dashboard_target),
    name: "combined-balance@2024",
    target_name: "combined-balance",
    target: 40,
    initial_estimate: 40,
    final_estimate: 40,
    relative_error: 0,
  };
}

function releaseOf(rows: JsonObject[]) {
  return buildCalibration({ ...fixture, targets: rows }, "populace-us-2024-multi-fact-test", null, {}, {}, {}, "us");
}

function lookup(cal: ReturnType<typeof releaseOf>, target: string) {
  const url = new URL("https://dashboard.test/api/microcosm/target-investigation");
  url.searchParams.set("target", target);
  return latestMicrocosmTargetInvestigation(url.toString(), cal);
}

function detailOf(cal: ReturnType<typeof releaseOf>, target: string) {
  const detail = lookup(cal, target);
  if (!detail.available || !("target" in detail)) throw new Error(`No detail for ${target}.`);
  return detail as typeof detail & { repo_searches: { label: string; query: string }[] };
}

function identityOf(chronicle: unknown) {
  const fields = chronicle as Row;
  return Object.fromEntries(IDENTITY_FIELDS.map((field) => [field, fields[field]]));
}

// What the targets page lists for the release's one row.
function listedChronicle(cal: ReturnType<typeof releaseOf>) {
  const url = new URL("https://dashboard.test/api/microcosm/target-diagnostics");
  url.searchParams.set("limit", "10");
  const page = latestMicrocosmTargetDiagnosticsPage(url.toString(), cal);
  expect(page.targets).toHaveLength(1);
  return page.targets[0].chronicle;
}

const NULL_IDENTITY = Object.fromEntries(IDENTITY_FIELDS.map((field) => [field, null]));

describe("a target built from several facts shows the identity Microcosm gives it", () => {
  // A ratio-scaled cell keeps its base cell's identity (:1018-1023), and its
  // base is the representative (_value_representative_fact, :1239-1244). A
  // rolled-forward target with one base cell has that cell as its identity
  // (facts[:-2], :1030-1031), which is the representative too, and Microcosm
  // counts it in ledger_value_base_cells (:1174-1178).
  //
  // The Alabama cell keeps its location in its layout, and these operands have
  // no ordinary dimensions, so Microcosm lists no varying dimension on these
  // targets: it compares ordinary dimensions alone (_varying_dimension_ids,
  // :1626-1646; _dimensions, :5777-5784). A base cell with its location among
  // its ordinary dimensions has a ledger_filter_ field for it (:5914-5917), and
  // Microcosm lists it as varying, since national operands lack it (:1090,
  // :1323-1339). ORDINARY_LOCATION adds those fields, and each operation
  // appears both ways.
  const own = (fsaRow(ALABAMA_BALANCE).metadata as Record<string, string>);
  const ORDINARY_LOCATION = {
    "ledger_filter_ed_fsa.borrower_location": "al",
    ledger_dimension_set_key: "ledger.dimension_set.v2:location_for_this_test",
    ledger_aggregation_varying_dimensions: JSON.stringify(["ed_fsa.borrower_location"]),
  };
  const scaled = (metadata: Record<string, string> = {}) =>
    multiFactRow(ALABAMA_BALANCE, "ed_fsa.test.alabama_balance_scaled", "scaled_by_ratio", [
      own.ledger_fact_key,
      NUMERATOR,
      DENOMINATOR,
    ], { ledger_value_formula: "base * numerator / denominator", ...metadata });
  const rolledForward = (metadata: Record<string, string> = {}) =>
    multiFactRow(
      ALABAMA_BALANCE,
      "ed_fsa.test.alabama_balance_rolled_forward",
      "rolled_forward_by_ratio",
      [own.ledger_fact_key, NUMERATOR, DENOMINATOR],
      {
        ledger_value_base_cells: "1",
        ledger_value_base_fact_keys: JSON.stringify([own.ledger_fact_key]),
        ...metadata,
      },
    );
  const ONE_FACT_IDENTITY = [
    { operation: "scaled_by_ratio", row: () => scaled() },
    { operation: "rolled_forward_by_ratio, one base cell", row: () => rolledForward() },
    { operation: "scaled_by_ratio, ordinary location", row: () => scaled(ORDINARY_LOCATION) },
    {
      operation: "rolled_forward_by_ratio, one base cell, ordinary location",
      row: () => rolledForward(ORDINARY_LOCATION),
    },
  ];

  for (const { operation, row } of ONE_FACT_IDENTITY) {
    test(`${operation}: its record and fact keys show, and its member facts are listed`, () => {
      const cal = releaseOf([row()]);
      const name = String(cal.rows[0].name);
      const raw = cal.rows[0].chronicle;
      const detail = detailOf(cal, name);
      const shown = detail.target.chronicle as unknown as Row;
      expect(identityOf(shown)).toEqual(identityOf(raw));
      expect(identityOf(shown)).toMatchObject({
        source_record_id: own.ledger_source_record_id,
        fact_key: own.ledger_fact_key,
        layout_groupby_value_id: "al",
        universe_constraint_set_key: own.ledger_universe_constraint_set_key,
      });
      expect([shown.member_fact_count, shown.member_fact_keys]).toEqual([
        3,
        [own.ledger_fact_key, NUMERATOR, DENOMINATOR],
      ]);
      expect(listedChronicle(cal)).toEqual(detail.target.chronicle);

      // A lookup by the target's own record id or fact key finds it.
      for (const key of [
        own.ledger_source_record_id,
        own.ledger_fact_key,
        own.ledger_semantic_fact_key,
        own.ledger_legacy_fact_key,
      ]) {
        const found = lookup(cal, key);
        expect([key, found.available]).toEqual([key, true]);
        expect("target" in found && found.target.name).toBe(name);
      }

      // Its code searches look for its own record and fact key, and for the
      // facts that translate it.
      const searches = detail.repo_searches.map((search) => [search.label, search.query]);
      expect(searches).toContainEqual(["Source record", `"${own.ledger_source_record_id}"`]);
      expect(searches).toContainEqual(["Fact key", `"${own.ledger_fact_key}"`]);
      expect(searches.filter(([label]) => label === "Member fact")).toEqual([
        ["Member fact", `"${NUMERATOR}"`],
        ["Member fact", `"${DENOMINATOR}"`],
      ]);
    });
  }

  // ledger_aggregation_varying_dimensions spans every fact, the ratio's
  // operands included (:1323-1339), so a dimension that varies only across
  // them leaves the base's identity alone.
  test("a dimension that varies across a ratio's operands leaves its base's identity", () => {
    const row = ONE_FACT_IDENTITY[0].row();
    (row.metadata as JsonObject).ledger_aggregation_varying_dimensions =
      JSON.stringify(["ed_fsa.borrower_age_band"]);
    const cal = releaseOf([row]);
    const shown = detailOf(cal, String(cal.rows[0].name)).target.chronicle;
    expect(identityOf(shown)).toEqual(identityOf(cal.rows[0].chronicle));
  });

  // A rolled-forward target can sum several base cells: a typed dimension
  // roster at one geography (_assert_rolled_forward_base_cells,
  // :3352-3408). Its identity is then those cells, so no one fact's record or
  // keys name it, and nothing in the metadata says whether they share a
  // universe constraint. Its hierarchy's dimensions are the ones every base
  // cell shares (_inherited_dimensions, :1578-1611, from the identity facts at
  // :1096-1100), so they say whether the cells share their group-by value.
  test("rolled_forward_by_ratio, two base cells: no one cell names it", () => {
    const parentPlus = fsaRow(PARENT_PLUS).metadata as Record<string, string>;
    // Parent PLUS and a second loan type: the cells differ in their group-by
    // value, so the hierarchy lists no loan type.
    const differing = multiFactRow(
      PARENT_PLUS,
      "ed_fsa.test.two_loan_types_rolled_forward",
      "rolled_forward_by_ratio",
      [parentPlus.ledger_fact_key, SECOND_BASE_CELL, NUMERATOR, DENOMINATOR],
      {
        ledger_value_base_cells: "2",
        ledger_value_base_fact_keys: JSON.stringify([parentPlus.ledger_fact_key, SECOND_BASE_CELL]),
      },
    );
    (differing.hierarchy as { dimensions: unknown[] }).dimensions = [];
    // Two age bands of Alabama's balance: the cells share their group-by
    // value, Alabama, which the hierarchy still lists.
    const sharing = multiFactRow(
      ALABAMA_BALANCE,
      "ed_fsa.test.alabama_two_age_bands_rolled_forward",
      "rolled_forward_by_ratio",
      [own.ledger_fact_key, SECOND_BASE_CELL, NUMERATOR, DENOMINATOR],
      {
        ledger_value_base_cells: "2",
        ledger_value_base_fact_keys: JSON.stringify([own.ledger_fact_key, SECOND_BASE_CELL]),
        ledger_aggregation_varying_dimensions: JSON.stringify(["ed_fsa.borrower_age_band"]),
      },
    );

    const cal = releaseOf([differing]);
    const shown = detailOf(cal, String(cal.rows[0].name)).target.chronicle as unknown as Row;
    expect(identityOf(shown)).toEqual(NULL_IDENTITY);
    expect([shown.member_fact_count, shown.member_fact_keys]).toEqual([
      4,
      [parentPlus.ledger_fact_key, SECOND_BASE_CELL, NUMERATOR, DENOMINATOR],
    ]);
    expect(lookup(cal, parentPlus.ledger_source_record_id).available).toBe(false);

    const shared = releaseOf([sharing]);
    const sharedShown = detailOf(shared, String(shared.rows[0].name)).target.chronicle;
    expect(identityOf(sharedShown)).toEqual({ ...NULL_IDENTITY, layout_groupby_value_id: "al" });
    expect(listedChronicle(shared)).toEqual(sharedShown);
  });

  // An allocated residual over the Alabama cell and a second cell, with the
  // base cells its receipt lists as fact_keys.base (:3188-3189). Its total
  // leaves out a dimension its partition's cells carry (:2798-2803), so
  // Microcosm lists that dimension in ledger_aggregation_varying_dimensions
  // (:1323-1339, :1626-1646). ORDINARY_LOCATION makes that dimension the
  // location.
  const allocated = (name: string, base: string[]) =>
    multiFactRow(
      ALABAMA_BALANCE,
      name,
      "allocated_residual_by_ratio",
      [...new Set([...base, own.ledger_fact_key, SECOND_BASE_CELL]), TOTAL],
      {
        ledger_allocation_receipt: JSON.stringify({
          operation: "allocated_residual_by_ratio",
          version: 1,
          fact_keys: {
            base,
            partition: [own.ledger_fact_key, SECOND_BASE_CELL],
            recipients: [SECOND_BASE_CELL],
            total: TOTAL,
          },
        }),
        ...ORDINARY_LOCATION,
      },
    );

  // An allocated residual's identity is the cells its selector matches
  // (:1024-1029), which its receipt lists as fact_keys.base (:3188-3189);
  // the first of them is the representative (:2993-3011, :1239-1244).
  test("allocated_residual_by_ratio: one base cell names it, two do not", () => {
    const one = releaseOf([allocated("ed_fsa.test.alabama_allocated", [own.ledger_fact_key])]);
    const oneShown = detailOf(one, String(one.rows[0].name)).target.chronicle as unknown as Row;
    expect(identityOf(oneShown)).toEqual(identityOf(one.rows[0].chronicle));
    expect(oneShown.member_fact_count).toBe(3);
    expect(lookup(one, own.ledger_source_record_id).available).toBe(true);

    const two = releaseOf([
      allocated("ed_fsa.test.alabama_two_cells_allocated", [own.ledger_fact_key, SECOND_BASE_CELL]),
    ]);
    const twoShown = detailOf(two, String(two.rows[0].name)).target.chronicle;
    // The hierarchy still lists Alabama, so both cells share it.
    expect(identityOf(twoShown)).toEqual({ ...NULL_IDENTITY, layout_groupby_value_id: "al" });
  });

  // A hierarchy that lists a group-by pair shows that every identity fact
  // shares it. One that leaves the pair out shows nothing: Microcosm lists a
  // pair only when every fact's ordinary dimensions share it or every fact's
  // layout carries it (_inherited_dimensions, ledger_targets.py:1578-1611), so
  // facts that share a value, one in its dimensions and one in its layout,
  // leave it out too. layout_groupby_unlisted_across_facts says only what the
  // fields establish: the identity spans several facts and its hierarchy lists
  // no group-by pair the target carries. Without a hierarchy the dashboard
  // cannot tell.
  test("layout_groupby_unlisted_across_facts says the hierarchy leaves a several-fact identity's group-by out", () => {
    const shownOf = (rows: JsonObject[], schemaVersion = 8) => {
      const cal = buildCalibration(
        { ...fixture, schema_version: schemaVersion, targets: rows },
        "populace-us-2024-multi-fact-test",
        null,
        {},
        {},
        {},
        "us",
      );
      return detailOf(cal, String(cal.rows[0].name)).target.chronicle as unknown as Row;
    };
    const unlisted = (rows: JsonObject[], schemaVersion = 8) => {
      const shown = shownOf(rows, schemaVersion);
      return "layout_groupby_unlisted_across_facts" in shown
        ? shown.layout_groupby_unlisted_across_facts
        : "absent";
    };
    const parentPlus = fsaRow(PARENT_PLUS).metadata as Record<string, string>;
    const twoLoanTypes = multiFactRow(
      PARENT_PLUS,
      "ed_fsa.test.two_loan_types_rolled_forward",
      "rolled_forward_by_ratio",
      [parentPlus.ledger_fact_key, SECOND_BASE_CELL, NUMERATOR, DENOMINATOR],
      { ledger_value_base_cells: "2" },
    );
    (twoLoanTypes.hierarchy as { dimensions: unknown[] }).dimensions = [];
    const twoAgeBands = multiFactRow(
      ALABAMA_BALANCE,
      "ed_fsa.test.alabama_two_age_bands_rolled_forward",
      "rolled_forward_by_ratio",
      [own.ledger_fact_key, SECOND_BASE_CELL, NUMERATOR, DENOMINATOR],
      { ledger_value_base_cells: "2" },
    );
    const withoutHierarchy = fsaRow(EXCLUDING_PARENT_PLUS);
    delete withoutHierarchy.hierarchy;
    // Allocated residuals with one base cell and with two.
    const oneBaseAllocated = allocated("ed_fsa.test.alabama_allocated", [own.ledger_fact_key]);
    const twoBaseAllocated = allocated("ed_fsa.test.alabama_two_cells_allocated", [
      own.ledger_fact_key,
      SECOND_BASE_CELL,
    ]);
    const cases = {
      excludingParentPlus: () => [fsaRow(EXCLUDING_PARENT_PLUS)],
      twoLoanTypes: () => [twoLoanTypes],
      twoAgeBands: () => [twoAgeBands],
      ratioScaled: () => [scaled()],
      ratioScaledOrdinaryLocation: () => [scaled(ORDINARY_LOCATION)],
      oneBaseRolledForward: () => [rolledForward()],
      oneBaseRolledForwardOrdinaryLocation: () => [rolledForward(ORDINARY_LOCATION)],
      oneBaseAllocated: () => [structuredClone(oneBaseAllocated)],
      twoBaseAllocated: () => [structuredClone(twoBaseAllocated)],
      singleFact: () => [fsaRow(ALABAMA_BALANCE)],
    };

    const results: Record<string, unknown> = {
      ...Object.fromEntries(Object.entries(cases).map(([name, rows]) => [name, unlisted(rows())])),
      // A schema 6 release has no hierarchy.
      withoutHierarchy: unlisted([withoutHierarchy], 6),
    };
    expect(results).toEqual({
      excludingParentPlus: true,
      twoLoanTypes: true,
      twoAgeBands: false,
      ratioScaled: false,
      ratioScaledOrdinaryLocation: false,
      oneBaseRolledForward: false,
      oneBaseRolledForwardOrdinaryLocation: false,
      oneBaseAllocated: false,
      twoBaseAllocated: false,
      singleFact: "absent",
      withoutHierarchy: null,
    });
    // Whether the identity spans several facts, which the Dimensions column
    // reads: a ratio's operands raise its member count, not its identity, and a
    // one-base rolled-forward or allocated residual target's identity is its
    // base cell (facts[:-2], :1030-1031; the selector's matches, :1024-1029).
    const spans = (rows: JsonObject[], schemaVersion = 8) => {
      const shown = shownOf(rows, schemaVersion);
      return "identity_spans_several_facts" in shown ? shown.identity_spans_several_facts : "absent";
    };
    const spanning: Record<string, unknown> = {
      ...Object.fromEntries(Object.entries(cases).map(([name, rows]) => [name, spans(rows())])),
      withoutHierarchy: spans([withoutHierarchy], 6),
    };
    expect(spanning).toEqual({
      excludingParentPlus: true,
      twoLoanTypes: true,
      twoAgeBands: true,
      ratioScaled: false,
      ratioScaledOrdinaryLocation: false,
      oneBaseRolledForward: false,
      oneBaseRolledForwardOrdinaryLocation: false,
      oneBaseAllocated: false,
      twoBaseAllocated: true,
      singleFact: "absent",
      withoutHierarchy: true,
    });
    // No shown field claims that the identity facts differ in the value.
    for (const rows of Object.values(cases)) {
      expect(shownOf(rows())).not.toHaveProperty("layout_groupby_value_varies");
    }
    expect(shownOf([withoutHierarchy], 6)).not.toHaveProperty("layout_groupby_value_varies");
  });

  // Targets that Microcosm's own catalog resolver and compiler build at
  // 0ff293b, from the reviews of 67e6d0b (hierarchy_probe.py) and 836fc66
  // (producer.py). Each sums two facts of loan type A, by a sum reference that
  // selects both:
  // - mixed storage: one fact carries loan type A in its dimensions and the
  //   other in its layout's group-by. The label validator reads the group-by
  //   pair as a dimension (:1490), so both describe loan type A, but the
  //   hierarchy lists no loan type. The resolver sorts the facts by key
  //   (:2178-2183) and the compiler takes the last as the representative
  //   (:1232-1245), so with keys a-explicit and b-layout the representative
  //   carries the layout pair loan type A, and with a-explicit renamed
  //   z-explicit it carries none;
  // - both facts carry loan type A in their dimensions, and either only the
  //   representative's layout carries loan type A or the other's carries B.
  //   Their layouts share no constant pair, so in these two cases the
  //   hierarchy lists loan type A. A constant layout pair would take the place
  //   of the ordinary one (_inherited_dimensions, :1582-1599).
  test("a hierarchy that leaves out a pair the identity facts share claims no variation", () => {
    const shown = PROBE_TARGETS.map((probe) => {
      const cal = buildCalibration(
        { ...fixture, targets: [probeRow(probe)] },
        "mixed-groupby-storage-test",
        null,
        {},
        {},
        {},
        "uk",
      );
      return [probe.case, listedChronicle(cal) as unknown as Row] as const;
    });
    const [mixed, onlyRepresentative, conflicting, renamed] = shown;
    const listed = [onlyRepresentative, conflicting];

    expect(mixed[0]).toBe("ordinary a + layout a; disjoint sex cells");
    expect(mixed[1]).not.toHaveProperty("layout_groupby_value_varies");
    expect(mixed[1]).toMatchObject({
      layout_groupby_dimension: "loan_type",
      layout_groupby_value_id: null,
      layout_groupby_unlisted_across_facts: true,
      identity_spans_several_facts: true,
      member_fact_count: 2,
    });
    expect(listed.map(([name]) => name)).toEqual([
      "ordinary a in both; only representative layout a",
      "ordinary a in both; first layout b and representative layout a",
    ]);
    for (const [, chronicle] of listed) {
      expect(chronicle).not.toHaveProperty("layout_groupby_value_varies");
      expect(chronicle).toMatchObject({
        layout_groupby_dimension: "loan_type",
        layout_groupby_value_id: "a",
        layout_groupby_unlisted_across_facts: false,
        identity_spans_several_facts: true,
      });
    }
    // The same facts as the first, one renamed: the representative carries no
    // group-by pair, so the fields say nothing about one, but the identity
    // still spans both facts.
    expect(renamed[0]).toBe("ordinary a + layout a; disjoint sex cells; explicit fact renamed z-explicit");
    expect(renamed[1]).not.toHaveProperty("layout_groupby_value_varies");
    expect(renamed[1]).toMatchObject({
      layout_groupby_value_id: null,
      layout_groupby_unlisted_across_facts: null,
      identity_spans_several_facts: true,
      member_fact_count: 2,
    });
  });

  // A member fact of a target is not the target, and one fact can be a member
  // of many targets: Microcosm restates its twelve CGT Table 5 region cells by
  // one Table 1 share (tools/generate_uk_target_references.py:1117-1119 at
  // 0ff293b). So a lookup by a member fact finds no target, and names every
  // target that lists it, for the investigation to go on with the one it
  // means. The investigation packet script prints the detail
  // (scripts/microcosm-investigation-packet.mjs:85-86).
  test("a lookup by a member fact names the targets that list it", () => {
    const parentPlus = fsaRow(PARENT_PLUS).metadata as Record<string, string>;
    const excluding = fsaRow(EXCLUDING_PARENT_PLUS);
    const excludingMetadata = excluding.metadata as Record<string, string>;
    const loanTypes = JSON.parse(excludingMetadata.ledger_member_fact_keys) as string[];
    const scaledAlabama = ONE_FACT_IDENTITY[0].row();
    const scaledParentPlus = multiFactRow(
      PARENT_PLUS,
      "ed_fsa.test.parent_plus_scaled",
      "scaled_by_ratio",
      [parentPlus.ledger_fact_key, NUMERATOR, DENOMINATOR],
    );
    const cal = releaseOf([excluding, scaledAlabama, scaledParentPlus]);
    const RELEASE_ID = "populace-us-2024-multi-fact-test";
    // The answer as the API sends it.
    const answer = (key: string) => lookup(cal, key) as unknown as Row;
    const excludingTarget = {
      name: `${EXCLUDING_PARENT_PLUS}@2024`,
      target_label: "Federal student loan balance excluding Parent PLUS",
    };

    // A loan type of the balance excluding Parent PLUS, by the key Microcosm
    // lists it under.
    expect(answer(loanTypes[0])).toEqual({
      available: false,
      release_id: RELEASE_ID,
      detail: `Target not found: ${loanTypes[0]}. It is a member fact of 1 target: ${excludingTarget.name}.`,
      member_of: [excludingTarget],
    });
    // Its representative loan type, by the record id and keys the row keeps.
    for (const key of [
      excludingMetadata.ledger_source_record_id,
      excludingMetadata.ledger_semantic_fact_key,
      excludingMetadata.ledger_legacy_fact_key,
    ]) {
      expect(answer(key)).toMatchObject({ available: false, member_of: [excludingTarget] });
    }
    // The numerator both ratio-scaled cells share names both.
    const numerator = answer(NUMERATOR);
    expect(numerator).toMatchObject({
      available: false,
      member_of: [
        { name: String(scaledAlabama.name), target_label: "Federal student loan balance, Alabama" },
        { name: String(scaledParentPlus.name), target_label: "Parent PLUS loan balance" },
      ],
    });
    expect(numerator.detail).toBe(
      `Target not found: ${NUMERATOR}. It is a member fact of 2 targets: ` +
        `${scaledAlabama.name}, ${scaledParentPlus.name}.`,
    );
    // A cell's own base fact still finds the cell.
    expect(answer(parentPlus.ledger_fact_key)).toMatchObject({ available: true });
    // A key that no target lists says nothing more.
    expect(answer("ledger.aggregate_fact.v2:no_such_fact")).toEqual({
      available: false,
      release_id: RELEASE_ID,
      detail: "Target not found: ledger.aggregate_fact.v2:no_such_fact",
      member_of: [],
    });
  });

  // A linear combination's identity is every fact (:1034). The five loan
  // types differ in their record, keys, group-by value and universe
  // constraint, so none shows. This is the result before this test was
  // written, kept.
  test("the FSA linear combination shows none of its five members' fields", () => {
    const cal = releaseOf([fsaRow(EXCLUDING_PARENT_PLUS)]);
    const shown = detailOf(cal, `${EXCLUDING_PARENT_PLUS}@2024`).target.chronicle as unknown as Row;
    expect(identityOf(shown)).toEqual(NULL_IDENTITY);
    expect(shown.value_operation).toBe("linear_combination");
    expect(shown.member_fact_count).toBe(5);
  });
});
