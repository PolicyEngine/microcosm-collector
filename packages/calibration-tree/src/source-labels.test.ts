import { expect, test } from "bun:test";

import { sourceAuthorityLabel } from "./source-labels";

test("uses curated authority names for Chronicle and calibration sources", () => {
  expect(sourceAuthorityLabel("irs_soi")).toBe("IRS Statistics of Income");
  expect(sourceAuthorityLabel("census_acs")).toBe(
    "Census · American Community Survey",
  );
  expect(sourceAuthorityLabel("census_pep")).toBe(
    "Census · Population Estimates Program",
  );
  expect(sourceAuthorityLabel("cms_medicaid")).toBe("CMS · Medicaid / CHIP");
  expect(sourceAuthorityLabel("hhs_acf_liheap")).toBe("HHS · LIHEAP");
  expect(sourceAuthorityLabel("ici")).toBe("Investment Company Institute");
});

test("names ED Federal Student Aid as Microcosm's US provider labels do", () => {
  // PolicyEngine/microcosm#1357 registers this label for provider ed_fsa in
  // microcosm.calibrate.provider_labels.
  expect(sourceAuthorityLabel("ed_fsa")).toBe("ED · Federal Student Aid");
});

test("uses the calibration-fit acronym-aware fallback for unknown sources", () => {
  expect(sourceAuthorityLabel("new_api_source")).toBe("New API Source");
  expect(sourceAuthorityLabel("constructor")).toBe("Constructor");
});

test("shares publisher labels across country dashboards", () => {
  expect(
    ["statbel", "onss", "jrc", "sfpd", "nasa", "eurostat"].map(
      sourceAuthorityLabel,
    ),
  ).toEqual(["Statbel", "ONSS", "JRC", "SFPD", "NASA", "Eurostat"]);
});
