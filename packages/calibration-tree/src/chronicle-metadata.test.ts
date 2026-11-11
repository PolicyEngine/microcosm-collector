import { describe, expect, test } from "bun:test";

import { normalizeChronicleMetadata } from "./chronicle-metadata";

describe("normalizeChronicleMetadata", () => {
  test("aliases every populated legacy Ledger field without an allowlist", () => {
    const metadata = {
      ledger_geography_id: "0400000US02",
      ledger_filter_income_range: "1_to_10k",
      ledger_future_field: "future-value",
      source_measure_id: "taxable_interest_amount",
    };

    expect(normalizeChronicleMetadata(metadata)).toEqual({
      ...metadata,
      chronicle_geography_id: "0400000US02",
      chronicle_filter_income_range: "1_to_10k",
      chronicle_future_field: "future-value",
    });
  });

  test("prefers populated Chronicle fields when both namespaces exist", () => {
    expect(
      normalizeChronicleMetadata({
        ledger_geography_id: "0400000US02",
        chronicle_geography_id: "0400000US06",
        // A release writes every metadata value as a string.
        ledger_universe_constraint_count: "4",
        chronicle_universe_constraint_count: "0",
      }),
    ).toMatchObject({
      chronicle_geography_id: "0400000US06",
      chronicle_universe_constraint_count: "0",
    });
  });

  test("falls back when a Chronicle string is blank and tolerates non-objects", () => {
    expect(
      normalizeChronicleMetadata({
        ledger_measure_unit: "usd",
        chronicle_measure_unit: "  ",
      }).chronicle_measure_unit,
    ).toBe("usd");
    expect(normalizeChronicleMetadata(null)).toEqual({});
    expect(normalizeChronicleMetadata([])).toEqual({});
  });
});
