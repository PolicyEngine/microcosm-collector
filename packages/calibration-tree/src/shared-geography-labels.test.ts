import { expect, test } from "bun:test";

import { createExplorerState } from "./calibration-explorer";
import { buildCalibrationTree, type CalibrationTreeTarget } from "./calibration-tree";
import {
  buildCalibration,
  latestMicrocosmTargetDiagnosticsPage,
  type Calibration,
} from "./latest-artifact";

// Microcosm's UK local-area builds calibrate parliamentary constituencies and
// local authorities side by side, and 115 names belong to one of each. In the
// 19 Sep local candidate (21,523 targets), "Ashfield" carries 18 constituency
// targets and 26 local-authority targets. The geography's identity is its
// level and GSS code; its label alone does not tell the two areas apart.
interface UkArea {
  id: string;
  label: string;
  level: string;
}

const ASHFIELD_CONSTITUENCY: UkArea = { id: "E14001063", label: "Ashfield", level: "constituency" };
const ASHFIELD_LOCAL_AUTHORITY: UkArea = { id: "E07000170", label: "Ashfield", level: "local_authority" };
const ABERDEEN_NORTH: UkArea = { id: "S14000105", label: "Aberdeen North", level: "constituency" };
const ABERDEEN_CITY: UkArea = { id: "S12000033", label: "Aberdeen City", level: "local_authority" };
const EAST_MIDLANDS: UkArea = { id: "E12000004", label: "East Midlands", level: "region" };
const ENGLAND: UkArea = { id: "E92000001", label: "England", level: "country" };
const UNITED_KINGDOM: UkArea = { id: "K02000001", label: "United Kingdom", level: "country" };

const LOCAL_AREA_RELEASE = "microcosm-uk-2024-25-dense-20261019T000432Z-ee5c2dbe";
const NATIONAL_RELEASE = "microcosm-uk-2024-25-national-20261019T083323Z-6cba5d71";

function populationTarget(area: UkArea, targetValue: number, finalEstimate: number) {
  const targetId = `ons.population.${area.id}`;
  return {
    name: `${targetId}@2025`,
    target_name: targetId,
    source: "ONS mid-year population estimates",
    target: targetValue,
    initial_estimate: targetValue * 1.1,
    final_estimate: finalEstimate,
    hierarchy: {
      provider: { id: "ons", label: "Office for National Statistics" },
      category: { id: "ons.population", label: "Population", provider_id: "ons" },
      geography: { id: area.id, label: area.label, level: area.level },
      dimensions: [],
      target: { id: targetId, label: `Population, ${area.label}` },
    },
  };
}

function ukCalibration(releaseId: string, areas: UkArea[]) {
  const diagnostics = {
    schema_version: 8,
    release_id: releaseId,
    targets: areas.map((area, index) =>
      populationTarget(area, 100_000 * (index + 1), 100_000 * (index + 1) * (1 + 0.01 * index))
    ),
  };
  return buildCalibration(
    diagnostics,
    releaseId,
    null,
    {},
    { country: { code: "uk" } },
    {},
    "uk",
  );
}

const LOCAL_AREAS = [
  ASHFIELD_CONSTITUENCY,
  ASHFIELD_LOCAL_AUTHORITY,
  ABERDEEN_NORTH,
  ABERDEEN_CITY,
  EAST_MIDLANDS,
  ENGLAND,
];

function geographyById(rows: Calibration["rows"]): Record<string, unknown> {
  return Object.fromEntries(rows.map((row) => [row.geography_id, row.geography]));
}

test("UK areas that share a name across geography levels carry their level in the label", () => {
  const calibration = ukCalibration(LOCAL_AREA_RELEASE, LOCAL_AREAS);

  expect(geographyById(calibration.rows)).toEqual({
    [ASHFIELD_CONSTITUENCY.id]: "Ashfield (constituency)",
    [ASHFIELD_LOCAL_AUTHORITY.id]: "Ashfield (local authority)",
    [ABERDEEN_NORTH.id]: "Aberdeen North",
    [ABERDEEN_CITY.id]: "Aberdeen City",
    [EAST_MIDLANDS.id]: "East Midlands",
    [ENGLAND.id]: "England",
  });
  // The level itself is unchanged, so the level filter still works.
  expect(
    calibration.rows.find((row) => row.geography_id === ASHFIELD_LOCAL_AUTHORITY.id)?.level,
  ).toBe("local_authority");
});

test("the targets page lists and filters same-named UK areas separately", () => {
  const calibration = ukCalibration(LOCAL_AREA_RELEASE, LOCAL_AREAS);
  const page = latestMicrocosmTargetDiagnosticsPage(
    `http://dashboard.test/api/microcosm/target-diagnostics?country=uk&geography=${
      encodeURIComponent("Ashfield (local authority)")
    }`,
    calibration,
  );

  expect(page.geographies).toEqual([
    "Aberdeen City",
    "Aberdeen North",
    "Ashfield (constituency)",
    "Ashfield (local authority)",
    "East Midlands",
    "England",
  ]);
  expect(page.levels).toEqual(["constituency", "country", "local_authority", "region"]);
  expect(page.filtered_total).toBe(1);
  expect(page.targets.map((row) => row.name)).toEqual([
    `ons.population.${ASHFIELD_LOCAL_AUTHORITY.id}@2025`,
  ]);
});

test("the explorer map gives same-named UK areas their own geography tiles and filter options", () => {
  const calibration = ukCalibration(LOCAL_AREA_RELEASE, LOCAL_AREAS);
  const rows = calibration.rows as CalibrationTreeTarget[];
  const geographyState = { ...createExplorerState(), breakdown: "geography" as const };

  const tree = buildCalibrationTree(rows, geographyState, LOCAL_AREA_RELEASE);
  const tiles = tree.groups.flatMap((group) => group.nodes);
  expect(tiles.map((tile) => [tile.label, tile.metrics.nTargets]).sort()).toEqual([
    ["Aberdeen City", 1],
    ["Aberdeen North", 1],
    ["Ashfield (constituency)", 1],
    ["Ashfield (local authority)", 1],
    ["East Midlands", 1],
    ["England", 1],
  ]);
  expect(tree.filterOptions.geographies).toContain("Ashfield (constituency)");
  expect(tree.filterOptions.geographies).toContain("Ashfield (local authority)");
  expect(tree.filterOptions.geographies).not.toContain("Ashfield");

  const filtered = buildCalibrationTree(
    rows,
    {
      ...geographyState,
      filters: { ...geographyState.filters, geographies: ["Ashfield (constituency)"] },
    },
    LOCAL_AREA_RELEASE,
  );
  expect(filtered.filteredMetrics.nTargets).toBe(1);
  expect(filtered.groups.flatMap((group) => group.nodes).map((tile) => tile.label)).toEqual([
    "Ashfield (constituency)",
  ]);
});

test("UK geography labels no other level uses are left as published", () => {
  const calibration = ukCalibration(NATIONAL_RELEASE, [UNITED_KINGDOM, ENGLAND, EAST_MIDLANDS]);

  expect(geographyById(calibration.rows)).toEqual({
    [UNITED_KINGDOM.id]: "United Kingdom",
    [ENGLAND.id]: "England",
    [EAST_MIDLANDS.id]: "East Midlands",
  });
});
