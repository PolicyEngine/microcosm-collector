// Golden calibration tree cases: representative builder inputs (US, UK and
// Belgium releases, US and UK staging runs, and US and UK comparisons between
// a release and a staging run) assembled from the fixtures the module tests
// already use, and a record of everything the builder writes for them. The
// record reduces each written part to its path, raw JSON digest and size,
// gzip size and deflate-stream digest, and adds the build ids, the manifest
// bytes, and what the readers return from the written bytes.
//
// fixtures/calibration-tree-golden.json is that record as produced by the
// builder before it moved into this package; the golden tests require the
// builder to reproduce it exactly. The cases import the builder through the
// package's public entry points. Regenerate the record only for an intended
// output change, from the package directory:
//
//   bun run src/test-support/calibration-tree-golden-cases.ts
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { gunzipSync } from "node:zlib";

import {
  loadCalibrationComparisonSource,
  loadCalibrationComparisonTargetDetail,
} from "@policyengine/calibration-tree/calibration-comparison-blob";
import { buildCalibrationComparisonBundles } from "@policyengine/calibration-tree/calibration-comparison-bundle";
import { createExplorerState, type ExplorerState } from "@policyengine/calibration-tree/calibration-explorer";
import {
  CALIBRATION_TREE_SCHEMA_VERSION,
  calibrationTreePartPath,
  calibrationTreeResponseFromBundle,
  calibrationTreeTargetsFromSummaries,
  parseCalibrationTreeIndex,
  parseCalibrationTreePart,
  type CalibrationTreeFilterIndexArtifact,
  type CalibrationTreeSourceArtifact,
  type CalibrationTreeTargetDetailsArtifact,
  type CalibrationTreeTargetSummaryArtifact,
  type CalibrationTreeTierArtifact,
} from "@policyengine/calibration-tree/calibration-tree-artifact";
import type { getCalibrationTreeBlob } from "@policyengine/calibration-tree/calibration-tree-blob";
import {
  buildCalibrationTreeBundle,
  type CalibrationTreeBundle,
  type CalibrationTreeBundleFile,
} from "@policyengine/calibration-tree/calibration-tree-bundle";
import {
  emptyCalibrationTreeManifest,
  serializeCalibrationTreeManifest,
  withCalibrationTreeManifestEntry,
  type CalibrationTreeManifestEntry,
} from "@policyengine/calibration-tree/calibration-tree-manifest";
import {
  COUNTRY_REGISTRY,
  type CountryRegistration,
  type MicrocosmCountry,
} from "@policyengine/calibration-tree/countries";
import {
  UK_LOCAL_CALIBRATION_DIR_ENV,
  buildCalibration,
  microcosmRepo,
  type Calibration,
} from "@policyengine/calibration-tree/latest-artifact";
import {
  resolveStagingCalibrationSource,
  stagingRepository,
} from "@policyengine/calibration-tree/staging-artifact";
import {
  TARGET_LOSS_BASIS_HASH_ALGORITHM,
  TARGET_LOSS_FORMULA,
  targetLossBasisHash,
} from "@policyengine/calibration-tree/target-loss-attribution";

type JsonObject = Record<string, unknown>;

const FIXTURES = join(import.meta.dir, "..", "fixtures");
export const CALIBRATION_TREE_GOLDEN_RECORD_PATH = join(FIXTURES, "calibration-tree-golden.json");

// gzipSync writes a fixed ten-byte header: magic, deflate, no flags, zero
// mtime, maximum-compression flag, then the operating-system byte, which
// zlib sets per platform (3 on Linux, 19 on macOS). Everything after it is
// the deflate stream and the CRC-32 and size trailer.
export const GZIP_HEADER_WITHOUT_OS = [0x1f, 0x8b, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02];
const GZIP_HEADER_BYTES = 10;

export interface GoldenFileRecord {
  part: string;
  path: string;
  sha256: string;
  rawBytes: number;
  gzipBytes: number;
  deflateSha256: string;
}

export interface GoldenBundleRecord {
  buildArtifactId: string;
  files: GoldenFileRecord[];
}

export interface GoldenSourceRecord extends GoldenBundleRecord {
  readers: {
    programRootSha256: string;
    geographyRootSha256: string;
  };
}

export interface GoldenComparisonRecord {
  pairArtifactId: string;
  reported: GoldenBundleRecord;
  shared: GoldenBundleRecord;
  firstTargetDetailSha256: string;
}

export const GOLDEN_SOURCE_CASES = [
  "us-release",
  "uk-release",
  "uk-release-loss-attribution",
  "be-release",
  "us-staging",
  "uk-staging",
  "uk-staging-staged-bundle",
] as const;
export type GoldenSourceCaseName = (typeof GOLDEN_SOURCE_CASES)[number];

export const GOLDEN_COMPARISON_CASES = ["us-comparison", "uk-comparison"] as const;
export type GoldenComparisonCaseName = (typeof GOLDEN_COMPARISON_CASES)[number];

export interface CalibrationTreeGoldenRecord {
  schemaVersion: typeof CALIBRATION_TREE_SCHEMA_VERSION;
  sources: Record<GoldenSourceCaseName, GoldenSourceRecord>;
  comparisons: Record<GoldenComparisonCaseName, GoldenComparisonRecord>;
  manifest: { sha256: string; rawBytes: number };
}

export interface GoldenReleaseInput {
  country: MicrocosmCountry;
  releaseId: string;
  hfCommitSha: string;
  publishedAt: string;
  diagnosticsText: string;
  releaseManifestText: string | null;
}

interface GoldenSourceBuild {
  country: MicrocosmCountry;
  bundle: CalibrationTreeBundle;
  manifestEntry: CalibrationTreeManifestEntry;
}

function sha256Text(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function sha256Bytes(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function fixtureText(relativePath: string): string {
  return readFileSync(join(FIXTURES, relativePath), "utf8");
}

function compressedFixtureText(relativePath: string): string {
  return gunzipSync(readFileSync(join(FIXTURES, relativePath))).toString("utf8");
}

// --- Inputs ---------------------------------------------------------------

const US_RELEASE_ID = "populace-us-2024-buildp-sparse-rmloss100-cae8640-20260728T011454Z";
const UK_RELEASE_ID = "microcosm-uk-2024-25-national-golden";
const UK_LOSS_ATTRIBUTION_RELEASE_ID = "microcosm-uk-2024-25-loss-attribution-golden";
const BE_RELEASE_ID = "microcosm-be-2024-national-golden";

const US_STAGING_RUN_ID = "us-calibration-golden-20260930T120000Z";
const UK_STAGING_RUN_ID = "uk-calibration-golden-20261002T231556Z";
const UK_STAGED_BUNDLE_RUN_ID = "uk-frs-calibration-attempt-20261003T081500Z-golden";

const US_STAGING_REVISION = "5c2a9e1d7b3f4a6c8e0d2b4f6a8c0e2d4b6f8a0c";
const UK_STAGING_REVISION = "8e4b2d6f0a1c3e5a7c9e1b3d5f7a9c1e3b5d7f9a";
const UK_STAGED_BUNDLE_REVISION = "40439a3f8d241cd361bb693488b42e7258623a00";

// Assign each identity-only legacy target a deterministic target and
// estimates, so the tree carries real fit bands and errors.
function withLegacyValues(targets: JsonObject[]): JsonObject[] {
  return targets.map((target, targetIndex) => {
    const targetValue = 1_000 * (1 + (targetIndex % 97));
    return {
      ...target,
      target: targetValue,
      initial_estimate: targetValue * (1 + ((targetIndex % 31) - 15) / 50),
      final_estimate: targetValue * (1 + ((targetIndex % 23) - 11) / 100),
    };
  });
}

// Attach the weighted target-error attribution Microcosm reports from
// diagnostics schema 6 on: provided weights and scales, each target's capped
// scaled error and loss contribution, and the basis digest the reader
// verifies. Comparisons are built only between builds that report it.
function withReportedLossAttribution(diagnostics: JsonObject): JsonObject {
  const cap = 1;
  const weighted = (diagnostics.targets as JsonObject[]).map((target, targetIndex) => ({
    ...target,
    name: String(target.name),
    target: Number(target.target),
    final_estimate: Number(target.final_estimate),
    target_loss_weight: 1 + (targetIndex % 4),
    target_loss_scale: Math.max(Math.abs(Number(target.target)), 1),
  }));
  const totalWeight = weighted.reduce((sum, target) => sum + target.target_loss_weight, 0);
  const targets = weighted.map((target) => {
    const cappedError = Math.min(
      Math.abs(target.final_estimate - target.target) / target.target_loss_scale,
      cap,
    );
    const share = target.target_loss_weight / totalWeight;
    return {
      ...target,
      target_loss_weight_share: share,
      final_capped_scaled_error: cappedError,
      final_loss_contribution: share * cappedError,
    };
  });
  return {
    ...diagnostics,
    final_loss: targets.reduce((sum, target) => sum + target.final_loss_contribution, 0),
    diagnostic_warnings: [],
    target_loss_basis: {
      formula: TARGET_LOSS_FORMULA,
      cap,
      target_count: targets.length,
      total_target_weight: totalWeight,
      weight_kind: "provided",
      scale_kind: "provided",
      hash_algorithm: TARGET_LOSS_BASIS_HASH_ALGORITHM,
      sha256: targetLossBasisHash(targets.map((target) => ({
        name: target.name,
        target_loss_weight: target.target_loss_weight,
        target_loss_scale: target.target_loss_scale,
      }))),
    },
    targets,
  };
}

function usLegacyTargets(): JsonObject[] {
  const fixture = JSON.parse(compressedFixtureText("us-legacy-target-identities.json.gz")) as {
    targets: JsonObject[];
  };
  return withLegacyValues(fixture.targets);
}

// Schema 6 keeps the legacy target reader and adds reported attribution.
function usReleaseDiagnostics(): JsonObject {
  return withReportedLossAttribution({ schema_version: 6, targets: usLegacyTargets() });
}

// The four structured UK geographies and two receipts lines used by the
// schema 7 and schema 8 reader tests.
const UK_GEOGRAPHIES = [
  { id: "K02000001", label: "United Kingdom" },
  { id: "K03000001", label: "Great Britain" },
  { id: "E92000001", label: "England" },
  { id: "S92000003", label: "Scotland" },
];
const UK_RECEIPT_LINES = [
  { id: "income_tax", label: "Income tax receipts" },
  { id: "national_insurance", label: "National insurance contributions" },
];

function ukHierarchyTarget(options: {
  name: string;
  provider: { id: string; label: string };
  category: { id: string; label: string };
  geography: { id: string; label: string };
  dimension: { id: string; label: string } | null;
  targetLabel: string;
  target: number;
  initialEstimate: number;
  finalEstimate: number;
}): JsonObject {
  return {
    name: `${options.name}@2025`,
    target_name: options.name,
    source: `${options.provider.label} citation`,
    target: options.target,
    initial_estimate: options.initialEstimate,
    final_estimate: options.finalEstimate,
    hierarchy: {
      provider: options.provider,
      category: { ...options.category, provider_id: options.provider.id },
      geography: { ...options.geography, level: "country" },
      dimensions: options.dimension
        ? [{
            id: `${options.category.id}_line`,
            label: "Economic and fiscal outlook line",
            value_id: options.dimension.id,
            value_label: options.dimension.label,
          }]
        : [],
      target: { id: options.name, label: options.targetLabel },
    },
  };
}

function ukHierarchyTargets(): JsonObject[] {
  const obr = { id: "obr", label: "Office for Budget Responsibility" };
  const ons = { id: "ons", label: "Office for National Statistics" };
  const receipts = { id: "obr.efo_receipts", label: "Economic and fiscal outlook receipts" };
  const population = { id: "ons.population", label: "Population estimates" };
  const targets: JsonObject[] = [];
  UK_GEOGRAPHIES.forEach((geography, geographyIndex) => {
    UK_RECEIPT_LINES.forEach((line, lineIndex) => {
      const targetValue = 1_000 * (geographyIndex + 1) + 250 * lineIndex;
      targets.push(ukHierarchyTarget({
        name: `obr.${line.id}.${geography.id}`,
        provider: obr,
        category: receipts,
        geography,
        dimension: line,
        targetLabel: `${line.label}, ${geography.label}`,
        target: targetValue,
        initialEstimate: targetValue * 0.9,
        finalEstimate: targetValue * (1 + 0.004 * (geographyIndex - lineIndex)),
      }));
    });
    const populationValue = 5_000_000 * (geographyIndex + 1);
    targets.push(ukHierarchyTarget({
      name: `ons.population.${geography.id}`,
      provider: ons,
      category: population,
      geography,
      dimension: null,
      targetLabel: `Population, ${geography.label}`,
      target: populationValue,
      initialEstimate: populationValue * 1.08,
      finalEstimate: populationValue * (1 - 0.0000001 * geographyIndex),
    }));
  });
  return targets;
}

function ukHierarchyDiagnostics(targets: JsonObject[]): JsonObject {
  return withReportedLossAttribution({ schema_version: 8, release_id: UK_RELEASE_ID, targets });
}

// The candidate a later staging run would produce: every receipts estimate
// moves, Scotland's population target is dropped and Wales's is added.
function ukStagingTargets(): JsonObject[] {
  const moved: JsonObject[] = ukHierarchyTargets()
    .filter((target) => target.target_name !== "ons.population.S92000003")
    .map((target, targetIndex) => ({
      ...target,
      final_estimate: Number(target.final_estimate) * (1 + 0.01 * ((targetIndex % 5) - 2)),
    }));
  const wales = { id: "W92000004", label: "Wales" };
  moved.push(ukHierarchyTarget({
    name: `ons.population.${wales.id}`,
    provider: { id: "ons", label: "Office for National Statistics" },
    category: { id: "ons.population", label: "Population estimates" },
    geography: wales,
    dimension: null,
    targetLabel: `Population, ${wales.label}`,
    target: 3_100_000,
    initialEstimate: 3_200_000,
    finalEstimate: 3_099_000,
  }));
  return moved;
}

// The candidate a later US staging run would produce: the first 1,500
// release targets, every estimate moved.
function usStagingDiagnostics(): JsonObject {
  const targets = usLegacyTargets().slice(0, 1_500).map((target, targetIndex) => ({
    ...target,
    final_estimate: Number(target.final_estimate) * (1 + 0.003 * ((targetIndex % 7) - 3)),
  }));
  return withReportedLossAttribution({ schema_version: 6, targets });
}

export function goldenReleaseInputs(): Record<
  "us-release" | "uk-release" | "uk-release-loss-attribution" | "be-release",
  GoldenReleaseInput
> {
  return {
    "us-release": {
      country: "us",
      releaseId: US_RELEASE_ID,
      hfCommitSha: "7d3e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e",
      publishedAt: "2026-07-28T01:14:54.000Z",
      diagnosticsText: JSON.stringify(usReleaseDiagnostics()),
      releaseManifestText: null,
    },
    "uk-release": {
      country: "uk",
      releaseId: UK_RELEASE_ID,
      hfCommitSha: "f3a5c7e9b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9a1",
      publishedAt: "2026-10-04T18:57:22.000Z",
      diagnosticsText: JSON.stringify(ukHierarchyDiagnostics(ukHierarchyTargets())),
      releaseManifestText: JSON.stringify({ country: { code: "uk" } }),
    },
    "uk-release-loss-attribution": {
      country: "uk",
      releaseId: UK_LOSS_ATTRIBUTION_RELEASE_ID,
      hfCommitSha: "a9c1e3b5d7f9a1c3e5b7d9f1a3c5e7b9d1f3a5c7",
      publishedAt: "2026-10-06T09:30:00.000Z",
      diagnosticsText: fixtureText("target-loss-attribution/valid-uniform.json"),
      releaseManifestText: null,
    },
    "be-release": {
      country: "be",
      releaseId: BE_RELEASE_ID,
      hfCommitSha: "b2d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c4e6b8d0",
      publishedAt: "2026-09-16T12:00:00.000Z",
      diagnosticsText: fixtureText("be-release/calibration_diagnostics.json"),
      releaseManifestText: fixtureText("be-release/release_manifest.json"),
    },
  };
}

// --- Builder, as the publisher drives it -----------------------------------

function bundleComparison(calibration: Calibration) {
  return {
    releaseId: calibration.release_id,
    calibrationProvenance: calibration.calibration_provenance,
    status: calibration.target_loss_attribution.status,
    aggregate: calibration.target_loss_attribution.aggregate,
    cap: calibration.target_loss_attribution.cap,
    basisIdentifier: calibration.target_loss_attribution.basis_identifier,
    targetRepresentation: calibration.target_schema.target_representation,
  };
}

function indexFile(bundle: CalibrationTreeBundle): CalibrationTreeBundleFile {
  const file = bundle.files.find((candidate) => candidate.part === "index");
  if (!file) throw new Error("Calibration tree bundle has no index.");
  return file;
}

// Repository overrides and the UK local-run directory change which inputs
// the builder reads; the record uses each country's registered defaults.
function repositoryEnvironmentNames(): string[] {
  const names = new Set<string>([UK_LOCAL_CALIBRATION_DIR_ENV]);
  for (const registration of Object.values(COUNTRY_REGISTRY) as CountryRegistration[]) {
    for (const name of [
      registration.repo_env,
      registration.revision_env,
      registration.staging?.repo_env,
      registration.staging?.revision_env,
    ]) {
      if (name) names.add(name);
    }
  }
  return [...names];
}

// Scopes that overlap, such as the two sides of a comparison that
// `goldenComparisonRecord` builds under `Promise.all`, share one cleared
// environment: the first scope to start saves and clears the variables, and
// the last one to finish restores them. Scopes may also nest.
let registeredRepositoryScopeCount = 0;
let environmentOutsideRegisteredRepositories = new Map<string, string | undefined>();

export async function withRegisteredRepositories<Result>(
  operation: () => Promise<Result>,
): Promise<Result> {
  if (registeredRepositoryScopeCount === 0) {
    environmentOutsideRegisteredRepositories = new Map(
      repositoryEnvironmentNames().map((name) => [name, process.env[name]]),
    );
    for (const name of environmentOutsideRegisteredRepositories.keys()) delete process.env[name];
  }
  registeredRepositoryScopeCount += 1;
  try {
    return await operation();
  } finally {
    registeredRepositoryScopeCount -= 1;
    if (registeredRepositoryScopeCount === 0) {
      for (const [name, value] of environmentOutsideRegisteredRepositories) {
        if (value == null) delete process.env[name];
        else process.env[name] = value;
      }
    }
  }
}

// Each scope replaces `globalThis.fetch` with a reader of its own files, so
// scopes that overlap run one after another: a scope starts only after every
// earlier scope has finished and put back the fetch it replaced. A scope must
// not start another inside itself, because the inner one would wait forever.
let huggingFaceFileScopesBefore: Promise<void> = Promise.resolve();

export async function withHuggingFaceFiles<Result>(
  files: Map<string, string>,
  operation: () => Promise<Result>,
): Promise<Result> {
  const earlierScopesFinished = huggingFaceFileScopesBefore;
  let finishThisScope = () => {};
  huggingFaceFileScopesBefore = new Promise<void>((resolve) => {
    finishThisScope = resolve;
  });
  await earlierScopesFinished;
  const fetchOutsideScope = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const body = files.get(String(input));
    return body == null
      ? new Response(null, { status: 404 })
      : new Response(body, { headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  try {
    return await operation();
  } finally {
    globalThis.fetch = fetchOutsideScope;
    finishThisScope();
  }
}

// `diagnosticsJson` replaces the parsed document while every identity input
// (the source digest, and so the build ID) stays the recorded file's.
function buildReleaseSource(
  input: GoldenReleaseInput,
  diagnosticsJson: JsonObject = JSON.parse(input.diagnosticsText) as JsonObject,
): GoldenSourceBuild {
  const prefix = `releases/${input.releaseId}`;
  const diagnostics = {
    json: diagnosticsJson,
    source: {
      path: `${prefix}/calibration_diagnostics.json`,
      sha256: sha256Text(input.diagnosticsText),
    } satisfies CalibrationTreeSourceArtifact,
  };
  const releaseManifest = input.releaseManifestText == null
    ? null
    : {
        json: JSON.parse(input.releaseManifestText) as JsonObject,
        source: {
          path: `${prefix}/release_manifest.json`,
          sha256: sha256Text(input.releaseManifestText),
        } satisfies CalibrationTreeSourceArtifact,
      };
  const calibration = buildCalibration(
    diagnostics.json,
    input.releaseId,
    null,
    {},
    releaseManifest?.json ?? {},
    {},
    input.country,
    "huggingface_immutable",
    diagnostics.source.sha256,
    input.hfCommitSha,
  );
  const bundle = buildCalibrationTreeBundle({
    country: input.country,
    createdAt: input.publishedAt,
    releaseId: input.releaseId,
    hfRepo: microcosmRepo(input.country),
    hfCommitSha: input.hfCommitSha,
    sourceArtifacts: {
      calibrationDiagnostics: diagnostics.source,
      buildManifest: null,
      releaseManifest: releaseManifest?.source ?? null,
      demographics: null,
    },
    rows: calibration.rows,
    calibrationProvenance: calibration.calibration_provenance,
    lossAttributionAvailable: calibration.target_loss_attribution.status !== "unavailable",
    comparison: bundleComparison(calibration),
  });
  const build = bundle.index.build;
  const index = indexFile(bundle);
  return {
    country: input.country,
    bundle,
    manifestEntry: {
      buildArtifactId: build.buildArtifactId,
      kind: "release",
      sourceId: build.sourceId,
      label: build.label,
      releaseId: input.releaseId,
      stagingRunId: null,
      hfRepo: microcosmRepo(input.country),
      hfCommitSha: input.hfCommitSha,
      treeSchemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
      indexSha256: index.sha256,
      indexBytes: index.rawBytes,
      createdAt: build.createdAt,
      updatedAt: build.createdAt,
    },
  };
}

async function buildStagingSource(options: {
  country: MicrocosmCountry;
  runId: string;
  revisionSha: string;
  files: Map<string, string>;
}): Promise<GoldenSourceBuild> {
  const repository = stagingRepository(options.country);
  if (!repository) throw new Error(`Staging is not configured for ${options.country}.`);
  const source = await withHuggingFaceFiles(options.files, () =>
    resolveStagingCalibrationSource(options.runId, 0, options.country, options.revisionSha)
  );
  if (!source) throw new Error(`Staging run ${options.runId} has no calibration diagnostics.`);
  const { calibration } = source;
  const bundle = buildCalibrationTreeBundle({
    country: options.country,
    buildKind: "staging",
    sourceId: options.runId,
    label: source.candidateReleaseId,
    createdAt: source.updatedAt,
    releaseId: source.candidateReleaseId,
    hfRepo: repository.repo,
    hfCommitSha: options.revisionSha,
    sourceArtifacts: { ...source.sourceArtifacts, demographics: null },
    rows: calibration.rows,
    calibrationProvenance: calibration.calibration_provenance,
    lossAttributionAvailable: calibration.target_loss_attribution.status !== "unavailable",
    comparison: bundleComparison(calibration),
  });
  const index = indexFile(bundle);
  return {
    country: options.country,
    bundle,
    manifestEntry: {
      buildArtifactId: bundle.index.buildArtifactId,
      kind: "staging",
      sourceId: options.runId,
      label: source.candidateReleaseId,
      releaseId: null,
      stagingRunId: options.runId,
      hfRepo: repository.repo,
      hfCommitSha: options.revisionSha,
      treeSchemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
      indexSha256: index.sha256,
      indexBytes: index.rawBytes,
      createdAt: source.updatedAt,
      updatedAt: source.updatedAt,
    },
  };
}

function stagingFileUrl(country: MicrocosmCountry, revision: string, path: string): string {
  return `https://huggingface.co/datasets/${stagingRepository(country)!.repo}/resolve/${revision}/${path}`;
}

// A version 1 run: progress names the candidate and the diagnostics sit at
// the legacy run path.
function usVersion1StagingFiles(): Map<string, string> {
  const runId = US_STAGING_RUN_ID;
  const url = (path: string) => stagingFileUrl("us", US_STAGING_REVISION, `runs/${runId}/${path}`);
  return new Map([
    [url("progress.json"), JSON.stringify({
      schema_version: 1,
      run_id: runId,
      candidate_release_id: "populace-us-2024-golden-candidate",
      updated_at: "2026-09-30T12:00:00+00:00",
    })],
    [url("run_manifest.json"), JSON.stringify({ schema_version: 1, run_id: runId })],
    [url("calibration_diagnostics.json"), JSON.stringify(usStagingDiagnostics())],
  ]);
}

function version2RunManifest(
  runId: string,
  updatedAt: string,
  artifacts: JsonObject[],
): JsonObject {
  return {
    schema_name: "microcosm.staging.run-manifest",
    schema_version: 2,
    run_id: runId,
    country_code: "GB",
    operation_id: "uk_national_calibration",
    pipeline: { id: "uk_national_calibration", version: "2026.09" },
    candidate_id: `${runId}-candidate`,
    release_id: null,
    run_kind: "calibration",
    non_release: true,
    started_at: "2026-10-02T20:00:00+00:00",
    updated_at: updatedAt,
    status: "completed",
    current_stage: "complete",
    sample: { mode: "full" },
    delivery: {
      contract_version: 2,
      enabled: true,
      mode: "local_only",
      run_id: runId,
      configured_repository: null,
      upload_attempts: 0,
      upload_successes: 0,
      read_back: "not_requested",
      last_error_code: null,
      opt_out_reason: null,
    },
    artifacts,
    failure: null,
    paths: {
      progress: `runs/${runId}/progress.json`,
      events: `runs/${runId}/events.ndjson`,
      calibration_progress: null,
    },
  };
}

// A version 2 run that uploaded its diagnostics with its telemetry.
function ukVersion2StagingFiles(
  runId: string = UK_STAGING_RUN_ID,
  updatedAt = "2026-10-02T23:15:56+00:00",
): Map<string, string> {
  const diagnosticsText = JSON.stringify(ukHierarchyDiagnostics(ukStagingTargets()));
  const manifest = version2RunManifest(runId, updatedAt, [{
    logical_name: "calibration_diagnostics",
    artifact_kind: "aggregate_diagnostics",
    contract_relative_path: "artifacts/calibration_diagnostics.json",
    media_type: "application/json",
    sha256: sha256Text(diagnosticsText),
    classification: "aggregate",
  }]);
  const url = (path: string) => stagingFileUrl("uk", UK_STAGING_REVISION, `runs/${runId}/${path}`);
  return new Map([
    [url("run_manifest.json"), JSON.stringify(manifest)],
    [url("artifacts/calibration_diagnostics.json"), diagnosticsText],
  ]);
}

// The UK staging repository at the recorded revision as the publisher's
// staging inventory reads it: its runs tree listing and, for each run, the
// recorded UK version 2 run's manifest and diagnostics under that run's id.
export function ukStagingRepositoryFiles(
  runs: ReadonlyArray<{ runId: string; updatedAt: string }>,
): { revisionSha: string; files: Map<string, string> } {
  const files = new Map<string, string>();
  const listing: JsonObject[] = [];
  for (const { runId, updatedAt } of runs) {
    for (const [url, body] of ukVersion2StagingFiles(runId, updatedAt)) files.set(url, body);
    listing.push(
      { type: "file", path: `runs/${runId}/run_manifest.json` },
      { type: "file", path: `runs/${runId}/artifacts/calibration_diagnostics.json` },
    );
  }
  const repository = stagingRepository("uk")!.repo;
  files.set(
    `https://huggingface.co/api/datasets/${repository}/tree/${UK_STAGING_REVISION}/runs?recursive=true`,
    JSON.stringify(listing),
  );
  return { revisionSha: UK_STAGING_REVISION, files };
}

// A version 2 run whose diagnostics travel in its staged dataset bundle in
// the release repository, located by the staged-dataset receipt.
function ukStagedBundleStagingFiles(): Map<string, string> {
  const runId = UK_STAGED_BUNDLE_RUN_ID;
  const diagnosticsText = JSON.stringify(ukHierarchyDiagnostics(ukStagingTargets()));
  const releaseRepository = COUNTRY_REGISTRY.uk.repo;
  const receiptText = JSON.stringify({
    contract_version: 1,
    run_id: runId,
    status: "uploaded",
    mode: "local_and_remote",
    repository: releaseRepository,
    prefix: `staged/${runId}`,
    revision: UK_STAGED_BUNDLE_REVISION,
    error_code: null,
    opt_out_reason: null,
    files: {
      "calibration_diagnostics.json": {
        bytes: Buffer.byteLength(diagnosticsText, "utf8"),
        sha256: sha256Text(diagnosticsText),
      },
      "microcosm_uk_2024_25.h5": { bytes: 1, sha256: "0".repeat(64) },
    },
  });
  const manifest = version2RunManifest(runId, "2026-10-03T08:30:00+00:00", [{
    logical_name: "staged_dataset",
    artifact_kind: "build_metadata",
    contract_relative_path: "artifacts/staged_dataset.json",
    media_type: "application/json",
    sha256: sha256Text(receiptText),
    classification: "non_row_level",
  }]);
  const url = (path: string) => stagingFileUrl("uk", UK_STAGING_REVISION, `runs/${runId}/${path}`);
  return new Map([
    [url("run_manifest.json"), JSON.stringify(manifest)],
    [url("artifacts/staged_dataset.json"), receiptText],
    [
      `https://huggingface.co/datasets/${releaseRepository}/resolve/${UK_STAGED_BUNDLE_REVISION}/staged/${runId}/calibration_diagnostics.json`,
      diagnosticsText,
    ],
  ]);
}

const sourceBuilds = new Map<GoldenSourceCaseName, Promise<GoldenSourceBuild>>();

function buildSourceCase(name: GoldenSourceCaseName): Promise<GoldenSourceBuild> {
  return withRegisteredRepositories(async () => {
    switch (name) {
      case "us-release":
      case "uk-release":
      case "uk-release-loss-attribution":
      case "be-release":
        return buildReleaseSource(goldenReleaseInputs()[name]);
      case "us-staging":
        return buildStagingSource({
          country: "us",
          runId: US_STAGING_RUN_ID,
          revisionSha: US_STAGING_REVISION,
          files: usVersion1StagingFiles(),
        });
      case "uk-staging":
        return buildStagingSource({
          country: "uk",
          runId: UK_STAGING_RUN_ID,
          revisionSha: UK_STAGING_REVISION,
          files: ukVersion2StagingFiles(),
        });
      case "uk-staging-staged-bundle":
        return buildStagingSource({
          country: "uk",
          runId: UK_STAGED_BUNDLE_RUN_ID,
          revisionSha: UK_STAGING_REVISION,
          files: ukStagedBundleStagingFiles(),
        });
    }
  });
}

export function goldenSourceBuild(name: GoldenSourceCaseName): Promise<GoldenSourceBuild> {
  let build = sourceBuilds.get(name);
  if (!build) {
    build = buildSourceCase(name);
    sourceBuilds.set(name, build);
  }
  return build;
}

// --- Record ---------------------------------------------------------------

function fileRecord(file: CalibrationTreeBundleFile): GoldenFileRecord {
  return {
    part: file.part,
    path: file.path,
    sha256: file.sha256,
    rawBytes: file.rawBytes,
    gzipBytes: file.gzipBytes,
    deflateSha256: sha256Bytes(file.compressed.subarray(GZIP_HEADER_BYTES)),
  };
}

export function bundleRecord(bundle: CalibrationTreeBundle): GoldenBundleRecord {
  return {
    buildArtifactId: bundle.index.buildArtifactId,
    files: bundle.files.map(fileRecord),
  };
}

// Stored bundle files by Blob path, served the way the Blob client returns them.
type StoredFiles = Map<string, Uint8Array>;

function storeBundle(storedFiles: StoredFiles, bundle: CalibrationTreeBundle): void {
  for (const file of bundle.files) storedFiles.set(file.path, file.compressed);
}

function storedJson(storedFiles: StoredFiles, path: string): unknown {
  const compressed = storedFiles.get(path);
  if (!compressed) throw new Error(`Stored calibration tree file ${path} is missing.`);
  return JSON.parse(gunzipSync(compressed).toString("utf8"));
}

function storedBlobReader(storedFiles: StoredFiles): typeof getCalibrationTreeBlob {
  return (async (options: Parameters<typeof getCalibrationTreeBlob>[0]) => {
    const path = calibrationTreePartPath(options.country, options.buildArtifactId, options.part);
    const compressed = storedFiles.get(path);
    if (!compressed) return null;
    return {
      statusCode: 200 as const,
      stream: new Blob([new Uint8Array(compressed)]).stream(),
      headers: new Headers(),
      blob: {
        url: `https://blob.example/${path}`,
        downloadUrl: `https://blob.example/${path}?download=1`,
        pathname: path,
        contentDisposition: "inline",
        cacheControl: "public, max-age=31536000",
        uploadedAt: new Date("2026-10-15T00:00:00.000Z"),
        etag: `"${sha256Bytes(compressed)}"`,
        contentType: "application/gzip",
        size: compressed.byteLength,
      },
    };
  }) as typeof getCalibrationTreeBlob;
}

// The explorer's root responses, read back from the stored bytes the way the
// dashboard loads a published tree.
function readerRecord(bundle: CalibrationTreeBundle): GoldenSourceRecord["readers"] {
  const storedFiles: StoredFiles = new Map();
  storeBundle(storedFiles, bundle);
  const index = parseCalibrationTreeIndex(
    storedJson(storedFiles, calibrationTreePartPath(bundle.index.country, bundle.index.buildArtifactId, "index")),
  );
  const part = (descriptor: { part: Parameters<typeof parseCalibrationTreePart>[1]; path: string }) =>
    parseCalibrationTreePart(storedJson(storedFiles, descriptor.path), descriptor.part);
  const loaded = {
    index,
    tiers: index.parts.tiers.map((descriptor) => part(descriptor) as CalibrationTreeTierArtifact),
    filterIndex: part(index.parts.filterIndex) as CalibrationTreeFilterIndexArtifact,
    targetSummaries: calibrationTreeTargetsFromSummaries(
      index,
      index.parts.targetSummaries.map(
        (descriptor) => part(descriptor) as CalibrationTreeTargetSummaryArtifact,
      ),
    ),
    targetDetailShard: index.parts.targetDetails[0]
      ? part(index.parts.targetDetails[0]) as CalibrationTreeTargetDetailsArtifact
      : undefined,
  };
  const rootResponse = (breakdown: ExplorerState["breakdown"]) =>
    sha256Text(JSON.stringify(
      calibrationTreeResponseFromBundle(loaded, { ...createExplorerState(), breakdown }),
    ));
  return {
    programRootSha256: rootResponse("program"),
    geographyRootSha256: rootResponse("geography"),
  };
}

export async function goldenSourceRecord(name: GoldenSourceCaseName): Promise<GoldenSourceRecord> {
  const { bundle } = await goldenSourceBuild(name);
  return { ...bundleRecord(bundle), readers: readerRecord(bundle) };
}

/**
 * A recorded release case compiled from another diagnostics document under
 * the recorded file's identity: the same source digest, build ID, commit and
 * publication time. It shows whether that document builds the recorded tree
 * byte for byte. The build ID and the recorded source digest are derived from
 * the file's bytes, so a document with other bytes would otherwise get
 * another build ID.
 */
export function goldenReleaseRecordFromDocument(
  name: "us-release" | "uk-release" | "uk-release-loss-attribution" | "be-release",
  diagnosticsJson: JsonObject,
): Promise<GoldenSourceRecord> {
  return withRegisteredRepositories(async () => {
    const { bundle } = buildReleaseSource(goldenReleaseInputs()[name], diagnosticsJson);
    return { ...bundleRecord(bundle), readers: readerRecord(bundle) };
  });
}

const COMPARISON_SOURCES: Record<
  GoldenComparisonCaseName,
  { current: GoldenSourceCaseName; candidate: GoldenSourceCaseName }
> = {
  "us-comparison": { current: "us-release", candidate: "us-staging" },
  "uk-comparison": { current: "uk-release", candidate: "uk-staging" },
};

// Comparisons are built the way the dashboard builds them lazily: both
// sources are read back from their stored bytes.
export async function goldenComparisonRecord(
  name: GoldenComparisonCaseName,
): Promise<GoldenComparisonRecord> {
  const [current, candidate] = await Promise.all([
    goldenSourceBuild(COMPARISON_SOURCES[name].current),
    goldenSourceBuild(COMPARISON_SOURCES[name].candidate),
  ]);
  const storedFiles: StoredFiles = new Map();
  storeBundle(storedFiles, current.bundle);
  storeBundle(storedFiles, candidate.bundle);
  const getBlob = storedBlobReader(storedFiles);
  const country = current.country;
  const bundles = buildCalibrationComparisonBundles(
    await loadCalibrationComparisonSource(country, current.bundle.index.buildArtifactId, getBlob),
    await loadCalibrationComparisonSource(country, candidate.bundle.index.buildArtifactId, getBlob),
  );
  storeBundle(storedFiles, bundles.reported);
  storeBundle(storedFiles, bundles.shared);
  const firstTargetDetail = await loadCalibrationComparisonTargetDetail({
    country,
    buildArtifactId: bundles.reported.index.buildArtifactId,
    targetOrdinal: 0,
    getBlob,
  });
  return {
    pairArtifactId: bundles.pairArtifactId,
    reported: bundleRecord(bundles.reported),
    shared: bundleRecord(bundles.shared),
    firstTargetDetailSha256: sha256Text(JSON.stringify(firstTargetDetail)),
  };
}

// The manifest the publisher would hold after publishing every recorded
// source, latest release per country first.
export async function goldenManifestRecord(): Promise<CalibrationTreeGoldenRecord["manifest"]> {
  let manifest = emptyCalibrationTreeManifest();
  const latestReleases = new Set<GoldenSourceCaseName>(["us-release", "uk-release", "be-release"]);
  for (const name of GOLDEN_SOURCE_CASES) {
    const { country, manifestEntry } = await goldenSourceBuild(name);
    manifest = withCalibrationTreeManifestEntry(
      manifest,
      country,
      manifestEntry,
      latestReleases.has(name),
    );
  }
  const serialized = serializeCalibrationTreeManifest(manifest);
  return { sha256: sha256Text(serialized), rawBytes: Buffer.byteLength(serialized, "utf8") };
}

export async function recordCalibrationTreeGoldens(): Promise<CalibrationTreeGoldenRecord> {
  const sources = {} as CalibrationTreeGoldenRecord["sources"];
  for (const name of GOLDEN_SOURCE_CASES) sources[name] = await goldenSourceRecord(name);
  const comparisons = {} as CalibrationTreeGoldenRecord["comparisons"];
  for (const name of GOLDEN_COMPARISON_CASES) comparisons[name] = await goldenComparisonRecord(name);
  return {
    schemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    sources,
    comparisons,
    manifest: await goldenManifestRecord(),
  };
}

if (import.meta.main) {
  const record = await recordCalibrationTreeGoldens();
  await Bun.write(CALIBRATION_TREE_GOLDEN_RECORD_PATH, `${JSON.stringify(record, null, 2)}\n`);
  console.log(`Recorded ${CALIBRATION_TREE_GOLDEN_RECORD_PATH}`);
}
