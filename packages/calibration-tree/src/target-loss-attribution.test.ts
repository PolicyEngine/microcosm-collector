import { describe, expect, test } from "bun:test";

import malformedPartial from "./fixtures/target-loss-attribution/malformed-partial.json";
import unavailableWarning from "./fixtures/target-loss-attribution/unavailable-warning.json";
import validUnequal from "./fixtures/target-loss-attribution/valid-unequal-custom-scales.json";
import validUniform from "./fixtures/target-loss-attribution/valid-uniform.json";
import {
  HISTORICAL_ATTRIBUTION_SUPPORT,
  type HistoricalAttributionSupport,
} from "./target-loss-attribution-manifest";
import {
  classifyHistoricalAttributionEvidence,
  classifyInheritedAttributionEvidence,
  normalizeTargetLossAttribution,
  orderedTargetNamesHash,
  reconstructTargetLossAttribution,
  targetLossBasisHash,
} from "./target-loss-attribution";

type JsonObject = Record<string, unknown>;

function normalizeReported(fixture: { targets: unknown[] } & JsonObject) {
  return normalizeTargetLossAttribution({
    diagnostics: fixture,
    rows: fixture.targets as JsonObject[],
    releaseId: "schema-v6-fixture",
    buildManifest: {},
    releaseFamily: "national",
  });
}

function evidence(entry: HistoricalAttributionSupport) {
  return {
    releaseId: entry.releaseId,
    buildId: entry.buildId,
    buildSha: entry.buildSha,
    producerCommit: entry.producerCommit,
    releaseFamily: entry.releaseFamily,
    diagnosticsSchema: entry.diagnosticsSchema,
    targetCount: entry.targetCount,
    orderedTargetNamesSha256: entry.orderedTargetNamesSha256,
    producerTargetSurfaceSha256: entry.producerTargetSurfaceSha256,
    weightingIdentifier: entry.weightingIdentifier,
    lossCap: entry.lossCap ?? null,
  };
}

function syntheticSupport(
  recipe: HistoricalAttributionSupport["recipe"],
  overrides: Partial<HistoricalAttributionSupport> = {},
): HistoricalAttributionSupport {
  return {
    releaseId: "synthetic",
    buildId: "synthetic",
    buildSha: "abcdef0",
    producerCommit: null,
    releaseFamily: "national",
    diagnosticsSchema: 5,
    targetCount: 2,
    orderedTargetNamesSha256: "unused",
    producerTargetSurfaceSha256: null,
    weightingIdentifier: "synthetic-weighting",
    recipe,
    expectedStatus: recipe === "legacy_current_doctrine_v1" ? "derived" : "exact_reconstructed",
    tolerance: { kind: "floating_point", absolute: 1e-12, relative: 1e-12 },
    ...overrides,
  };
}

function reconstruct(
  recipe: HistoricalAttributionSupport["recipe"],
  rows: JsonObject[],
  finalLoss: number,
  diagnostics: JsonObject = {},
  overrides: Partial<HistoricalAttributionSupport> = {},
) {
  return reconstructTargetLossAttribution(
    syntheticSupport(recipe, { targetCount: rows.length, ...overrides }),
    diagnostics,
    rows,
    finalLoss,
    [],
  );
}

describe("schema-version-6 reported target-loss attribution", () => {
  test("accepts Microcosm's uniform fixture as authoritative", () => {
    const result = normalizeReported(validUniform as JsonObject & { targets: unknown[] });

    expect(result.attribution.status).toBe("reported");
    expect(result.attribution.aggregate).toBeCloseTo(0.15, 15);
    expect(result.attribution.targets).toHaveLength(2);
    expect(result.rows.map((row) => row.final_loss_contribution)).toEqual([0.05, 0.1]);
    expect(result.provenance).toMatchObject({
      mode: "direct",
      dataset_release_id: "schema-v6-fixture",
      calibration_source_id: "schema-v6-fixture",
      validation: { status: "not_applicable", reason: null },
    });
  });

  test("accepts unequal weights and custom scales", () => {
    const result = normalizeReported(validUnequal as JsonObject & { targets: unknown[] });

    expect(result.attribution.status).toBe("reported");
    expect(result.attribution.cap).toBe(0.5);
    expect(result.attribution.targets.map((row) => row.target_loss_weight_share)).toEqual([
      0.25,
      0.75,
    ]);
    expect(result.attribution.aggregate).toBeCloseTo(0.275, 15);
  });

  test("fails closed on a structured producer warning", () => {
    const result = normalizeReported(unavailableWarning as JsonObject & { targets: unknown[] });

    expect(result.attribution.status).toBe("unavailable");
    expect(result.attribution.producer_warnings[0].code).toBe(
      "target_loss_attribution_alignment_error",
    );
    expect(result.rows.every((row) => row.final_loss_contribution == null)).toBe(true);
  });

  test("fails closed without leaking a malformed partial contract", () => {
    const result = normalizeReported(malformedPartial as JsonObject & { targets: unknown[] });

    expect(result.attribution.status).toBe("unavailable");
    expect(result.rows.every((row) => row.target_loss_weight == null)).toBe(true);
    expect(result.rows.every((row) => row.final_loss_contribution == null)).toBe(true);
  });

  test("rejects internally inconsistent values rather than reconstructing", () => {
    const fixture = structuredClone(validUniform) as JsonObject & { targets: JsonObject[] };
    fixture.targets[0].target_loss_weight_share = 0.75;
    const result = normalizeReported(fixture);

    expect(result.attribution.status).toBe("unavailable");
    expect(result.attribution.reason).toContain("disagree");
  });

  test("reproduces Microcosm's basis hashes", () => {
    const uniform = validUniform.targets.map((row, index) => ({ index, ...row }));
    const unequal = validUnequal.targets.map((row, index) => ({ index, ...row }));

    expect(targetLossBasisHash(uniform)).toBe(validUniform.target_loss_basis.sha256);
    expect(targetLossBasisHash(unequal)).toBe(validUnequal.target_loss_basis.sha256);
    expect(targetLossBasisHash([
      { ...uniform[0], target_loss_weight: 2 },
      uniform[1],
    ])).not.toBe(validUniform.target_loss_basis.sha256);
  });
});

describe("audited historical support manifest", () => {
  test("classifies every pinned release and staging candidate from all evidence", () => {
    expect(HISTORICAL_ATTRIBUTION_SUPPORT).toHaveLength(24);
    expect(new Set(HISTORICAL_ATTRIBUTION_SUPPORT.map((entry) => entry.releaseId)).size).toBe(24);

    for (const entry of HISTORICAL_ATTRIBUTION_SUPPORT) {
      expect(classifyHistoricalAttributionEvidence(evidence(entry))).toEqual({
        status: entry.expectedStatus,
        recipe: entry.recipe,
        reason: null,
      });
    }
    expect(
      HISTORICAL_ATTRIBUTION_SUPPORT.filter(
        (entry) => entry.expectedStatus === "exact_reconstructed",
      ),
    ).toHaveLength(18);
    expect(
      HISTORICAL_ATTRIBUTION_SUPPORT.filter((entry) => entry.expectedStatus === "derived"),
    ).toHaveLength(6);
  });

  test("pins the completed schema-version-2 staging candidate", () => {
    const entry = HISTORICAL_ATTRIBUTION_SUPPORT.find(
      (candidate) =>
        candidate.releaseId ===
        "populace-us-2024-f0af251-0ad74ed34493-20260619T181855Z",
    );

    expect(entry).toMatchObject({
      buildSha: "0ad74ed",
      diagnosticsSchema: 2,
      targetCount: 4356,
      producerTargetSurfaceSha256:
        "67b491fe59f72e4622fd0d13c0f6a71e43e1c6ed74afae4032cb238515bd0269",
      recipe: "newer_sqrt_value_50_50_v2",
      expectedStatus: "exact_reconstructed",
    });
    expect(classifyHistoricalAttributionEvidence(evidence(entry!))).toEqual({
      status: "exact_reconstructed",
      recipe: "newer_sqrt_value_50_50_v2",
      reason: null,
    });
  });

  test("pins the September 2026 local-area diagnostics", () => {
    const entry = HISTORICAL_ATTRIBUTION_SUPPORT.find(
      (candidate) =>
        candidate.releaseId ===
        "populace-us-2024-buildo-acs-local-767312d60-20260923T074941Z",
    );

    expect(entry).toMatchObject({
      diagnosticsSha256:
        "f39d10a72415eb85ac0faa231f7970bffcc408bd53ef0f05985ea8dd0ac94903",
      buildSha: "767312d60",
      diagnosticsSchema: null,
      targetCount: 4459,
      orderedTargetNamesSha256:
        "3ec3156612fa78d193055eaba7a5212b2bc21b533c037670cd7e8d3af7735500",
      recipe: "uniform_target_scale_cap_100pct_v1",
      expectedStatus: "exact_reconstructed",
    });
    expect(classifyHistoricalAttributionEvidence(evidence(entry!))).toEqual({
      status: "exact_reconstructed",
      recipe: "uniform_target_scale_cap_100pct_v1",
      reason: null,
    });
  });

  test("refuses a familiar weighting name on an unexpected target surface", () => {
    const entry = HISTORICAL_ATTRIBUTION_SUPPORT.find(
      (candidate) => candidate.expectedStatus === "exact_reconstructed",
    )!;
    const classification = classifyHistoricalAttributionEvidence({
      ...evidence(entry),
      orderedTargetNamesSha256: orderedTargetNamesHash([{ name: "unexpected" }]),
    });

    expect(classification.status).toBe("unavailable");
    expect(classification.reason).toBe("ordered target surface mismatch");
  });
});

describe("inherited historical attribution", () => {
  const parent = HISTORICAL_ATTRIBUTION_SUPPORT.find(
    (entry) =>
      entry.releaseId ===
      "populace-us-2024-buildp-sparse-rmloss100-cae8640-20260728T011454Z",
  )!;
  const inheritedEvidence = (
    overrides: Partial<Parameters<typeof classifyInheritedAttributionEvidence>[0]> = {},
  ) => ({
    parentBuildId: parent.buildId,
    declaredDiagnosticsSchema: parent.diagnosticsSchema,
    declaredDiagnosticsSha256: parent.diagnosticsSha256 ?? null,
    observedDiagnosticsSha256: parent.diagnosticsSha256 ?? null,
    releaseFamily: parent.releaseFamily,
    diagnosticsSchema: parent.diagnosticsSchema,
    targetCount: parent.targetCount,
    orderedTargetNamesSha256: parent.orderedTargetNamesSha256,
    producerTargetSurfaceSha256: parent.producerTargetSurfaceSha256,
    weightingIdentifier: parent.weightingIdentifier,
    lossCap: parent.lossCap ?? null,
    ...overrides,
  });

  test("resolves a byte-identical inherited diagnostic through its pinned parent", () => {
    expect(classifyInheritedAttributionEvidence(inheritedEvidence())).toEqual({
      status: "exact_reconstructed",
      recipe: "concept_budget_sqrt_value_50_50_v3",
      reason: null,
    });
  });

  test("rejects a diagnostics digest mismatch", () => {
    const classification = classifyInheritedAttributionEvidence(inheritedEvidence({
      observedDiagnosticsSha256: "0".repeat(64),
    }));
    expect(classification).toEqual({
      status: "unavailable",
      recipe: null,
      reason: "downloaded diagnostics do not match the digest declared by the release",
    });
  });

  test("rejects a target-surface mismatch", () => {
    const classification = classifyInheritedAttributionEvidence(inheritedEvidence({
      producerTargetSurfaceSha256: "0".repeat(64),
    }));
    expect(classification).toEqual({
      status: "unavailable",
      recipe: null,
      reason: "diagnostics target-surface digest does not match the pinned parent digest",
    });
  });

  test("rejects an unsupported parent build", () => {
    const classification = classifyInheritedAttributionEvidence(inheritedEvidence({
      parentBuildId: "unknown-parent",
    }));
    expect(classification).toEqual({
      status: "unavailable",
      recipe: null,
      reason: "no pinned historical attribution recipe matches the declared parent build",
    });
  });

  test("rejects a declared schema mismatch", () => {
    const classification = classifyInheritedAttributionEvidence(inheritedEvidence({
      declaredDiagnosticsSchema: 6,
    }));
    expect(classification).toEqual({
      status: "unavailable",
      recipe: null,
      reason: "declared diagnostics schema does not match the downloaded diagnostics",
    });
  });
});

describe("historical target-loss recipes", () => {
  const historicalRows: JsonObject[] = [
    {
      name: "count",
      target: 100,
      final_estimate: 150,
      metadata: { measure_mode: "count", source_measure_id: "people" },
    },
    {
      name: "amount",
      target: 10_000,
      final_estimate: 11_000,
      metadata: { measure_mode: "sum", source_measure_id: "benefit_amount" },
    },
  ];

  test("keeps the historical and newer amount/count classifiers separate", () => {
    const diagnostics = { options: { target_loss_scales: { kind: "default_target", cap: 1 } } };
    const historical = reconstruct(
      "historical_sqrt_value_50_50_v1",
      historicalRows,
      0.3,
      diagnostics,
    );
    const newerAggregate = (0.5 * 10 + 0.1 * 100) / 110;
    const newer = reconstruct(
      "newer_sqrt_value_50_50_v2",
      historicalRows,
      newerAggregate,
      diagnostics,
    );

    expect(historical.status).toBe("exact_reconstructed");
    expect(historical.aggregate).toBeCloseTo(0.3, 15);
    expect(newer.status).toBe("exact_reconstructed");
    expect(newer.aggregate).toBeCloseTo(newerAggregate, 15);
    expect(newer.aggregate).not.toBeCloseTo(historical.aggregate!, 3);
  });

  test("applies congressional-district concept budgets before 50/50 balancing", () => {
    const rows: JsonObject[] = [
      {
        name: "district-1",
        target: 100,
        final_estimate: 190,
        entity: "household",
        period: 2024,
        registry: { family: "census" },
        metadata: {
          ledger_geography_level: "congressional_district",
          ledger_geography_id: "5001700US0101",
          state_fips: "01",
          measure_mode: "indicator_sum",
        },
      },
      {
        name: "district-2",
        target: 100,
        final_estimate: 110,
        entity: "household",
        period: 2024,
        registry: { family: "census" },
        metadata: {
          ledger_geography_level: "congressional_district",
          ledger_geography_id: "5001700US0102",
          state_fips: "01",
          measure_mode: "indicator_sum",
        },
      },
      {
        name: "national",
        target: 100,
        final_estimate: 110,
        entity: "household",
        period: 2024,
        registry: { family: "census" },
        metadata: { ledger_geography_level: "country", measure_mode: "indicator_sum" },
      },
    ];
    const diagnostics = { options: { target_loss_scales: { kind: "default_target", cap: 1 } } };
    const result = reconstruct(
      "concept_budget_sqrt_value_50_50_v3",
      rows,
      0.3,
      diagnostics,
    );

    expect(result.status).toBe("exact_reconstructed");
    expect(result.targets.map((row) => row.target_loss_weight_share)).toEqual([0.25, 0.25, 0.5]);
    expect(result.aggregate).toBeCloseTo(0.3, 15);
  });

  test("applies complete declared family multipliers after the base recipe", () => {
    const rows: JsonObject[] = [
      {
        name: "family-a",
        target: 100,
        final_estimate: 120,
        registry: { family: "a" },
        metadata: { measure_mode: "indicator_sum" },
      },
      {
        name: "family-b",
        target: 100,
        final_estimate: 110,
        registry: { family: "b" },
        metadata: { measure_mode: "indicator_sum" },
      },
    ];
    const diagnostics = {
      options: { target_loss_scales: { kind: "default_target", cap: 1 } },
      build: { target_loss_family_multipliers: { a: 2 } },
    };
    const result = reconstruct(
      "concept_budget_sqrt_value_50_50_v3",
      rows,
      1 / 6,
      diagnostics,
    );

    expect(result.status).toBe("exact_reconstructed");
    expect(result.targets.map((row) => row.target_loss_weight_share)).toEqual([2 / 3, 1 / 3]);
  });

  test("accepts only the pinned six-decimal tolerance for rounded local producers", () => {
    const rows: JsonObject[] = [
      { name: "one", value: 100, estimate: 110 },
      { name: "two", value: 200, estimate: 160 },
    ];
    const rounded = reconstruct(
      "uniform_target_scale_cap_100pct_v1",
      rows,
      0.1500004,
      {},
      {
        releaseFamily: "local_area",
        tolerance: { kind: "six_decimal_quantization", absolute: 0.5e-6, relative: 0 },
      },
    );
    const fullPrecision = reconstruct(
      "uniform_target_scale_cap_100pct_v1",
      rows,
      0.1500004,
    );

    expect(rounded.status).toBe("exact_reconstructed");
    expect(fullPrecision.status).toBe("unavailable");
  });

  test("derives a current-doctrine view while preserving legacy final loss", () => {
    const rows: JsonObject[] = [
      { name: "nation/source/program/count/all", target: 100, final_estimate: 120, measure: "count" },
      { name: "nation/source/program/total/all", target: 1000, final_estimate: 1100, measure: "total" },
    ];
    const result = reconstruct("legacy_current_doctrine_v1", rows, 7_510_000_000);

    expect(result.status).toBe("derived");
    expect(result.aggregate).toBeCloseTo(0.15, 15);
    expect(result.historical_final_loss).toBe(7_510_000_000);
    expect(result.verification?.difference).toBeNull();
  });

  test("fails closed for custom scales, missing values, and incomplete multipliers", () => {
    const customScale = reconstruct(
      "newer_sqrt_value_50_50_v2",
      historicalRows,
      0.1,
      { options: { target_loss_scales: { kind: "provided", cap: 1 } } },
    );
    const missingValue = reconstruct(
      "uniform_target_scale_cap_100pct_v1",
      [{ name: "missing", target: 1 }],
      0.1,
    );
    const incompleteMultiplier = reconstruct(
      "concept_budget_sqrt_value_50_50_v3",
      historicalRows,
      0.1,
      {
        options: { target_loss_scales: { kind: "default_target", cap: 1 } },
        build: { target_loss_family_multipliers: { absent_family: 2 } },
      },
    );

    expect(customScale.status).toBe("unavailable");
    expect(missingValue.status).toBe("unavailable");
    expect(incompleteMultiplier.status).toBe("unavailable");
  });
});
