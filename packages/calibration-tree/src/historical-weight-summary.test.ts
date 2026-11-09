import { expect, test } from "bun:test";

import { historicalWeightSummaryForDiagnostics } from "./historical-weight-summary";

const RELEASE_ID =
  "populace-us-2024-buildo-acs-local-767312d60-20260923T074941Z";
const DIAGNOSTICS_SHA256 =
  "f39d10a72415eb85ac0faa231f7970bffcc408bd53ef0f05985ea8dd0ac94903";

test("returns the H5-derived September local-area weight summary", () => {
  expect(
    historicalWeightSummaryForDiagnostics(RELEASE_ID, DIAGNOSTICS_SHA256),
  ).toEqual({
    weight_entity: "household",
    n_nonzero: 1_585_847,
    n_records: 1_588_854,
    effective_sample_size: 13_631.348394968125,
    top_1pct_weight_share: 0.6930830701181169,
  });
});

test("does not apply historical weights without both pinned identities", () => {
  expect(historicalWeightSummaryForDiagnostics(RELEASE_ID, null)).toBeNull();
  expect(historicalWeightSummaryForDiagnostics(RELEASE_ID, "0".repeat(64))).toBeNull();
  expect(
    historicalWeightSummaryForDiagnostics("another-release", DIAGNOSTICS_SHA256),
  ).toBeNull();
});
