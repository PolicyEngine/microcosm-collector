import { describe, expect, test } from "bun:test";

import type { Calibration } from "./latest-artifact";
import {
  buildTargetChangeDataset,
  type TargetChangeSide,
} from "./target-change";

type RowInput = {
  name: string;
  contribution: number;
  share: number;
  error: number;
  source?: string;
  variable?: string;
  geography?: string;
};

function calibration(
  releaseId: string,
  rows: RowInput[],
  options: {
    status?: Calibration["target_loss_attribution"]["status"];
    cap?: number;
    basis?: string;
  } = {},
): Calibration {
  const aggregate = rows.reduce((sum, row) => sum + row.contribution, 0);
  const status = options.status ?? "reported";
  return {
    release_id: releaseId,
    rows: rows.map((row) => ({
      name: `${row.name}@2024`,
      base_name: row.name,
      source: row.source ?? "source",
      variable: row.variable ?? row.name,
      variable_key: row.variable ?? row.name,
      geography: row.geography ?? "United States",
      target: 100,
      final_estimate: 100 + row.error * 100,
      target_loss_weight: row.share * 100,
      target_loss_weight_share: row.share,
      target_loss_scale: 100,
      final_capped_scaled_error: row.error,
      final_loss_contribution: row.contribution,
    })),
    target_loss_attribution: {
      status,
      aggregate: status === "unavailable" ? null : aggregate,
      historical_final_loss: aggregate,
      cap: options.cap ?? 2,
      basis_identifier: options.basis ?? "sqrt:target",
      basis_hash: "hash",
      verification: null,
      producer_warnings: [],
      reason: status === "unavailable" ? "fixture unavailable" : null,
      targets: [],
    },
  } as unknown as Calibration;
}

function side(row: TargetChangeSide | null): TargetChangeSide {
  if (!row) throw new Error("Expected target side");
  return row;
}

describe("target change attribution", () => {
  test("reported changes preserve weights, additions, removals, and the aggregate delta", () => {
    const current = calibration("current", [
      { name: "shared", contribution: 0.1, share: 0.4, error: 0.25 },
      { name: "removed", contribution: 0.2, share: 0.6, error: 1 / 3 },
    ]);
    const candidate = calibration("candidate", [
      { name: "shared", contribution: 0.15, share: 0.75, error: 0.2 },
      { name: "added", contribution: 0.05, share: 0.25, error: 0.2 },
    ]);

    const result = buildTargetChangeDataset(current, candidate);
    expect(result.available).toBe(true);
    expect(result.rows.find((row) => row.name === "shared")?.reported_change).toBeCloseTo(0.05);
    expect(result.rows.find((row) => row.name === "added")?.reported_change).toBeCloseTo(0.05);
    expect(result.rows.find((row) => row.name === "removed")?.reported_change).toBeCloseTo(-0.2);
    expect(Object.fromEntries(
      result.rows.map((row) => [row.name, row.comparison_fit]),
    )).toEqual({
      shared: "improved",
      removed: "not_applicable",
      added: "not_applicable",
    });
    expect(result.summaries.reported).toEqual(expect.objectContaining({
      shared: 1,
      added: 1,
      removed: 1,
    }));
    expect(result.summaries.reported?.currentScore).toBeCloseTo(0.3);
    expect(result.summaries.reported?.candidateScore).toBeCloseTo(0.2);
    expect(result.summaries.reported?.grossIncrease).toBeCloseTo(0.1);
    expect(result.summaries.reported?.grossReduction).toBeCloseTo(0.2);
    expect(result.summaries.reported?.netChange).toBeCloseTo(-0.1);
    expect(result.summaries.reported?.reconciliationDifference).toBeCloseTo(0);
    expect(result.rows.find((row) => row.name === "shared")).toMatchObject({
      current_target_ordinal: 0,
      candidate_target_ordinal: 0,
    });
    expect(result.rows.find((row) => row.name === "removed")).toMatchObject({
      current_target_ordinal: 1,
      candidate_target_ordinal: null,
    });
    expect(result.rows.find((row) => row.name === "added")).toMatchObject({
      current_target_ordinal: null,
      candidate_target_ordinal: 1,
    });
  });

  test("shared mode normalizes each side over shared targets and applies pooled weights", () => {
    const current = calibration("current", [
      { name: "one", contribution: 0.08, share: 0.8, error: 0.1 },
      { name: "two", contribution: 0.02, share: 0.1, error: 0.2 },
      { name: "removed", contribution: 0.03, share: 0.1, error: 0.3 },
    ]);
    const candidate = calibration("candidate", [
      { name: "one", contribution: 0.08, share: 0.2, error: 0.4 },
      { name: "two", contribution: 0.04, share: 0.4, error: 0.1 },
      { name: "added", contribution: 0.08, share: 0.4, error: 0.2 },
    ]);

    const result = buildTargetChangeDataset(current, candidate);
    const one = result.rows.find((row) => row.name === "one");
    const two = result.rows.find((row) => row.name === "two");
    const expectedOne = ((0.8 / 0.9) + (0.2 / 0.6)) / 2;
    expect(one?.pooled_weight_share).toBeCloseTo(expectedOne);
    expect((one?.pooled_weight_share ?? 0) + (two?.pooled_weight_share ?? 0)).toBeCloseTo(1);
    expect(one?.shared_change).toBeCloseTo(expectedOne * (0.4 - 0.1));
    expect(one?.comparison_fit).toBe("regressed");
    expect(two?.comparison_fit).toBe("improved");
    expect(result.summaries.shared?.comparisonTargets).toBe(2);
    expect(result.summaries.shared?.reconciliationDifference).toBeCloseTo(0);
  });

  test("shared rows use candidate metadata while removed rows keep current metadata", () => {
    const current = calibration("current", [
      { name: "shared", contribution: 0.1, share: 0.5, error: 0.2, source: "old" },
      { name: "removed", contribution: 0.1, share: 0.5, error: 0.2, source: "old" },
    ]);
    const candidate = calibration("candidate", [
      { name: "shared", contribution: 0.2, share: 1, error: 0.2, source: "new" },
    ]);

    const result = buildTargetChangeDataset(current, candidate);
    expect(result.rows.find((row) => row.name === "shared")?.source).toBe("new");
    expect(result.rows.find((row) => row.name === "removed")?.source).toBe("old");
  });

  test("classifies equal shared-target errors as unchanged", () => {
    const result = buildTargetChangeDataset(
      calibration("current", [
        { name: "same", contribution: 0.2, share: 1, error: 0.2 },
      ]),
      calibration("candidate", [
        { name: "same", contribution: 0.2, share: 1, error: 0.2 },
      ]),
    );

    expect(result.rows[0].comparison_fit).toBe("unchanged");
  });

  test("uses cross-format Chronicle matches without changing contribution arithmetic", () => {
    const current = calibration("current", [
      { name: "legacy-name", contribution: 0.1, share: 1, error: 0.1 },
    ]);
    const candidate = calibration("candidate", [
      { name: "structured-name", contribution: 0.2, share: 1, error: 0.2 },
    ]);
    current.rows[0].chronicle = { fact_key: "agency.population.total" };
    candidate.rows[0].chronicle = { fact_key: "agency.population.total" };
    candidate.rows[0].target_representation = "structured";
    candidate.rows[0].dimension_adapter = "structured";
    candidate.rows[0].dimensions = {};

    const result = buildTargetChangeDataset(current, candidate);
    expect(result.available).toBe(true);
    expect(result.matching).toMatchObject({
      current_representation: "legacy",
      candidate_representation: "structured",
      matched_by: { chronicle_fact_key: 1 },
    });
    expect(result.rows[0]).toMatchObject({
      comparison_status: "shared",
      match_kind: "chronicle_fact_key",
      current_name: "legacy-name@2024",
      candidate_name: "structured-name@2024",
      reported_change: 0.1,
    });
    expect(result.summaries.reported?.reconciliationDifference).toBeCloseTo(0);
  });

  test("fails closed when attribution is unavailable or a row is incomplete", () => {
    const unavailable = buildTargetChangeDataset(
      calibration("current", [], { status: "unavailable" }),
      calibration("candidate", []),
    );
    expect(unavailable.available).toBe(false);
    expect(unavailable.rows).toEqual([]);

    const current = calibration("current", [
      { name: "shared", contribution: 0.1, share: 1, error: 0.1 },
    ]);
    delete current.rows[0].final_loss_contribution;
    const incomplete = buildTargetChangeDataset(
      current,
      calibration("candidate", [
        { name: "shared", contribution: 0.1, share: 1, error: 0.1 },
      ]),
    );
    expect(incomplete.available).toBe(false);
    expect(incomplete.reason).toContain("incomplete");
  });

  test("preserves ambiguous duplicate rows as additions and removals", () => {
    const current = calibration("current", [
      { name: "duplicate", contribution: 0.1, share: 0.5, error: 0.2 },
      { name: "duplicate", contribution: 0.2, share: 0.5, error: 0.4 },
    ]);
    const candidate = calibration("candidate", [
      { name: "duplicate", contribution: 0.2, share: 1, error: 0.2 },
    ]);
    const result = buildTargetChangeDataset(current, candidate);
    expect(result.available).toBe(true);
    expect(result.rows).toHaveLength(3);
    expect(result.summaries.reported).toMatchObject({
      shared: 0,
      added: 1,
      removed: 2,
      reconciliationDifference: 0,
    });
    expect(result.matching.ambiguous_key_groups.base_name).toBe(1);
    expect(result.modeReasons.shared).toContain("no shared targets");
  });

  test("retains additive results while warning about methodology differences", () => {
    const current = calibration("current", [
      { name: "shared", contribution: 0.1, share: 1, error: 0.1 },
    ], { cap: 1, basis: "old" });
    const candidate = calibration("candidate", [
      { name: "shared", contribution: 0.2, share: 1, error: 0.2 },
    ], { cap: 2, basis: "new" });

    const result = buildTargetChangeDataset(current, candidate);
    expect(result.available).toBe(true);
    expect(result.methodology.comparable).toBe(false);
    expect(result.methodology.warning).toContain("different");
    expect(side(result.rows[0].current).contribution).toBe(0.1);
    expect(side(result.rows[0].candidate).contribution).toBe(0.2);
  });
});
