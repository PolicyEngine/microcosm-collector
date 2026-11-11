import { expect, test } from "bun:test";
import fc from "fast-check";

import {
  COUNTRY_CAPABILITIES,
  COUNTRY_REGISTRY,
  CountryNotConfiguredError,
  DEFAULT_COUNTRY,
  countryCapabilities,
  countryParam,
  countryPointerPath,
  countryRegistration,
  hasCapability,
  isCountry,
  isCountryCapability,
  isCountryNotConfiguredBody,
  isSelectableCountry,
  resolveRegisteredRepository,
  resolveRegisteredStagingRepository,
  selectableCountries,
  type MicrocosmCountry,
} from "./countries";

const COUNTRIES = Object.keys(COUNTRY_REGISTRY) as MicrocosmCountry[];

test("every registration carries the full shape", () => {
  for (const country of COUNTRIES) {
    const registration = countryRegistration(country);
    expect(registration.repo).toMatch(/^[a-z0-9-]+\/[a-z0-9-]+$/);
    expect(registration.revision.length).toBeGreaterThan(0);
    expect(registration.label.length).toBeGreaterThan(0);
    expect(registration.dataset_label.length).toBeGreaterThan(0);
    expect(registration.geography.length).toBeGreaterThan(0);
    expect(
      registration.geography_id === null || registration.geography_id.length > 0,
    ).toBe(true);
    expect(["public", "private"]).toContain(registration.visibility);
    expect(registration.capabilities.length).toBeGreaterThan(0);
    expect(registration.capabilities.every(isCountryCapability)).toBe(true);
    expect(new Set(registration.capabilities).size).toBe(registration.capabilities.length);
    if (registration.repo_env != null) {
      expect(registration.repo_env).toMatch(/^[A-Z][A-Z0-9_]*$/);
    }
    if (registration.revision_env != null) {
      expect(registration.revision_env).toMatch(/^[A-Z][A-Z0-9_]*$/);
    }
    if (registration.staging) {
      expect(registration.staging.repo).toMatch(/^[a-z0-9-]+\/[a-z0-9-]+$/);
      expect(registration.staging.revision.length).toBeGreaterThan(0);
      if (
        registration.staging.repo_env != null ||
        registration.staging.revision_env != null
      ) {
        expect(registration.staging.repo_env).toMatch(/^[A-Z][A-Z0-9_]*$/);
        expect(registration.staging.revision_env).toMatch(/^[A-Z][A-Z0-9_]*$/);
      }
      if (registration.staging.token_env != null) {
        expect(registration.staging.token_env).toMatch(/^[A-Z][A-Z0-9_]*$/);
      }
    }
    expect(hasCapability(country, "staging")).toBe(registration.staging != null);
  }
});

test("keeps the live registrations on their published repositories and labels", () => {
  expect(countryRegistration("us")).toMatchObject({
    repo: "policyengine/populace-us",
    repo_env: "POPULACE_HF_REPO",
    revision: "main",
    label: "United States",
    dataset_label: "Microcosm US",
    geography: "United States",
    geography_id: "0100000US",
    visibility: "public",
    capabilities: COUNTRY_CAPABILITIES,
    staging: {
      repo: "policyengine/populace-us-staging",
      repo_env: "POPULACE_STAGING_HF_REPO",
      revision_env: "POPULACE_STAGING_HF_REVISION",
    },
  });
  expect(countryRegistration("uk")).toMatchObject({
    repo: "policyengine/populace-uk-private",
    repo_env: "POPULACE_UK_HF_REPO",
    label: "United Kingdom",
    dataset_label: "Microcosm UK",
    visibility: "private",
    capabilities: ["calibration", "targets", "compare", "cross_dataset", "staging"],
    staging: {
      repo: "policyengine/populace-uk-staging",
      repo_env: "POPULACE_UK_STAGING_HF_REPO",
      revision_env: "POPULACE_UK_STAGING_HF_REVISION",
      token_env: "POPULACE_UK_STAGING_HF_TOKEN",
    },
    jurisdiction_aliases: ["GB"],
  });
  expect(countryRegistration("be")).toMatchObject({
    repo: "policyengine/populace-be-private",
    repo_env: "POPULACE_BE_HF_REPO",
    label: "Belgium",
    dataset_label: "Microcosm Belgium",
    geography: "Belgium",
    visibility: "private",
  });
});

test("selectable countries follow registry order and exclude fixtures", () => {
  expect(selectableCountries()).toEqual(["us", "uk", "be"]);
  expect(countryRegistration("zz").fixture).toBe(true);
  expect(countryRegistration("am").fixture).toBe(true);
  expect(selectableCountries()).not.toContain("zz");
});

test("repository resolution applies explicit deployment overrides", () => {
  expect(resolveRegisteredRepository("uk", {})).toEqual({
    repo: "policyengine/populace-uk-private",
    revision: "main",
  });
  expect(
    resolveRegisteredRepository("uk", {
      POPULACE_UK_HF_REPO: "example/uk-release",
      POPULACE_UK_HF_REVISION: "release-revision",
    }),
  ).toEqual({
    repo: "example/uk-release",
    revision: "release-revision",
  });
  expect(
    resolveRegisteredStagingRepository("uk", {
      POPULACE_UK_STAGING_HF_REPO: "example/uk-staging",
      POPULACE_UK_STAGING_HF_REVISION: "staging-revision",
    }),
  ).toEqual({
    repo: "example/uk-staging",
    revision: "staging-revision",
  });
  expect(resolveRegisteredStagingRepository("be", {})).toBeNull();
});

test("fixture registrations are valid countries that requests cannot select", () => {
  expect(isCountry("zz")).toBe(true);
  expect(isCountry("am")).toBe(true);
  expect(isSelectableCountry("zz")).toBe(false);
  expect(isSelectableCountry("am")).toBe(false);
  expect(countryParam("zz")).toEqual({ configured: false, requested: "zz" });
  expect(countryParam("am")).toEqual({ configured: false, requested: "am" });
});

test("a country parameter is exact; only its absence selects the registry default", () => {
  expect(DEFAULT_COUNTRY).toBe("us");
  expect(countryParam("be")).toEqual({ configured: true, country: "be", explicit: true });
  expect(countryParam("uk")).toEqual({ configured: true, country: "uk", explicit: true });
  expect(countryParam("us")).toEqual({ configured: true, country: "us", explicit: true });
  expect(countryParam("")).toEqual({ configured: true, country: DEFAULT_COUNTRY, explicit: false });
  expect(countryParam(null)).toEqual({ configured: true, country: DEFAULT_COUNTRY, explicit: false });
  expect(countryParam(undefined)).toEqual({
    configured: true,
    country: DEFAULT_COUNTRY,
    explicit: false,
  });
  // The production report (2026-10-20): ?country=nz served the US release.
  expect(countryParam("nz")).toEqual({ configured: false, requested: "nz" });
  expect(countryParam("BE")).toEqual({ configured: false, requested: "BE" });
  expect(countryParam("fr")).toEqual({ configured: false, requested: "fr" });
  expect(countryParam(" us")).toEqual({ configured: false, requested: " us" });
  expect(countryParam("constructor")).toEqual({ configured: false, requested: "constructor" });
  expect(countryParam("__proto__")).toEqual({ configured: false, requested: "__proto__" });
  expect(isCountry("constructor")).toBe(false);
  expect(isCountry("__proto__")).toBe(false);
  expect(isCountry(null)).toBe(false);
});

// Invariant: a present country parameter resolves to itself or to nothing.
// It never resolves to a different country (the default included).
function assertNeverAnotherCountry(value: string): void {
  const param = countryParam(value);
  if (value === "") {
    expect(param).toEqual({ configured: true, country: DEFAULT_COUNTRY, explicit: false });
  } else if (param.configured) {
    expect<string>(param.country).toBe(value);
    expect(param.explicit).toBe(true);
    expect(isSelectableCountry(value)).toBe(true);
  } else {
    expect(param.requested).toBe(value);
    expect(isSelectableCountry(value)).toBe(false);
  }
}

test("every two-letter code resolves to itself or is not configured", () => {
  const letters = "abcdefghijklmnopqrstuvwxyz";
  let configured = 0;
  for (const first of letters) {
    for (const second of letters) {
      for (const code of [`${first}${second}`, `${first}${second}`.toUpperCase()]) {
        assertNeverAnotherCountry(code);
        if (countryParam(code).configured) configured += 1;
      }
    }
  }
  expect(configured).toBe(selectableCountries().length);
});

test("property: any string resolves to itself or is not configured", () => {
  fc.assert(
    fc.property(
      fc.oneof(
        fc.string(),
        fc.string({ unit: "grapheme", maxLength: 8 }),
        fc.stringMatching(/^[A-Za-z]{2,3}$/),
        fc.constantFrom(...COUNTRIES, ...COUNTRIES.map((code) => code.toUpperCase())),
      ),
      (value) => assertNeverAnotherCountry(value),
    ),
    { numRuns: 2_000 },
  );
});

test("a not-configured error names the requested code and why it has no release", () => {
  const unknown = new CountryNotConfiguredError("nz");
  expect(unknown.body()).toEqual({
    error: "country_not_configured",
    requested_country: "nz",
    reason: "unknown_country",
    detail: 'No Microcosm calibration release is configured for country "nz".',
  });
  expect(isCountryNotConfiguredBody(unknown.body())).toBe(true);
  // A registered country without a manifest entry: a newly registered country
  // before its first publication.
  expect(new CountryNotConfiguredError("be").body()).toMatchObject({
    requested_country: "be",
    reason: "no_published_release",
  });
  expect(new CountryNotConfiguredError("zz").reason).toBe("unknown_country");
  expect(isCountryNotConfiguredBody({ detail: "HF fetch failed 502" })).toBe(false);
  expect(isCountryNotConfiguredBody(null)).toBe(false);
});

test("capability gates read the registration", () => {
  expect(hasCapability("us", "staging")).toBe(true);
  expect(hasCapability("am", "staging")).toBe(true);
  expect(hasCapability("us", "model_coverage")).toBe(true);
  expect(hasCapability("uk", "staging")).toBe(true);
  expect(hasCapability("be", "pipeline")).toBe(false);
  expect(hasCapability("be", "cross_dataset")).toBe(true);
  expect(countryCapabilities("be")).toEqual([
    "calibration",
    "targets",
    "compare",
    "cross_dataset",
  ]);
  expect(countryCapabilities("zz")).toEqual(["calibration", "targets", "compare"]);
  expect(isCountryCapability("staging")).toBe(true);
  expect(isCountryCapability("dashboard_admin")).toBe(false);
  expect(isCountryCapability(7)).toBe(false);
});

test("only the UK resolves its current release through a line pointer", () => {
  // microcosm#823: the UK default line is promoted through latest-national.json.
  expect(countryPointerPath("uk")).toBe("latest-national.json");
  expect(countryPointerPath("us")).toBe("latest.json");
  expect(countryPointerPath("be")).toBe("latest.json");
  for (const country of COUNTRIES) {
    const pointer = countryRegistration(country).pointer_path;
    if (pointer != null) expect(pointer).toMatch(/^latest-[a-z]+(-k[1-9][0-9]*)?\.json$/);
  }
});

test("no registration says how its releases are published: every reader looks for releases.json", () => {
  // A dataset repository with releases.json is read from it and its build
  // folders; one without is read from its tags and pointer files. So a
  // repository's switch to releases.json needs no change to its registration.
  for (const country of COUNTRIES) {
    expect(Object.hasOwn(countryRegistration(country), "release_layout"), country).toBe(false);
  }
});
