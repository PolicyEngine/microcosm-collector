import { describe, expect, test } from "bun:test";

import type { Calibration } from "./latest-artifact";
import { matchTargetSurfaces } from "./target-surface-matcher";

type Row = Calibration["rows"][number];

function calibration(
  rows: Row[],
  representation: "legacy" | "structured" | "hierarchy" | "mixed" = "legacy",
): Calibration {
  return {
    rows,
    target_schema: {
      diagnostics_schema_version: representation === "hierarchy" ? 8 : 7,
      structured_dimensions: representation !== "legacy",
      target_representation: representation,
    },
  } as unknown as Calibration;
}

function hierarchy(name: string, valueId: string): Row {
  return {
    name,
    base_name: name,
    target_representation: "hierarchy",
    dimension_adapter: "hierarchy",
    source: "agency",
    variable: "agency.population",
    target_dimensions: [
      {
        key: "age",
        label: "Age",
        value: "Published age label",
        value_id: valueId,
        source_key: "age",
        raw_value: valueId,
      },
    ],
    chronicle: { fact_key: null },
  };
}

function legacy(
  name: string,
  options: { baseName?: string; factKey?: string | null } = {},
): Row {
  return {
    name,
    base_name: options.baseName ?? name.replace(/@[^@]+$/, ""),
    target_representation: "legacy",
    dimension_adapter: "legacy_name",
    chronicle: { fact_key: options.factKey ?? null },
  };
}

function structured(
  name: string,
  dimensions: Record<string, string>,
  options: {
    baseName?: string;
    factKey?: string | null;
    source?: string;
    variable?: string;
    measure?: string;
  } = {},
): Row {
  return {
    name,
    base_name: options.baseName ?? name.replace(/@[^@]+$/, ""),
    target_representation: "structured",
    dimension_adapter: "structured",
    source: options.source ?? "agency",
    variable: options.variable ?? "population",
    measure: options.measure ?? "count",
    dimensions,
    chronicle: { fact_key: options.factKey ?? null },
  };
}

describe("target surface matching", () => {
  test("matches exact period-normalized target names before other identities", () => {
    const result = matchTargetSurfaces(
      calibration([legacy("population@2024", { factKey: "old-fact" })]),
      calibration([legacy("population@2025", { factKey: "new-fact" })]),
    );

    expect(result.matches).toHaveLength(1);
    expect(result.matches[0]).toMatchObject({
      comparison_status: "shared",
      match_kind: "base_name",
      current_name: "population@2024",
      candidate_name: "population@2025",
    });
    expect(result.matching.matched_by).toEqual({
      base_name: 1,
      chronicle_fact_key: 0,
      structured_identity: 0,
    });
  });

  test("uses a unique Chronicle fact key to bridge legacy and structured names", () => {
    const result = matchTargetSurfaces(
      calibration([legacy("legacy-population", { factKey: "agency.population.total" })]),
      calibration(
        [structured("new-population", {}, { factKey: "agency.population.total" })],
        "structured",
      ),
    );

    expect(result.matches[0]).toMatchObject({
      match_kind: "chronicle_fact_key",
      current_representation: "legacy",
      candidate_representation: "structured",
    });
    expect(result.matching).toMatchObject({
      current_representation: "legacy",
      candidate_representation: "structured",
      matched_by: { chronicle_fact_key: 1 },
    });
  });

  test("canonicalizes structured dimension order without using labels", () => {
    const current = structured("old-name", { region: "north", sex: "female" });
    current.source_label = "Old display label";
    const candidate = structured("new-name", { sex: "female", region: "north" });
    candidate.source_label = "New display label";
    const result = matchTargetSurfaces(
      calibration([current], "structured"),
      calibration([candidate], "structured"),
    );

    expect(result.matches[0].match_kind).toBe("structured_identity");
    expect(result.matching.matched_by.structured_identity).toBe(1);
  });

  test("matches schema 8 identities from raw hierarchy dimension values", () => {
    const result = matchTargetSurfaces(
      calibration([hierarchy("old-name", "adult")], "hierarchy"),
      calibration([hierarchy("new-name", "adult")], "hierarchy"),
    );

    expect(result.matches[0]).toMatchObject({
      match_kind: "structured_identity",
      current_representation: "hierarchy",
      candidate_representation: "hierarchy",
    });
    expect(result.matching).toMatchObject({
      current_representation: "hierarchy",
      candidate_representation: "hierarchy",
      matched_by: { structured_identity: 1 },
    });
  });

  test("lets unique fallback keys resolve a duplicated target name", () => {
    const result = matchTargetSurfaces(
      calibration([
        legacy("same@2024", { factKey: "fact-a" }),
        legacy("same@2025", { factKey: "fact-b" }),
      ]),
      calibration([
        legacy("same@2026", { factKey: "fact-b" }),
        legacy("same@2027", { factKey: "fact-a" }),
      ]),
    );

    expect(result.matches.map((match) => match.match_kind)).toEqual([
      "chronicle_fact_key",
      "chronicle_fact_key",
    ]);
    expect(result.matching.ambiguous_key_groups.base_name).toBe(1);
  });

  test("does not guess when a fallback key is many-to-one", () => {
    const result = matchTargetSurfaces(
      calibration([
        legacy("old-a", { factKey: "shared-fact" }),
        legacy("old-b", { factKey: "shared-fact" }),
      ]),
      calibration([legacy("new", { factKey: "shared-fact" })]),
    );

    expect(result.matches.map((match) => match.comparison_status).sort()).toEqual([
      "added",
      "removed",
      "removed",
    ]);
    expect(result.matching.ambiguous_key_groups.chronicle_fact_key).toBe(1);
  });

  test("preserves every row once and generates deterministic unique identifiers", () => {
    const current = calibration([
      legacy(""),
      legacy("duplicate"),
      legacy("duplicate"),
    ]);
    const candidate = calibration([legacy("candidate-only")]);
    const first = matchTargetSurfaces(current, candidate);
    const second = matchTargetSurfaces(current, candidate);
    const ids = first.matches.map((match) => match.comparison_id);

    expect(first.matches).toHaveLength(4);
    expect(new Set(ids).size).toBe(4);
    expect(ids).toEqual(second.matches.map((match) => match.comparison_id));
    expect(first.matches.every((match) => match.match_kind == null)).toBe(true);
  });
});
