import { describe, expect, test } from "bun:test";

import type { ExplorerState } from "./calibration-explorer";
import maineRelease from "./fixtures/us-766b7c7-maine-targets.json";
import type { CalibrationProvenance } from "./target-loss-attribution";
import {
  applyExplorerFilters,
  buildCalibrationTree,
  effectiveNodeMetric,
  fitBandForTarget,
  type CalibrationTreeDimension,
  type CalibrationTreeTarget,
} from "./calibration-tree";

const EMPTY_FILTERS = {
  geographyLevels: [],
  geographies: [],
  fitBands: [],
  comparisonFits: [],
  calibrationStatuses: [],
};

function state(
  path: ExplorerState["path"],
  breakdown: ExplorerState["breakdown"] = "program",
): ExplorerState {
  return { breakdown, path, filters: EMPTY_FILTERS };
}

function target(
  name: string,
  overrides: Partial<CalibrationTreeTarget>,
): CalibrationTreeTarget {
  return {
    name,
    abs_relative_error: 0.04,
    calibration_status: "included",
    target_dimensions: [],
    ...overrides,
  };
}

const rows: CalibrationTreeTarget[] = [
  target("population/child/CA", {
    source: "census",
    variable: "population",
    measure: "count",
    source_measure_id: "population",
    level: "state",
    geography: "CA",
    target_dimensions: [
      { key: "bd_age", label: "Age", value: "Child" },
      { key: "bd_sex", label: "Sex", value: "Female" },
    ],
  }),
  target("population/adult/CA", {
    source: "census",
    variable: "population",
    measure: "count",
    source_measure_id: "population",
    level: "state",
    geography: "CA",
    abs_relative_error: 0.12,
    calibration_status: "skipped",
    target_dimensions: [
      { key: "bd_age", label: "Age", value: "Adult" },
      { key: "bd_sex", label: "Sex", value: "Female" },
    ],
  }),
  target("population/child/NY", {
    source: "census",
    variable: "population",
    measure: "count",
    source_measure_id: "population",
    level: "state",
    geography: "NY",
    target_dimensions: [{ key: "bd_age", label: "Age", value: "Child" }],
  }),
  ...["CA", "United States"].flatMap((geography) =>
    ["count", "total"].map((measure) =>
      target(`ctc/${geography}/${measure}`, {
        source: "irs_soi",
        variable: "ctc",
        measure,
        source_measure_id: measure === "count" ? "ctc_claims" : "ctc_amount",
        level: geography === "United States" ? "national" : "state",
        geography,
        target_dimensions: [
          { key: "bd_income_band", label: "Income band", value: "All" },
          { key: "bd_filing_status", label: "Filing status", value: "All" },
        ],
      }),
    ),
  ),
  ...["All", "Under 1"].flatMap((incomeBand) =>
    ["count", "total"].map((measure) =>
      target(`interest/CA/${incomeBand}/${measure}`, {
        source: "irs_soi",
        variable: "taxable interest income",
        measure,
        source_measure_id:
          measure === "count" ? "taxable_interest_returns" : "taxable_interest_amount",
        level: "state",
        geography: "CA",
        target_dimensions: [
          { key: "bd_income_band", label: "Income band", value: incomeBand },
          { key: "bd_filing_status", label: "Filing status", value: "All" },
        ],
      }),
    ),
  ),
];

describe("source, geography, and declared-dimension hierarchy", () => {
  test("aggregates normalized target contributions from leaves to the root", () => {
    const attributedRows = [
      target("one", {
        source: "source-a",
        variable: "program-a",
        target_loss_weight_share: 0.1,
        final_capped_scaled_error: 0.2,
        final_loss_contribution: 0.02,
      }),
      target("two", {
        source: "source-a",
        variable: "program-a",
        target_loss_weight_share: 0.1,
        final_capped_scaled_error: 0.3,
        final_loss_contribution: 0.03,
      }),
      target("three", {
        source: "source-b",
        variable: "program-b",
        target_loss_weight_share: 0.8,
        final_capped_scaled_error: 0.125,
        final_loss_contribution: 0.1,
      }),
    ];
    const tree = buildCalibrationTree(attributedRows, state({ dimensions: [] }));

    expect(tree.lossAttributionAvailable).toBe(true);
    expect(tree.filteredMetrics.loss).toBeCloseTo(0.15, 15);
    expect(tree.filteredMetrics.targetLossWeightShare).toBeCloseTo(1, 15);
    expect(tree.filteredMetrics.weightedAverageCappedError).toBeCloseTo(0.15, 15);
    expect(tree.groups[0].metrics.weightedAverageCappedError).toBeCloseTo(0.25, 15);
    expect(
      effectiveNodeMetric(tree.groups.flatMap((group) => group.nodes), "weight")
        .reduce((sum, value) => sum + value, 0),
    ).toBeCloseTo(1, 15);
    expect(tree.groups.reduce((sum, group) => sum + group.metrics.loss, 0)).toBeCloseTo(
      0.15,
      15,
    );
  });

  test("reports an ordinary unavailable state when contributions are absent", () => {
    const tree = buildCalibrationTree(rows, state({ dimensions: [] }));

    expect(tree.lossAttributionAvailable).toBe(false);
    expect(tree.filteredMetrics.loss).toBe(0);
  });

  test("presents targets without geography as national before their dimensions", () => {
    const geographylessRows = ["Adult", "Child"].map((age) =>
      target(`geographyless/${age}`, {
        source: "test",
        variable: "geographyless program",
        target_dimensions: [{ key: "bd_age", label: "Age", value: age }],
      }),
    );
    const programPath = {
      source: "test",
      program: "geographyless program",
      dimensions: [],
    };
    const tree = buildCalibrationTree(geographylessRows, state(programPath));

    expect(tree.currentLevel).toEqual({ kind: "geography", label: "Geography" });
    expect(tree.groups[0].nodes.map((node) => node.label)).toEqual(["United States"]);

    const national = buildCalibrationTree(
      geographylessRows,
      state({ ...programPath, geography: "United States" }),
    );
    expect(national.currentLevel).toEqual({ kind: "dimension", key: "bd_age", label: "Age" });
    expect(national.groups.map((group) => group.id)).toEqual(["bd_age"]);
    expect(national.groups[0].nodes.map((node) => node.label)).toEqual(["Adult", "Child"]);

    const adult = buildCalibrationTree(
      geographylessRows,
      state({
        ...programPath,
        geography: "United States",
        dimensions: [{ key: "bd_age", label: "Age", value: "Adult" }],
      }),
    );
    expect(adult.currentLevel).toEqual({ kind: "target", label: "Targets" });
    expect(adult.groups[0].nodes.map((node) => node.id)).toEqual([
      "geographyless/Adult",
    ]);
  });

  test("renders statistics inside source groups at the overview", () => {
    const tree = buildCalibrationTree(rows, state({ dimensions: [] }), "release-1");

    expect(tree.currentLevel).toEqual({ kind: "overview", label: "Programs" });
    expect(tree.groups.map((group) => group.id)).toEqual(["irs_soi", "census"]);
    expect(tree.groups[0].label).toBe("IRS Statistics of Income");
    expect(tree.groups.flatMap((group) => group.nodes)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          kind: "program",
          label: "CTC",
          selection: { kind: "program", source: "irs_soi", value: "ctc" },
          metrics: expect.objectContaining({ nTargets: 4 }),
        }),
        expect.objectContaining({
          kind: "program",
          label: "Population",
          metrics: expect.objectContaining({ nTargets: 3 }),
        }),
      ]),
    );
  });

  test("prefers artifact publisher labels for source groups", () => {
    const tree = buildCalibrationTree(
      [
        target("artifact-labelled", {
          source: "novastat_agency",
          source_label: "Nova Statistics Agency",
          variable: "population",
        }),
      ],
      state({ dimensions: [] }),
    );

    expect(tree.groups[0].label).toBe("Nova Statistics Agency");
  });

  test("retains artifact source and category labels throughout the selected path", () => {
    const tree = buildCalibrationTree(
      [
        target("obr-income-tax", {
          source: "obr",
          source_label: "Office for Budget Responsibility",
          variable: "efo_receipts",
          variable_label: "EFO receipts",
          geography: "United Kingdom",
        }),
      ],
      state({
        source: "obr",
        program: "efo_receipts",
        dimensions: [],
      }),
    );

    expect(tree.pathLabels).toEqual({
      source: "Office for Budget Responsibility",
      program: "EFO receipts",
    });
    expect(tree.groups[0].label).toBe("EFO receipts");
  });

  test("renders geography first, then programs grouped by their source", () => {
    const overview = buildCalibrationTree(
      rows,
      state({ dimensions: [] }, "geography"),
    );

    expect(overview.currentLevel).toEqual({ kind: "geography", label: "Geography" });
    expect(overview.groups).toHaveLength(1);
    expect(overview.groups[0].label).toBe("Geography");
    expect(overview.groups[0].nodes.map((node) => [node.id, node.metrics.nTargets])).toEqual([
      ["CA", 8],
      ["United States", 2],
      ["NY", 1],
    ]);

    const programs = buildCalibrationTree(
      rows,
      state({ geography: "CA", dimensions: [] }, "geography"),
    );
    expect(programs.currentLevel).toEqual({ kind: "overview", label: "Programs" });
    expect(
      programs.groups.map((group) => [
        group.label,
        group.nodes.map((node) => [node.id, node.label]),
      ]),
    ).toEqual([
      [
        "IRS Statistics of Income",
        [
          ["taxable interest income", "Taxable interest income"],
          ["ctc", "CTC"],
        ],
      ],
      ["Census", [["population", "Population"]]],
    ]);
  });

  test("descends directly from a statistic to geography without a measure level", () => {
    const tree = buildCalibrationTree(
      rows,
      state({ source: "irs_soi", program: "ctc", dimensions: [] }),
    );

    expect(tree.currentLevel).toEqual({ kind: "geography", label: "Geography" });
    expect(tree.groups[0].nodes.map((node) => [node.id, node.metrics.nTargets])).toEqual([
      ["CA", 2],
      ["United States", 2],
    ]);
    expect(tree.groups[0].nodes[0].selection).toEqual({ kind: "geography", value: "CA" });
  });

  test("shows CTC targets immediately because its declared dimensions are constant", () => {
    const tree = buildCalibrationTree(
      rows,
      state({ source: "irs_soi", program: "ctc", geography: "CA", dimensions: [] }),
    );

    expect(tree.currentLevel).toEqual({ kind: "target", label: "Targets" });
    expect(tree.groups).toHaveLength(1);
    expect(tree.groups[0].nodes.map((node) => [node.kind, node.id, node.label])).toEqual([
      ["target", "ctc/CA/total", "Ctc Amount"],
      ["target", "ctc/CA/count", "Ctc Claims"],
    ]);
    expect(tree.dimensionOrder).toEqual([
      { key: "bd_filing_status", label: "Filing status" },
      { key: "bd_income_band", label: "Income band" },
    ]);
  });

  test("keeps All as an income-band value when other values exist and skips filing status", () => {
    const income = buildCalibrationTree(
      rows,
      state({
        source: "irs_soi",
        program: "taxable interest income",
        geography: "CA",
        dimensions: [],
      }),
    );

    expect(income.currentLevel).toEqual({
      kind: "dimension",
      key: "bd_income_band",
      label: "Income band",
    });
    expect(income.groups[0].nodes.map((node) => [node.label, node.metrics.nTargets])).toEqual([
      ["All", 2],
      ["Under 1", 2],
    ]);

    const targets = buildCalibrationTree(
      rows,
      state({
        source: "irs_soi",
        program: "taxable interest income",
        geography: "CA",
        dimensions: [{ key: "bd_income_band", label: "Income band", value: "All" }],
      }),
    );
    expect(targets.currentLevel).toEqual({ kind: "target", label: "Targets" });
    expect(targets.groups[0].nodes).toHaveLength(2);
  });

  test("uses only varying dimensions as breakdowns while retaining present dimension metadata", () => {
    const tree = buildCalibrationTree(
      rows,
      state({ source: "census", program: "population", geography: "CA", dimensions: [] }),
    );

    expect(tree.currentLevel).toEqual({ kind: "dimension", key: "bd_age", label: "Age" });
    expect(tree.groups[0].nodes.map((node) => node.label)).toEqual(["Adult", "Child"]);
    expect(tree.dimensionOrder).toEqual([
      { key: "bd_age", label: "Age" },
      { key: "bd_sex", label: "Sex" },
    ]);
  });

  test("automatically uses a new semantic dimension when it varies", () => {
    const dynamicRows = ["Employed", "Unemployed"].map((employmentStatus) =>
      target(`dynamic/${employmentStatus}`, {
        source: "test",
        variable: "dynamic",
        geography: "United States",
        target_dimensions: [
          {
            key: "bd_employment_status",
            label: "Employment Status",
            value: employmentStatus,
          },
        ],
      }),
    );

    const tree = buildCalibrationTree(
      dynamicRows,
      state({
        source: "test",
        program: "dynamic",
        geography: "United States",
        dimensions: [],
      }),
    );

    expect(tree.currentLevel).toEqual({
      kind: "dimension",
      key: "bd_employment_status",
      label: "Employment status",
    });
    expect(tree.groups[0].nodes.map((node) => node.label)).toEqual([
      "Employed",
      "Unemployed",
    ]);
    expect(tree.dimensionOrder).toEqual([
      { key: "bd_employment_status", label: "Employment status" },
    ]);
  });

  test("lets a new lower-cardinality dimension move ahead of an existing dimension", () => {
    const dynamicRows = ["Adult", "Child", "Senior"].flatMap((age) =>
      ["Employed", "Unemployed"].map((employmentStatus) =>
        target(`dynamic/${age}/${employmentStatus}`, {
          source: "test",
          variable: "dynamic",
          geography: "United States",
          target_dimensions: [
            { key: "bd_age", label: "Age Group", value: age },
            {
              key: "bd_employment_status",
              label: "Employment Status",
              value: employmentStatus,
            },
          ],
        }),
      ),
    );

    const tree = buildCalibrationTree(
      dynamicRows,
      state({
        source: "test",
        program: "dynamic",
        geography: "United States",
        dimensions: [],
      }),
    );

    expect(tree.dimensionOrder).toEqual([
      { key: "bd_employment_status", label: "Employment status" },
      { key: "bd_age", label: "Age group" },
    ]);
    expect(tree.currentLevel).toEqual({
      kind: "dimension",
      key: "bd_employment_status",
      label: "Employment status",
    });

    const employed = buildCalibrationTree(
      dynamicRows,
      state({
        source: "test",
        program: "dynamic",
        geography: "United States",
        dimensions: [
          {
            key: "bd_employment_status",
            label: "Employment status",
            value: "Employed",
          },
        ],
      }),
    );

    expect(employed.currentLevel).toEqual({
      kind: "dimension",
      key: "bd_age",
      label: "Age group",
    });
  });

  test("does not turn measurement concepts or anonymous legacy dimensions into breakdowns", () => {
    const measurementRows = [
      ["bd_measure", "Measure"],
      ["bd_amount", "Amount"],
      ["bd_count", "Count"],
      ["dim0", "Breakdown 1"],
    ].flatMap(([key, label]) =>
      ["First", "Second"].map((value) =>
        target(`measurement/${key}/${value}`, {
          source: "test",
          variable: "measurement",
          geography: "United States",
          target_dimensions: [{ key, label, value }],
        }),
      ),
    );

    const tree = buildCalibrationTree(
      measurementRows,
      state({
        source: "test",
        program: "measurement",
        geography: "United States",
        dimensions: [],
      }),
    );

    expect(tree.currentLevel).toEqual({ kind: "target", label: "Targets" });
    expect(tree.dimensionOrder).toEqual([]);
    expect(tree.groups[0].nodes).toHaveLength(8);
  });

  test("partitions sparse and ambiguous dimensions without missing buckets or duplicate targets", () => {
    const sparseRows = [
      target("sparse/age/adult", {
        source: "test",
        variable: "sparse",
        geography: "United States",
        target_dimensions: [
          { key: "bd_age", label: "Age", value: "Adult" },
          { key: "bd_program", label: "Program", value: "A" },
        ],
      }),
      target("sparse/age/senior", {
        source: "test",
        variable: "sparse",
        geography: "United States",
        target_dimensions: [
          { key: "bd_age", label: "Age", value: "Senior" },
          { key: "bd_program", label: "Program", value: "B" },
        ],
      }),
      target("sparse/program/a", {
        source: "test",
        variable: "sparse",
        geography: "United States",
        target_dimensions: [{ key: "bd_program", label: "Program", value: "A" }],
      }),
      target("sparse/program/b", {
        source: "test",
        variable: "sparse",
        geography: "United States",
        target_dimensions: [{ key: "bd_program", label: "Program", value: "B" }],
      }),
      target("sparse/no-dimensions", {
        source: "test",
        variable: "sparse",
        geography: "United States",
      }),
      target("sparse/ambiguous-income", {
        source: "test",
        variable: "sparse",
        geography: "United States",
        target_dimensions: [
          { key: "bd_income_band", label: "Income band", value: "All" },
          { key: "bd_income_band", label: "Income band", value: "Total" },
        ],
      }),
    ];

    const tree = buildCalibrationTree(
      sparseRows,
      state({ source: "test", program: "sparse", geography: "United States", dimensions: [] }),
    );

    expect(tree.currentLevel).toEqual({ kind: "mixed", label: "Breakdowns and targets" });
    expect(tree.groups.map((group) => [group.id, group.nodes.length, group.metrics.nTargets])).toEqual([
      ["bd_program", 2, 2],
      ["bd_age", 2, 2],
      ["targets", 2, 2],
    ]);
    expect(tree.groups.flatMap((group) => group.nodes).some((node) => node.id === "__missing__")).toBe(false);
    expect(tree.groups.reduce((sum, group) => sum + group.metrics.nTargets, 0)).toBe(6);
    expect(tree.groups.find((group) => group.id === "targets")?.nodes.map((node) => node.id)).toEqual([
      "sparse/ambiguous-income",
      "sparse/no-dimensions",
    ]);

    const programA = buildCalibrationTree(
      sparseRows,
      state({
        source: "test",
        program: "sparse",
        geography: "United States",
        dimensions: [{ key: "bd_program", label: "Program", value: "A" }],
      }),
    );
    expect(programA.groups.flatMap((group) => group.nodes).map((node) => node.id)).toEqual([
      "sparse/program/a",
    ]);
  });
});

describe("schema 8 ordered hierarchy", () => {
  const inheritedProvenance: CalibrationProvenance = {
    mode: "inherited",
    dataset_release_id: "candidate-release",
    calibration_source_id: "parent-build",
    diagnostics_sha256: "a".repeat(64),
    target_surface_sha256: "b".repeat(64),
    validation: { status: "verified", reason: null },
  };

  const hierarchyRows: CalibrationTreeTarget[] = [
    target("target-adult@2025", {
      source: "provider-a",
      source_label: "Provider A — authored",
      variable: "provider-a.category",
      variable_label: "Category A — authored",
      geography: "United Kingdom — authored",
      level: "country",
      target_label: "Adult target — authored",
      target_representation: "hierarchy",
      target_dimensions: [
        {
          key: "dimension.age",
          label: "Age — authored",
          value: "Adults — authored",
          value_id: "adult",
        },
        {
          key: "dimension.sex",
          label: "Sex — authored",
          value: "Female — authored",
          value_id: "female",
        },
      ],
    }),
    target("target-child@2025", {
      source: "provider-a",
      source_label: "Provider A — authored",
      variable: "provider-a.category",
      variable_label: "Category A — authored",
      geography: "United Kingdom — authored",
      level: "country",
      target_label: "Child target — authored",
      target_representation: "hierarchy",
      target_dimensions: [
        {
          key: "dimension.age",
          label: "Age — authored",
          value: "Children — authored",
          value_id: "child",
        },
        {
          key: "dimension.sex",
          label: "Sex — authored",
          value: "Female — authored",
          value_id: "female",
        },
      ],
    }),
    target("target-total@2025", {
      source: "provider-a",
      source_label: "Provider A — authored",
      variable: "provider-a.category",
      variable_label: "Category A — authored",
      geography: "United Kingdom — authored",
      level: "country",
      target_label: "Total target — authored",
      target_representation: "hierarchy",
      target_dimensions: [],
    }),
  ];

  const hierarchyPath = {
    source: "provider-a",
    program: "provider-a.category",
    geography: "United Kingdom — authored",
    dimensions: [],
  };

  test("uses producer order for ragged paths and keeps zero-dimension targets", () => {
    const tree = buildCalibrationTree(hierarchyRows, state(hierarchyPath));

    expect(tree.currentLevel).toEqual({
      kind: "mixed",
      label: "Breakdowns and targets",
    });
    expect(tree.dimensionOrder).toEqual([
      { key: "dimension.age", label: "Age — authored" },
      { key: "dimension.sex", label: "Sex — authored" },
    ]);
    expect(tree.groups.map((group) => group.id)).toEqual([
      "dimension.age",
      "targets",
    ]);
    expect(tree.groups[0].nodes.map((node) => [node.id, node.label])).toEqual([
      ["adult", "Adults — authored"],
      ["child", "Children — authored"],
    ]);
    expect(tree.groups[1].nodes[0].label).toBe("Total target — authored");
  });

  test("uses stable value ids while preserving labels and collapsing one choice", () => {
    const tree = buildCalibrationTree(
      hierarchyRows,
      state({
        ...hierarchyPath,
        dimensions: [
          { key: "dimension.age", label: "Age — authored", value: "adult" },
        ],
      }),
    );

    expect(tree.currentLevel).toEqual({ kind: "target", label: "Targets" });
    expect(tree.pathLabels.geography).toBe("United Kingdom — authored");
    expect(tree.pathLabels.dimensions).toEqual(["Adults — authored"]);
    expect(tree.groups[0].nodes.map((node) => node.label)).toEqual([
      "Adult target — authored",
    ]);
    expect(tree.groups[0].nodes[0].target?.target_dimensions).toHaveLength(2);
  });

  test("preserves inherited calibration provenance through hierarchy navigation", () => {
    const tree = buildCalibrationTree(
      hierarchyRows,
      state({
        ...hierarchyPath,
        dimensions: [
          { key: "dimension.age", label: "Age — authored", value: "adult" },
        ],
      }),
      "candidate-release",
      true,
      inheritedProvenance,
    );

    expect(tree.calibrationProvenance).toEqual(inheritedProvenance);
  });
});

// The reader keeps the first spelling of a label that rows spell more than
// one way and reports the others (validateHierarchyTargets), so the builder
// receives every row with its own spelling and must not refuse them. The
// fixtures copy release populace-us-2024-0fb05b6-4b57d15a287c-20260930T150401Z,
// whose national EITC rows label income band under_1 two ways.
describe("schema 8 labels that rows spell more than one way", () => {
  const INCOME_KEY = "us:statutes/26/62#adjusted_gross_income";
  const CHILDREN_KEY = "irs_soi.qualifying_children";
  const eitcPath = {
    source: "irs_soi",
    program: "irs_soi.eitc",
    geography: "United States",
    dimensions: [],
  };

  function eitcRow(
    name: string,
    dimensions: CalibrationTreeDimension[],
  ): CalibrationTreeTarget {
    return target(name, {
      source: "irs_soi",
      source_label: "IRS Statistics of Income",
      variable: "irs_soi.eitc",
      variable_label: "EITC",
      geography: "United States",
      level: "country",
      target_label: name,
      target_representation: "hierarchy",
      target_dimensions: dimensions,
    });
  }

  function incomeBand(
    valueId: string,
    valueLabel: string,
    dimensionLabel = "Adjusted gross income",
  ): CalibrationTreeDimension {
    return {
      key: INCOME_KEY,
      label: dimensionLabel,
      value: valueLabel,
      value_id: valueId,
    };
  }

  const oneChild: CalibrationTreeDimension = {
    key: CHILDREN_KEY,
    label: "Qualifying children",
    value: "One qualifying child",
    value_id: "one",
  };

  // The two rows that label value ID under_1 differently. The builder skips a
  // dimension with a single value ID as a level, so the third row, in
  // another income band, makes the income band a level it groups.
  const tableRow = eitcRow("irs_soi.ty2023.table_2_5.eitc_total.under_1", [
    incomeBand("under_1", "No adjusted gross income"),
  ]);
  const filingSeasonRow = eitcRow(
    "irs_soi.ty2024.filing_season_week47.eitc_by_agi.under_1.returns",
    [incomeBand("under_1", "No adjusted gross income and deficit")],
  );
  const otherBandRow = eitcRow("irs_soi.ty2023.table_2_5.eitc_total.1k_to_2k", [
    incomeBand("1k_to_2k", "$1,000 under $2,000"),
  ]);

  function incomeNodes(rows: CalibrationTreeTarget[]) {
    const tree = buildCalibrationTree(rows, state(eitcPath));
    return tree.groups
      .find((group) => group.id === INCOME_KEY)
      ?.nodes.map((treeNode) => [treeNode.id, treeNode.label, treeNode.metrics.nTargets]);
  }

  test("labels a value ID that rows spell two ways with its first spelling in row order", () => {
    expect(incomeNodes([tableRow, filingSeasonRow, otherBandRow])).toEqual([
      ["under_1", "No adjusted gross income", 2],
      ["1k_to_2k", "$1,000 under $2,000", 1],
    ]);
    expect(incomeNodes([filingSeasonRow, tableRow, otherBandRow])).toEqual([
      ["under_1", "No adjusted gross income and deficit", 2],
      ["1k_to_2k", "$1,000 under $2,000", 1],
    ]);
  });

  test("names a selected value ID that rows spell two ways with its first spelling", () => {
    const tree = buildCalibrationTree(
      [tableRow, filingSeasonRow, otherBandRow],
      state({
        ...eitcPath,
        dimensions: [
          { key: INCOME_KEY, label: "Adjusted gross income", value: "under_1" },
        ],
      }),
    );

    expect(tree.pathLabels.dimensions).toEqual(["No adjusted gross income"]);
    expect(tree.groups.flatMap((group) => group.nodes).map((treeNode) => treeNode.id)).toEqual([
      "irs_soi.ty2023.table_2_5.eitc_total.under_1",
      "irs_soi.ty2024.filing_season_week47.eitc_by_agi.under_1.returns",
    ]);
  });

  test("keeps a value ID's first spelling when a filter hides the row that carries it", () => {
    // A published tree is compiled without filters and keeps its node labels
    // when a filter is applied, so a live tree must label a value from all
    // of its rows, not only the visible ones, and its selected path must
    // agree.
    const poorlyFitTableRow = { ...tableRow, abs_relative_error: 0.5 };
    const rows = [poorlyFitTableRow, filingSeasonRow, otherBandRow];
    const wellFitOnly = { ...EMPTY_FILTERS, fitBands: ["0_5" as const] };

    const level = buildCalibrationTree(rows, {
      breakdown: "program",
      path: eitcPath,
      filters: wellFitOnly,
    });
    const selected = buildCalibrationTree(rows, {
      breakdown: "program",
      path: {
        ...eitcPath,
        dimensions: [
          { key: INCOME_KEY, label: "Adjusted gross income", value: "under_1" },
        ],
      },
      filters: wellFitOnly,
    });

    expect(
      level.groups
        .find((group) => group.id === INCOME_KEY)
        ?.nodes.map((treeNode) => [treeNode.id, treeNode.label, treeNode.metrics.nTargets]),
    ).toEqual([
      ["1k_to_2k", "$1,000 under $2,000", 1],
      ["under_1", "No adjusted gross income", 1],
    ]);
    expect(selected.pathLabels.dimensions).toEqual(["No adjusted gross income"]);
  });

  test("labels a dimension that rows spell two ways with its first spelling in row order", () => {
    const plainRow = eitcRow("irs_soi.ty2023.table_2_5.eitc_total.under_1", [
      incomeBand("under_1", "No adjusted gross income"),
    ]);
    const abbreviatedRow = eitcRow("irs_soi.ty2023.table_2_5.eitc_total.1k_to_2k", [
      incomeBand("1k_to_2k", "$1,000 under $2,000", "Adjusted gross income (AGI)"),
    ]);

    const tree = buildCalibrationTree([plainRow, abbreviatedRow], state(eitcPath));

    const plainDimension = { key: INCOME_KEY, label: "Adjusted gross income" };
    expect(tree.dimensionOrder).toEqual([plainDimension]);
    expect(tree.currentLevel).toEqual({ kind: "dimension", ...plainDimension });
    expect(tree.groups.map((group) => [group.id, group.label])).toEqual([
      [INCOME_KEY, "Adjusted gross income"],
    ]);
    expect(tree.groups[0].nodes.map((treeNode) => treeNode.selection)).toEqual([
      { kind: "dimension_value", ...plainDimension, value: "1k_to_2k" },
      { kind: "dimension_value", ...plainDimension, value: "under_1" },
    ]);
    expect(
      buildCalibrationTree([abbreviatedRow, plainRow], state(eitcPath)).groups.map(
        (group) => group.label,
      ),
    ).toEqual(["Adjusted gross income (AGI)"]);
  });

  test("lists a dimension that rows in separate branches spell two ways with its first spelling", () => {
    // Neither row's income band becomes a level, so only the dimension order
    // of the whole scope sees both spellings.
    const directRow = eitcRow("irs_soi.direct.under_1", [
      incomeBand("under_1", "No adjusted gross income"),
    ]);
    const childrenRow = eitcRow("irs_soi.children.under_1", [
      oneChild,
      incomeBand("under_1", "No adjusted gross income", "Adjusted gross income (AGI)"),
    ]);

    const tree = buildCalibrationTree([directRow, childrenRow], state(eitcPath));

    expect(tree.dimensionOrder).toEqual([
      { key: INCOME_KEY, label: "Adjusted gross income" },
      { key: CHILDREN_KEY, label: "Qualifying children" },
    ]);
    expect(tree.groups.flatMap((group) => group.nodes).map((treeNode) => treeNode.id)).toEqual([
      "irs_soi.children.under_1",
      "irs_soi.direct.under_1",
    ]);
  });

  test("gives a dimension reached through two parent branches its first spelling in row order", () => {
    // The children branch is grouped first because its first row comes
    // first, but the earliest row that carries the income band is a direct
    // row, so the merged income level takes the direct rows' spelling.
    const rows = [
      eitcRow("irs_soi.children.total", [
        oneChild,
        { key: "irs_soi.filing_status", label: "Filing status", value: "All", value_id: "all" },
      ]),
      eitcRow("irs_soi.direct.under_1", [incomeBand("under_1", "No adjusted gross income")]),
      eitcRow("irs_soi.direct.1k_to_2k", [incomeBand("1k_to_2k", "$1,000 under $2,000")]),
      eitcRow("irs_soi.children.under_1", [
        oneChild,
        incomeBand("under_1", "No adjusted gross income", "Adjusted gross income (AGI)"),
      ]),
      eitcRow("irs_soi.children.1k_to_2k", [
        oneChild,
        incomeBand("1k_to_2k", "$1,000 under $2,000", "Adjusted gross income (AGI)"),
      ]),
    ];

    const tree = buildCalibrationTree(rows, state(eitcPath));

    expect(tree.dimensionOrder).toEqual([
      { key: CHILDREN_KEY, label: "Qualifying children" },
      { key: "irs_soi.filing_status", label: "Filing status" },
      { key: INCOME_KEY, label: "Adjusted gross income" },
    ]);
    expect(
      tree.groups.map((group) => [group.id, group.label, group.metrics.nTargets]),
    ).toEqual([
      [INCOME_KEY, "Adjusted gross income", 4],
      ["targets", "Targets", 1],
    ]);
  });

  // The reader keeps the first spelling in the whole file, which can belong
  // to another program. In the same release, taxable pension income rows
  // spell income band "all" both "All returns" and "All returns, total".
  function pensionRow(
    name: string,
    dimensions: CalibrationTreeDimension[],
  ): CalibrationTreeTarget {
    return {
      ...eitcRow(name, dimensions),
      variable: "irs_soi.taxable_pension_income",
      variable_label: "Taxable pension income",
    };
  }

  const historicPensionRow = pensionRow(
    "irs_soi.ty2022.historic_table_2.us.all.taxable_pension_income_amount",
    [incomeBand("all", "All returns")],
  );
  const totalEitcRow = eitcRow("irs_soi.ty2023.table_1_4.all.eitc_amount", [
    incomeBand("all", "All returns, total"),
  ]);

  test("labels a value ID that a node spells two ways with the spelling the reader keeps", () => {
    const rows = [
      historicPensionRow,
      totalEitcRow,
      eitcRow("irs_soi.ty2022.historic_table_2.us.all.eitc_amount", [
        incomeBand("all", "All returns"),
      ]),
      tableRow,
    ];

    expect(incomeNodes(rows)).toEqual([
      ["all", "All returns", 2],
      ["under_1", "No adjusted gross income", 1],
    ]);
    const selected = buildCalibrationTree(
      rows,
      state({
        ...eitcPath,
        dimensions: [{ key: INCOME_KEY, label: "Adjusted gross income", value: "all" }],
      }),
    );
    expect(selected.pathLabels.dimensions).toEqual(["All returns"]);
  });

  test("labels a dimension that a program spells two ways with the spelling the reader keeps", () => {
    const rows = [
      historicPensionRow,
      eitcRow("irs_soi.ty2023.table_2_5.eitc_total.under_1", [
        incomeBand("under_1", "No adjusted gross income", "AGI"),
      ]),
      otherBandRow,
    ];

    const tree = buildCalibrationTree(rows, state(eitcPath));

    const readerDimension = { key: INCOME_KEY, label: "Adjusted gross income" };
    expect(tree.dimensionOrder).toEqual([readerDimension]);
    expect(tree.currentLevel).toEqual({ kind: "dimension", ...readerDimension });
    expect(tree.groups.map((group) => [group.id, group.label])).toEqual([
      [INCOME_KEY, "Adjusted gross income"],
    ]);
  });

  test("keeps the spelling of a value ID that a node spells only one way", () => {
    // Before label variants were accepted, such a node built with its own
    // spelling. Changing it would change trees already published.
    expect(incomeNodes([historicPensionRow, totalEitcRow, tableRow])).toEqual([
      ["all", "All returns, total", 1],
      ["under_1", "No adjusted gross income", 1],
    ]);
  });
});

describe("calibration tree metrics and filters", () => {
  test("classifies targets without generated model data as skipped", () => {
    const missingModelData = target("missing-model-data", {
      source: "test",
      variable: "missing model data",
      level: "national",
      geography: "United States",
      calibration_status: "not_materialized",
      calibration_status_label: "Not materialized",
      calibration_status_reason: "The required measure column was not generated.",
    });

    const filtered = applyExplorerFilters([missingModelData], {
      ...EMPTY_FILTERS,
      calibrationStatuses: ["skipped"],
    });
    expect(filtered).toEqual([missingModelData]);

    const tree = buildCalibrationTree(
      [missingModelData],
      state({
        source: "test",
        program: "missing model data",
        geography: "United States",
        dimensions: [],
      }),
    );
    expect(tree.filterOptions.calibrationStatuses).toEqual(["included", "skipped"]);
    expect(tree.groups[0].nodes[0].target).toEqual(
      expect.objectContaining({
        calibration_status: "skipped",
        calibration_status_label: "Skipped",
        calibration_status_reason: "The required measure column was not generated.",
      }),
    );
  });

  test("falls back to target count when the selected sizing metric is all zero", () => {
    const nodes = buildCalibrationTree(
      rows.map((row) => ({ ...row, abs_relative_error: null })),
      state({ dimensions: [] }),
    ).groups.flatMap((group) => group.nodes);

    expect(effectiveNodeMetric(nodes, "loss")).toEqual(nodes.map((node) => node.metrics.nTargets));
    expect(effectiveNodeMetric(nodes, "weight")).toEqual(nodes.map((node) => node.metrics.nTargets));
  });

  test("classifies fit bands on the legend boundaries", () => {
    const band = (abs_relative_error: number | null) => fitBandForTarget({ abs_relative_error });
    expect([
      band(0), band(0.05), band(0.050001), band(0.1), band(0.100001),
      band(0.2), band(0.200001), band(0.4), band(0.400001), band(null),
    ]).toEqual([
      "0_5", "0_5", "5_10", "5_10", "10_20",
      "10_20", "20_40", "20_40", "40_plus", "unscored",
    ]);
  });

  test("ORs values within filters and ANDs across filter categories", () => {
    const filtered = applyExplorerFilters(rows, {
      geographyLevels: ["state"],
      geographies: ["CA", "NY"],
      fitBands: ["0_5", "10_20"],
      comparisonFits: [],
      calibrationStatuses: ["included", "skipped"],
    });

    expect(filtered.filter((row) => row.variable === "population").map((row) => row.name)).toEqual([
      "population/child/CA",
      "population/adult/CA",
      "population/child/NY",
    ]);
  });

  test("keeps branch dimensions stable when filters leave only one visible value", () => {
    const tree = buildCalibrationTree(rows, {
      breakdown: "program",
      path: {
        source: "census",
        program: "population",
        geography: "CA",
        dimensions: [],
      },
      filters: {
        geographyLevels: ["state"],
        geographies: ["CA"],
        fitBands: ["0_5"],
        comparisonFits: [],
        calibrationStatuses: ["included"],
      },
    });

    expect(tree.currentLevel).toEqual({ kind: "dimension", key: "bd_age", label: "Age" });
    expect(tree.groups[0].nodes.map((node) => node.label)).toEqual(["Child"]);
    expect(tree.filteredMetrics.nTargets).toBe(1);
  });

  test("treats missing geography as national in filters and filter options", () => {
    const rowsWithMissing = [
      ...rows,
      { name: "missing-place", source: "other", variable: "unknown" },
    ];
    const national = applyExplorerFilters(
      rowsWithMissing,
      {
        geographyLevels: ["national"],
        geographies: ["United States"],
        fitBands: [],
        comparisonFits: [],
        calibrationStatuses: [],
      },
    );

    expect(national.map((row) => row.name)).toContain("missing-place");

    const tree = buildCalibrationTree(rowsWithMissing, state({ dimensions: [] }));
    expect(tree.filterOptions.geographyLevels).not.toContain("__missing__");
    expect(tree.filterOptions.geographies).not.toContain("__missing__");
    expect(tree.filterOptions.geographyLevels).toContain("national");
    expect(tree.filterOptions.geographies).toContain("United States");
  });
});

// Release populace-us-2024-buildo-acs-local-766b7c7-20261017T030317Z names
// state 0400000US23 "Maine" on its SNAP and Medicaid targets and "ME" on its
// state population target, which comes later in the file. The reader keeps
// "Maine" and reports "ME" (validateHierarchyTargets) without rewriting the
// rows, so the builder receives both spellings for one geography ID and must
// still list one geography.
describe("geographies that rows spell more than one way", () => {
  function maineRow(
    name: string,
    overrides: Partial<CalibrationTreeTarget>,
  ): CalibrationTreeTarget {
    return target(name, {
      level: "state",
      geography: "Maine",
      geography_id: "0400000US23",
      target_label: name,
      target_representation: "hierarchy",
      ...overrides,
    });
  }

  const POPULATION = {
    source: "census_population",
    source_label: "Census population",
    variable: "census_population.resident_population",
    variable_label: "Resident population",
  };

  const snapRow = maineRow(
    "usda_snap.fy2024.state_benefits.nero.me.total_benefits@2024",
    {
      source: "usda_snap",
      source_label: "USDA · SNAP",
      variable: "usda_snap.usda_snap",
      variable_label: "SNAP",
    },
  );
  const medicaidRow = maineRow(
    "cms_medicaid.month2024_12.state_enrollment.me.total_medicaid_enrollment@2024",
    {
      source: "cms_medicaid",
      source_label: "CMS · Medicaid / CHIP",
      variable: "cms_medicaid.cms_medicaid",
      variable_label: "Medicaid and CHIP",
    },
  );
  const statePopulationRow = maineRow("pop_state_23@2024", {
    ...POPULATION,
    geography: "ME",
    target_label: "ME resident population",
  });
  const districtPopulationRow = maineRow("pop_cd_2301@2024", {
    ...POPULATION,
    level: "congressional_district",
    geography: "ME congressional district 1",
    geography_id: "5001900US2301",
    target_label: "ME congressional district 1 resident population",
  });
  const releaseRows = [
    snapRow,
    medicaidRow,
    statePopulationRow,
    districtPopulationRow,
  ];
  const populationPath = {
    source: POPULATION.source,
    program: POPULATION.variable,
    dimensions: [],
  };

  function nodeSummaries(tree: ReturnType<typeof buildCalibrationTree>) {
    return tree.groups
      .flatMap((group) => group.nodes)
      .map((treeNode) => [treeNode.id, treeNode.label, treeNode.metrics.nTargets]);
  }

  test("lists a geography ID that rows spell two ways as one node with its first spelling in row order", () => {
    const tree = buildCalibrationTree(
      releaseRows,
      state({ dimensions: [] }, "geography"),
    );

    expect(nodeSummaries(tree)).toEqual([
      ["Maine", "Maine", 3],
      ["ME congressional district 1", "ME congressional district 1", 1],
    ]);
    expect(tree.groups[0].nodes[0].selection).toEqual({
      kind: "geography",
      value: "Maine",
    });

    const populationFirst = buildCalibrationTree(
      [statePopulationRow, snapRow, medicaidRow, districtPopulationRow],
      state({ dimensions: [] }, "geography"),
    );
    expect(nodeSummaries(populationFirst)).toEqual([
      ["ME", "ME", 3],
      ["ME congressional district 1", "ME congressional district 1", 1],
    ]);
  });

  test("labels the geography the same way under a program whose rows spell it only the other way", () => {
    // A geography's label is also its selection value, so it must be the
    // same in every view that lists the geography.
    const tree = buildCalibrationTree(releaseRows, state(populationPath));

    expect(nodeSummaries(tree)).toEqual([
      ["Maine", "Maine", 1],
      ["ME congressional district 1", "ME congressional district 1", 1],
    ]);
  });

  test("selects every row of the geography ID whichever breakdown reaches it", () => {
    const programs = buildCalibrationTree(
      releaseRows,
      state({ geography: "Maine", dimensions: [] }, "geography"),
    );
    expect(
      programs.groups.map((group) => [group.id, group.nodes.map((treeNode) => treeNode.id)]),
    ).toEqual([
      ["census_population", ["census_population.resident_population"]],
      ["cms_medicaid", ["cms_medicaid.cms_medicaid"]],
      ["usda_snap", ["usda_snap.usda_snap"]],
    ]);

    const targets = buildCalibrationTree(
      releaseRows,
      state({ ...populationPath, geography: "Maine" }, "geography"),
    );
    expect(targets.pathLabels.geography).toBe("Maine");
    expect(nodeSummaries(targets)).toEqual([
      ["pop_state_23@2024", "ME resident population", 1],
    ]);
  });

  test("keeps a geography's label when filters hide the rows that carry it", () => {
    // A published tree is compiled without filters and keeps its node labels
    // when a filter is applied, so a live tree must label a geography from
    // all of its rows, not only the visible ones.
    const rows = releaseRows.map((row) =>
      row === statePopulationRow ? { ...row, abs_relative_error: 0.5 } : row,
    );
    const poorlyFitOnly = { ...EMPTY_FILTERS, fitBands: ["40_plus" as const] };
    const geographyRoot = buildCalibrationTree(rows, {
      breakdown: "geography",
      path: { dimensions: [] },
      filters: poorlyFitOnly,
    });
    expect(nodeSummaries(geographyRoot)).toEqual([["Maine", "Maine", 1]]);

    const populationGeographies = buildCalibrationTree(rows, {
      breakdown: "program",
      path: populationPath,
      filters: poorlyFitOnly,
    });
    expect(nodeSummaries(populationGeographies)).toEqual([["Maine", "Maine", 1]]);

    // The Geography filter selects the geography's label, so it keeps the row
    // that spells the geography "ME".
    const poorlyFitInMaine = buildCalibrationTree(rows, {
      breakdown: "program",
      path: populationPath,
      filters: { ...poorlyFitOnly, geographies: ["Maine"] },
    });
    expect(nodeSummaries(poorlyFitInMaine)).toEqual([["Maine", "Maine", 1]]);
  });
});

// Every row of state 0400000US23 and of Maine's two congressional districts in
// release populace-us-2024-buildo-acs-local-766b7c7-20261017T030317Z, in
// release order, as buildCalibration returns them (the fixture records how it
// was made). 84 of the state's 85 targets spell it "Maine"; its state
// population target, the last of them, spells it "ME".
describe("the Geography filter on a geography that rows spell more than one way", () => {
  const maineRows = maineRelease.rows as unknown as CalibrationTreeTarget[];
  const geographyRoot = state({ dimensions: [] }, "geography");
  const filteredBy = (
    treeState: ExplorerState,
    geographies: string[],
  ): ExplorerState => ({
    ...treeState,
    filters: { ...EMPTY_FILTERS, geographies },
  });

  function nodeSummaries(tree: ReturnType<typeof buildCalibrationTree>) {
    return tree.groups
      .flatMap((group) => group.nodes)
      .map((treeNode) => [treeNode.label, treeNode.metrics.nTargets]);
  }

  test("offers one option per geography, labelled as the geography's node", () => {
    const tree = buildCalibrationTree(maineRows, geographyRoot);

    expect(nodeSummaries(tree)).toEqual([
      ["Maine", 85],
      ["ME congressional district 1", 1],
      ["ME congressional district 2", 1],
    ]);
    expect(tree.filterOptions.geographies).toEqual([
      "Maine",
      "ME congressional district 1",
      "ME congressional district 2",
    ]);
  });

  test("selecting Maine selects all 85 of its targets, whatever their spelling", () => {
    const geographies = buildCalibrationTree(
      maineRows,
      filteredBy(geographyRoot, ["Maine"]),
    );
    expect(nodeSummaries(geographies)).toEqual([["Maine", 85]]);
    expect(geographies.filteredMetrics.nTargets).toBe(85);

    const programs = buildCalibrationTree(
      maineRows,
      filteredBy(state({ dimensions: [] }), ["Maine"]),
    );
    expect(programs.filteredMetrics.nTargets).toBe(85);
    expect(
      programs.groups.map((group) => [group.id, group.metrics.nTargets]),
    ).toEqual([
      ["irs_soi", 81],
      ["usda_snap", 2],
      ["census_population", 1],
      ["cms_medicaid", 1],
    ]);

    // The filter keeps every program of the Maine node, as the unfiltered
    // node lists them.
    const maine = state({ geography: "Maine", dimensions: [] }, "geography");
    expect(buildCalibrationTree(maineRows, filteredBy(maine, ["Maine"])))
      .toEqual(buildCalibrationTree(maineRows, maine));

    const populationGeographies = buildCalibrationTree(
      maineRows,
      filteredBy(
        state({
          source: "census_population",
          program: "census_population.resident_population",
          dimensions: [],
        }),
        ["Maine"],
      ),
    );
    expect(nodeSummaries(populationGeographies)).toEqual([["Maine", 1]]);
  });

  test("a spelling that is not an option, such as ME, selects no target", () => {
    const tree = buildCalibrationTree(
      maineRows,
      filteredBy(geographyRoot, ["ME"]),
    );

    expect(nodeSummaries(tree)).toEqual([]);
    expect(tree.filteredMetrics.nTargets).toBe(0);
  });

  test("an option selects only its own geography when another geography's rows use the same spelling", () => {
    // County A is spelled "Kent" and then "Kent County"; county B is spelled
    // "Kent County". The tree lists A as "Kent" and B as "Kent County", so
    // the option "Kent County" must not select A's second row.
    const county = (name: string, geographyId: string, geography: string) =>
      target(name, {
        source: "census",
        variable: "population",
        level: "county",
        geography_id: geographyId,
        geography,
      });
    const rows = [
      county("population/a/1", "A", "Kent"),
      county("population/a/2", "A", "Kent County"),
      county("population/b/1", "B", "Kent County"),
    ];

    expect(nodeSummaries(buildCalibrationTree(rows, geographyRoot))).toEqual([
      ["Kent", 2],
      ["Kent County", 1],
    ]);
    expect(
      nodeSummaries(buildCalibrationTree(rows, filteredBy(geographyRoot, ["Kent County"]))),
    ).toEqual([["Kent County", 1]]);
    expect(
      nodeSummaries(buildCalibrationTree(rows, filteredBy(geographyRoot, ["Kent"]))),
    ).toEqual([["Kent", 2]]);
  });
});
