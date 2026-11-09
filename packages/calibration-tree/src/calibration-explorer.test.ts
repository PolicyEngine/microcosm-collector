import { describe, expect, test } from "bun:test";

import {
  calibrationExplorerSourceIdentity,
  createExplorerState,
  explorerReducer,
  nextLevelExplorerStates,
  parentExplorerState,
  selectExplorerNode,
  type ExplorerState,
} from "./calibration-explorer";

function state(overrides: Partial<ExplorerState> = {}): ExplorerState {
  return {
    breakdown: "program",
    path: { dimensions: [] },
    filters: {
      geographyLevels: [],
      geographies: [],
      fitBands: [],
      comparisonFits: [],
      calibrationStatuses: [],
    },
    ...overrides,
  };
}

describe("calibration explorer semantic navigation", () => {
  test("changes explorer identity with the country or data source", () => {
    const usReleaseA = calibrationExplorerSourceIdentity({
      country: "us",
      release: "release-a",
    });

    expect(usReleaseA).not.toBe(
      calibrationExplorerSourceIdentity({ country: "us", release: "release-b" }),
    );
    expect(usReleaseA).not.toBe(
      calibrationExplorerSourceIdentity({ country: "uk", release: "release-a" }),
    );
    expect(usReleaseA).not.toBe(
      calibrationExplorerSourceIdentity({ country: "us", stagingRunId: "release-a" }),
    );
    expect(
      calibrationExplorerSourceIdentity({ country: "us" }),
    ).toBe(calibrationExplorerSourceIdentity({ country: "us" }));
  });

  test("starts at all programs with no inherited route state", () => {
    expect(createExplorerState()).toEqual(state());
  });

  test("program selection atomically records source and program", () => {
    const selected = selectExplorerNode(state(), {
      kind: "program",
      source: "census",
      value: "population",
    });

    expect(selected.path).toEqual({
      source: "census",
      program: "population",
      dimensions: [],
    });
  });

  test("derives only the directly selectable child levels for prefetching", () => {
    const current = state({
      filters: {
        geographyLevels: ["state"],
        geographies: [],
        fitBands: [],
        comparisonFits: [],
        calibrationStatuses: [],
      },
    });

    const children = nextLevelExplorerStates(current, [
      { kind: "program", source: "census", value: "population" },
      { kind: "program", source: "irs_soi", value: "ctc" },
      { kind: "target", value: "already-a-leaf" },
    ]);

    expect(children.map((child) => child.path)).toEqual([
      { source: "census", program: "population", dimensions: [] },
      { source: "irs_soi", program: "ctc", dimensions: [] },
    ]);
    expect(children.every((child) => child.filters === current.filters)).toBe(true);
  });

  test("up removes dimensions, then geography, then source and program together", () => {
    const filters = {
      geographyLevels: ["state"],
      geographies: ["CA"],
      fitBands: ["10_20" as const],
      comparisonFits: [],
      calibrationStatuses: ["included" as const],
    };
    const deep = state({
      path: {
        source: "census",
        program: "population",
        geography: "CA",
        dimensions: [
          { key: "bd_age", value: "65_plus" },
          { key: "bd_sex", value: "female" },
        ],
        target: "target/1",
      },
      filters,
    });

    const one = parentExplorerState(deep);
    expect(one.path.target).toBeUndefined();
    expect(one.path.dimensions).toEqual([{ key: "bd_age", value: "65_plus" }]);
    expect(one.filters).toEqual(filters);

    const two = parentExplorerState(one);
    expect(two.path.dimensions).toEqual([]);
    expect(two.path.geography).toBe("CA");

    const three = parentExplorerState(two);
    expect(three.path.geography).toBeUndefined();
    expect(three.path.program).toBe("population");

    const root = parentExplorerState(three);
    expect(root.path).toEqual({ dimensions: [] });
    expect(root.filters).toEqual(filters);
  });

  test("drills and moves up entirely through local reducer state", () => {
    const root = createExplorerState();
    const program = explorerReducer(root, {
      type: "select",
      selection: { kind: "program", source: "census", value: "population" },
    });
    const geography = explorerReducer(program, {
      type: "select",
      selection: { kind: "geography", value: "CA" },
    });

    expect(geography.path).toEqual({
      source: "census",
      program: "population",
      geography: "CA",
      dimensions: [],
    });
    expect(explorerReducer(geography, { type: "up" }).path.geography).toBeUndefined();
    expect(explorerReducer(program, { type: "up" })).toEqual(root);
  });

  test("drills geography first when geography is the primary breakdown", () => {
    const root = state({ breakdown: "geography" });
    const geography = explorerReducer(root, {
      type: "select",
      selection: { kind: "geography", value: "CA" },
    });
    const program = explorerReducer(geography, {
      type: "select",
      selection: { kind: "program", source: "irs_soi", value: "ctc" },
    });

    expect(geography.path).toEqual({ geography: "CA", dimensions: [] });
    expect(program.path).toEqual({
      source: "irs_soi",
      program: "ctc",
      geography: "CA",
      dimensions: [],
    });
    expect(explorerReducer(program, { type: "up" }).path).toEqual({
      geography: "CA",
      dimensions: [],
    });
    expect(explorerReducer(geography, { type: "up" })).toEqual(root);
  });

  test("changing the primary breakdown resets navigation but preserves filters", () => {
    const filters = {
      geographyLevels: ["state"],
      geographies: ["CA"],
      fitBands: ["10_20" as const],
      comparisonFits: [],
      calibrationStatuses: ["included" as const],
    };
    const selected = state({
      path: { source: "census", program: "population", dimensions: [] },
      filters,
    });

    expect(
      explorerReducer(selected, { type: "breakdown", breakdown: "geography" }),
    ).toEqual(state({ breakdown: "geography", filters }));
  });

  test("breadcrumb navigation jumps to an ancestor without clearing filters", () => {
    const filters = {
      geographyLevels: ["state"],
      geographies: ["CA"],
      fitBands: ["10_20" as const],
      comparisonFits: [],
      calibrationStatuses: ["included" as const],
    };
    const deep = state({
      path: {
        source: "census",
        program: "population",
        geography: "CA",
        dimensions: [{ key: "bd_age", label: "Age", value: "Adult" }],
        target: "population/adult/CA",
      },
      filters,
    });

    const geography = explorerReducer(deep, {
      type: "navigate",
      path: {
        source: "census",
        program: "population",
        geography: "CA",
        dimensions: [],
      },
    });
    expect(geography.path).toEqual({
      source: "census",
      program: "population",
      geography: "CA",
      dimensions: [],
    });
    expect(geography.filters).toEqual(filters);

    expect(
      explorerReducer(deep, { type: "navigate", path: { dimensions: [] } }),
    ).toEqual(state({ filters }));
  });

  test("allows geography-less programs to select dimensions and targets", () => {
    const program = selectExplorerNode(state(), {
      kind: "program",
      source: "irs_soi",
      value: "ctc",
    });

    expect(
      selectExplorerNode(program, {
        kind: "dimension_value",
        key: "bd_income_band",
        label: "Income band",
        value: "All",
      }).path,
    ).toEqual({
      source: "irs_soi",
      program: "ctc",
      dimensions: [{ key: "bd_income_band", label: "Income band", value: "All" }],
    });
    expect(
      selectExplorerNode(program, { kind: "target", value: "ctc/all/count" }).path,
    ).toEqual({
      source: "irs_soi",
      program: "ctc",
      dimensions: [],
      target: "ctc/all/count",
    });
    expect(
      selectExplorerNode(state(), {
        kind: "dimension_value",
        key: "bd_age",
        label: "Age",
        value: "Adult",
      }),
    ).toEqual(state());
  });
});
