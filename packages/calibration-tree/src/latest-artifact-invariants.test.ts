import { expect, test } from "bun:test";
import fc from "fast-check";

import edFsaFamily from "./fixtures/ed-fsa-family/ed-fsa-family-schema-8.json";
import {
  buildCalibration,
  buildComparison,
  latestMicrocosmTargetDiagnosticsPage,
  microcosmVariableSummary,
} from "./latest-artifact";

// Generated-input checks of the readers that #277 adds, from the independent
// review's scoped run (fast-check, seed 277, 20,000 runs each). Each holds
// within the scope its comment states; outside it, the review's strict run
// found the counterexample the comment names, each by design. Each runs
// through a public reader that calls the helper, so the helpers stay private.
const params = { numRuns: 20_000, seed: 277, endOnFailure: false };
// 20,000 releases take a few seconds; leave room on a slow runner.
const TIMEOUT_MS = 60_000;

type JsonObject = Record<string, unknown>;
type Calibration = ReturnType<typeof buildCalibration>;

const fixture = edFsaFamily as JsonObject;
const fsaTargets = fixture.targets as JsonObject[];
// One single-fact FSA target, and the release it builds.
const singleFactTarget = fsaTargets.find(
  (row) => !(row.metadata as JsonObject).ledger_member_fact_count,
)!;
const fsaRelease = buildCalibration(fixture, "invariants-test", null, {}, {}, {}, "us");

function releaseOf(rows: JsonObject[]): Calibration {
  return { ...fsaRelease, rows } as Calibration;
}

function targetsPage(cal: Calibration, variable?: string) {
  const url = new URL("https://dashboard.test/api/microcosm/target-diagnostics");
  url.searchParams.set("limit", "500");
  if (variable) url.searchParams.set("variable", variable);
  return latestMicrocosmTargetDiagnosticsPage(url.toString(), cal);
}

// The universe constraint count the targets page shows for a release whose
// one target's metadata carries `value` as ledger_universe_constraint_count
// (countOrNull, through chronicleFactFields).
function shownCount(value: unknown): unknown {
  const target = structuredClone(singleFactTarget);
  (target.metadata as JsonObject).ledger_universe_constraint_count = value;
  const cal = buildCalibration({ ...fixture, targets: [target] }, "count-test", null, {}, {}, {}, "us");
  const [row] = targetsPage(cal).targets;
  return (row.chronicle as JsonObject).universe_constraint_count;
}

// Out of scope: countOrNull(-0) returns -0, which JSON writes as 0.
const isCount = (value: unknown) =>
  value === null || (typeof value === "number" && Number.isSafeInteger(value) && value >= 0);

const countLike = fc.oneof(
  fc.anything(),
  fc.double(),
  fc.integer(),
  fc.bigInt().map(String),
  fc.string(),
  fc.stringMatching(/^\s*-?[0-9]{0,20}(\.[0-9]{0,3})?(e[0-9]{1,3})?\s*$/),
  fc.constantFrom(
    "",
    " ",
    "\n",
    "-0",
    "0x10",
    "1e3",
    "Infinity",
    "NaN",
    "2.5",
    "-1",
    "9007199254740992",
    "٣",
    " 7 ",
    "﻿7",
  ),
);

test(
  "a universe constraint count reads null or a safe non-negative integer",
  () => {
    fc.assert(fc.property(countLike, (value) => isCount(shownCount(value))), params);
  },
  TIMEOUT_MS,
);

test(
  "a universe constraint count reads n and String(n) as n",
  () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: Number.MAX_SAFE_INTEGER }),
        (n) => shownCount(n) === n && shownCount(String(n)) === n,
      ),
      params,
    );
  },
  TIMEOUT_MS,
);

const levelValue = fc.oneof(
  fc.constantFrom("country", "state", "national", "region", "", null, undefined, " "),
  fc.string({ maxLength: 4 }),
  fc.integer({ min: -2, max: 2 }),
);
const category = fc.array(fc.record({ level: levelValue }), { maxLength: 8 });

// The levels a category's rows carry: sorted, unique and non-empty, read as
// the page's Level filter reads them.
const inputLevels = (rows: { level: unknown }[]) =>
  [...new Set(rows.map((row) => String(row.level ?? "")))].filter(Boolean).sort();

// The FSA targets the comparison property relabels, each its own target.
const comparisonTargets = fsaRelease.rows.slice(0, 8);

// A category's level and levels as each reader reports them: the targets
// API's variable summary keeps none = "", and the comparison none = null
// (categoryLevels). Both group the rows under one variable.
const READERS = [
  {
    none: "",
    read: (rows: { level: unknown }[]) =>
      microcosmVariableSummary(rows.map((row) => ({ ...row, variable_key: "v" }))),
  },
  {
    none: null,
    read: (rows: { level: unknown }[]) => {
      const cal = releaseOf(
        rows.map((row, index) => ({ ...comparisonTargets[index], level: row.level, variable_key: "v" })),
      );
      return buildComparison(cal, cal).variables;
    },
  },
] as const;

// Out of scope: with none = null (the comparison's) and no level at all, a
// category reads level null, as one at two levels does, by design.
for (const { none, read } of READERS) {
  test(
    `a category's levels are its rows' sorted, unique, non-empty levels; level null exactly when two or more (none = ${JSON.stringify(none)})`,
    () => {
      fc.assert(
        fc.property(category, (rows) => {
          const summaries = read(rows);
          if (rows.length === 0) {
            expect(summaries).toEqual([]);
            return;
          }
          expect(summaries).toHaveLength(1);
          const { level, levels } = summaries[0];
          // A reader that returns no levels, or levels the rows do not carry,
          // fails here.
          expect(levels).toEqual(inputLevels(rows));
          expect(levels).toEqual([...levels].sort());
          expect(new Set(levels).size).toBe(levels.length);
          expect(levels.every((value) => typeof value === "string" && value.length > 0)).toBe(true);
          if (levels.length > 0 || none !== null) expect(level === null).toBe(levels.length >= 2);
          expect(level).toBe(levels.length >= 2 ? null : (levels[0] ?? none));
        }),
        params,
      );
    },
    TIMEOUT_MS,
  );
}

const geographies = ["United States", "Alabama", "Alaska", "Wales", "Flanders"];
const facetRow = fc
  .record({
    geography: fc.oneof(fc.constantFrom(...geographies), fc.constant(""), fc.constant(undefined)),
    level: fc.constantFrom("country", "state", "region"),
    dims: fc.array(
      fc.record({
        key: fc.constantFrom("loc", "sex", "age"),
        value: fc.oneof(
          fc.constantFrom(...geographies),
          fc.constantFrom("Male", "Female", "18-64"),
          fc.constant(""),
        ),
      }),
      { maxLength: 3 },
    ),
  })
  .map((row) => ({
    variable_key: "v",
    geography: row.geography,
    level: row.level,
    target_dimensions: [
      ...new Map(row.dims.map((dim) => [dim.key, { ...dim, label: dim.key }])).values(),
    ],
  }));

// The facets the targets page lists for one variable (computeDimensions,
// which leaves out a dimension that only repeats the geography:
// repeatsGeography).
test(
  "the facets never hide Geography, nor a dimension that differs from some row's geography",
  () => {
    fc.assert(
      fc.property(fc.array(facetRow, { maxLength: 10 }), (rows) => {
        const facets = targetsPage(releaseOf(rows), "v").dimensions.map((facet) => facet.key);
        const geographyValues = new Set(rows.map((row) => row.geography).filter(Boolean));
        if (geographyValues.size > 1) expect(facets).toContain("geography");
        for (const key of ["loc", "sex", "age"]) {
          const differs = rows.some((row) => {
            const dim = row.target_dimensions.find((candidate) => candidate.key === key);
            return dim && dim.value && dim.value !== (row.geography || undefined);
          });
          const values = new Set(
            rows
              .map((row) => row.target_dimensions.find((candidate) => candidate.key === key)?.value)
              .filter(Boolean),
          );
          if (differs && values.size > 1) expect(facets).toContain(key);
          // A dimension that equals its row's geography wherever it appears
          // only repeats the Geography facet.
          if (!differs) expect(facets).not.toContain(key);
        }
      }),
      params,
    );
  },
  TIMEOUT_MS,
);
