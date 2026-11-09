import { expect, test } from "bun:test";

import {
  readStructuredDimensions,
  structuredDimensionKey,
} from "./structured-dimension-reader";

test("structured dimension keys derive from ids rather than display labels", () => {
  const result = readStructuredDimensions(
    { origin: "north", destination: "east" },
    {
      origin: { label: "Region", values: { north: "North" } },
      destination: { label: "Region", values: { east: "East" } },
    },
  );

  expect(result.dimensions).toEqual([
    expect.objectContaining({ key: "bd_origin", label: "Region", value: "North" }),
    expect.objectContaining({ key: "bd_destination", label: "Region", value: "East" }),
  ]);
});

test("punctuated and reserved ids receive distinct query-safe keys", () => {
  const punctuated = structuredDimensionKey("us:statutes/26/62#income");
  const reserved = structuredDimensionKey("x0_7573");

  expect(punctuated).toMatch(/^bd_[a-z0-9_]+$/);
  expect(reserved).toMatch(/^bd_[a-z0-9_]+$/);
  expect(punctuated).not.toBe(reserved);
  expect(structuredDimensionKey("age_band")).toBe("bd_age_band");
});
