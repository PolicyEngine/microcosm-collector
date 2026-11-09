import { expect, test } from "bun:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import beDiagnosticsFixture from "./fixtures/be-release/calibration_diagnostics.json";
import beReleaseManifestFixture from "./fixtures/be-release/release_manifest.json";

// Deprecated upstream identifiers: fixtures mirror current Microcosm release
// names and its `ledger_*` Chronicle metadata contract.

import {
  buildCalibration,
  buildComparison,
  calibrationAsReleased,
  calibrationLossKind,
  classifyApiError,
  diagnosticsDimensions,
  MICROCOSM_HF_REPO_ENV,
  MICROCOSM_BE_HF_REPO_ENV,
  MICROCOSM_BE_HF_REVISION_ENV,
  MICROCOSM_UK_HF_REPO_ENV,
  MICROCOSM_UK_HF_REVISION_ENV,
  hfResolveUrl,
  latestMicrocosmCalibrationHighlights,
  latestMicrocosmCalibrationSummary,
  latestMicrocosmTargetDiagnosticsPage,
  latestMicrocosmTargetInvestigation,
  loadPointerReleaseId,
  loadPublishedCutReleases,
  loadRecordedReleases,
  loadRelease,
  loadReleases,
  mergeReleaseInventory,
  microcosmRepo,
  microcosmRevision,
  microcosmTargetTreemap,
  CountryNotConfiguredError,
  countryParam,
  isCountry,
  parseHashedJsonArtifact,
  releaseCountry,
  releasePresentation,
  releasePublisherLabels,
  releaseRole,
  UK_LOCAL_CALIBRATION_DIR_ENV,
  type ArtifactCountry,
  type Calibration,
  type ReleaseEntry,
} from "./latest-artifact";
import {
  emptyCalibrationTreeManifest,
  withCalibrationTreeManifestEntry,
  type CalibrationTreeManifest,
  type CalibrationTreeManifestEntry,
} from "./calibration-tree-manifest";
import { buildCalibrationTree } from "./calibration-tree";
import { missingReleasesRecord, missingRevision } from "./test-support/releases-record-responses";

test("keeps Microcosm deployment configuration on its published Populace env contract", () => {
  expect([
    MICROCOSM_HF_REPO_ENV,
    MICROCOSM_UK_HF_REPO_ENV,
    MICROCOSM_UK_HF_REVISION_ENV,
    MICROCOSM_BE_HF_REPO_ENV,
    MICROCOSM_BE_HF_REVISION_ENV,
  ]).toEqual([
    "POPULACE_HF_REPO",
    "POPULACE_UK_HF_REPO",
    "POPULACE_UK_HF_REVISION",
    "POPULACE_BE_HF_REPO",
    "POPULACE_BE_HF_REVISION",
  ]);
});

test("hashes calibration diagnostics before parsing JSON", () => {
  const artifact = parseHashedJsonArtifact(
    new TextEncoder().encode('{"value":1}\n'),
  );
  expect(artifact.payload).toEqual({ value: 1 });
  expect(artifact.sha256).toBe(
    "3a37782e8974c48eebf2a0517c866ad15641c53b3d31993188796b56aeb79624",
  );
});

test("a country parameter names its own country or is not configured, never US by fallback", () => {
  expect(countryParam("be")).toEqual({ configured: true, country: "be", explicit: true });
  expect(countryParam("uk")).toEqual({ configured: true, country: "uk", explicit: true });
  expect(countryParam("us")).toEqual({ configured: true, country: "us", explicit: true });
  expect(countryParam("BE")).toEqual({ configured: false, requested: "BE" });
  expect(countryParam("fr")).toEqual({ configured: false, requested: "fr" });
  expect(countryParam("nz")).toEqual({ configured: false, requested: "nz" });
  expect(countryParam(null)).toEqual({ configured: true, country: "us", explicit: false });
});

test("the conformance fixture is a registered code that requests cannot select", () => {
  expect(isCountry("zz")).toBe(true);
  expect(countryParam("zz")).toEqual({ configured: false, requested: "zz" });
  expect(countryParam("ZZ")).toEqual({ configured: false, requested: "ZZ" });
});

test("a country without a configured release is a 404 not-configured response", () => {
  expect(classifyApiError(new CountryNotConfiguredError("nz"))).toEqual({
    status: 404,
    body: {
      error: "country_not_configured",
      requested_country: "nz",
      reason: "unknown_country",
      detail: 'No Microcosm calibration release is configured for country "nz".',
    },
  });
  expect(classifyApiError(new CountryNotConfiguredError("be", "no_published_release"))).toEqual({
    status: 404,
    body: {
      error: "country_not_configured",
      requested_country: "be",
      reason: "no_published_release",
      detail: "No Microcosm calibration release has been published for Belgium yet.",
    },
  });
  expect(classifyApiError(new Error("HF fetch failed 500")).status).toBe(502);
});

test("uses the private Belgium repository and country revision", () => {
  expect(microcosmRepo("be")).toBe("policyengine/populace-be-private");
  expect(microcosmRevision("be")).toBe("main");
  expect(hfResolveUrl("latest.json", "be")).toBe(
    "https://huggingface.co/datasets/policyengine/populace-be-private/resolve/main/latest.json",
  );
});

test("loads an explicit UK local schema 8 artifact without a network request", async () => {
  const directory = await mkdtemp(join(tmpdir(), "microcosm-schema-8-"));
  const prior = process.env[UK_LOCAL_CALIBRATION_DIR_ENV];
  process.env[UK_LOCAL_CALIBRATION_DIR_ENV] = directory;
  try {
    await Promise.all([
      writeFile(
        join(directory, "calibration_diagnostics.json"),
        JSON.stringify({
          schema_version: 8,
          release_id: "local-schema-8",
          targets: [
            {
              name: "provider.target@2025",
              target_name: "provider.target",
              source: "Citation",
              target: 100,
              initial_estimate: 90,
              final_estimate: 100,
              hierarchy: {
                provider: { id: "provider", label: "Provider label" },
                category: {
                  id: "provider.category",
                  label: "Category label",
                  provider_id: "provider",
                },
                geography: {
                  id: "K02000001",
                  label: "United Kingdom",
                  level: "country",
                },
                dimensions: [],
                target: { id: "provider.target", label: "Target label" },
              },
            },
          ],
        }),
      ),
      writeFile(
        join(directory, "release_manifest.json"),
        JSON.stringify({ country: { code: "uk" } }),
      ),
    ]);

    const pointer = await loadPointerReleaseId(0, "uk");
    const releases = await loadReleases(0, "uk");
    const calibration = await loadRelease("latest", 0, "uk");

    expect(pointer.release_id).toBe("local-schema-8");
    expect(releases.map((release) => release.release_id)).toEqual([
      "local-schema-8",
    ]);
    expect(calibration.source).toBe("local_filesystem");
    expect(calibration.target_schema.target_representation).toBe("hierarchy");
    expect(calibration.rows[0].target_label).toBe("Target label");
  } finally {
    if (prior == null) delete process.env[UK_LOCAL_CALIBRATION_DIR_ENV];
    else process.env[UK_LOCAL_CALIBRATION_DIR_ENV] = prior;
    await rm(directory, { recursive: true, force: true });
  }
});

test("loads trimmed Belgium diagnostics without optional US artifact fields", () => {
  expect("loss_trajectory" in beDiagnosticsFixture).toBe(false);
  expect("past_cap_census" in beDiagnosticsFixture).toBe(false);
  expect(beDiagnosticsFixture.targets.every((row) => !("registry" in row))).toBe(true);
  const cal = buildCalibration(
    beDiagnosticsFixture,
    "microcosm-be-2026-chronicle-3cef97b-20260823T134247Z",
    null,
    {},
    beReleaseManifestFixture,
    {},
    "be",
  );

  expect(cal.country).toBe("be");
  expect(cal.diagnostics_status).toBe("ok");
  expect(cal.included_target_count).toBe(3);
  expect(cal.loss_trajectory).toEqual([]);
  expect(cal).toMatchObject({
    effective_sample_size: 560.7099759427821,
    realized_max_weight_ratio: 49.58337498396423,
    top_1pct_weight_share: 0.5115731886787078,
  });
  expect(latestMicrocosmCalibrationSummary(cal)).toMatchObject({
    effective_sample_size: 560.7099759427821,
    realized_max_weight_ratio: 49.58337498396423,
    top_1pct_weight_share: 0.5115731886787078,
  });
  expect(cal.description).toBe(
    "Microcosm-BE: synthetic Belgian population calibrated to Belgian administrative and national-accounts targets (sums of Chronicle facts; surveys validation-only). Support records: US survey donor pool, reweighted; a Belgian donor pool is the planned upgrade. Cross-engine agreement is evidence about the encodings.",
  );
  expect(cal.rows.every((row) => row.registry == null)).toBe(true);

  expect(cal.rows[0]).toMatchObject({
    source: "statbel",
    variable: "population",
    family: "statbel/population",
    geography: "Brussels",
    level: "region",
    dimension_adapter: "legacy_filter",
    breakdown: "Male · 0–17",
    target_dimensions: [
      expect.objectContaining({ key: "bd_sex", label: "Sex", value: "Male" }),
      expect.objectContaining({ key: "bd_age_band", label: "Age band", value: "0–17" }),
    ],
  });
  expect(cal.rows[1]).toMatchObject({
    source: "jrc",
    variable: "national income tax amount",
    geography: "Belgium",
    level: "national",
    dimension_adapter: "legacy_name",
  });
  expect(cal.rows[2]).toMatchObject({
    source: "eurostat",
    variable: "taxable movable income analogue",
    geography: "Belgium",
    level: "national",
  });
});

test("derives Belgium population region, sex, and age-band browser facets", () => {
  const first = beDiagnosticsFixture.targets[0];
  const target = (name: string) => ({
    ...first,
    name: `${name}@2026`,
    target_name: name,
    filter: `cell_${name.replace(/^statbel_population_/, "")}`,
  });
  const cal = buildCalibration(
    {
      ...beDiagnosticsFixture,
      schema_version: 2,
      targets: [
        target("statbel_population_be1_male_0_17"),
        target("statbel_population_be2_female_18_64"),
        target("statbel_population_be3_male_65_plus"),
      ],
    },
    "be-facets",
    null,
    {},
    beReleaseManifestFixture,
    {},
    "be",
  );
  const result = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?variable=statbel%20%2F%20population",
    cal,
  );

  expect(result.sources).toEqual(["statbel"]);
  expect(result.dimensions).toEqual([
    { key: "geography", label: "Region", values: ["Brussels", "Flanders", "Wallonia"] },
    { key: "bd_sex", label: "Sex", values: ["Female", "Male"] },
    { key: "bd_age_band", label: "Age band", values: ["0–17", "18–64", "65+"] },
  ]);
});

test("diagnosticsDimensions drops malformed entries and normalizes optional metadata", () => {
  expect(diagnosticsDimensions({ dimensions: [] })).toEqual({});
  expect(
    diagnosticsDimensions({
      dimensions: {
        region: {
          label: "  Region  ",
          role: "geography",
          level: "  region  ",
          values: { north: "  North  ", south: " ", broken: 3 },
          order: [" south ", 3, "north", " "],
        },
        sex: { label: "Sex", role: "category", values: { female: "Female" } },
        missing_label: { values: { value: "Value" } },
        scalar: "Region",
      },
    }),
  ).toEqual({
    region: {
      label: "Region",
      role: "geography",
      level: "region",
      values: { north: "North" },
      order: ["south", "north"],
    },
    sex: { label: "Sex", values: { female: "Female" } },
  });
});

test("schema 8 preserves the complete producer-authored hierarchy", () => {
  const cal = buildCalibration(
    {
      schema_version: 8,
      targets: [
        {
          name: "obr.income_tax@2025",
          target_name: "obr.income_tax",
          source: "OBR synthetic citation",
          target: 100,
          initial_estimate: 90,
          final_estimate: 101,
          hierarchy: {
            provider: {
              id: "obr",
              label: "Office for Budget Responsibility — authored",
            },
            category: {
              id: "obr.efo_receipts",
              label: "Economic and fiscal outlook receipts — authored",
              provider_id: "obr",
            },
            geography: {
              id: "K02000001",
              label: "United Kingdom — authored",
              level: "country",
            },
            dimensions: [
              {
                id: "obr.efo_line",
                label: "Economic and fiscal outlook line — authored",
                value_id: "income_tax",
                value_label: "Income tax receipts — authored",
              },
            ],
            target: {
              id: "obr.income_tax",
              label: "Income tax target — authored",
            },
          },
        },
      ],
    },
    "schema-8",
  );

  expect(cal.target_schema).toEqual({
    diagnostics_schema_version: 8,
    structured_dimensions: false,
    target_representation: "hierarchy",
  });
  expect(cal.hierarchy_label_variants).toEqual([]);
  expect(latestMicrocosmCalibrationSummary(cal).label_variants).toEqual({
    count: 0,
    entries: 0,
    examples: [],
  });
  expect(cal.rows[0]).toMatchObject({
    family: "obr/obr.efo_receipts",
    source: "obr",
    source_label: "Office for Budget Responsibility — authored",
    variable: "obr.efo_receipts",
    variable_label: "Economic and fiscal outlook receipts — authored",
    geography: "United Kingdom — authored",
    geography_id: "K02000001",
    level: "country",
    target_label: "Income tax target — authored",
    target_representation: "hierarchy",
    dimension_adapter: "hierarchy",
    target_dimensions: [
      {
        key: "obr.efo_line",
        label: "Economic and fiscal outlook line — authored",
        value: "Income tax receipts — authored",
        value_id: "income_tax",
        source_key: "obr.efo_line",
        raw_value: "income_tax",
        rank: 0,
      },
    ],
  });
});

test("top-level schema version selects one reader and rejects unsupported versions", () => {
  expect(
    buildCalibration(
      {
        schema_version: 6,
        targets: [{
          name: "legacy.population@2025",
          target_name: "legacy.population",
          source: "Citation",
          target: 100,
          initial_estimate: 90,
          final_estimate: 100,
        }],
      },
      "schema-6",
    ).target_schema.target_representation,
  ).toBe("legacy");
  expect(() =>
    buildCalibration(
      {
        schema_version: 7,
        targets: [{ name: "legacy-row", source: "Citation" }],
      },
      "invalid-schema-7",
    ),
  ).toThrow("Schema 7 target source.id");
  expect(() =>
    buildCalibration(
      { schema_version: 8, targets: [{ name: "missing-hierarchy" }] },
      "invalid-schema-8",
    ),
  ).toThrow("hierarchy must be an object");
  // Schema 9 is read (schema-9-diagnostics.test.ts); 10 is not.
  for (const schemaVersion of [undefined, 0, 10]) {
    expect(() =>
      buildCalibration(
        { schema_version: schemaVersion, targets: [] },
        "unsupported-schema",
      ),
    ).toThrow("schema_version");
  }
});

test("structured dimensions shape rows and honor artifact value order", () => {
  const target = (
    suffix: string,
    dimensions: Record<string, string>,
  ) => ({
    name: `fixture.population.${suffix}@2026`,
    target_name: `fixture.population.${suffix}`,
    source: { id: "novastat_agency", citation: "ZZ official population table" },
    variable: { id: "population", measure: "count" },
    metadata: {
      chronicle_record_ids: [`novastat_agency.population.${suffix}`],
      variable: "population",
      source_measure_id: "population_count",
    },
    dimensions,
    target: 100,
    initial_estimate: 90,
    final_estimate: 100,
  });
  const cal = buildCalibration(
    {
      schema_version: 7,
      dimensions: {
        region: {
          label: "Region",
          role: "geography",
          level: "region",
          values: { north: "North", south: "South" },
        },
        sex: {
          label: "Sex",
          values: { female: "Female", male: "Male" },
          order: ["male", "female"],
        },
        age_band: {
          label: "Age band",
          values: { "65_plus": "65+", "0_17": "0–17", "18_64": "18–64" },
        },
      },
      targets: [
        target("north_female_0_17", {
          region: "north",
          sex: "female",
          age_band: "0_17",
          settlement_type: "urban_core",
        }),
        target("south_male_65_plus", {
          region: "south",
          sex: "male",
          age_band: "65_plus",
          settlement_type: "urban_core",
        }),
        target("north_male_18_64", {
          region: "north",
          sex: "male",
          age_band: "18_64",
          settlement_type: "urban_core",
        }),
      ],
    },
    "structured-dimensions",
  );

  expect(cal.target_schema).toEqual({
    diagnostics_schema_version: 7,
    structured_dimensions: true,
    target_representation: "structured",
  });
  expect(cal.rows.every((row) => row.dimension_adapter === "structured")).toBe(true);
  expect(cal.rows.every((row) => row.target_representation === "structured")).toBe(true);
  expect(cal.rows[0]).toMatchObject({
    family: "novastat_agency/population",
    geography: "North",
    level: "region",
    breakdown: "Female · 0–17 · Urban Core",
    target_dimensions: [
      {
        key: "bd_sex",
        label: "Sex",
        value: "Female",
        source_key: "sex",
        raw_value: "female",
        rank: 1,
      },
      {
        key: "bd_age_band",
        label: "Age band",
        value: "0–17",
        source_key: "age_band",
        raw_value: "0_17",
        rank: 1,
      },
      {
        key: "bd_settlement_type",
        label: "Settlement Type",
        value: "Urban Core",
        source_key: "settlement_type",
        raw_value: "urban_core",
      },
    ],
  });
  const page = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?variable=novastat_agency%20%2F%20population%20%C2%B7%20count",
    cal,
  );
  expect(page.target_schema).toEqual(cal.target_schema);
  expect(page.dimensions).toEqual([
    { key: "geography", label: "Region", values: ["North", "South"] },
    { key: "bd_sex", label: "Sex", values: ["Male", "Female"] },
    { key: "bd_age_band", label: "Age band", values: ["65+", "0–17", "18–64"] },
  ]);
});

test("legacy UK geography metadata resolves GSS identifiers across target-name formats", () => {
  const legacyTarget = (
    name: string,
    geographyId: string,
    geographyName?: string,
  ) => ({
    name: `${name}@2025`,
    target_name: name,
    period: 2025,
    entity: "household",
    measure: { kind: "column", name },
    filter: null,
    source: "UK source citation",
    metadata: {
      ledger_geography_level: "country",
      ledger_geography_id: geographyId,
      ...(geographyName ? { ledger_geography_name: geographyName } : {}),
    },
    registry: { family: "uk_source" },
    target: 100,
    initial_estimate: 90,
    final_estimate: 100,
  });
  const cal = buildCalibration(
    {
      schema_version: 6,
      targets: [
        legacyTarget("obr.receipts.income_tax", "K02000001"),
        legacyTarget("dwp/uc/payment_band", "K03000001"),
        legacyTarget("ons.population.england", "E92000001"),
        legacyTarget("scotgov.council_tax.band_a", "S92000003"),
        legacyTarget("ons.population.published", "K02000001", "Published area"),
        legacyTarget("ons.population.named_uk", "K02000001", "United Kingdom"),
        legacyTarget("ons.population.unknown", "X00000000"),
      ],
    },
    "uk-legacy-geography",
    null,
    {},
    {},
    {},
    "uk",
  );

  expect(cal.rows.map((row) => [row.geography, row.level])).toEqual([
    ["United Kingdom", "country"],
    ["Great Britain", "country"],
    ["England", "country"],
    ["Scotland", "country"],
    ["Published area", "country"],
    ["United Kingdom", "country"],
    ["United Kingdom", "national"],
  ]);
});

test("legacy US geography normalization remains unchanged", () => {
  const cal = buildCalibration(
    {
      schema_version: 6,
      targets: [
        {
          name: "census.population.total@2025",
          target_name: "census.population.total",
          source: "Census citation",
          metadata: {
            ledger_geography_level: "country",
            ledger_geography_id: "0100000US",
            ledger_geography_name: "United States",
          },
          target: 100,
          initial_estimate: 90,
          final_estimate: 100,
        },
      ],
    },
    "us-legacy-geography",
  );

  expect(cal.rows[0]).toMatchObject({
    geography: "United States",
    level: "national",
  });
});

test("structured UK geography preserves producer identity and ordering", () => {
  const geographyValues = {
    K02000001: "United Kingdom",
    K03000001: "Great Britain",
    E92000001: "England",
    S92000003: "Scotland",
  };
  const order = ["K02000001", "K03000001", "E92000001", "S92000003"];
  const targets = order.map((geographyId) => ({
    name: `ons.population.${geographyId}@2025`,
    target_name: `ons.population.${geographyId}`,
    source: { id: "ons", citation: "ONS population table" },
    variable: { id: "population", measure: "count" },
    dimensions: { geography_country: geographyId },
    target: 100,
    initial_estimate: 90,
    final_estimate: 100,
  }));
  const cal = buildCalibration(
    {
      schema_version: 7,
      dimensions: {
        geography_country: {
          label: "Geography",
          role: "geography",
          level: "country",
          values: geographyValues,
          order,
        },
      },
      targets,
    },
    "uk-structured-geography",
    null,
    {},
    {},
    {},
    "uk",
  );
  const page = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?variable=ons%20%2F%20population%20%C2%B7%20count",
    cal,
  );

  expect(cal.rows[3]).toMatchObject({
    geography: "Scotland",
    geography_id: "S92000003",
    geography_dimension_id: "geography_country",
    geography_rank: 3,
    level: "country",
  });
  expect(page.dimensions[0]).toEqual({
    key: "geography",
    label: "Geography",
    values: ["United Kingdom", "Great Britain", "England", "Scotland"],
  });
});

test("structured geography falls back to deterministic sorting when order is incomplete", () => {
  const cal = buildCalibration(
    {
      schema_version: 7,
      dimensions: {
        geography_country: {
          label: "Geography",
          role: "geography",
          level: "country",
          values: { K02000001: "United Kingdom", E92000001: "England" },
          order: ["K02000001"],
        },
      },
      targets: ["K02000001", "E92000001"].map((geographyId) => ({
        name: `ons.population.${geographyId}@2025`,
        source: { id: "ons" },
        variable: { id: "population" },
        dimensions: { geography_country: geographyId },
        target: 100,
        initial_estimate: 90,
        final_estimate: 100,
      })),
    },
    "uk-structured-geography-incomplete-order",
    null,
    {},
    {},
    {},
    "uk",
  );
  const page = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?variable=ons%20%2F%20population",
    cal,
  );

  expect(page.dimensions[0]?.values).toEqual(["England", "United Kingdom"]);
});

test("structured targets reject multiple populated geography dimensions", () => {
  expect(() =>
    buildCalibration(
      {
        schema_version: 7,
        dimensions: {
          geography_country: {
            label: "Country",
            role: "geography",
            level: "country",
          },
          geography_region: {
            label: "Region",
            role: "geography",
            level: "region",
          },
        },
        targets: [
          {
            name: "ons.population@2025",
            source: { id: "ons" },
            variable: { id: "population" },
            dimensions: {
              geography_country: "E92000001",
              geography_region: "north_east",
            },
            target: 100,
            initial_estimate: 90,
            final_estimate: 100,
          },
        ],
      },
      "uk-ambiguous-structured-geography",
      null,
      {},
      {},
      {},
      "uk",
    )
  ).toThrow("at most one populated geography-role dimension");
});

test("structured facet ordering falls back when any displayed value lacks a rank", () => {
  const target = (suffix: string, category: string) => ({
    name: `fixture.population.${suffix}@2026`,
    source: { id: "novastat_agency", citation: "Citation" },
    variable: { id: "population", measure: "count" },
    metadata: {
      chronicle_record_ids: ["novastat_agency.population.total"],
      variable: "population",
      source_measure_id: "population_count",
    },
    dimensions: { category },
    target: 1,
    initial_estimate: 1,
    final_estimate: 1,
  });
  const cal = buildCalibration(
    {
      schema_version: 7,
      dimensions: {
        category: {
          label: "Category",
          values: { zeta: "Zeta" },
          order: ["zeta"],
        },
      },
      targets: [target("zeta", "zeta"), target("beta", "beta")],
    },
    "partial-dimension-order",
  );
  const page = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?variable=novastat_agency%20%2F%20population%20%C2%B7%20count",
    cal,
  );

  expect(page.dimensions).toEqual([
    { key: "bd_category", label: "Category", values: ["Beta", "Zeta"] },
  ]);
});

test("schema 7 rejects a mixture of structured and legacy rows", () => {
  const base = {
    source: "ZZ official population table",
    metadata: {
      chronicle_record_ids: ["novastat_agency.population.total"],
      variable: "population",
      source_measure_id: "population_count",
    },
    target: 100,
    initial_estimate: 90,
    final_estimate: 100,
  };
  expect(() => buildCalibration(
    {
      schema_version: 7,
      dimensions: {
        region: {
          label: "Region",
          role: "geography",
          values: { north: "North", south: "South" },
        },
        sex: { label: "Sex" },
        age_band: { label: "Age band" },
      },
      targets: [
        {
          ...base,
          name: "fixture_population_north_female_0_17@2026",
          source: { id: "novastat_agency", citation: "ZZ official population table" },
          variable: { id: "population", measure: "count" },
          filter: "cell_south_male_65_plus",
          dimensions: { region: "north", sex: "female", age_band: "0_17" },
        },
        {
          ...base,
          name: "fixture_population_south_male_18_64@2026",
          filter: "cell_south_male_18_64",
        },
        {
          ...base,
          name: "novastat_agency.population.total@2026",
          filter: null,
        },
      ],
    },
    "mixed-dimension-adapters",
  )).toThrow("Schema 7 target source.id");
});

test("structured rows do not require a dimensions dictionary, including empty objects", () => {
  const base = {
    source: { id: "novastat_agency", citation: "Citation" },
    variable: { id: "population", measure: "count" },
    metadata: { variable: "population", source_measure_id: "population_count" },
    filter: "cell_south_male_18_64",
    target: 1,
    initial_estimate: 1,
    final_estimate: 1,
  };
  const cal = buildCalibration(
    {
      schema_version: 7,
      targets: [
        {
          ...base,
          name: "fixture_population_age@2026",
          dimensions: { age_band: "65_plus" },
        },
        {
          ...base,
          name: "fixture_population_total@2026",
          dimensions: {},
        },
      ],
    },
    "dictionary-free-dimensions",
  );

  expect(cal.target_schema.structured_dimensions).toBe(false);
  expect(cal.rows[0]).toMatchObject({
    dimension_adapter: "structured",
    geography: "United States",
    breakdown: "65+",
    target_dimensions: [
      expect.objectContaining({
        key: "bd_age_band",
        label: "Age Band",
        value: "65+",
      }),
    ],
  });
  expect(cal.rows[1]).toMatchObject({
    dimension_adapter: "structured",
    geography: "United States",
    breakdown: "",
    target_dimensions: [],
  });
});

test("structured dimensions prevent whole-population estimate-scope warnings", () => {
  const target = (recordSet: string, category: string, targetValue: number) => ({
    name: `source.example.${category}.amount@2026`,
    source: { id: "source", citation: "Citation" },
    variable: { id: "example", measure: "amount" },
    metadata: {
      variable: "example",
      source_measure_id: "example_amount",
      ledger_layout_record_set_id: recordSet,
    },
    dimensions: { category },
    target: targetValue,
    initial_estimate: 100,
    final_estimate: 80,
  });
  const cal = buildCalibration(
    {
      schema_version: 7,
      dimensions: { category: { label: "Category" } },
      targets: [
        target("source.example.slice_a", "a", 10),
        target("source.example.slice_b", "b", 20),
      ],
    },
    "structured-scope",
  );

  expect(cal.rows.every((row) => row.estimate_warning == null)).toBe(true);
});

test("keeps legacy US dotted target families when Chronicle publisher metadata is present", () => {
  const cal = buildCalibration(
    {
      schema_version: 2,
      targets: [
        {
          name: "irs.population.total@2024",
          target_name: "irs.population.total",
          period: 2024,
          entity: "person",
          measure: { kind: "column", name: "person" },
          source: "IRS SOI",
          metadata: {
            chronicle_record_ids: ["irs.population.cy2024.total"],
            variable: "population",
          },
          target: 100,
          initial_estimate: 95,
          final_estimate: 99,
          relative_error: -0.01,
          within_tolerance: true,
        },
      ],
    },
    "us-dotted-family",
  );

  expect(cal.rows[0]).toMatchObject({
    source: "irs",
    variable: "population",
    family: "irs.population.total",
  });
});

test("live-US-shaped schema 5 rows preserve the legacy dotted contract", () => {
  const sourceCitation =
    "Bureau of Economic Analysis, National Income and Product Accounts, Table 1.12";
  const cal = buildCalibration(
    {
      schema_version: 5,
      targets: [
        {
          name: "bea_nipa.cy2023.proprietors_income.a041rc.amount@2024",
          filter: null,
          source: sourceCitation,
          metadata: {
            chronicle_record_ids: [
              "bea_nipa.cy2023.proprietors_income.a041rc.amount",
            ],
            variable: "proprietors_income",
            source_measure_id: "proprietors_income_amount",
            ledger_geography_level: "country",
            ledger_geography_id: "0100000US",
            ledger_layout_groupby_dimension: "bea_nipa.line_code",
            ledger_layout_groupby_value_id: "a041rc",
            ledger_measure_unit: "usd",
          },
          target: 100,
          initial_estimate: 90,
          final_estimate: 99,
          relative_error: -0.01,
          within_tolerance: true,
        },
      ],
    },
    "live-us-shaped",
  );
  const responseRow = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics",
    cal,
  ).targets[0];

  // JSON round-tripping matches the API boundary and locks every legacy field;
  // the additional contract fields are strictly additive.
  expect(JSON.parse(JSON.stringify(responseRow))).toEqual({
    name: "bea_nipa.cy2023.proprietors_income.a041rc.amount@2024",
    target: 100,
    initial_estimate: 90,
    final_estimate: 99,
    relative_error: -0.01,
    within_tolerance: true,
    base_name: "bea_nipa.cy2023.proprietors_income.a041rc.amount",
    family: "bea_nipa.cy2023.proprietors_income.a041rc.amount",
    state: null,
    geography: "United States",
    level: "national",
    source: "bea_nipa",
    source_label: "BEA Nipa",
    variable: "proprietors income",
    variable_label: null,
    measure: "total",
    target_role: null,
    source_measure_id: "proprietors_income_amount",
    policyengine_variables: [],
    policyengine_map_to: null,
    policyengine_filter_variable: null,
    materializer: null,
    measure_mode: null,
    error_kind: "relative",
    initial_error: -0.1,
    final_error: -0.01,
    initial_miss: -10,
    final_miss: -1,
    abs_final_miss: 1,
    absolute_improvement: 9,
    abs_error: 0.01,
    breakdown: "a041rc",
    dims: ["a041rc"],
    target_dimensions: [
      {
        key: "bd_line_code",
        label: "Line Code",
        value: "a041rc",
        source_key: "ledger_layout_groupby_value_id",
        raw_value: "a041rc",
      },
    ],
    dimension_adapter: "legacy_name",
    target_representation: "legacy",
    variable_key: "bea_nipa / proprietors income · total",
    source_citation: sourceCitation,
    source_url: null,
    entity: null,
    aggregation: null,
    measure_name: null,
    period: null,
    chronicle: {
      fact_key: null,
      source_record_id: null,
      semantic_fact_key: null,
      aggregate_fact_key: null,
      legacy_fact_key: null,
      period_type: null,
      source_period: null,
      target_period: null,
      geography_level: "country",
      geography_id: "0100000US",
      geography_vintage: null,
      domain: null,
      entity_name: null,
      entity_role: null,
      measure_concept: null,
      source_concept: null,
      concept_relation: null,
      concept_authority: null,
      measure_unit: "usd",
      value_operation: null,
      layout_record_set_id: null,
      layout_groupby_dimension: "bea_nipa.line_code",
      layout_groupby_value_id: "a041rc",
      layout_measure_id: null,
      dimension_set_key: null,
      universe_constraint_set_key: null,
      universe_constraint_count: null,
      filters: [],
    },
    calibration_status: "included",
    calibration_status_label: "Included",
    calibration_status_reason: null,
    initial_relative_error: -0.1,
    abs_relative_error: 0.01,
    improvement: 0.09000000000000001,
    direction: "under",
  });
});

// A v2-shaped target: AGI bracket × return type × filing status, with @period.
function agiTarget(band: string, ret: string, filing: string, rel: number) {
  return {
    name: `nation/irs/adjusted gross income/total/${band}/${ret}/${filing}@2024`,
    target_name: `nation/irs/adjusted gross income/total/${band}/${ret}/${filing}`,
    period: 2024,
    entity: "household",
    aggregation: "sum",
    measure: { kind: "column", name: "adjusted_gross_income" },
    source: "IRS SOI Table 1.1",
    target: 100,
    initial_estimate: 140,
    final_estimate: 100 * (1 + rel),
    relative_error: rel,
    within_tolerance: Math.abs(rel) <= 0.1,
  };
}

function calibration(targets: object[], releaseId = "rel-a"): Calibration {
  return buildCalibration(
    {
      schema_version: 2,
      targets,
      final_loss: 0.02,
      fraction_within_10pct: 0.9,
    },
    releaseId,
  );
}

const SAMPLE = calibration([
  agiTarget("AGI in 200k-500k", "taxable", "All", -0.086),
  agiTarget("AGI in 200k-500k", "taxable", "Married Filing Jointly", -0.102),
  agiTarget("AGI in 30k-40k", "taxable", "All", 0.236),
  { name: "US06/snap-cost", target: 100, initial_estimate: 90, final_estimate: 99, relative_error: -0.01, within_tolerance: true },
]);

function page(url: string, cal = SAMPLE) {
  return latestMicrocosmTargetDiagnosticsPage(`http://x/api/microcosm/target-diagnostics${url}`, cal);
}

test("v2 metadata and parsing survive enrichment", () => {
  const row = SAMPLE.rows[0];
  expect(row.variable_key).toBe("irs / adjusted gross income · total");
  expect(row.variable).toBe("adjusted gross income"); // @2024 stripped via target_name
  expect(row.geography).toBe("United States");
  expect(row.entity).toBe("household");
  expect(row.aggregation).toBe("sum");
  expect(row.period).toBe(2024);
  expect(row.source_citation).toBe("IRS SOI Table 1.1");
});

test("legacy Ledger metadata populates the complete Chronicle contract", () => {
  const cal = calibration([
    {
      name: "irs_soi.ty2022.historic_table_2.state_agi.ak.1_to_10k.taxable_interest_amount@2024",
      target_name: "irs_soi.ty2022.historic_table_2.state_agi.ak.1_to_10k.taxable_interest_amount",
      target: 100,
      initial_estimate: 90,
      final_estimate: 99,
      registry: { family: "irs_soi" },
      metadata: {
        ledger_fact_key: "fact-key",
        ledger_source_record_id: "source-record",
        ledger_semantic_fact_key: "semantic-key",
        ledger_aggregate_fact_key: "aggregate-key",
        ledger_legacy_fact_key: "legacy-key",
        ledger_period_type: "tax_year",
        source_period: "2022",
        target_period: "2024",
        ledger_geography_level: "state",
        ledger_geography_id: "0400000US02",
        ledger_geography_vintage: "2020_census",
        ledger_domain: "all_returns",
        ledger_entity_name: "tax_unit",
        ledger_entity_role: "filing_unit",
        ledger_measure_concept: "irs_soi.taxable_interest",
        ledger_source_concept: "irs_soi.taxable_interest",
        ledger_concept_relation: "identical",
        ledger_concept_authority: "IRS",
        ledger_measure_unit: "usd",
        ledger_value_operation: "identity",
        ledger_layout_record_set_id: "irs_soi.ty2022.historic_table_2.state_agi.ak",
        ledger_layout_groupby_dimension: "us:statutes/26/62#adjusted_gross_income",
        ledger_layout_groupby_value_id: "1_to_10k",
        ledger_layout_measure_id: "taxable_interest_amount",
        ledger_dimension_set_key: "dimension-set",
        ledger_universe_constraint_set_key: "universe-set",
        // microcosm writes the count as a string (ledger_targets.py:5913) and
        // TargetSpec turns every metadata value into one (registry.py:149-150).
        ledger_universe_constraint_count: "2",
        ledger_filter_income_range: "1_to_10k",
        ledger_filter_filing_status: "all",
        source_measure_id: "taxable_interest_amount",
        variable: "taxable_interest_income",
        filing_status: "All",
      },
    },
  ]);
  const row = cal.rows[0];

  expect(row.geography).toBe("AK");
  expect(row.level).toBe("state");
  expect(row.state).toBe("AK");
  expect(row.measure).toBe("total");
  expect(row.target_dimensions).toEqual([
    expect.objectContaining({ label: "Income band", value: "1 to 10k" }),
    expect.objectContaining({ label: "Filing status", value: "All" }),
  ]);
  expect(row.chronicle).toMatchObject({
    fact_key: "fact-key",
    source_record_id: "source-record",
    semantic_fact_key: "semantic-key",
    aggregate_fact_key: "aggregate-key",
    legacy_fact_key: "legacy-key",
    period_type: "tax_year",
    source_period: "2022",
    target_period: "2024",
    geography_level: "state",
    geography_id: "0400000US02",
    geography_vintage: "2020_census",
    domain: "all_returns",
    entity_name: "tax_unit",
    entity_role: "filing_unit",
    measure_concept: "irs_soi.taxable_interest",
    source_concept: "irs_soi.taxable_interest",
    concept_relation: "identical",
    concept_authority: "IRS",
    measure_unit: "usd",
    value_operation: "identity",
    layout_record_set_id: "irs_soi.ty2022.historic_table_2.state_agi.ak",
    layout_groupby_dimension: "us:statutes/26/62#adjusted_gross_income",
    layout_groupby_value_id: "1_to_10k",
    layout_measure_id: "taxable_interest_amount",
    dimension_set_key: "dimension-set",
    universe_constraint_set_key: "universe-set",
    universe_constraint_count: 2,
    filters: expect.arrayContaining([
      expect.objectContaining({ label: "Income band", value: "1 to 10k" }),
      expect.objectContaining({ label: "Filing status", value: "All" }),
    ]),
  });
});

// A universe constraint count is a whole, non-negative number of constraint
// rows (ledger_targets.py:5911-5913 at 0ff293b); any other string reads null.
test("a universe constraint count that is not a whole count reads null", () => {
  const counts = ["2.5", "-1", " ", ""];
  const cal = calibration(
    counts.map((count, index) => ({
      name: `irs_soi.ty2022.historic_table_2.state_agi.ak.count_${index}.taxable_interest_amount@2024`,
      target_name: `irs_soi.ty2022.historic_table_2.state_agi.ak.count_${index}.taxable_interest_amount`,
      target: 100,
      initial_estimate: 90,
      final_estimate: 99,
      metadata: {
        ledger_source_record_id: `source-record-${index}`,
        ledger_geography_id: "0400000US02",
        ledger_universe_constraint_set_key: "universe-set",
        ledger_universe_constraint_count: count,
      },
    })),
  );
  expect(
    cal.rows.map((row) => {
      const chronicle = row.chronicle as Record<string, unknown>;
      return [chronicle.source_record_id, chronicle.universe_constraint_count];
    }),
  ).toEqual(counts.map((_, index) => [`source-record-${index}`, null]));
});

test("metadata-free dotted state targets retain geography and measure", () => {
  const cal = calibration([
    {
      name: "irs_soi.ty2022.historic_table_2.state_broad.me.all.net_capital_gains_amount",
      target: 100,
      initial_estimate: 90,
      final_estimate: 99,
    },
    {
      name: "irs_soi.ty2022.historic_table_2.state_broad.me.all.net_capital_gains_returns",
      target: 100,
      initial_estimate: 90,
      final_estimate: 99,
    },
  ]);

  expect(cal.rows.map((row) => ({
    geography: row.geography,
    level: row.level,
    state: row.state,
    measure: row.measure,
    variable_key: row.variable_key,
  }))).toEqual([
    {
      geography: "ME",
      level: "state",
      state: "ME",
      measure: "total",
      variable_key: "irs_soi / net capital gains · total",
    },
    {
      geography: "ME",
      level: "state",
      state: "ME",
      measure: "count",
      variable_key: "irs_soi / net capital gains · count",
    },
  ]);
});

test("legacy geography and income metadata drive the explorer hierarchy", () => {
  const legacyTarget = (state: "ak" | "ca", geoId: string, band: string) => ({
    name: `irs_soi.ty2022.historic_table_2.state_agi.${state}.${band}.taxable_interest_amount@2024`,
    target: 100,
    initial_estimate: 100,
    final_estimate: 100,
    registry: { family: "irs_soi" },
    metadata: {
      ledger_geography_level: "state",
      ledger_geography_id: geoId,
      ledger_layout_groupby_dimension: "us:statutes/26/62#adjusted_gross_income",
      ledger_layout_groupby_value_id: band,
      ledger_filter_income_range: band,
      ledger_filter_filing_status: "all",
      source_measure_id: "taxable_interest_amount",
      variable: "taxable_interest_income",
      filing_status: "All",
    },
  });
  const cal = calibration([
    legacyTarget("ak", "0400000US02", "1_to_10k"),
    legacyTarget("ak", "0400000US02", "10k_to_25k"),
    legacyTarget("ca", "0400000US06", "1_to_10k"),
  ]);
  const filters = {
    geographyLevels: [],
    geographies: [],
    fitBands: [],
    comparisonFits: [],
    calibrationStatuses: [],
  };
  const program = buildCalibrationTree(cal.rows, {
    breakdown: "program",
    path: {
      source: "irs_soi",
      program: "taxable interest income",
      dimensions: [],
    },
    filters,
  });

  expect(program.currentLevel).toEqual({ kind: "geography", label: "Geography" });
  expect(program.groups[0].nodes.map((node) => node.id)).toEqual(["AK", "CA"]);

  const alaska = buildCalibrationTree(cal.rows, {
    breakdown: "program",
    path: {
      source: "irs_soi",
      program: "taxable interest income",
      geography: "AK",
      dimensions: [],
    },
    filters,
  });
  expect(alaska.currentLevel).toEqual({
    kind: "dimension",
    key: "bd_income_band",
    label: "Income band",
  });
  expect(alaska.groups[0].nodes.map((node) => node.label)).toEqual([
    "1 to 10k",
    "10k to 25k",
  ]);
});

test("FIPS admin target collapses to a measure family", () => {
  const snap = SAMPLE.rows.find((r) => r.variable === "snap-cost")!;
  expect(snap.geography).toBe("CA");
  expect(snap.source).toBe("admin");
  expect(snap.family).toBe("snap-cost");
});

test("variable filter isolates a variable's breakdowns", () => {
  const result = page("?variable=irs%20%2F%20adjusted%20gross%20income%20%C2%B7%20total");
  expect(result.filtered_total).toBe(3);
  for (const row of result.targets) expect(row.variable_key).toBe("irs / adjusted gross income · total");
});

test("program filter includes both count and amount targets", () => {
  const cal = calibration([
    {
      name: "irs_soi.ty2022.historic_table_2.us.all.taxable_interest_amount@2024",
      target_name: "irs_soi.ty2022.historic_table_2.us.all.taxable_interest_amount",
      target: 100,
      initial_estimate: 100,
      final_estimate: 90,
      relative_error: -0.1,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "taxable_interest_income",
        source_measure_id: "taxable_interest_amount",
        ledger_geography_level: "country",
        ledger_geography_id: "0100000US",
        ledger_measure_unit: "usd",
        ledger_layout_groupby_value_id: "all",
      },
    },
    {
      name: "irs_soi.ty2022.historic_table_2.us.all.taxable_interest_returns@2024",
      target_name: "irs_soi.ty2022.historic_table_2.us.all.taxable_interest_returns",
      target: 100,
      initial_estimate: 100,
      final_estimate: 95,
      relative_error: -0.05,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "taxable_interest_income",
        source_measure_id: "taxable_interest_returns",
        ledger_geography_level: "country",
        ledger_geography_id: "0100000US",
        ledger_measure_unit: "count",
        ledger_layout_groupby_value_id: "all",
      },
    },
    {
      name: "irs_soi.ty2022.historic_table_2.us.all.eitc_returns@2024",
      target_name: "irs_soi.ty2022.historic_table_2.us.all.eitc_returns",
      target: 100,
      initial_estimate: 100,
      final_estimate: 100,
      relative_error: 0,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "eitc",
        source_measure_id: "eitc_returns",
        ledger_geography_level: "country",
        ledger_geography_id: "0100000US",
        ledger_measure_unit: "count",
        ledger_layout_groupby_value_id: "all",
      },
    },
  ]);
  const result = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?program=irs_soi%20%2F%20taxable%20interest%20income",
    cal,
  );
  expect(result.filtered_total).toBe(2);
  expect(result.targets.map((row) => row.measure).sort()).toEqual(["count", "total"]);

  const countOnly = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?program=irs_soi%20%2F%20taxable%20interest%20income&measure=count",
    cal,
  );
  expect(countOnly.filtered_total).toBe(1);
  expect(countOnly.targets[0].measure).toBe("count");
  expect(countOnly.filters?.measure).toBe("count");
});

test("targets without geography metadata default to the national geography", () => {
  const cal = calibration([
    {
      name: "selection_mass_protection.keogh_distributions@2024",
      target_name: "selection_mass_protection.keogh_distributions",
      target: 100,
      initial_estimate: 100,
      final_estimate: 100,
      relative_error: 0,
      registry: { family: "unspecified" },
      metadata: {
        variable: "keogh_distributions",
        source_measure_id: "keogh_distributions",
      },
    },
  ]);
  expect(cal.rows[0].geography).toBe("United States");
  expect(cal.rows[0].level).toBe("national");

  const result = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?level=national&geography=United%20States",
    cal,
  );
  expect(result.filtered_total).toBe(1);
  expect(result.targets[0].name).toBe("selection_mass_protection.keogh_distributions@2024");
});

test("dimensions are the axes that vary; constants drop", () => {
  const result = page("?variable=irs%20%2F%20adjusted%20gross%20income%20%C2%B7%20total");
  const labels = result.dimensions.map((d) => d.label);
  expect(labels).toContain("Income band");
  expect(labels).toContain("Filing status");
  expect(labels).not.toContain("Return type"); // all "taxable" -> constant
});

test("income band facets sort total first, then descending numeric bands", () => {
  const rows = ["total", "under_1", "1_to_10k", "50k_plus", "500k_to_1m", "1m_plus"]
    .map((band) => ({
      name: `irs_soi.ty2022.historic_table_2.us.${band}.adjusted_gross_income@2024`,
      target_name: `irs_soi.ty2022.historic_table_2.us.${band}.adjusted_gross_income`,
      target: 100,
      initial_estimate: 100,
      final_estimate: 100,
      relative_error: 0,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "adjusted_gross_income",
        source_measure_id: "adjusted_gross_income",
        ledger_geography_level: "country",
        ledger_geography_id: "0100000US",
        ledger_measure_unit: "usd",
        ledger_layout_groupby_dimension: "us:statutes/26/62#adjusted_gross_income",
        ledger_layout_groupby_value_id: band,
        ledger_filter_income_range: band === "total" ? "all" : band,
        filing_status: "All",
      },
    }));
  const cal = calibration(rows);
  const result = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?variable=irs_soi%20%2F%20adjusted%20gross%20income%20%C2%B7%20total",
    cal,
  );
  const incomeBand = result.dimensions.find((dim) => dim.label === "Income band");

  expect(incomeBand?.values).toEqual([
    "Total",
    "1m plus",
    "500k to 1m",
    "50k plus",
    "1 to 10k",
    "under 1",
  ]);
});

test("facet filter narrows to a single breakdown", () => {
  const income = page("?variable=irs%20%2F%20adjusted%20gross%20income%20%C2%B7%20total").dimensions.find(
    (d) => d.label === "Income band",
  )!;
  const result = page(
    `?variable=irs%20%2F%20adjusted%20gross%20income%20%C2%B7%20total&facet=${income.key}:AGI in 30k-40k`,
  );
  expect(result.filtered_total).toBe(1);
  expect(result.targets[0].dims).toContain("AGI in 30k-40k");
});

test("calibration summary reports per-family fit", () => {
  const summary = latestMicrocosmCalibrationSummary(SAMPLE);
  expect(summary.total_targets).toBe(4);
  expect(summary.included_target_count).toBe(4);
  expect(summary.family_fit.length).toBeGreaterThan(0);
});

test("release highlights split bounded percent fit from absolute miss magnitude", () => {
  const cal = calibration([
    {
      name: "source.us.total.relative-bounded@2024",
      target_name: "source.us.total.relative-bounded",
      target: 100,
      initial_estimate: 1000,
      final_estimate: 900,
      relative_error: 8,
      within_tolerance: false,
    },
    {
      name: "source.us.total.relative-extreme@2024",
      target_name: "source.us.total.relative-extreme",
      target: 1,
      initial_estimate: 120,
      final_estimate: 100,
      relative_error: 99,
      within_tolerance: false,
    },
    {
      name: "source.us.total.absolute-large@2024",
      target_name: "source.us.total.absolute-large",
      target: 1_000_000_000,
      initial_estimate: 1_600_000_000,
      final_estimate: 1_400_000_000,
      relative_error: 0.4,
      within_tolerance: false,
    },
  ]);
  const highlights = latestMicrocosmCalibrationHighlights(cal, 10);

  expect(highlights.extreme_relative_outlier_count).toBe(1);
  expect(highlights.worst_bounded_relative_fit.map((row) => row.base_name)).toContain(
    "source.us.total.relative-bounded",
  );
  expect(highlights.worst_bounded_relative_fit.map((row) => row.base_name)).not.toContain(
    "source.us.total.relative-extreme",
  );
  expect(highlights.extreme_relative_outliers[0].base_name).toBe(
    "source.us.total.relative-extreme",
  );
  expect(highlights.largest_absolute_misses[0].base_name).toBe(
    "source.us.total.absolute-large",
  );
  expect(highlights.largest_absolute_misses[0].abs_final_miss).toBe(400_000_000);
  expect(highlights.biggest_absolute_improvements[0].absolute_improvement).toBe(200_000_000);
});

test("calibration inclusion status uses skipped and dropped metadata", () => {
  const cal = buildCalibration(
    {
      schema_version: 2,
      targets: [
        { name: "included@2024", target: 1, initial_estimate: 1, final_estimate: 1 },
        { name: "skipped@2024", target_name: "skipped", target: 1 },
        { name: "dropped@2024", target_name: "dropped", target: 1 },
      ],
      skipped: [{ name: "skipped", reason: "No support." }],
    },
    "rel-a",
    null,
    {
      gates: {
        target_compilation: {
          declared_targets: 3,
          compiled_candidate_targets: 2,
          dropped_target_names: ["dropped"],
        },
      },
    },
  );

  expect(cal.included_target_count).toBe(1);
  expect(cal.dropped_target_names).toEqual(["dropped"]);
  expect(cal.rows.map((row) => row.calibration_status)).toEqual([
    "included",
    "skipped",
    "not_materialized",
  ]);
  const page = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics",
    cal,
  );
  expect(page.summary.included_target_count).toBe(1);
  expect(page.summary.skipped_target_count).toBe(1);
  expect(page.summary.dropped_target_count).toBe(1);
  expect(page.targets[1].calibration_status_reason).toBe("No support.");
});

test("healthcare scope includes ACA, Medicaid, Medicare, and PTC targets", () => {
  const cal = calibration([
    {
      name: "cms_aca.oep2024.state_marketplace.ca.aptc_recipients@2024",
      target_name: "cms_aca.oep2024.state_marketplace.ca.aptc_recipients",
      target: 100,
      initial_estimate: 50,
      final_estimate: 80,
      relative_error: -0.2,
      registry: { family: "cms_aca" },
      metadata: {
        target_role: "aca_ptc_recipients",
        base_variable: "assigned_aca_ptc",
        count_map_to: "person",
        count_filter_variable: "is_aca_ptc_eligible",
        measure_mode: "positive_count",
        materializer: "policyengine_variable",
        source_measure_id: "aptc_recipients",
        ledger_geography_level: "state",
        ledger_geography_id: "0400000US06",
      },
    },
    {
      name: "US06/cms_medicaid/total_medicaid_enrollment@2024",
      target: 100,
      initial_estimate: 90,
      final_estimate: 95,
      relative_error: -0.05,
      metadata: {
        target_role: "medicaid_enrollment",
        base_variable: "medicaid_enrolled",
        measure_mode: "positive_count",
      },
    },
    {
      name: "nation/cms_medicare/part_b_premium_income@2024",
      target: 100,
      initial_estimate: 100,
      final_estimate: 101,
      relative_error: 0.01,
      metadata: {
        target_role: "medicare_part_b_premium_total",
        base_variable: "gross_medicare_part_b_premium",
        measure_mode: "sum",
      },
    },
    {
      name: "irs_soi.ty2022.historic_table_2.us.all.premium_tax_credit_amount@2024",
      target_name: "irs_soi.ty2022.historic_table_2.us.all.premium_tax_credit_amount",
      target: 100,
      initial_estimate: 60,
      final_estimate: 70,
      relative_error: -0.3,
      registry: { family: "irs_soi" },
      metadata: {
        target_role: "aca_spending",
        base_variable: "assigned_aca_ptc",
        measure_mode: "sum",
        source_measure_id: "premium_tax_credit_amount",
        ledger_geography_level: "country",
        ledger_geography_id: "0100000US",
      },
    },
    agiTarget("AGI in 30k-40k", "taxable", "All", 0.01),
  ]);

  const result = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?scope=healthcare",
    cal,
  );

  expect(result.total_targets).toBe(4);
  expect(result.filtered_total).toBe(4);
  expect(result.scope_counts).toEqual({ healthcare: 4 });
  expect(
    latestMicrocosmTargetDiagnosticsPage("http://x/api/microcosm/target-diagnostics", cal)
      .scope_counts,
  ).toEqual({ healthcare: 4 });
  expect(result.summary.fraction_within_10pct).toBe(0.5);
  expect(result.targets.map((row) => row.name)).not.toContain(
    "nation/irs/adjusted gross income/total/AGI in 30k-40k/taxable/All@2024",
  );
  const aptc = result.targets.find((row) => row.source_measure_id === "aptc_recipients");
  expect(aptc?.policyengine_variables).toEqual(["assigned_aca_ptc"]);
  expect(aptc?.policyengine_map_to).toBe("person");
  expect(aptc?.policyengine_filter_variable).toBe("is_aca_ptc_eligible");
  expect(aptc?.measure_mode).toBe("positive_count");
  const ptcAmount = result.targets.find(
    (row) => row.source_measure_id === "premium_tax_credit_amount",
  );
  expect(ptcAmount?.policyengine_variables).toEqual(["assigned_aca_ptc"]);
  expect(result.variables.map((row) => row.source)).toContain("cms_aca");
  expect(result.variables.map((row) => row.source)).toContain("irs_soi");
  expect(result.variables.find((row) => row.source === "cms_aca")?.policyengine_variables).toEqual([
    "assigned_aca_ptc",
  ]);
});

test("mapped measure concepts fill policyengine_variables when releases omit base_variables", () => {
  const target = (name: string, metadata: object) => ({
    name: `${name}@2024`,
    target_name: name,
    target: 100,
    initial_estimate: 90,
    final_estimate: 95,
    relative_error: -0.05,
    metadata,
  });
  const cal = calibration([
    target("census_stc.fy2023.individual_income_tax_collections.dc.t40.collections", {
      target_role: "state_income_tax",
      source_measure_id: "collections",
      ledger_measure_concept: "census_stc.individual_income_tax_collections",
    }),
    target("irs_soi.ty2022.table_1_4.us.all.returns.count", {
      target_role: "soi_fiscal_distribution",
      ledger_measure_concept: "irs_soi.individual_income_tax_returns",
    }),
    target("irs_soi.ty2022.table_1_4.us.all.returns_with_base.count", {
      target_role: "soi_fiscal_distribution",
      base_variables: "adjusted_gross_income",
      ledger_measure_concept: "irs_soi.individual_income_tax_returns",
    }),
    target("census_pep.v2024.resident_population.dc.age_0_4.population", {
      target_role: "population_age",
      ledger_measure_concept: "census_pep.resident_population",
    }),
    target("jct.fy2024.tax_expenditures.us.mortgage_interest.revenue_loss", {
      target_role: "jct_tax_expenditure",
      ledger_measure_concept: "jct.individual_tax_expenditure_revenue_loss",
    }),
  ]);

  const rows = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics",
    cal,
  ).targets;
  const variablesFor = (prefix: string) =>
    rows.find((row) => String(row.name).startsWith(prefix))?.policyengine_variables;

  expect(variablesFor("census_stc.")).toEqual(["state_income_tax"]);
  expect(variablesFor("irs_soi.ty2022.table_1_4.us.all.returns.")).toEqual(["tax_unit_is_filer"]);
  // Published base_variables still win over the mapping fallback.
  expect(variablesFor("irs_soi.ty2022.table_1_4.us.all.returns_with_base.")).toEqual([
    "adjusted_gross_income",
  ]);
  // No model variable measures these targets.
  expect(variablesFor("census_pep.")).toEqual([]);
  expect(variablesFor("jct.")).toEqual([]);
  expect(rows.find((row) => String(row.name).startsWith("census_stc."))?.policyengine_map_to).toBeNull();
});

test("comparison matches on base_name across the @period boundary", () => {
  // B drops one target, changes one fit, adds a new one.
  const b = calibration(
    [
      agiTarget("AGI in 200k-500k", "taxable", "All", -0.02), // improved (|−0.02| < |−0.086|)
      agiTarget("AGI in 30k-40k", "taxable", "All", 0.30), // regressed
      { name: "nation/cbo/individual_income_tax@2024", target_name: "nation/cbo/individual_income_tax", target: 1, initial_estimate: 1, final_estimate: 1, relative_error: 0, within_tolerance: true },
    ],
    "rel-b",
  );
  const cmp = buildComparison(SAMPLE, b);
  expect(cmp.summary.common).toBe(2);
  expect(cmp.summary.improved).toBe(1);
  expect(cmp.summary.regressed).toBe(1);
  expect(cmp.summary.added).toBe(1); // cbo income tax, only in B
  expect(cmp.summary.removed).toBe(2); // the MFJ AGI row and snap-cost, only in A
  expect(cmp.summary.losses_comparable).toBe(false);
  expect(cmp.variables[0].variable_key).toBe("irs / adjusted gross income · total");
  expect(cmp.variables[0].common_targets).toBe(2);
  expect(cmp.variables[0].relative_targets).toBe(2);
  expect(cmp.variables[0].improved).toBe(1);
  expect(cmp.variables[0].regressed).toBe(1);
  expect(Array.isArray(cmp.rows[0].target_dimensions)).toBe(true);
});

test("comparison exposes each release's weighted target-error aggregate", () => {
  const current = {
    ...SAMPLE,
    final_loss: 0.91,
    target_loss_attribution: {
      ...SAMPLE.target_loss_attribution,
      status: "reported" as const,
      aggregate: 0.123,
    },
  };
  const candidate = {
    ...SAMPLE,
    release_id: "weighted-candidate",
    final_loss: 0.82,
    target_loss_attribution: {
      ...SAMPLE.target_loss_attribution,
      status: "reported" as const,
      aggregate: 0.087,
    },
  };

  const cmp = buildComparison(current, candidate);

  expect(cmp.a.weighted_target_error).toBe(0.123);
  expect(cmp.b.weighted_target_error).toBe(0.087);
  expect(cmp.a.weighted_target_error).not.toBe(cmp.a.final_loss);
  expect(cmp.b.weighted_target_error).not.toBe(cmp.b.final_loss);
});

test("comparison matches renamed legacy and structured targets by Chronicle fact key", () => {
  const current = calibration([
    {
      name: "legacy.population.total@2024",
      target_name: "legacy.population.total",
      metadata: { ledger_fact_key: "agency.population.total" },
      target: 100,
      initial_estimate: 90,
      final_estimate: 95,
    },
  ], "legacy-current");
  const candidate = buildCalibration(
    {
      schema_version: 7,
      targets: [
        {
          name: "resident-population@2024",
          source: { id: "agency", label: "Statistical agency" },
          variable: { id: "resident_population", measure: "count" },
          dimensions: {},
          metadata: { ledger_fact_key: "agency.population.total" },
          target: 100,
          initial_estimate: 90,
          final_estimate: 99,
        },
      ],
    },
    "structured-candidate",
  );

  const cmp = buildComparison(current, candidate);
  expect(cmp.summary).toMatchObject({
    common: 1,
    added: 0,
    removed: 0,
    improved: 1,
    matching: {
      current_representation: "legacy",
      candidate_representation: "structured",
      matched_by: { chronicle_fact_key: 1 },
    },
  });
  expect(cmp.rows[0]).toMatchObject({
    match_kind: "chronicle_fact_key",
    current_name: "legacy.population.total@2024",
    candidate_name: "resident-population@2024",
    current_representation: "legacy",
    candidate_representation: "structured",
  });
});

test("comparison preserves duplicate names and resolves them by unique fallback keys", () => {
  const row = (name: string, targetName: string, factKey: string) => ({
    name,
    target_name: targetName,
    metadata: { ledger_fact_key: factKey },
    target: 100,
    initial_estimate: 100,
    final_estimate: 100,
  });
  const current = calibration([
    row("same@2024", "same", "fact-a"),
    row("same@2025", "same", "fact-b"),
  ], "duplicate-current");
  const candidate = calibration([
    row("renamed-a@2026", "renamed-a", "fact-a"),
    row("renamed-b@2026", "renamed-b", "fact-b"),
  ], "duplicate-candidate");

  const cmp = buildComparison(current, candidate);
  expect(cmp.summary).toMatchObject({
    common: 2,
    added: 0,
    removed: 0,
    matching: {
      matched_by: { chronicle_fact_key: 2 },
      ambiguous_key_groups: { base_name: 0 },
    },
  });
  expect(new Set(cmp.rows.map((comparison) => comparison.comparison_id)).size).toBe(2);
});

test("new target loss weighting metadata marks loss as normalized", () => {
  const normalized = buildCalibration(
    {
      schema_version: 6,
      targets: [],
      initial_loss: 0.42,
      final_loss: 0.39,
      options: {
        target_loss_scales: { wages: 1 },
        target_loss_weights: { wages: 1 },
      },
    },
    "normalized-release",
  );
  const raw = buildCalibration(
    {
      schema_version: 5,
      targets: [],
      initial_loss: 752_000_000_000,
      final_loss: 751_000_000_000,
    },
    "raw-release",
  );

  expect(latestMicrocosmCalibrationSummary(normalized).loss_kind).toBe("normalized_target_loss");
  expect(latestMicrocosmCalibrationSummary(raw).loss_kind).toBe("raw_optimizer_objective");
  expect(buildComparison(raw, normalized).summary.losses_comparable).toBe(false);
  expect(buildComparison(raw, normalized).summary.loss_kind).toBe("mixed");
});

test("verified historical attribution marks a metadata-poor loss as normalized", () => {
  expect(calibrationLossKind({}, {}, "exact_reconstructed")).toBe(
    "normalized_target_loss",
  );
  expect(calibrationLossKind({}, {}, "reported")).toBe("normalized_target_loss");
  expect(calibrationLossKind({}, {}, "derived")).toBe("raw_optimizer_objective");
  expect(calibrationLossKind({}, {}, "unavailable")).toBe("raw_optimizer_objective");
});

test("dotted chronicle zero targets use structural-zero percentage errors", () => {
  const cal = calibration([
    {
      name: "irs_soi.ty2022.historic_table_2.us.under_1.real_estate_taxes_amount@2024",
      target_name: "irs_soi.ty2022.historic_table_2.us.under_1.real_estate_taxes_amount",
      target: 0,
      initial_estimate: 100,
      final_estimate: 90,
      relative_error: 90,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "real_estate_taxes",
        source_measure_id: "real_estate_taxes_amount",
        ledger_geography_level: "country",
        ledger_geography_id: "0100000US",
        ledger_layout_groupby_value_id: "under_1",
        filing_status: "All",
      },
    },
  ]);
  expect(cal.rows[0].source).toBe("irs_soi");
  expect(cal.rows[0].variable).toBe("real estate taxes");
  expect(cal.rows[0].measure).toBe("total");
  expect(cal.rows[0].geography).toBe("United States");
  expect(cal.rows[0].breakdown).toBe("under 1 · All");
  expect(cal.rows[0].error_kind).toBe("relative");
  expect(cal.rows[0].initial_error).toBe(1);
  expect(cal.rows[0].final_error).toBe(1);
  expect(cal.rows[0].initial_relative_error).toBe(1);
  expect(cal.rows[0].abs_relative_error).toBe(1);
});

test("dotted chronicle zero targets accept numerical zero noise", () => {
  const cal = calibration([
    {
      name: "irs_soi.zero_target@2024",
      target: 0,
      initial_estimate: 0,
      final_estimate: 1e-4,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "zero_target",
        source_measure_id: "zero_target_amount",
        ledger_geography_level: "country",
        ledger_geography_id: "0100000US",
      },
    },
  ]);
  expect(cal.rows[0].error_kind).toBe("relative");
  expect(cal.rows[0].initial_relative_error).toBe(0);
  expect(cal.rows[0].abs_relative_error).toBe(0);
});

test("dotted chronicle zero targets reject values above the structural-zero tolerance", () => {
  const cal = calibration([
    {
      name: "irs_soi.zero_target@2024",
      target: 0,
      initial_estimate: 0,
      final_estimate: 1.0001e-4,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "zero_target",
        source_measure_id: "zero_target_amount",
        ledger_geography_level: "country",
        ledger_geography_id: "0100000US",
      },
    },
  ]);
  expect(cal.rows[0].error_kind).toBe("relative");
  expect(cal.rows[0].abs_relative_error).toBe(1);
});

test("source measure details become breakdown dimensions", () => {
  const cal = calibration([
    {
      name: "irs_soi.ty2022.historic_table_2.state_eitc.az.az.eitc_amount@2024",
      target_name: "irs_soi.ty2022.historic_table_2.state_eitc.az.az.eitc_amount",
      target: 100,
      initial_estimate: 100,
      final_estimate: 90,
      relative_error: -0.1,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "eitc",
        source_measure_id: "eitc_amount",
        ledger_geography_level: "state",
        ledger_geography_id: "0400000US04",
        ledger_layout_groupby_value_id: "az",
        filing_status: "All",
      },
    },
    {
      name: "irs_soi.ty2022.historic_table_2.state_eitc.az.az.eitc_no_children_amount@2024",
      target_name: "irs_soi.ty2022.historic_table_2.state_eitc.az.az.eitc_no_children_amount",
      target: 100,
      initial_estimate: 100,
      final_estimate: 90,
      relative_error: -0.1,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "eitc",
        source_measure_id: "eitc_no_children_amount",
        ledger_geography_level: "state",
        ledger_geography_id: "0400000US04",
        ledger_layout_groupby_value_id: "az",
        filing_status: "All",
      },
    },
  ]);
  expect(cal.rows[0].variable).toBe("eitc");
  expect(cal.rows[0].measure).toBe("total");
  expect(cal.rows[0].breakdown).toBe("all qualifying children · All");
  expect(cal.rows[0].dims).toEqual(["all qualifying children", "All"]);
  expect(cal.rows[0].variable_key).toBe("irs_soi / eitc · total");
  expect(cal.rows[1].breakdown).toBe("no qualifying children · All");
  const result = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?variable=irs_soi%20%2F%20eitc%20%C2%B7%20total",
    cal,
  );
  expect(result.dimensions.map((dim) => dim.label)).toContain("Qualifying children");
});

test("metadata dimensions skip geography repeated as layout breakdown", () => {
  const cal = calibration([
    {
      name: "irs_soi.ty2022.historic_table_2.state_broad.az.all.ctc_amount@2024",
      target_name: "irs_soi.ty2022.historic_table_2.state_broad.az.all.ctc_amount",
      target: 100,
      initial_estimate: 100,
      final_estimate: 90,
      relative_error: -0.1,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "ctc",
        source_measure_id: "ctc_amount",
        ledger_geography_level: "state",
        ledger_geography_id: "0400000US04",
        ledger_layout_record_set_id: "irs_soi.ty2022.historic_table_2.state_broad.az",
        ledger_layout_groupby_dimension: "state",
        ledger_layout_groupby_value_id: "all",
        ledger_filter_income_range: "all",
        filing_status: "All",
      },
    },
    {
      name: "irs_soi.ty2022.historic_table_2.state_broad.ca.all.ctc_amount@2024",
      target_name: "irs_soi.ty2022.historic_table_2.state_broad.ca.all.ctc_amount",
      target: 100,
      initial_estimate: 100,
      final_estimate: 90,
      relative_error: -0.1,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "ctc",
        source_measure_id: "ctc_amount",
        ledger_geography_level: "state",
        ledger_geography_id: "0400000US06",
        ledger_layout_record_set_id: "irs_soi.ty2022.historic_table_2.state_broad.ca",
        ledger_layout_groupby_dimension: "state",
        ledger_layout_groupby_value_id: "all",
        ledger_filter_income_range: "all",
        filing_status: "All",
      },
    },
  ]);
  expect(cal.rows[0].geography).toBe("AZ");
  expect(cal.rows[0].breakdown).toBe("All · All");
  const result = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics?variable=irs_soi%20%2F%20ctc%20%C2%B7%20total",
    cal,
  );
  expect(result.dimensions.map((dim) => dim.label)).toEqual(["Geography"]);
});

test("EITC table 2.5 child groups come from record set ids", () => {
  const cal = calibration([
    {
      name: "irs_soi.ty2022.table_2_5.eitc_by_agi_children.no_qualifying_children.25k_to_30k.eitc_total@2024",
      target_name: "irs_soi.ty2022.table_2_5.eitc_by_agi_children.no_qualifying_children.25k_to_30k.eitc_total",
      target: 535000,
      initial_estimate: 1995625464.431402,
      final_estimate: 910866264.4027674,
      relative_error: 1701.5537652388175,
      filter: null,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "eitc",
        source_measure_id: "eitc_total",
        ledger_geography_level: "country",
        ledger_geography_id: "0100000US",
        ledger_layout_record_set_id:
          "irs_soi.ty2022.table_2_5.eitc_by_agi_children.no_qualifying_children",
        ledger_layout_groupby_value_id: "25k_to_30k",
        filing_status: "All",
      },
    },
    {
      name: "irs_soi.ty2022.table_2_5.eitc_by_agi_children.one_qualifying_child.25k_to_30k.eitc_total@2024",
      target_name: "irs_soi.ty2022.table_2_5.eitc_by_agi_children.one_qualifying_child.25k_to_30k.eitc_total",
      target: 2717219000,
      initial_estimate: 1995625464.431402,
      final_estimate: 910866264.4027674,
      relative_error: -0.6647799590674262,
      filter: null,
      registry: { family: "irs_soi" },
      metadata: {
        variable: "eitc",
        source_measure_id: "eitc_total",
        ledger_geography_level: "country",
        ledger_geography_id: "0100000US",
        ledger_layout_record_set_id:
          "irs_soi.ty2022.table_2_5.eitc_by_agi_children.one_qualifying_child",
        ledger_layout_groupby_value_id: "25k_to_30k",
        filing_status: "All",
      },
    },
  ]);
  expect(cal.rows[0].breakdown).toBe("25k to 30k · no qualifying children · All");
  expect(cal.rows[0].dims).toEqual(["25k to 30k", "no qualifying children", "All"]);
  expect(cal.rows[0].estimate_warning).toContain("compiled model filter");
});

test("repeated unfiltered sibling estimates get generic scope warnings", () => {
  const cal = calibration([
    {
      name: "source.example.slice_a.under_50.amount@2024",
      target_name: "source.example.slice_a.under_50.amount",
      target: 10,
      initial_estimate: 100,
      final_estimate: 80,
      relative_error: 7,
      filter: null,
      registry: { family: "example" },
      metadata: {
        variable: "example",
        source_measure_id: "amount",
        ledger_geography_id: "0100000US",
        ledger_layout_record_set_id: "source.example.slice_a",
        ledger_layout_groupby_dimension: "age",
        ledger_layout_groupby_value_id: "under_50",
        ledger_layout_measure_id: "amount",
      },
    },
    {
      name: "source.example.slice_b.under_50.amount@2024",
      target_name: "source.example.slice_b.under_50.amount",
      target: 20,
      initial_estimate: 100,
      final_estimate: 80,
      relative_error: 3,
      filter: null,
      registry: { family: "example" },
      metadata: {
        variable: "example",
        source_measure_id: "amount",
        ledger_geography_id: "0100000US",
        ledger_layout_record_set_id: "source.example.slice_b",
        ledger_layout_groupby_dimension: "age",
        ledger_layout_groupby_value_id: "under_50",
        ledger_layout_measure_id: "amount",
      },
    },
  ]);
  expect(cal.rows[0].estimate_warning).toContain("sibling slices share the same estimate");
  expect(cal.rows[1].estimate_warning).toContain("sibling slices share the same estimate");
});

test("zero targets compare as structural-zero relative-error movers", () => {
  const target = {
    name: "irs_soi.ty2022.historic_table_2.us.under_1.real_estate_taxes_amount@2024",
    target_name: "irs_soi.ty2022.historic_table_2.us.under_1.real_estate_taxes_amount",
    target: 0,
    registry: { family: "irs_soi" },
    metadata: {
      variable: "real_estate_taxes",
      source_measure_id: "real_estate_taxes_amount",
      ledger_geography_level: "country",
      ledger_geography_id: "0100000US",
      ledger_layout_groupby_value_id: "under_1",
      filing_status: "All",
    },
  };
  const a = calibration([{ ...target, final_estimate: 100, relative_error: 100 }], "a");
  const b = calibration([{ ...target, final_estimate: 0, relative_error: 0 }], "b");
  const cmp = buildComparison(a, b);
  expect(cmp.summary.improved).toBe(1);
  expect(cmp.rows[0].error_kind).toBe("relative");
  expect(cmp.rows[0].a_error).toBe(1);
  expect(cmp.rows[0].b_error).toBe(0);
  expect(cmp.rows[0].abs_rel_delta).toBe(-1);
});

test("count and total measures split into distinct variables", () => {
  const cal = calibration([
    { name: "nation/irs/capital gains gross/total/AGI in 1m-inf/taxable/All@2024", target_name: "nation/irs/capital gains gross/total/AGI in 1m-inf/taxable/All", target: 100, initial_estimate: 90, final_estimate: 100, relative_error: 0, within_tolerance: true },
    { name: "nation/irs/capital gains gross/count/AGI in 1m-inf/taxable/All@2024", target_name: "nation/irs/capital gains gross/count/AGI in 1m-inf/taxable/All", target: 10, initial_estimate: 9, final_estimate: 10, relative_error: 0, within_tolerance: true },
  ]);
  const keys = new Set(cal.rows.map((r) => r.variable_key));
  expect(keys.has("irs / capital gains gross · total")).toBe(true);
  expect(keys.has("irs / capital gains gross · count")).toBe(true);
  expect(cal.rows.find((r) => r.measure === "count")).toBeTruthy();
  expect(cal.rows.find((r) => r.measure === "total")).toBeTruthy();
});

// A Build L ACS local-area row as published: the custom driver names the target
// value and post-calibration estimate `value`/`estimate`, with no canonical
// `target`/`final_estimate`/`initial_estimate`/`within_tolerance` (microcosm#398).
function localAreaTarget(name: string, value: number, estimate: number, rel: number) {
  return {
    name: `${name}@2024`,
    target_name: name,
    period: 2024,
    entity: "household",
    measure: { kind: "column", name },
    filter: null,
    source: "USDA SNAP FY2024",
    metadata: {},
    value,
    estimate,
    relative_error: rel,
  };
}

test("local-area diagnostics (value/estimate schema) render as included targets", () => {
  const cal = buildCalibration(
    {
      schema_version: 4,
      targets: [
        localAreaTarget("usda_snap.fy2024.state.ct.average_monthly_households", 229620.25, 243424.57, 0.0601),
        localAreaTarget("usda_snap.fy2024.state.me.average_monthly_households", 100000, 90000, -0.1),
      ],
      final_loss: 0.058,
      fraction_within_10pct: 0.87,
    },
    "populace-us-2024-buildl-acs-local-36de5d9a-20260712T104640Z",
    null,
    {},
    { dataset_role: "non_default_local_area", is_default: false, default_datasets: {} },
  );

  // The bug: without alias normalization every row read as "no estimate" and the
  // dashboard reported zero calibrated targets. All rows are now included.
  expect(cal.diagnostics_status).toBe("ok");
  expect(cal.included_target_count).toBe(2);
  expect(cal.rows.every((row) => row.calibration_status === "included")).toBe(true);
  // Aliases are mapped onto the canonical fit fields.
  expect(cal.rows[0].target).toBe(229620.25);
  expect(cal.rows[0].final_estimate).toBe(243424.57);
  // Relative-error-derived fit survives (used by the "within 10%" metrics).
  expect(cal.rows[0].abs_relative_error).toBeCloseTo(0.0601, 4);

  const summary = latestMicrocosmCalibrationSummary(cal);
  expect(summary.total_targets).toBe(2);
  expect(summary.included_target_count).toBe(2);
  expect(summary.diagnostics_status).toBe("ok");
  expect(summary.dataset_role).toBe("non_default_local_area");
  expect(summary.is_default).toBe(false);
  expect(summary.is_local_area).toBe(true);

  const page = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics",
    cal,
  );
  expect(page.summary.total_targets).toBe(2);
  expect(page.summary.included_target_count).toBe(2);
  expect(page.summary.diagnostics_status).toBe("ok");
  expect(page.is_local_area).toBe(true);
});

test("an audited unversioned local-area artifact uses the legacy reader", () => {
  const cal = buildCalibration(
    {
      targets: [
        {
          name: "usda_snap.fy2024.state.ct.average_monthly_households",
          target: 229620.25,
          initial_estimate: 280095,
          final_estimate: 229462,
        },
      ],
    },
    "populace-us-2024-buildp-acs-local-592ae5d6-20260819T020303Z",
    null,
    {},
    { dataset_role: "non_default_local_area", is_default: false },
    {},
    "us",
    "huggingface_immutable",
    "b6f05b652049f88c044f1907eeed13c378aea47aec9c6dd6518faa90d1a0dcd0",
  );

  expect(cal.target_schema).toEqual({
    diagnostics_schema_version: null,
    structured_dimensions: false,
    target_representation: "legacy",
  });
  expect(cal.diagnostics_status).toBe("ok");
  expect(cal.included_target_count).toBe(1);
});

test("canonical target/final_estimate are never overwritten by value/estimate aliases", () => {
  const cal = buildCalibration(
    {
      schema_version: 2,
      targets: [
        {
          name: "nation/irs/agi/total@2024",
          target_name: "nation/irs/agi/total",
          target: 100,
          initial_estimate: 90,
          final_estimate: 110,
          value: 999,
          estimate: 999,
          relative_error: 0.1,
        },
      ],
    },
    "rel",
  );
  expect(cal.diagnostics_status).toBe("ok");
  expect(cal.rows[0].target).toBe(100);
  expect(cal.rows[0].final_estimate).toBe(110);
  expect(cal.rows[0].calibration_status).toBe("included");
});

test("unreadable diagnostics rows report an explicit incompatible status, not a silent zero", () => {
  const cal = buildCalibration(
    {
      schema_version: 2,
      targets: [
        { name: "source.us.total.mystery-a@2024", target_name: "source.us.total.mystery-a", metadata: {}, unknown_metric: 1 },
        { name: "source.us.total.mystery-b@2024", target_name: "source.us.total.mystery-b", metadata: {} },
      ],
    },
    "rel",
  );
  expect(cal.diagnostics_status).toBe("incompatible");
  expect(cal.included_target_count).toBe(0);

  const page = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics",
    cal,
  );
  expect(page.summary.diagnostics_status).toBe("incompatible");
  // The rows are still counted — the zero-included is now explained, not silent.
  expect(page.summary.total_targets).toBe(2);
});

test("diagnostics with an empty targets list report an explicit empty status", () => {
  const cal = buildCalibration({ schema_version: 2, targets: [] }, "rel");
  expect(cal.diagnostics_status).toBe("empty");
  expect(latestMicrocosmCalibrationSummary(cal).diagnostics_status).toBe("empty");
});

test("diagnostics missing the targets array report incompatible", () => {
  const cal = buildCalibration({ schema_version: 2, final_loss: 0.1 }, "rel");
  expect(cal.diagnostics_status).toBe("incompatible");
});

test("releaseRole classifies national default vs non-default local-area", () => {
  expect(releaseRole({ default_datasets: { national: "populace_us_2024" } })).toEqual({
    dataset_role: null,
    is_default: true,
    is_local_area: false,
  });
  expect(
    releaseRole({
      dataset_role: "non_default_local_area",
      is_default: false,
      default_datasets: {},
    }),
  ).toEqual({
    dataset_role: "non_default_local_area",
    is_default: false,
    is_local_area: true,
  });
});

test("releasePresentation keeps only trimmed, capped intro slots", () => {
  const longIntro = `  ${"x".repeat(610)}  `;
  expect(
    releasePresentation({
      presentation: {
        overview_intro: "  Artifact overview.  ",
        targets_intro: longIntro,
        arbitrary_section: "Ignored",
      },
    }),
  ).toEqual({
    overview_intro: "Artifact overview.",
    targets_intro: "x".repeat(600),
  });
});

test("releasePresentation returns null when no valid intro slot exists", () => {
  expect(releasePresentation({})).toBeNull();
  expect(releasePresentation({ presentation: [] })).toBeNull();
  expect(releasePresentation({ presentation: "copy" })).toBeNull();
  expect(
    releasePresentation({
      presentation: {
        overview_intro: "   ",
        targets_intro: 12,
        unknown: "Ignored",
      },
    }),
  ).toBeNull();
});

test("presentation flows through calibration, summary, and target responses", () => {
  const presentation = {
    overview_intro: "Artifact overview.",
    targets_intro: "Artifact target prompt.",
  };
  const cal = buildCalibration(
    { schema_version: 2, targets: [] },
    "presentation-release",
    null,
    {},
    { presentation },
  );

  expect(cal.presentation).toEqual(presentation);
  expect(latestMicrocosmCalibrationSummary(cal).presentation).toEqual(presentation);
  expect(
    latestMicrocosmTargetDiagnosticsPage(
      "http://x/api/microcosm/target-diagnostics",
      cal,
    ).presentation,
  ).toEqual(presentation);
});

test("releasePublisherLabels keeps valid keys and trimmed non-empty labels", () => {
  expect(releasePublisherLabels({})).toEqual({});
  expect(releasePublisherLabels({ publisher_labels: [] })).toEqual({});
  expect(releasePublisherLabels({ publisher_labels: "labels" })).toEqual({});
  expect(
    releasePublisherLabels({
      publisher_labels: {
        novastat_agency: "  Nova Statistics Agency  ",
        IRS2: "IRS second series",
        "bad-key": "Dropped",
        _bad: "Dropped",
        blank: "  ",
        numeric: 12,
      },
    }),
  ).toEqual({
    novastat_agency: "Nova Statistics Agency",
    IRS2: "IRS second series",
  });
});

test("publisher labels flow through rows, variables, target responses, and treemaps", () => {
  const cal = buildCalibration(
    {
      schema_version: 2,
      targets: [
        {
          name: "fixture_population@2026",
          target_name: "fixture_population",
          source: "ZZ official population table",
          metadata: {
            chronicle_record_ids: ["novastat_agency.population.cy2026.total"],
            variable: "population",
            source_measure_id: "population_count",
          },
          target: 100,
          initial_estimate: 90,
          final_estimate: 100,
        },
      ],
    },
    "publisher-labels",
    null,
    {},
    { publisher_labels: { novastat_agency: "Nova Statistics Agency" } },
  );

  expect(cal.publisher_labels).toEqual({
    novastat_agency: "Nova Statistics Agency",
  });
  expect(cal.rows[0].source_label).toBe("Nova Statistics Agency");
  const page = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics",
    cal,
  );
  expect(page.targets[0].source_label).toBe("Nova Statistics Agency");
  expect(page.variables[0].source_label).toBe("Nova Statistics Agency");
  expect(microcosmTargetTreemap(cal.rows, cal.release_id).groups[0].label).toBe(
    "Nova Statistics Agency",
  );
});

test("publisher label lookup does not read inherited object properties", () => {
  const cal = buildCalibration(
    {
      schema_version: 2,
      targets: [
        {
          name: "constructor.population.total@2026",
          source: "Citation",
          metadata: { chronicle_record_ids: ["constructor.population.total"] },
          target: 1,
          initial_estimate: 1,
          final_estimate: 1,
        },
      ],
    },
    "publisher-prototype",
  );

  expect(cal.rows[0].source_label).toBe("Constructor");
});

test("fully structured targets ignore conflicting legacy identity fields", () => {
  const cal = buildCalibration(
    {
      schema_version: 7,
      dimensions: {
        region: {
          label: "Region",
          role: "geography",
          level: "region",
          values: { north: "North" },
        },
        sex: { label: "Sex", values: { female: "Female" } },
      },
      targets: [
        {
          name: "legacy.publisher.incorrect_variable.total@2026",
          source: {
            id: "novastat_agency",
            citation: "Official population table",
            label: "Nova Statistics Agency",
            url: "https://stats.example/population",
          },
          variable: {
            id: "resident_population",
            label: "Resident population",
            measure: "count",
          },
          dimensions: { region: "north", sex: "female" },
          filter: "cell_be1_male_65_plus",
          metadata: {
            chronicle_record_ids: ["legacy_agency.incorrect.total"],
            variable: "incorrect_variable",
            ledger_geography_level: "state",
            ledger_geography_id: "0400000US06",
            ledger_layout_groupby_value_id: "incorrect_breakdown",
          },
          target: 100,
          initial_estimate: 90,
          final_estimate: 100,
        },
      ],
    },
    "structured-only",
  );

  expect(cal.target_schema.target_representation).toBe("structured");
  expect(cal.rows[0]).toMatchObject({
    family: "novastat_agency/resident_population",
    source: "novastat_agency",
    source_label: "Nova Statistics Agency",
    source_citation: "Official population table",
    source_url: "https://stats.example/population",
    variable: "resident_population",
    variable_label: "Resident population",
    measure: "count",
    geography: "North",
    level: "region",
    state: null,
    breakdown: "Female",
    dimension_adapter: "structured",
    variable_key: "novastat_agency / resident_population · count",
    target_dimensions: [
      expect.objectContaining({ key: "bd_sex", label: "Sex", value: "Female" }),
    ],
  });
  const map = microcosmTargetTreemap(cal.rows, cal.release_id);
  expect(map.groups[0]).toMatchObject({
    source: "novastat_agency",
    label: "Nova Statistics Agency",
  });
  expect(map.groups[0].children[0]).toMatchObject({
    variable: "resident_population",
    label: "Resident population",
  });
  const tree = buildCalibrationTree(cal.rows, {
    breakdown: "program",
    path: { dimensions: [] },
    filters: {
      geographyLevels: [],
      geographies: [],
      fitBands: [],
      comparisonFits: [],
      calibrationStatuses: [],
    },
  });
  expect(tree.groups[0].nodes[0]).toMatchObject({
    id: "resident_population",
    label: "Resident population",
  });
});

test("structured categories group count and total rows by variable identity", () => {
  const target = (name: string, measure: "count" | "total") => ({
    name: `${name}@2025`,
    target_name: name,
    source: { id: "hmrc", citation: "HMRC SPI" },
    variable: { id: "spi_employment_income", measure },
    dimensions: {
      geography_country: "K02000001",
      total_income_lower_bound: "100000",
    },
    metadata: {},
    target: 100,
    initial_estimate: 90,
    final_estimate: 100,
  });
  const cal = buildCalibration(
    {
      schema_version: 7,
      dimensions: {
        geography_country: {
          label: "Country",
          role: "geography",
          level: "country",
          values: { K02000001: "United Kingdom" },
          order: ["K02000001"],
        },
        total_income_lower_bound: {
          label: "Total Income Lower Bound",
          values: { "100000": "100000" },
          order: ["100000"],
        },
      },
      targets: [
        target("hmrc/employment_income_amount_band_100000", "total"),
        target("hmrc/employment_income_count_band_100000", "count"),
      ],
    },
    "uk-schema-7-categories",
    null,
    {},
    {},
    {},
    "uk",
  );
  const tree = buildCalibrationTree(cal.rows, {
    breakdown: "program",
    path: { dimensions: [] },
    filters: {
      geographyLevels: [],
      geographies: [],
      fitBands: [],
      comparisonFits: [],
      calibrationStatuses: [],
    },
  });

  expect(cal.target_schema.target_representation).toBe("structured");
  expect(cal.rows.map((row) => row.measure)).toEqual(["total", "count"]);
  expect(cal.rows.every((row) => row.dimension_adapter === "structured")).toBe(true);
  expect(tree.groups).toHaveLength(1);
  expect(tree.groups[0].nodes).toHaveLength(1);
  expect(tree.groups[0].nodes[0]).toMatchObject({
    id: "spi_employment_income",
    label: "Spi employment income",
    metrics: { nTargets: 2 },
  });
});

test("structured dimension ids remain independent when display labels repeat", () => {
  const structuredTargets = [
    ["north", "east"],
    ["north", "west"],
    ["south", "east"],
    ["south", "west"],
  ].map(([origin, destination], index) => ({
    name: `population-${index}`,
    source: { id: "agency" },
    variable: { id: "population", measure: "count" },
    dimensions: { origin, destination },
    target: 100,
    initial_estimate: 90,
    final_estimate: 100,
  }));
  const diagnostics = {
    schema_version: 7,
    dimensions: {
      origin: {
        label: "Region",
        values: { north: "North", south: "South" },
      },
      destination: {
        label: "Region",
        values: { east: "East", west: "West" },
      },
    },
    targets: structuredTargets,
  };
  const cal = buildCalibration(diagnostics, "repeated-dimension-labels");

  expect(cal.rows[0].target_dimensions).toEqual([
    expect.objectContaining({ key: "bd_origin", label: "Region", value: "North" }),
    expect.objectContaining({ key: "bd_destination", label: "Region", value: "East" }),
  ]);

  const requestUrl = new URL("http://x/api/microcosm/target-diagnostics");
  requestUrl.searchParams.set("variable", "agency / population · count");
  requestUrl.searchParams.append("facet", "bd_origin:North");
  requestUrl.searchParams.append("facet", "bd_destination:East");
  const page = latestMicrocosmTargetDiagnosticsPage(requestUrl.toString(), cal);
  expect(page.dimensions).toEqual([
    { key: "bd_origin", label: "Region", values: ["North", "South"] },
    { key: "bd_destination", label: "Region", values: ["East", "West"] },
  ]);
  expect(page.filtered_total).toBe(1);

  const treeState = {
    breakdown: "program" as const,
    path: {
      source: "agency",
      program: "population",
      geography: "United States",
      dimensions: [],
    },
    filters: {
      geographyLevels: [],
      geographies: [],
      fitBands: [],
      comparisonFits: [],
      calibrationStatuses: [],
    },
  };
  const tree = buildCalibrationTree(cal.rows, treeState);
  expect(tree.dimensionOrder).toEqual([
    { key: "bd_destination", label: "Region" },
    { key: "bd_origin", label: "Region" },
  ]);
  expect(tree.groups[0]?.id).toBe("bd_destination");
  const destinationTree = buildCalibrationTree(cal.rows, {
    ...treeState,
    path: {
      ...treeState.path,
      dimensions: [{ key: "bd_destination", label: "Region", value: "East" }],
    },
  });
  expect(destinationTree.groups[0]?.id).toBe("bd_origin");

  expect(() =>
    buildCalibration(
      {
        ...diagnostics,
        targets: [
          ...structuredTargets,
          {
            name: "nation/legacy/population",
            target: 1,
            initial_estimate: 1,
            final_estimate: 1,
          },
        ],
      },
      "mixed-repeated-dimension-labels",
    ),
  ).toThrow("Schema 7 target source.id");
});

test("schema 7 refuses row-shape-based fallback to the legacy reader", () => {
  expect(() => buildCalibration(
    {
      schema_version: 7,
      targets: [
        {
          name: "bea_nipa.cy2023.proprietors_income.a041rc.amount@2024",
          source: "BEA citation",
          metadata: {
            chronicle_record_ids: ["bea.nipa.proprietors_income.amount"],
            source_measure_id: "amount",
            ledger_geography_level: "country",
          },
          target: 100,
          initial_estimate: 90,
          final_estimate: 99,
        },
        {
          name: "legacy.publisher.population.total@2026",
          source: {
            id: "artifact_agency",
            citation: "Official population table",
            label: "Artifact agency",
          },
          variable: {
            id: "resident_population",
            label: "Resident population",
            measure: "count",
          },
          dimensions: {},
          metadata: {
            chronicle_record_ids: ["chronicle_agency.population.total"],
            variable: "legacy_population",
          },
          target: 100,
          initial_estimate: 90,
          final_estimate: 100,
        },
      ],
    },
    "mixed-identities",
  )).toThrow("Schema 7 target source.id");
});

test("structured source and variable fields remain authoritative", () => {
  const target = {
    name: "legacy.publisher.population.total@2026",
    source: {
      id: "artifact_agency",
      citation: "Official population table",
      label: "Row agency label",
      url: "https://stats.example/population",
    },
    variable: {
      id: "resident_population",
      label: "Resident population",
      measure: "mean",
    },
    dimensions: {},
    metadata: {
      chronicle_record_ids: ["chronicle_agency.population.total"],
      variable: "legacy_population",
      source_measure_id: "legacy_population_count",
    },
    target: 100,
    initial_estimate: 90,
    final_estimate: 100,
  };
  const cal = buildCalibration(
    { schema_version: 7, targets: [target] },
    "structured-identifiers",
    null,
    {},
    { publisher_labels: { artifact_agency: "Manifest agency label" } },
  );

  expect(cal.rows[0]).toMatchObject({
    source: "artifact_agency",
    source_label: "Manifest agency label",
    source_citation: "Official population table",
    source_url: "https://stats.example/population",
    variable: "resident_population",
    variable_label: "Resident population",
    measure: "mean",
    variable_key: "artifact_agency / resident_population · mean",
  });
  const response = latestMicrocosmTargetDiagnosticsPage(
    "http://x/api/microcosm/target-diagnostics",
    cal,
  );
  expect(response.targets[0]).toMatchObject({
    source_url: "https://stats.example/population",
    variable_label: "Resident population",
  });
  expect(response.variables[0].variable_label).toBe("Resident population");

  const withoutChronicle = buildCalibration(
    {
      schema_version: 7,
      targets: [
        {
          ...target,
          metadata: {
            variable: "legacy_population",
            source_measure_id: "legacy_population_count",
          },
        },
      ],
    },
    "structured-source-fallback",
  );
  expect(withoutChronicle.rows[0]).toMatchObject({
    source: "artifact_agency",
    source_label: "Row agency label",
  });
});

const BE_COUNTRY_DEFAULTS: ArtifactCountry = {
  code: "be",
  label: "Belgium",
  geography_id: null,
  geography_label: "Belgium",
  repository_visibility: "private",
  capabilities: ["calibration", "targets", "compare", "cross_dataset"],
};

test("releaseCountry falls back to the registration when the manifest has no country block", () => {
  expect(releaseCountry({}, "be")).toEqual(BE_COUNTRY_DEFAULTS);
  expect(releaseCountry({ country: "BE" }, "be")).toEqual(BE_COUNTRY_DEFAULTS);
  expect(releaseCountry({}, "us")).toEqual({
    code: "us",
    label: "United States",
    geography_id: "0100000US",
    geography_label: "United States",
    repository_visibility: "public",
    capabilities: [
      "calibration",
      "targets",
      "compare",
      "cross_dataset",
      "staging",
      "model_coverage",
      "pipeline",
      "variables",
      "external_checks",
    ],
  });
});

test("releaseCountry lets well-typed string fields override the registration", () => {
  expect(
    releaseCountry(
      {
        country: {
          code: "BE",
          label: "Kingdom of Belgium",
          geography_id: "BE",
          geography_label: "Belgium (national)",
          repository_visibility: "public",
          presentation: { ignored: true },
        },
      },
      "be",
    ),
  ).toEqual({
    ...BE_COUNTRY_DEFAULTS,
    label: "Kingdom of Belgium",
    geography_id: "BE",
    geography_label: "Belgium (national)",
    repository_visibility: "public",
  });
});

test("releaseCountry ignores a block whose code names another country", () => {
  expect(
    releaseCountry(
      { country: { code: "us", label: "United States", repository_visibility: "public" } },
      "be",
    ),
  ).toEqual(BE_COUNTRY_DEFAULTS);
  expect(releaseCountry({ country: { code: 7, label: "Seven" } }, "be")).toEqual(
    BE_COUNTRY_DEFAULTS,
  );
  // An explicit `code: null` is a present, malformed field — the whole block
  // is ignored, exactly like a mismatched code.
  expect(
    releaseCountry(
      { country: { code: null, label: "Wrong label", geography_label: "Wrong geography" } },
      "be",
    ),
  ).toEqual(BE_COUNTRY_DEFAULTS);
});

test("releaseCountry narrows capabilities to the registration and never widens them", () => {
  expect(
    releaseCountry(
      { country: { code: "be", capabilities: ["targets", "staging", "calibration", "bogus", 3] } },
      "be",
    ).capabilities,
  ).toEqual(["calibration", "targets"]);
  expect(releaseCountry({ country: { capabilities: [] } }, "be").capabilities).toEqual([]);
  expect(releaseCountry({ country: { capabilities: "all" } }, "be").capabilities).toEqual(
    BE_COUNTRY_DEFAULTS.capabilities,
  );
});

test("releaseCountry ignores malformed field values", () => {
  expect(
    releaseCountry(
      {
        country: {
          label: "",
          geography_label: 12,
          geography_id: { id: "x" },
          repository_visibility: "open",
        },
      },
      "be",
    ),
  ).toEqual(BE_COUNTRY_DEFAULTS);
});

test("the calibration summary and target page carry the typed country block", () => {
  const cal = buildCalibration(
    { ...beDiagnosticsFixture, schema_version: 2 },
    "be-country",
    null,
    {},
    { ...beReleaseManifestFixture, country: { code: "be", label: "Kingdom of Belgium" } },
    {},
    "be",
  );
  expect(cal.country_info).toEqual({ ...BE_COUNTRY_DEFAULTS, label: "Kingdom of Belgium" });
  expect(latestMicrocosmCalibrationSummary(cal).country).toEqual(cal.country_info);
  expect(
    latestMicrocosmTargetDiagnosticsPage("http://x/api/microcosm/target-diagnostics", cal).country,
  ).toEqual(cal.country_info);
});

test("the artifact's national geography label shapes rows without a geography", () => {
  const cal = buildCalibration(
    { ...beDiagnosticsFixture, schema_version: 2 },
    "be-geography",
    null,
    {},
    { ...beReleaseManifestFixture, country: { geography_label: "Belgium (national)" } },
    {},
    "be",
  );
  const national = cal.rows.filter((row) => row.level === "national");
  expect(national.length).toBeGreaterThan(0);
  expect(national.every((row) => row.geography === "Belgium (national)")).toBe(true);
  expect(cal.rows[0]).toMatchObject({ geography: "Brussels", level: "region" });
});

test("the target page reports scope target counts for the release", () => {
  expect(page("").scope_counts).toEqual({ healthcare: 0 });
  expect(page("?scope=healthcare").scope_counts).toEqual({ healthcare: 0 });
});


test("schema 8 keeps the first spelling of an identifier two publishers label differently", () => {
  const target = (
    name: string,
    provider: string,
    geographyLabel: string,
  ) => ({
    name: `${name}@2025`,
    target_name: name,
    source: `${provider} citation`,
    target: 100,
    initial_estimate: 90,
    final_estimate: 101,
    hierarchy: {
      provider: { id: provider, label: provider.toUpperCase() },
      category: { id: `${provider}.count`, label: "Count", provider_id: provider },
      geography: { id: "E06000001", label: geographyLabel, level: "local_authority" },
      dimensions: [
        {
          id: "geography",
          label: "Geography",
          value_id: "e06000001",
          value_label: geographyLabel,
        },
      ],
      target: { id: name, label: `${name} target` },
    },
  });
  const cal = buildCalibration(
    {
      schema_version: 8,
      targets: [
        target("hmrc.count", "hmrc", "Hartlepool UA"),
        target("ons.count", "ons", "Hartlepool"),
      ],
    },
    "schema-8-variants",
  );

  expect(cal.rows).toHaveLength(2);
  expect(cal.hierarchy_label_variants).toEqual([
    {
      kind: "geography",
      id: "local_authority E06000001",
      labels: ["Hartlepool UA", "Hartlepool"],
    },
    {
      kind: "dimension value",
      id: "geography=e06000001",
      labels: ["Hartlepool UA", "Hartlepool"],
    },
  ]);
  // One identifier, varying as a geography and again as its dimension value.
  expect(latestMicrocosmCalibrationSummary(cal).label_variants).toEqual({
    count: 1,
    entries: 2,
    examples: [
      'geography local_authority E06000001: "Hartlepool UA" · "Hartlepool"',
      'dimension value geography=e06000001: "Hartlepool UA" · "Hartlepool"',
    ],
  });
});

// Microcosm release lines (microcosm#823): one folder per line, one tag per
// cut. All values are synthetic.
const LINE = "microcosm-uk-2024-25-national";
const DENSE_LINE = "microcosm-uk-2024-25-dense";

function folderRelease(id: string, date: string): ReleaseEntry {
  return {
    release_id: id,
    date,
    files: ["calibration_diagnostics.json"],
    has_calibration: true,
    dataset_role: null,
    is_default: true,
    is_local_area: false,
  };
}

function cutRelease(tag: string, cutAt: string, directory: string): ReleaseEntry {
  return { ...folderRelease(tag, cutAt), files: [], release_directory: directory };
}

test("the release picker lists every published cut in place of its line folder", () => {
  const usFolders = [
    folderRelease("populace-us-2024-b-20260915", "20260915"),
    folderRelease("populace-us-2024-a-20260909", "20260909"),
  ];
  expect(mergeReleaseInventory(usFolders, [], usFolders[0].release_id)).toBe(usFolders);

  const june = folderRelease("populace-uk-2023-test-20260619T023711Z", "20260619T023711Z");
  const lineFolder = folderRelease(LINE, LINE);
  const earlier = cutRelease(`${LINE}-20261002T230158Z-0a0a0a0a`, "20261002T230158Z", LINE);
  const current = cutRelease(`${LINE}-20261019T083323Z-0b0b0b0b`, "20261019T083323Z", LINE);
  const dense = {
    ...cutRelease(`${DENSE_LINE}-20261019T000432Z-0c0c0c0c`, "20261019T000432Z", DENSE_LINE),
    dataset_role: "non_default_local_area",
    is_default: false,
    is_local_area: true,
  };
  expect(
    mergeReleaseInventory([june, lineFolder], [earlier, dense, current], current.release_id)
      .map((release) => release.release_id),
  ).toEqual([current.release_id, dense.release_id, earlier.release_id, june.release_id]);
  // Until the dashboard's latest release moves to a cut, the folder build it
  // points at stays selectable.
  expect(
    mergeReleaseInventory([june, lineFolder], [current], LINE)
      .map((release) => release.release_id),
  ).toContain(LINE);
});

test("published cuts take their role from their own release manifest at their commit", async () => {
  const entry = (
    tag: string,
    directory: string | undefined,
    sha: string,
    id: string,
  ): CalibrationTreeManifestEntry => ({
    buildArtifactId: id.repeat(64),
    kind: "release",
    sourceId: tag,
    label: tag,
    releaseId: tag,
    ...(directory ? { releaseDirectory: directory } : {}),
    stagingRunId: null,
    hfRepo: "policyengine/populace-uk-private",
    hfCommitSha: sha,
    treeSchemaVersion: 6,
    indexSha256: "e".repeat(64),
    indexBytes: 10,
    createdAt: null,
    updatedAt: null,
  });
  const nationalCut = `${LINE}-20261019T083323Z-0b0b0b0b`;
  const denseCut = `${DENSE_LINE}-20261019T000432Z-0c0c0c0c`;
  let manifest = emptyCalibrationTreeManifest();
  for (const build of [
    entry(LINE, undefined, "9".repeat(40), "a"),
    entry(nationalCut, LINE, "1".repeat(40), "b"),
    entry(denseCut, DENSE_LINE, "2".repeat(40), "c"),
  ]) {
    manifest = withCalibrationTreeManifestEntry(manifest, "uk", build);
  }
  const originalFetch = globalThis.fetch;
  const requested: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    requested.push(url);
    if (url.endsWith(`/resolve/${"2".repeat(40)}/releases/${DENSE_LINE}/release_manifest.json`)) {
      return Response.json({ dataset_role: "non_default_local_area", is_default: false });
    }
    // An unreadable manifest keeps the default classification.
    return new Response(null, { status: 503 });
  }) as typeof fetch;
  try {
    const cuts = await loadPublishedCutReleases(0, "uk", manifest, [folderRelease(LINE, LINE)]);
    expect(cuts.map((cut) => [cut.release_id, cut.date, cut.release_directory, cut.is_local_area]))
      .toEqual([
        [nationalCut, "20261019T083323Z", LINE, false],
        [denseCut, "20261019T000432Z", DENSE_LINE, true],
      ]);
    expect(requested).toContain(
      `https://huggingface.co/datasets/${microcosmRepo("uk")}/resolve/${"1".repeat(40)}/releases/${LINE}/release_manifest.json`,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// A repository whose releases.json is removed again (a reverted switch) is
// read from its tags and folders, but the dashboard manifest keeps the entries
// the build-folder readers published, named `<release>/<build>`. Those readers
// list only tags and folders, so such an entry must not stop them. Ids and
// commits are synthetic.
test("a build-folder entry in the manifest does not stop the tagged builds being listed", async () => {
  const nationalTag = `${LINE}-20261019T083323Z-0b0b0b0b`;
  const buildFolder = `${LINE}/uk-frs-calibration-attempt-20261020T091500Z-1d1d1d1d`;
  const manifestEntry = (
    releaseId: string,
    releaseDirectory: string | undefined,
    commit: string,
    artifactDigit: string,
  ): CalibrationTreeManifestEntry => ({
    buildArtifactId: artifactDigit.repeat(64),
    kind: "release",
    sourceId: releaseId,
    label: releaseId,
    releaseId,
    ...(releaseDirectory ? { releaseDirectory } : {}),
    stagingRunId: null,
    hfRepo: "policyengine/populace-uk-private",
    hfCommitSha: commit,
    treeSchemaVersion: 6,
    indexSha256: "e".repeat(64),
    indexBytes: 10,
    createdAt: "2026-10-20T09:15:00.000Z",
    updatedAt: null,
  });
  let manifest = emptyCalibrationTreeManifest();
  for (const build of [
    manifestEntry(nationalTag, LINE, "1".repeat(40), "a"),
    // A build in its own folder records no separate releaseDirectory.
    manifestEntry(buildFolder, undefined, "6".repeat(40), "b"),
  ]) {
    manifest = withCalibrationTreeManifestEntry(manifest, "uk", build);
  }
  const originalFetch = globalThis.fetch;
  // No release manifest is readable: each entry keeps its default role.
  globalThis.fetch = (async (_input: string | URL | Request) =>
    new Response(null, { status: 503 })) as typeof fetch;
  try {
    const tagged = await loadPublishedCutReleases(0, "uk", manifest, [folderRelease(LINE, LINE)]);
    expect(tagged.map((release) => [release.release_id, release.release_directory])).toEqual([
      [nationalTag, LINE],
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// Microcosm can publish a release as a tag alone (`--no-latest --tag-only`):
// `releases/<release id>/` exists at the tag's commit and never on main, so
// the folder listing cannot name it. A folder on main without calibration
// diagnostics does not name a published release either: the publisher reads
// that release from its tag. Release ids and commits are synthetic.
test("a release published only as a tag is listed from the dashboard manifest", async () => {
  const folderReleaseId = "populace-us-2024-folder-20260915";
  const uncalibratedFolderReleaseId = "populace-us-2024-uncalibrated-folder-20261001";
  const tagOnlyReleaseId = "populace-us-2024-tag-only-local-20261017T030317Z";
  const tagOnlyCommit = "3".repeat(40);
  const manifestEntry = (
    releaseId: string,
    commit: string,
    artifactDigit: string,
  ): CalibrationTreeManifestEntry => ({
    buildArtifactId: artifactDigit.repeat(64),
    kind: "release",
    sourceId: releaseId,
    label: releaseId,
    releaseId,
    stagingRunId: null,
    hfRepo: "policyengine/populace-us",
    hfCommitSha: commit,
    treeSchemaVersion: 6,
    indexSha256: "e".repeat(64),
    indexBytes: 10,
    createdAt: null,
    updatedAt: null,
  });
  let manifest = emptyCalibrationTreeManifest();
  for (const build of [
    manifestEntry(folderReleaseId, "4".repeat(40), "a"),
    manifestEntry(tagOnlyReleaseId, tagOnlyCommit, "b"),
    manifestEntry(uncalibratedFolderReleaseId, "5".repeat(40), "c"),
  ]) {
    manifest = withCalibrationTreeManifestEntry(manifest, "us", build);
  }
  const folders = [
    folderRelease(folderReleaseId, "20260915"),
    {
      ...folderRelease(uncalibratedFolderReleaseId, "20261001"),
      files: ["release_manifest.json"],
      has_calibration: false,
    },
  ];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith(`/resolve/${tagOnlyCommit}/releases/${tagOnlyReleaseId}/release_manifest.json`)) {
      return Response.json({ dataset_role: "non_default_local_area", is_default: false });
    }
    return new Response(null, { status: 503 });
  }) as typeof fetch;
  try {
    const tagged = await loadPublishedCutReleases(0, "us", manifest, folders);
    expect(
      tagged
        .map((release) => [
          release.release_id,
          release.date,
          release.release_directory,
          release.has_calibration,
          release.is_local_area,
        ])
        .sort(),
    ).toEqual([
      [tagOnlyReleaseId, "20261017T030317Z", undefined, true, true],
      [uncalibratedFolderReleaseId, "20261001", undefined, true, false],
    ]);
    // Each release is listed once, newest first.
    const inventory = mergeReleaseInventory(folders, tagged, folderReleaseId);
    expect(inventory.map((release) => [release.release_id, release.has_calibration])).toEqual([
      [tagOnlyReleaseId, true],
      [uncalibratedFolderReleaseId, true],
      [folderReleaseId, true],
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// The dashboard manifest keeps the build published from a release series'
// shared folder before its per-attempt tags existed. If the folder's copy on
// main has no calibration diagnostics, the manifest reader returns that build,
// and it must still leave the picker once the series' tags are listed.
test("a release series' shared folder build leaves the picker even when its copy on main lacks diagnostics", async () => {
  const currentTag = `${LINE}-20261019T083323Z-0b0b0b0b`;
  const manifestEntry = (
    releaseId: string,
    releaseDirectory: string | undefined,
    commit: string,
    artifactDigit: string,
  ): CalibrationTreeManifestEntry => ({
    buildArtifactId: artifactDigit.repeat(64),
    kind: "release",
    sourceId: releaseId,
    label: releaseId,
    releaseId,
    ...(releaseDirectory ? { releaseDirectory } : {}),
    stagingRunId: null,
    hfRepo: "policyengine/populace-uk-private",
    hfCommitSha: commit,
    treeSchemaVersion: 6,
    indexSha256: "e".repeat(64),
    indexBytes: 10,
    createdAt: null,
    updatedAt: null,
  });
  let manifest = emptyCalibrationTreeManifest();
  for (const build of [
    manifestEntry(LINE, undefined, "9".repeat(40), "a"),
    manifestEntry(currentTag, LINE, "1".repeat(40), "b"),
  ]) {
    manifest = withCalibrationTreeManifestEntry(manifest, "uk", build);
  }
  const sharedFolderWithoutDiagnostics = {
    ...folderRelease(LINE, LINE),
    files: ["release_manifest.json"],
    has_calibration: false,
  };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (_input: string | URL | Request) =>
    new Response(null, { status: 503 })) as typeof fetch;
  try {
    const tagged = await loadPublishedCutReleases(
      0,
      "uk",
      manifest,
      [sharedFolderWithoutDiagnostics],
    );
    expect(
      mergeReleaseInventory([sharedFolderWithoutDiagnostics], tagged, currentTag)
        .map((release) => release.release_id),
    ).toEqual([currentTag]);
    // While the dashboard's latest release is the shared folder build, it is
    // listed once, as that published build.
    const whileLatest = mergeReleaseInventory([sharedFolderWithoutDiagnostics], tagged, LINE);
    expect(whileLatest.map((release) => [release.release_id, release.has_calibration])).toEqual([
      [currentTag, true],
      [LINE, true],
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a cut's investigation packet cites its line folder at the cut's commit", () => {
  const tag = `${LINE}-20261019T083323Z-0b0b0b0b`;
  const sha = "1".repeat(40);
  const cal: Calibration = {
    ...buildCalibration(
      {
        schema_version: 8,
        targets: [{
          name: "provider.target@2025",
          target_name: "provider.target",
          source: "Citation",
          target: 100,
          initial_estimate: 90,
          final_estimate: 100,
          hierarchy: {
            provider: { id: "provider", label: "Provider label" },
            category: { id: "provider.category", label: "Category label", provider_id: "provider" },
            geography: { id: "K02000001", label: "United Kingdom", level: "country" },
            dimensions: [],
            target: { id: "provider.target", label: "Target label" },
          },
        }],
      },
      tag,
      null,
      {},
      {},
      {},
      "uk",
      "huggingface_immutable",
      null,
      sha,
    ),
    release_directory: LINE,
  };
  const packet = latestMicrocosmTargetInvestigation(
    `https://dashboard.example/api/microcosm/target-investigation?target=${encodeURIComponent(String(cal.rows[0].name))}`,
    cal,
  );
  expect(packet).toMatchObject({
    available: true,
    release_id: tag,
    source_artifact: {
      hf_repo: microcosmRepo("uk"),
      hf_revision: sha,
      calibration_diagnostics_path: `releases/${LINE}/calibration_diagnostics.json`,
      build_manifest_path: `releases/${LINE}/build_manifest.json`,
      release_manifest_path: `releases/${LINE}/release_manifest.json`,
    },
  });
});

// Build folders (microcosm-emitter#24): every build in
// releases/<release>/<build>/, and releases.json as the only record of
// which builds exist and which is current. All values are synthetic.
const NATIONAL_OLDER = "uk-frs-calibration-attempt-20261014T214443Z-d12ff9b3";
const NATIONAL_CURRENT = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71";
const DENSE_BUILD = "uk-local-candidate-f100-s42-20261019T000432Z-ee5c2dbe";
const RECORDED_RELEASES = {
  schema_version: 1,
  default: LINE,
  releases: {
    [LINE]: {
      role: "national",
      current: NATIONAL_CURRENT,
      builds: {
        [NATIONAL_OLDER]: { published_at: "2026-10-14T21:44:43+00:00", withdrawn: true },
        [NATIONAL_CURRENT]: { published_at: "2026-10-19T08:33:23+00:00", withdrawn: false },
      },
    },
    [DENSE_LINE]: {
      role: "local_area",
      current: null,
      builds: {
        [DENSE_BUILD]: { published_at: "2026-10-19T07:53:18+00:00", withdrawn: false },
      },
    },
  },
};

function publishedBuildManifest(releaseIds: string[]) {
  return releaseIds.reduce(
    (manifest, releaseId, index) => withCalibrationTreeManifestEntry(manifest, "uk", {
      buildArtifactId: String(index + 1).repeat(64),
      kind: "release",
      sourceId: releaseId,
      label: releaseId,
      releaseId,
      stagingRunId: null,
      hfRepo: microcosmRepo("uk"),
      hfCommitSha: String(index + 1).repeat(40),
      treeSchemaVersion: 6,
      indexSha256: "e".repeat(64),
      indexBytes: 10,
      createdAt: null,
      updatedAt: null,
    }),
    emptyCalibrationTreeManifest(),
  );
}

async function withRecordedReleases<T>(
  record: unknown,
  run: (requested: string[]) => Promise<T>,
  recordResponse: () => Response = () => Response.json(record),
): Promise<T> {
  const originalFetch = globalThis.fetch;
  const requested: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    requested.push(url);
    if (url === hfResolveUrl("releases.json", "uk")) return recordResponse();
    throw new Error(`Unexpected URL ${url}`);
  }) as typeof fetch;
  try {
    return await run(requested);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

// The picker inventory of a repository that has releases.json.
async function recordedInventory(manifest: CalibrationTreeManifest) {
  const releases = await loadRecordedReleases(0, "uk", manifest);
  if (!releases) throw new Error("The fixture repository's releases.json was not read.");
  return releases;
}

test("the release picker lists every build releases.json lists, named by its folder, with its release's role and publication time", async () => {
  const manifest = publishedBuildManifest([
    `${LINE}/${NATIONAL_OLDER}`,
    `${LINE}/${NATIONAL_CURRENT}`,
    `${DENSE_LINE}/${DENSE_BUILD}`,
  ]);
  const releases = await withRecordedReleases(RECORDED_RELEASES, () =>
    loadRecordedReleases(0, "uk", manifest),
  );
  expect(releases).toEqual([
    {
      release_id: `${LINE}/${NATIONAL_CURRENT}`,
      date: "20261019T083323Z",
      files: [],
      has_calibration: true,
      dataset_role: "national",
      is_default: true,
      is_local_area: false,
      release: LINE,
      build: NATIONAL_CURRENT,
      withdrawn: false,
    },
    {
      release_id: `${DENSE_LINE}/${DENSE_BUILD}`,
      date: "20261019T075318Z",
      files: [],
      has_calibration: true,
      dataset_role: "local_area",
      is_default: false,
      is_local_area: true,
      release: DENSE_LINE,
      build: DENSE_BUILD,
      withdrawn: false,
    },
    {
      release_id: `${LINE}/${NATIONAL_OLDER}`,
      date: "20261014T214443Z",
      files: [],
      has_calibration: true,
      dataset_role: "national",
      is_default: true,
      is_local_area: false,
      release: LINE,
      build: NATIONAL_OLDER,
      // Listed and selectable; the picker labels it "Withdrawn".
      withdrawn: true,
    },
  ]);
});

test("a listed build counts as calibrated only once its calibration tree is in the dashboard manifest", async () => {
  const releases = await withRecordedReleases(RECORDED_RELEASES, () =>
    recordedInventory(publishedBuildManifest([`${LINE}/${NATIONAL_CURRENT}`])),
  );
  expect(Object.fromEntries(releases.map((release) => [release.release_id, release.has_calibration])))
    .toEqual({
      [`${LINE}/${NATIONAL_CURRENT}`]: true,
      [`${DENSE_LINE}/${DENSE_BUILD}`]: false,
      [`${LINE}/${NATIONAL_OLDER}`]: false,
    });
});

test("the release picker reads releases.json and nothing else from the dataset repository", async () => {
  const requested = await withRecordedReleases(RECORDED_RELEASES, async (requested) => {
    await loadRecordedReleases(0, "uk", emptyCalibrationTreeManifest());
    return requested;
  });
  expect(requested).toEqual([hfResolveUrl("releases.json", "uk")]);
});

test("the dashboard's latest release stays listed until reconciliation moves it to releases.json's current build", async () => {
  // Microcosm's migration has written releases.json, but the dashboard
  // manifest's latest release is still the tagged build published before
  // the switch. The deployment's pre-promotion check requires the latest
  // release to be listed and calibrated.
  const taggedBuild = `${LINE}-20261019T083323Z-6cba5d71`;
  const taggedEntry: CalibrationTreeManifestEntry = {
    buildArtifactId: "9".repeat(64),
    kind: "release",
    sourceId: taggedBuild,
    label: taggedBuild,
    releaseId: taggedBuild,
    releaseDirectory: LINE,
    stagingRunId: null,
    hfRepo: microcosmRepo("uk"),
    hfCommitSha: "9".repeat(40),
    treeSchemaVersion: 6,
    indexSha256: "e".repeat(64),
    indexBytes: 10,
    createdAt: "2026-10-19T10:21:22.000Z",
    updatedAt: "2026-10-19T10:21:22.000Z",
  };
  const recordedCurrent = `${LINE}/${NATIONAL_CURRENT}`;
  const beforeReconciliation = withCalibrationTreeManifestEntry(
    publishedBuildManifest([recordedCurrent]),
    "uk",
    taggedEntry,
    true,
  );
  const releases = await withRecordedReleases(RECORDED_RELEASES, () =>
    recordedInventory(beforeReconciliation),
  );
  expect(releases.map((release) => release.release_id)).toEqual([
    taggedBuild,
    recordedCurrent,
    `${DENSE_LINE}/${DENSE_BUILD}`,
    `${LINE}/${NATIONAL_OLDER}`,
  ]);
  expect(releases[0]).toEqual({
    release_id: taggedBuild,
    date: "20261019T102122Z",
    files: [],
    has_calibration: true,
    dataset_role: null,
    is_default: true,
    is_local_area: false,
    release_directory: LINE,
  });

  // Once the latest release is a build releases.json lists, only the
  // recorded builds are listed.
  const recordedCurrentEntry = beforeReconciliation.countries.uk?.builds.find(
    (build) => build.releaseId === recordedCurrent,
  );
  if (!recordedCurrentEntry) throw new Error("The recorded build is missing from the fixture.");
  const afterReconciliation = withCalibrationTreeManifestEntry(
    beforeReconciliation,
    "uk",
    recordedCurrentEntry,
    true,
  );
  const reconciled = await withRecordedReleases(RECORDED_RELEASES, () =>
    recordedInventory(afterReconciliation),
  );
  expect(reconciled.map((release) => release.release_id)).toEqual([
    recordedCurrent,
    `${DENSE_LINE}/${DENSE_BUILD}`,
    `${LINE}/${NATIONAL_OLDER}`,
  ]);
});

test("the release picker reads no record from a repository without releases.json", async () => {
  const requested = await withRecordedReleases(null, async (requested) => {
    await expect(loadRecordedReleases(0, "uk", emptyCalibrationTreeManifest())).resolves.toBeNull();
    return requested;
  }, missingReleasesRecord);
  // The route then lists the release folders and tagged builds instead.
  expect(requested).toEqual([hfResolveUrl("releases.json", "uk")]);
});

test("for the release picker, a 404 that says the revision is missing is an error, not a repository without releases.json", async () => {
  await withRecordedReleases(null, async () => {
    await expect(loadRecordedReleases(0, "uk", emptyCalibrationTreeManifest())).rejects.toThrow("404");
  }, missingRevision);
});

// The writer's ReleasesRecord.parse refuses a record over MAX_RECORD_BYTES
// (4 MiB), the size check_change keeps every record within.
test("the release picker refuses a releases.json larger than the release writer accepts", async () => {
  // Trailing whitespace keeps the record otherwise valid.
  const oversized = JSON.stringify(RECORDED_RELEASES) + " ".repeat(4 * 1024 * 1024);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    if (String(input) === hfResolveUrl("releases.json", "uk")) return new Response(oversized);
    throw new Error(`Unexpected URL ${String(input)}`);
  }) as typeof fetch;
  try {
    await expect(loadRecordedReleases(0, "uk", emptyCalibrationTreeManifest()))
      .rejects.toThrow("releases.json is larger than 4194304 bytes.");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("the release picker refuses a releases.json the release writer would refuse", async () => {
  const withdrawnCurrent = structuredClone(RECORDED_RELEASES);
  withdrawnCurrent.releases[LINE].builds[NATIONAL_CURRENT].withdrawn = true;
  await withRecordedReleases(withdrawnCurrent, async () => {
    await expect(loadRecordedReleases(0, "uk", emptyCalibrationTreeManifest()))
      .rejects.toThrow("is withdrawn");
  });
});

function nationalDiagnostics(releaseId: string) {
  return {
    schema_version: 8,
    release_id: releaseId,
    targets: [{
      name: "provider.target@2025",
      target_name: "provider.target",
      source: "Citation",
      target: 100,
      initial_estimate: 90,
      final_estimate: 100,
      hierarchy: {
        provider: { id: "provider", label: "Provider label" },
        category: { id: "provider.category", label: "Category label", provider_id: "provider" },
        geography: { id: "K02000001", label: "United Kingdom", level: "country" },
        dimensions: [],
        target: { id: "provider.target", label: "Target label" },
      },
    }],
  };
}

test("a build keeps its folder as its release id when its diagnostics name only its release", () => {
  const id = `${LINE}/${NATIONAL_CURRENT}`;
  const calibration = calibrationAsReleased(
    buildCalibration(nationalDiagnostics(LINE), id, null, {}, {}, {}, "uk"),
    id,
    id,
  );
  expect(calibration.release_id).toBe(id);
  expect(calibration.release_directory).toBe(id);

  // A release folder keeps the id its diagnostics carry, as before.
  const folder = buildCalibration(nationalDiagnostics("x"), "x", null, {}, {}, {}, "uk");
  expect(calibrationAsReleased(folder, "x", "x")).toBe(folder);
});

test("a build's investigation packet cites its own folder at its pinned commit", () => {
  const id = `${LINE}/${NATIONAL_CURRENT}`;
  const sha = "2".repeat(40);
  const cal = calibrationAsReleased(
    buildCalibration(nationalDiagnostics(LINE), id, null, {}, {}, {}, "uk", "huggingface_immutable", null, sha),
    id,
    id,
  );
  const packet = latestMicrocosmTargetInvestigation(
    `https://dashboard.example/api/microcosm/target-investigation?target=${encodeURIComponent(String(cal.rows[0].name))}`,
    cal,
  );
  expect(packet).toMatchObject({
    available: true,
    release_id: id,
    source_artifact: {
      hf_revision: sha,
      calibration_diagnostics_path: `releases/${id}/calibration_diagnostics.json`,
      release_manifest_path: `releases/${id}/release_manifest.json`,
    },
  });
});
