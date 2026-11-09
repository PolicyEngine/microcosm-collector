import { expect, test } from "bun:test";

import { buildReformValidation } from "./reforms";

test("benchmark defaults to the full-year FY2027 figure; FY2026 kept for reference", () => {
  const built = buildReformValidation(
    {
      release_id: "r",
      reforms: [
        {
          id: "obbba_salt_limit",
          name: "SALT",
          in_sample: false,
          jct: { score: 31617000000, score_fy2027: 79250000000 },
          microcosm: { budget_effect: 64000000000 },
        },
      ],
    },
    "r",
    null,
  );
  expect(built.available).toBe(true);
  if (!built.available) return;
  const row = built.rows[0];
  // Benchmark = FY2027 (not the partial FY2026 ramp), FY2026 kept for reference.
  expect(row.jct_score).toBe(79250000000);
  expect(row.jct_score_fy2026).toBe(31617000000);
  // Error is vs FY2027: ~-19% (under), not the +102% you'd get vs FY2026.
  expect(row.relative_error).toBeLessThan(0);
  expect(row.abs_relative_error!).toBeLessThan(0.25);
});

test("percent-unit rate rows keep their unit; missing unit means currency", () => {
  const built = buildReformValidation(
    {
      release_id: "r",
      reforms: [
        {
          id: "spm_state_ut_child",
          name: "Utah SPM child poverty rate",
          in_sample: false,
          unit: "percent",
          jct: { score: 0.059 },
          microcosm: { budget_effect: 0.062 },
        },
        {
          id: "obbba_salt_limit",
          name: "SALT",
          in_sample: false,
          jct: { score: 79250000000 },
          microcosm: { budget_effect: 64000000000 },
        },
      ],
    },
    "r",
    null,
  );
  if (!built.available) return;
  expect(built.rows[0].unit).toBe("percent");
  // Relative error works identically on fractions: (0.062-0.059)/0.059 ≈ +5.1%.
  expect(built.rows[0].abs_relative_error!).toBeCloseTo(0.0508, 3);
  expect(built.rows[1].unit).toBe("currency-USD");
});

test("in-sample rows (no FY2027) fall back to their annual benchmark", () => {
  const built = buildReformValidation(
    {
      release_id: "r",
      reforms: [
        {
          id: "jct.tax_expenditures.salt",
          name: "SALT deduction",
          in_sample: true,
          jct: { score: 21700000000 },
          microcosm: { budget_effect: 20800000000 },
        },
      ],
    },
    "r",
    null,
  );
  if (!built.available) return;
  expect(built.rows[0].jct_score).toBe(21700000000);
});

function raw(reforms: object[], releaseId = "rel-a") {
  return { schema_version: 1, release_id: releaseId, scoring_window: "FY2025-2034", reforms };
}

const obbba = {
  id: "obbba",
  name: "One Big Beautiful Bill Act",
  category: "OBBBA",
  in_sample: false,
  jct: { score: -4000, score_type: "conventional", window: "FY2025-2034", source: "JCX-29-25" },
  microcosm: { budget_effect: -3600, window: "FY2025-2034" },
};
const salt = {
  id: "obbba_salt",
  name: "OBBBA — SALT cap to $40k",
  category: "OBBBA",
  in_sample: false,
  jct: { score: -1000, source: "JCX-30-25" },
  microcosm: { budget_effect: -1050 },
};

test("derives microcosm-vs-JCT error per reform", () => {
  const v = buildReformValidation(raw([obbba, salt]), "rel-a");
  const row = v.rows.find((r) => r.id === "obbba")!;
  expect(row.abs_error).toBe(400); // −3600 − (−4000)
  expect(row.relative_error).toBeCloseTo(0.1, 6); // 400 / 4000
  expect(row.abs_relative_error).toBeCloseTo(0.1, 6);
  expect(row.within_10pct).toBe(true);
  expect(row.direction).toBe("over"); // microcosm less negative than JCT
});

test("normalizes legacy populace reform calculations", () => {
  const v = buildReformValidation(
    raw([
      {
        id: "legacy",
        name: "Legacy calculation",
        in_sample: true,
        jct: { score: 1000 },
        populace: {
          budget_effect: 900,
          window: "FY2024",
          annual: { "2024": 900 },
        },
      },
    ]),
    "rel-a",
  );

  expect(v.rows[0].microcosm_estimate).toBe(900);
  expect(v.rows[0].microcosm_window).toBe("FY2024");
  expect(v.rows[0].microcosm_annual).toEqual({ "2024": 900 });
  expect(v.rows[0].relative_error).toBeCloseTo(-0.1, 6);
  expect(v.summary.n_scored).toBe(1);
});

test("prefers current microcosm calculations over legacy populace values", () => {
  const v = buildReformValidation(
    raw([
      {
        id: "both",
        name: "Both calculation formats",
        jct: { score: 1000 },
        microcosm: { budget_effect: 1100 },
        populace: { budget_effect: 900 },
      },
    ]),
    "rel-a",
  );

  expect(v.rows[0].microcosm_estimate).toBe(1100);
  expect(v.rows[0].relative_error).toBeCloseTo(0.1, 6);
});

test("summary counts only scored reforms and averages |error|", () => {
  const unscored = { id: "x", name: "No microcosm estimate", in_sample: false, jct: { score: -500 } };
  const v = buildReformValidation(raw([obbba, salt, unscored]), "rel-a");
  expect(v.summary.n_reforms).toBe(3);
  expect(v.summary.n_scored).toBe(2); // unscored has no microcosm estimate
  expect(v.summary.within_10pct).toBe(2); // obbba 10%, salt 5%
  expect(v.summary.mean_abs_relative_error).toBeCloseTo((0.1 + 0.05) / 2, 6);
});

test("summary isolates the out-of-sample reforms from in-sample targets", () => {
  const inSample = {
    id: "jct_mortgage",
    name: "Mortgage interest deduction",
    in_sample: true,
    jct: { score: 1000 },
    microcosm: { budget_effect: 2000 }, // |error| 100% — but in-sample
  };
  const v = buildReformValidation(raw([obbba, salt, inSample]), "rel-a");
  expect(v.summary.n_out_of_sample).toBe(2); // obbba + salt
  expect(v.summary.n_out_of_sample_scored).toBe(2);
  expect(v.summary.out_of_sample_within_10pct).toBe(2);
  // out-of-sample mean excludes the 100% in-sample miss.
  expect(v.summary.out_of_sample_mean_abs_relative_error).toBeCloseTo((0.1 + 0.05) / 2, 6);
});

test("zero JCT score leaves the row unscored, not a dollar delta as a ratio", () => {
  const v = buildReformValidation(
    raw([
      { id: "z", name: "Zero-cost", jct: { score: 0 }, microcosm: { budget_effect: 25 } },
      { id: "ok", name: "Scored", jct: { score: 1000 }, microcosm: { budget_effect: 1100 } },
    ]),
    "rel-a",
  );
  const zero = v.rows[0];
  // abs_error/direction still describe the row, but there is no relative error.
  expect(zero.abs_error).toBe(25);
  expect(zero.relative_error).toBeNull();
  expect(zero.abs_relative_error).toBeNull();
  expect(zero.within_10pct).toBeNull();
  // The zero-benchmark row must not enter the scored aggregates (a raw $25
  // treated as a fraction would blow up mean/median and within_10pct).
  expect(v.summary.n_scored).toBe(1);
  expect(v.summary.mean_abs_relative_error).toBeCloseTo(0.1, 6);
});
