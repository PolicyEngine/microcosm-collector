import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

import { createExplorerState } from "./calibration-explorer";
import { buildCalibrationTree } from "./calibration-tree";
import { buildCalibration } from "./latest-artifact";

const RELEASE_ID =
  "populace-us-2024-buildp-sparse-rmloss100-cae8640-20260728T011454Z";

function legacyIdentityFixture(): Record<string, unknown> {
  const compressed = readFileSync(
    new URL("./fixtures/us-legacy-target-identities.json.gz", import.meta.url),
  );
  return JSON.parse(gunzipSync(compressed).toString("utf8"));
}

function normalizedIdentity(row: Record<string, unknown>) {
  const dimensions = Array.isArray(row.target_dimensions)
    ? row.target_dimensions.map((value) => {
        const dimension = value as Record<string, unknown>;
        return {
          key: dimension.key,
          label: dimension.label,
          value: dimension.value,
        };
      })
    : [];

  return {
    name: row.name,
    family: row.family,
    source: row.source,
    variable: row.variable,
    variable_key: row.variable_key,
    geography: row.geography,
    level: row.level,
    state: row.state,
    measure: row.measure,
    breakdown: row.breakdown,
    dimension_adapter: row.dimension_adapter,
    dimensions,
  };
}

test("the pinned US release retains its legacy source and statistic grouping", () => {
  const calibration = buildCalibration(legacyIdentityFixture(), RELEASE_ID);
  const tree = buildCalibrationTree(calibration.rows, createExplorerState());
  const nodeCounts = Object.fromEntries(
    tree.groups.map((group) => [group.id, group.nodes.length]),
  );

  expect(calibration.target_schema.target_representation).toBe("legacy");
  expect(calibration.rows.every((row) => row.target_representation === "legacy")).toBe(true);
  expect(calibration.rows).toHaveLength(5_659);
  expect(tree.groups).toHaveLength(15);
  expect(Object.values(nodeCounts).reduce((sum, count) => sum + count, 0)).toBe(53);
  expect(nodeCounts).toEqual({
    irs_soi: 33,
    census_population: 1,
    cms_medicaid: 3,
    ssa: 2,
    usda_snap: 2,
    cms_aca: 2,
    state_income_tax: 1,
    hhs_acf_tanf: 1,
    jct: 1,
    cbo: 1,
    bea: 2,
    cms_medicare: 1,
    federal_reserve: 1,
    hhs_acf_liheap: 1,
    unspecified: 1,
  });
  expect(tree.groups.find((group) => group.id === "bea")?.nodes.map((node) => node.label))
    .toEqual(["Amount", "Wages salaries"]);
});

test("the pinned US release retains representative normalized target identities", () => {
  const calibration = buildCalibration(legacyIdentityFixture(), RELEASE_ID);
  const rowsByName = new Map(
    calibration.rows.map((row) => [String(row.name), row]),
  );
  const names = [
    "irs_soi.ty2024.filing_season_week47.eitc_all_returns.earned_income_credit.total_earned_income_credit_returns@2024",
    "irs_soi.ty2022.historic_table_2.state_agi.al.1_to_10k.taxable_interest_returns@2024",
    "census_pep.cy2024.national_resident_population_age.0_to_4.population@2024",
    "cms_medicaid.month2024_12.state_enrollment.ak.total_medicaid_chip_enrollment@2024",
    "ssa_supplement.cy2024.oasdi_ssi_payments.social_security_benefits.payment_amount@2024",
    "census_stc.fy2023.individual_income_tax_collections.al.t40.collections@2024",
    "bea_nipa.cy2023.proprietors_income.a041rc.amount@2024",
    "selection_mass_protection.keogh_distributions@0",
  ];
  const identities = names.map((name) => {
    const row = rowsByName.get(name);
    if (!row) throw new Error(`Pinned legacy fixture is missing ${name}`);
    return normalizedIdentity(row);
  });

  expect(identities).toEqual([
    {
      name: names[0],
      family:
        "irs_soi.ty2024.filing_season_week47.eitc_all_returns.earned_income_credit.total_earned_income_credit_returns",
      source: "irs_soi",
      variable: "eitc",
      variable_key: "irs_soi / eitc · count",
      geography: "United States",
      level: "national",
      state: null,
      measure: "count",
      breakdown: "earned income credit · All",
      dimension_adapter: "legacy_name",
      dimensions: [
        { key: "bd_breakdown", label: "Breakdown", value: "earned income credit" },
        { key: "bd_filing_status", label: "Filing status", value: "All" },
      ],
    },
    {
      name: names[1],
      family:
        "irs_soi.ty2022.historic_table_2.state_agi.al.1_to_10k.taxable_interest_returns",
      source: "irs_soi",
      variable: "taxable interest income",
      variable_key: "irs_soi / taxable interest income · count",
      geography: "AL",
      level: "state",
      state: "AL",
      measure: "count",
      breakdown: "1 to 10k · All",
      dimension_adapter: "legacy_name",
      dimensions: [
        { key: "bd_breakdown", label: "Breakdown", value: "1 to 10k" },
        { key: "bd_filing_status", label: "Filing status", value: "All" },
      ],
    },
    {
      name: names[2],
      family: "census_pep.cy2024.national_resident_population_age.0_to_4.population",
      source: "census_population",
      variable: "population",
      variable_key: "census_population / population",
      geography: "United States",
      level: "national",
      state: null,
      measure: null,
      breakdown: "0 to 4",
      dimension_adapter: "legacy_name",
      dimensions: [{ key: "bd_breakdown", label: "Breakdown", value: "0 to 4" }],
    },
    {
      name: names[3],
      family:
        "cms_medicaid.month2024_12.state_enrollment.ak.total_medicaid_chip_enrollment",
      source: "cms_medicaid",
      variable: "total medicaid chip enrollment",
      variable_key: "cms_medicaid / total medicaid chip enrollment",
      geography: "AK",
      level: "state",
      state: "AK",
      measure: null,
      breakdown: "",
      dimension_adapter: "legacy_name",
      dimensions: [],
    },
    {
      name: names[4],
      family:
        "ssa_supplement.cy2024.oasdi_ssi_payments.social_security_benefits.payment_amount",
      source: "ssa",
      variable: "payment",
      variable_key: "ssa / payment · total",
      geography: "United States",
      level: "national",
      state: null,
      measure: "total",
      breakdown: "social security benefits",
      dimension_adapter: "legacy_name",
      dimensions: [
        { key: "bd_breakdown", label: "Breakdown", value: "social security benefits" },
      ],
    },
    {
      name: names[5],
      family: "census_stc.fy2023.individual_income_tax_collections.al.t40.collections",
      source: "state_income_tax",
      variable: "collections",
      variable_key: "state_income_tax / collections",
      geography: "AL",
      level: "state",
      state: "AL",
      measure: null,
      breakdown: "t40",
      dimension_adapter: "legacy_name",
      dimensions: [{ key: "bd_breakdown", label: "Breakdown", value: "t40" }],
    },
    {
      name: names[6],
      family: "bea_nipa.cy2023.proprietors_income.a041rc.amount",
      source: "bea",
      variable: "amount",
      variable_key: "bea / amount",
      geography: "United States",
      level: "national",
      state: null,
      measure: null,
      breakdown: "a041rc",
      dimension_adapter: "legacy_name",
      dimensions: [{ key: "bd_breakdown", label: "Breakdown", value: "a041rc" }],
    },
    {
      name: names[7],
      family: "selection_mass_protection.keogh_distributions",
      source: "unspecified",
      variable: "keogh distributions",
      variable_key: "unspecified / keogh distributions",
      geography: "United States",
      level: "national",
      state: null,
      measure: null,
      breakdown: "",
      dimension_adapter: "legacy_name",
      dimensions: [],
    },
  ]);
});
