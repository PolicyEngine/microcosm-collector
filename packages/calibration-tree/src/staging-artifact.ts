import { createHash } from "node:crypto";

import {
  type Calibration,
  type MicrocosmCountry,
  asObject,
  assertSafeReleaseId,
  assertSafeReleaseOrBuildFolderId,
  buildCalibration,
  buildComparison,
  latestMicrocosmCalibrationSummary,
  latestMicrocosmTargetDiagnosticsPage,
  loadCountryRepositoryHashedJson,
  loadPointerReleaseId,
  loadPublishedCutReleases,
  loadRecordedReleases,
  loadRelease,
  loadReleases,
  microcosmCountryGeography,
  microcosmRepo,
  type ReleaseEntry,
} from "./latest-artifact";
import { readCalibrationTreeManifest } from "./calibration-tree-blob";
import {
  calibrationBuildsNewestFirst,
  type CalibrationTreeManifest,
  type CalibrationTreeManifestEntry,
} from "./calibration-tree-manifest";
import type { CalibrationTreeSourceArtifact } from "./calibration-tree-artifact";
import {
  countryRegistration,
  resolveRegisteredStagingRepository,
  stagingLocalAreaPipelines,
  type RepositoryEnvironment,
  type ResolvedRepository,
} from "./countries";
import {
  type ReformValidation,
  buildReformValidation,
} from "./reforms";
import {
  buildTargetChangeDataset,
  type TargetChangeDataset,
} from "./target-change";
import { buildTargetChangeDatasetFromSummaryAndCalibration } from "./calibration-build-comparison";
import { loadCalibrationComparisonSource } from "./calibration-comparison-blob";
import {
  IncompatibleStagingDataError,
  isStructuredStagingDocument,
  parseStagingCalibrationProgress,
  parseStagingEvents,
  parseStagingManifest,
  parseStagingProgress,
  parseStagingRunIndex,
  validateStagingRunConsistency,
  type StagingSchemaVersion,
} from "./staging-contract";

type JsonObject = Record<string, unknown>;

interface TargetChangeCacheEntry {
  expiresAt: number;
  promise: Promise<TargetChangeDataset | null>;
}

interface StagingCalibrationCacheEntry {
  expiresAt: number;
  promise: Promise<Calibration | null>;
}

const TARGET_CHANGE_FINAL_CACHE_SECONDS = 21_600;
const TARGET_CHANGE_MUTABLE_CACHE_SECONDS = 30;
const TARGET_CHANGE_CACHE_LIMIT = 8;
const STAGING_CALIBRATION_CACHE_LIMIT = 8;
const targetChangeCache = new Map<string, TargetChangeCacheEntry>();
const stagingCalibrationCache = new Map<string, StagingCalibrationCacheEntry>();

export const MICROCOSM_STAGING_HF_REPO_ENV = "POPULACE_STAGING_HF_REPO";
export const MICROCOSM_STAGING_HF_REVISION_ENV = "POPULACE_STAGING_HF_REVISION";
export type StagingRepository = ResolvedRepository;

// Resolve staging telemetry from the country registry. The exported US values
// retain the legacy API and deployment-variable behavior for existing callers.
export function stagingRepository(
  country: MicrocosmCountry,
  environment: RepositoryEnvironment = process.env,
): StagingRepository | null {
  return resolveRegisteredStagingRepository(country, environment);
}

export const MICROCOSM_STAGING_HF_REPO = stagingRepository("us")!.repo;
export const MICROCOSM_STAGING_HF_REVISION = stagingRepository("us")!.revision;

export async function resolveStagingRevisionSha(
  country: MicrocosmCountry,
  revisionOverride?: string,
): Promise<string> {
  const repository = stagingSource(country, revisionOverride);
  const response = await fetch(
    `https://huggingface.co/api/datasets/${repository.repo}/revision/${encodeURIComponent(repository.revision)}`,
    {
      ...stagingFetchOptions(0, country),
      signal: AbortSignal.timeout(30_000),
    },
  );
  if (!response.ok) {
    throw new StagingFetchError(
      response.status,
      `revision ${repository.revision}`,
      repository,
    );
  }
  const payload = asObject(await response.json());
  const sha = typeof payload.sha === "string" ? payload.sha.toLowerCase() : "";
  if (!/^[0-9a-f]{40,64}$/.test(sha)) {
    throw new Error(
      `Hugging Face returned an invalid commit SHA for ${repository.repo}@${repository.revision}.`,
    );
  }
  return sha;
}

// Staging telemetry is served for countries registered with the `staging`
// capability. Repository and credential selection happens in this server data
// module and never enters the API response model.
export function stagingUnavailableReason(country: MicrocosmCountry): string | null {
  return stagingRepository(country)
    ? null
    : `${microcosmCountryGeography(country)} has no staging repository.`;
}

function requiredStagingRepository(country: MicrocosmCountry): StagingRepository {
  const repository = stagingRepository(country);
  if (repository) return repository;
  throw new Error(
    stagingUnavailableReason(country) ?? "Staging repository is not configured.",
  );
}

function stagingResolveUrlFor(repository: StagingRepository, path: string): string {
  return `https://huggingface.co/datasets/${repository.repo}/resolve/${repository.revision}/${path}`;
}

export function stagingResolveUrl(path: string, country: MicrocosmCountry = "us"): string {
  return stagingResolveUrlFor(requiredStagingRepository(country), path);
}

function stagingTreeUrl(repository: StagingRepository): string {
  return `https://huggingface.co/api/datasets/${repository.repo}/tree/${repository.revision}/runs?recursive=true`;
}

function stagingRepoUrl(repository: StagingRepository): string {
  return `https://huggingface.co/api/datasets/${repository.repo}`;
}

function stagingSource(
  country: MicrocosmCountry,
  revisionOverride?: string,
): StagingRepository {
  const repository = requiredStagingRepository(country);
  return revisionOverride
    ? { ...repository, revision: revisionOverride }
    : repository;
}

function unavailableStaging(country: MicrocosmCountry) {
  const detail = stagingUnavailableReason(country);
  if (!detail) return null;
  return {
    available: false as const,
    source_repo: null,
    revision: null,
    detail,
  };
}

class StagingFetchError extends Error {
  constructor(
    public readonly status: number,
    path: string,
    repository: StagingRepository,
  ) {
    super(stagingFetchMessage(status, path, repository));
  }
}

function stagingFetchMessage(
  status: number,
  path: string,
  repository: StagingRepository,
): string {
  if (status === 401 || status === 403) {
    return (
      `Staging repository ${repository.repo} is not readable by this deployment ` +
      `(${status} fetching ${path}). Configure its server-side read credential.`
    );
  }
  if (status === 404) {
    return `Staging artifact not found (${path}).`;
  }
  return `Staging fetch failed ${status}: ${path}`;
}

function stagingToken(country: MicrocosmCountry): string | undefined {
  const tokenEnv = countryRegistration(country).staging?.token_env;
  if (tokenEnv) return process.env[tokenEnv];
  return process.env.HF_TOKEN ?? process.env.HUGGINGFACE_TOKEN;
}

function hfHeaders(country: MicrocosmCountry): HeadersInit | undefined {
  const token = stagingToken(country);
  return token ? { Authorization: `Bearer ${token}` } : undefined;
}

function stagingFetchOptions(
  revalidate: number,
  country: MicrocosmCountry,
): RequestInit {
  return {
    headers: hfHeaders(country),
    ...(revalidate > 0 ? { next: { revalidate } } : { cache: "no-store" as const }),
  };
}

async function stagingJson(
  path: string,
  revalidate: number,
  country: MicrocosmCountry,
  revisionOverride?: string,
): Promise<JsonObject> {
  const repository = stagingSource(country, revisionOverride);
  const res = await fetch(
    stagingResolveUrlFor(repository, path),
    stagingFetchOptions(revalidate, country),
  );
  if (!res.ok) throw new StagingFetchError(res.status, path, repository);
  return asObject(await res.json());
}

async function stagingJsonOrNull(
  path: string,
  revalidate: number,
  country: MicrocosmCountry,
  revisionOverride?: string,
): Promise<JsonObject | null> {
  try {
    return await stagingJson(path, revalidate, country, revisionOverride);
  } catch (error) {
    if (error instanceof StagingFetchError && error.status !== 404) throw error;
    return null;
  }
}

interface HashedStagingJson {
  payload: JsonObject;
  sha256: string;
  source: CalibrationTreeSourceArtifact;
}

async function stagingHashedJsonOrNull(
  path: string,
  revalidate: number,
  country: MicrocosmCountry,
  revisionOverride?: string,
): Promise<HashedStagingJson | null> {
  const repository = stagingSource(country, revisionOverride);
  const res = await fetch(
    stagingResolveUrlFor(repository, path),
    stagingFetchOptions(revalidate, country),
  );
  if (!res.ok) {
    if (res.status === 404) return null;
    throw new StagingFetchError(res.status, path, repository);
  }
  const bytes = new Uint8Array(await res.arrayBuffer());
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new IncompatibleStagingDataError(`${path} is not valid JSON.`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new IncompatibleStagingDataError(`${path} must contain a JSON object.`);
  }
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  return {
    payload: parsed as JsonObject,
    sha256,
    source: {
      path,
      sha256,
      hfRepo: repository.repo,
      hfCommitSha: repository.revision,
    },
  };
}

async function stagingTextOrNull(
  path: string,
  revalidate: number,
  country: MicrocosmCountry,
  revisionOverride?: string,
): Promise<string | null> {
  const repository = stagingSource(country, revisionOverride);
  const res = await fetch(
    stagingResolveUrlFor(repository, path),
    stagingFetchOptions(revalidate, country),
  );
  if (!res.ok) {
    if (res.status === 404) return null;
    throw new StagingFetchError(res.status, path, repository);
  }
  return res.text();
}

async function stagingTree(
  revalidate: number,
  country: MicrocosmCountry,
  revisionOverride?: string,
): Promise<JsonObject[]> {
  const repository = stagingSource(country, revisionOverride);
  const entries: JsonObject[] = [];
  const visited = new Set<string>();
  let url: string | null = stagingTreeUrl(repository);
  while (url) {
    if (visited.has(url)) {
      throw new Error("Staging repository tree pagination repeated a page URL.");
    }
    visited.add(url);
    const res: Response = await fetch(
      url,
      stagingFetchOptions(revalidate, country),
    );
    if (!res.ok) throw new StagingFetchError(res.status, "runs tree", repository);
    const tree: unknown = await res.json();
    if (!Array.isArray(tree)) {
      throw new IncompatibleStagingDataError(
        "runs tree response must be an array.",
      );
    }
    entries.push(...tree.map(asObject));
    const link = res.headers.get("link") ?? "";
    const next = /<([^>]+)>;\s*rel="next"/.exec(link);
    url = next ? next[1] : null;
  }
  return entries;
}

export interface StagingRunSummary {
  run_id: string;
  candidate_release_id: string | null;
  release_id: string | null;
  country_code: string | null;
  run_kind: string | null;
  non_release: boolean | null;
  schema_version: StagingSchemaVersion | null;
  status: string | null;
  stage: string | null;
  started_at: string | null;
  updated_at: string | null;
  progress_path: string;
  run_manifest_path: string;
}

export interface IncompatibleStagingRun {
  run_id: string;
  run_manifest_path: string;
  detail: string;
}

export interface StagingRunDetail {
  available: boolean;
  source_repo: string | null;
  revision: string | null;
  detail?: string | null;
  run_id: string;
  candidate_release_id: string | null;
  release_id: string | null;
  country_code: string | null;
  run_kind: string | null;
  non_release: boolean | null;
  schema_version: StagingSchemaVersion | null;
  delivery: JsonObject | null;
  progress: JsonObject | null;
  run_manifest: JsonObject | null;
  calibration_progress: JsonObject | null;
  events: JsonObject[];
  has_calibration: boolean;
  calibration: ReturnType<typeof latestMicrocosmCalibrationSummary> | null;
  reform_validation: ReformValidation | null;
  build_manifest: JsonObject | null;
  release_manifest: JsonObject | null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function booleanValue(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function schemaVersionValue(value: unknown): StagingSchemaVersion | null {
  return value === 1 || value === 2 || value === 3 ? value : null;
}

function objectOrNull(value: unknown): JsonObject | null {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}

export function stagingTargetChangeCacheTtlSeconds(status: unknown): number {
  return ["passed", "published", "completed", "blocked", "failed"].includes(
    String(status ?? "").trim(),
  )
    ? TARGET_CHANGE_FINAL_CACHE_SECONDS
    : TARGET_CHANGE_MUTABLE_CACHE_SECONDS;
}

function summaryFromProgress(runId: string, progress: JsonObject | null): StagingRunSummary {
  const candidate = stringValue(progress?.candidate_release_id);
  return {
    run_id: runId,
    candidate_release_id: candidate,
    release_id: stringValue(progress?.release_id),
    country_code: stringValue(progress?.country_code),
    run_kind: stringValue(progress?.run_kind),
    non_release: booleanValue(progress?.non_release),
    schema_version: schemaVersionValue(progress?.schema_version),
    status: stringValue(progress?.status),
    stage: stringValue(progress?.stage),
    started_at: stringValue(progress?.started_at),
    updated_at: stringValue(progress?.updated_at),
    progress_path: `runs/${runId}/progress.json`,
    run_manifest_path: `runs/${runId}/run_manifest.json`,
  };
}

function summaryFromManifest(
  runId: string,
  manifest: JsonObject,
): StagingRunSummary {
  return {
    run_id: runId,
    candidate_release_id: stringValue(manifest.candidate_release_id),
    release_id: stringValue(manifest.release_id),
    country_code: stringValue(manifest.country_code),
    run_kind: stringValue(manifest.run_kind),
    non_release: booleanValue(manifest.non_release),
    schema_version: schemaVersionValue(manifest.schema_version),
    status: stringValue(manifest.status),
    stage: stringValue(manifest.stage),
    started_at: stringValue(manifest.started_at),
    updated_at: stringValue(manifest.updated_at),
    progress_path: `runs/${runId}/progress.json`,
    run_manifest_path: `runs/${runId}/run_manifest.json`,
  };
}

function sortRuns(a: StagingRunSummary, b: StagingRunSummary): number {
  return (
    compareTimestampsDescending(a.updated_at, b.updated_at) ||
    compareTimestampsDescending(a.started_at, b.started_at) ||
    b.run_id.localeCompare(a.run_id)
  );
}

function compareTimestampsDescending(
  a: string | null,
  b: string | null,
): number {
  const aTime = a == null ? Number.NEGATIVE_INFINITY : Date.parse(a);
  const bTime = b == null ? Number.NEGATIVE_INFINITY : Date.parse(b);
  const normalizedA = Number.isNaN(aTime) ? Number.NEGATIVE_INFINITY : aTime;
  const normalizedB = Number.isNaN(bTime) ? Number.NEGATIVE_INFINITY : bTime;
  if (normalizedA === normalizedB) return 0;
  return normalizedB > normalizedA ? 1 : -1;
}

const RUN_PATH = /^runs\/([A-Za-z0-9][A-Za-z0-9._-]*)\//;
const RUN_MANIFEST_PATH =
  /^runs\/([A-Za-z0-9][A-Za-z0-9._-]*)\/run_manifest\.json$/;

export async function loadStagingRuns(
  revalidate: number,
  country: MicrocosmCountry = "us",
  revisionOverride?: string,
) {
  const unavailable = unavailableStaging(country);
  if (unavailable) {
    return {
      ...unavailable,
      truncated: false,
      runs: [] as StagingRunSummary[],
      incompatible_runs: [] as IncompatibleStagingRun[],
    };
  }
  const repository = stagingSource(country, revisionOverride);
  let treeMissing = false;
  let tree: JsonObject[] = [];
  try {
    tree = await stagingTree(revalidate, country, revisionOverride);
  } catch (error) {
    if (!(error instanceof StagingFetchError) || error.status !== 404) throw error;
    // A staging repo may exist before any tree listing is public.
    treeMissing = true;
  }

  const manifestPaths = new Map<string, string>();
  const listedRunIds = new Set<string>();
  for (const entry of tree) {
    if (entry.type !== "file" || typeof entry.path !== "string") continue;
    const runMatch = RUN_PATH.exec(entry.path);
    if (runMatch) listedRunIds.add(runMatch[1]);
    const manifestMatch = RUN_MANIFEST_PATH.exec(entry.path);
    if (manifestMatch) manifestPaths.set(manifestMatch[1], entry.path);
  }

  const manifestResults = await Promise.all(
    [...manifestPaths].map(async ([runId, path]) => {
      try {
        const manifest = parseStagingManifest(
          await stagingJson(path, revalidate, country, revisionOverride),
        );
        if (manifest.run_id !== runId) {
          throw new IncompatibleStagingDataError(
            `run manifest id ${String(manifest.run_id)} does not match directory ${runId}.`,
          );
        }
        validateStagingRunConsistency(runId, { runManifest: manifest });
        return { manifest, problem: null };
      } catch (error) {
        return {
          manifest: null,
          problem: {
            run_id: runId,
            run_manifest_path: path,
            detail: error instanceof Error ? error.message : String(error),
          } satisfies IncompatibleStagingRun,
        };
      }
    }),
  );
  const listedManifests = manifestResults.flatMap(({ manifest }) =>
    manifest == null ? [] : [manifest],
  );
  const incompatibleRuns = manifestResults.flatMap(({ problem }) =>
    problem == null ? [] : [problem],
  );
  const structuredManifests = listedManifests.filter(isStructuredStagingDocument);
  const structuredManifestIds = new Set(
    structuredManifests.map((manifest) => String(manifest.run_id)),
  );
  const v1ManifestIds = new Set(
    listedManifests
      .filter((manifest) => manifest.schema_version === 1)
      .map((manifest) => String(manifest.run_id)),
  );

  let index: JsonObject | null = null;
  let indexedRuns: StagingRunSummary[] = [];
  if (treeMissing || v1ManifestIds.size > 0) {
    index = await stagingJsonOrNull(
      "runs.json",
      revalidate,
      country,
      revisionOverride,
    );
    if (index?.schema_version === 1) indexedRuns = parseStagingRunIndex(index);
  }

  // HF answers 404 (not 401/403) for private repos when auth is missing or
  // expired, so "everything 404'd" is ambiguous between "no runs yet" and "we
  // can't see the repo". Disambiguate via the repo API before reporting an
  // empty list — a silent empty state hides a broken token.
  if (index == null && treeMissing && listedManifests.length === 0) {
    const repoRes = await fetch(
      stagingRepoUrl(repository),
      stagingFetchOptions(revalidate, country),
    );
    if (!repoRes.ok) {
      return {
        available: false,
        source_repo: repository.repo,
        revision: repository.revision,
        detail:
          `Staging repository ${repository.repo} is not visible (HTTP ${repoRes.status}). ` +
          "It is private — a missing or expired HF token reads as 404, not 401.",
        runs: [],
        incompatible_runs: incompatibleRuns,
      };
    }
  }

  const byId = new Map<string, StagingRunSummary>(
    indexedRuns.map((run) => [run.run_id, run]),
  );
  const v1RunIds = new Set(v1ManifestIds);
  if (index?.schema_version === 1) {
    for (const runId of listedRunIds) {
      if (!structuredManifestIds.has(runId)) v1RunIds.add(runId);
    }
  }
  const missing = [...v1RunIds]
    .filter((runId) => !byId.has(runId))
    .sort((a, b) => (a < b ? 1 : a > b ? -1 : 0));
  const MAX_UNINDEXED_FETCH = 50;
  const fetched = await Promise.all(
    missing.slice(0, MAX_UNINDEXED_FETCH).map(async (runId) => {
      const raw = await stagingJsonOrNull(
        `runs/${runId}/progress.json`,
        revalidate,
        country,
        revisionOverride,
      );
      const progress = raw == null ? null : parseStagingProgress(raw);
      validateStagingRunConsistency(runId, { progress });
      return summaryFromProgress(runId, progress);
    }),
  );
  for (const run of fetched) byId.set(run.run_id, run);
  for (const manifest of structuredManifests) {
    const runId = String(manifest.run_id);
    byId.set(runId, summaryFromManifest(runId, manifest));
  }
  const truncated = missing.length > MAX_UNINDEXED_FETCH;

  return {
    available: true,
    source_repo: repository.repo,
    revision: repository.revision,
    truncated,
    runs: [...byId.values()].sort(sortRuns),
    incompatible_runs: incompatibleRuns,
  };
}

// A build that staged its finished dataset bundle records where it went in a
// reviewed `staged_dataset` telemetry artifact: the release repository, the
// `staged/<run_id>/` prefix, the commit and every file's digest. The bundle's
// `calibration_diagnostics.json` is the same per-target artifact a release
// carries, so an unreleased candidate can be inspected through the same views.
const STAGED_BUNDLE_DIAGNOSTICS_FILE = "calibration_diagnostics.json";
// Microcosm's UK local lane stages its outputs under the dataset's own stem,
// so its per-target diagnostics arrive as `<dataset>.diagnostics.json`.
const STAGED_BUNDLE_STEM_DIAGNOSTICS_SUFFIX = ".diagnostics.json";
// A recorded file name is joined to the staged prefix in an authenticated
// release-repository URL, so it must be one path segment.
const STAGED_BUNDLE_FILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$/;
const STAGED_BUNDLE_LANDED = new Set(["uploaded", "already_staged"]);
const STAGED_BUNDLE_PREFIX = /^staged\/[A-Za-z0-9][A-Za-z0-9._-]*$/;
const GIT_COMMIT_SHA = /^[0-9a-f]{40}$/;
const SHA_256_HEX = /^[0-9a-f]{64}$/;

export interface ResolvedStagingCalibrationSource {
  calibration: Calibration;
  candidateReleaseId: string;
  updatedAt: string | null;
  stagingRepo: string;
  stagingRevision: string;
  sourceArtifacts: {
    calibrationDiagnostics: CalibrationTreeSourceArtifact;
    buildManifest: CalibrationTreeSourceArtifact | null;
    releaseManifest: CalibrationTreeSourceArtifact | null;
    stagingRunManifest: CalibrationTreeSourceArtifact | null;
    stagingProgress: CalibrationTreeSourceArtifact | null;
    stagedDatasetReceipt: CalibrationTreeSourceArtifact | null;
  };
}

// The bundle's per-target diagnostics file among the files its receipt
// recorded: `calibration_diagnostics.json`, or else the one
// `<dataset>.diagnostics.json`. A bundle recording none, several stem
// files, or a stem name that is not one path segment has no target view
// rather than a guessed one. The count includes invalid names, so an
// invalid name beside a valid one still makes the receipt ambiguous.
export function stagedBundleDiagnosticsFile(files: JsonObject | null): string | null {
  if (!files) return null;
  if (Object.hasOwn(files, STAGED_BUNDLE_DIAGNOSTICS_FILE)) {
    return STAGED_BUNDLE_DIAGNOSTICS_FILE;
  }
  const stemFiles = Object.keys(files).filter((name) =>
    name.endsWith(STAGED_BUNDLE_STEM_DIAGNOSTICS_SUFFIX)
  );
  return stemFiles.length === 1 && STAGED_BUNDLE_FILE_NAME.test(stemFiles[0])
    ? stemFiles[0]
    : null;
}

async function loadStagedBundleCalibrationSource(
  runId: string,
  runManifest: JsonObject | null,
  progress: JsonObject | null,
  candidateReleaseId: string,
  revalidate: number,
  country: MicrocosmCountry,
  stagingRevision: string,
  runManifestSource: CalibrationTreeSourceArtifact | null,
  progressSource: CalibrationTreeSourceArtifact | null,
): Promise<ResolvedStagingCalibrationSource | null> {
  const staging = stagingSource(country, stagingRevision);
  const artifacts = objectOrNull(runManifest?.artifacts);
  const stagedArtifact = objectOrNull(artifacts?.staged_dataset);
  const receiptPath = stringValue(stagedArtifact?.staging_path);
  if (!receiptPath) return null;
  const receipt = await stagingHashedJsonOrNull(
    receiptPath,
    revalidate,
    country,
    stagingRevision,
  );
  if (!receipt) {
    throw new IncompatibleStagingDataError(
      "artifact staged_dataset is declared but its receipt is absent.",
    );
  }
  if (receipt.sha256 !== stringValue(stagedArtifact?.sha256)) {
    throw new IncompatibleStagingDataError(
      "artifact staged_dataset digest does not match its run manifest declaration.",
    );
  }
  const delivery = receipt.payload;
  // A skipped, failed or disabled staging leaves nothing to read.
  if (!STAGED_BUNDLE_LANDED.has(String(delivery.status))) return null;
  if (stringValue(delivery.run_id) !== runId) {
    throw new IncompatibleStagingDataError("staged dataset receipt names another run.");
  }
  const repository = stringValue(delivery.repository);
  const prefix = stringValue(delivery.prefix);
  const revision = stringValue(delivery.revision);
  if (!repository || !prefix || !revision) {
    throw new IncompatibleStagingDataError(
      "staged dataset receipt is missing its repository, prefix or revision.",
    );
  }
  // The release credential is only ever sent to the country's registered
  // release repository, never to a repository a telemetry file names.
  if (repository !== microcosmRepo(country)) {
    throw new IncompatibleStagingDataError(
      `staged dataset repository ${repository} is not ${microcosmCountryGeography(country)}'s ` +
        "registered release repository; staged bundles are read only from there.",
    );
  }
  if (!STAGED_BUNDLE_PREFIX.test(prefix) || prefix !== `staged/${runId}`) {
    throw new IncompatibleStagingDataError("staged dataset prefix is not staged/<run_id>.");
  }
  if (!GIT_COMMIT_SHA.test(revision)) {
    throw new IncompatibleStagingDataError("staged dataset revision is not a commit sha.");
  }
  const files = objectOrNull(delivery.files);
  const diagnosticsFile = stagedBundleDiagnosticsFile(files);
  // A bundle without calibration diagnostics (a spine, say) has no target view.
  if (!diagnosticsFile) return null;
  const declared = objectOrNull(files?.[diagnosticsFile]);
  const expectedDigest = stringValue(declared?.sha256);
  if (!expectedDigest || !SHA_256_HEX.test(expectedDigest)) return null;
  const diagnostics = await loadCountryRepositoryHashedJson(
    `${prefix}/${diagnosticsFile}`,
    revalidate,
    country,
    revision,
  );
  if (!diagnostics) {
    throw new IncompatibleStagingDataError(
      `staged bundle ${prefix} records ${diagnosticsFile} but the file is absent at revision ${revision}.`,
    );
  }
  if (diagnostics.sha256 !== expectedDigest) {
    throw new IncompatibleStagingDataError(
      `staged bundle ${diagnosticsFile} does not match the digest the run recorded.`,
    );
  }
  const updatedAt = stringValue(progress?.updated_at) ??
    stringValue(runManifest?.updated_at);
  const calibration = buildCalibration(
    diagnostics.payload,
    candidateReleaseId,
    updatedAt,
    {},
    {},
    {},
    country,
    "huggingface_staged_bundle",
    diagnostics.sha256,
    revision,
  );
  return {
    calibration,
    candidateReleaseId,
    updatedAt,
    stagingRepo: staging.repo,
    stagingRevision,
    sourceArtifacts: {
      calibrationDiagnostics: {
        path: `${prefix}/${diagnosticsFile}`,
        sha256: diagnostics.sha256,
        hfRepo: repository,
        hfCommitSha: revision,
      },
      buildManifest: null,
      releaseManifest: null,
      stagingRunManifest: runManifestSource,
      stagingProgress: progressSource,
      stagedDatasetReceipt: receipt.source,
    },
  };
}

export async function loadStagingCalibration(
  runId: string,
  revalidate: number,
  country: MicrocosmCountry = "us",
): Promise<Calibration | null> {
  if (revalidate <= 0) {
    return loadStagingCalibrationUncached(runId, revalidate, country);
  }
  const cacheKey = `${country}:${runId}:${revalidate}`;
  const now = Date.now();
  for (const [key, entry] of stagingCalibrationCache) {
    if (entry.expiresAt <= now) stagingCalibrationCache.delete(key);
  }
  const cached = stagingCalibrationCache.get(cacheKey);
  if (cached && cached.expiresAt > now) return cached.promise;

  const promise = loadStagingCalibrationUncached(runId, revalidate, country);
  stagingCalibrationCache.set(cacheKey, {
    promise,
    expiresAt: now + revalidate * 1000,
  });
  while (stagingCalibrationCache.size > STAGING_CALIBRATION_CACHE_LIMIT) {
    const oldest = stagingCalibrationCache.keys().next().value;
    if (typeof oldest !== "string") break;
    stagingCalibrationCache.delete(oldest);
  }
  try {
    const result = await promise;
    if (!result) stagingCalibrationCache.delete(cacheKey);
    return result;
  } catch (error) {
    stagingCalibrationCache.delete(cacheKey);
    throw error;
  }
}

async function loadStagingCalibrationUncached(
  runId: string,
  revalidate: number,
  country: MicrocosmCountry,
): Promise<Calibration | null> {
  const source = await resolveStagingCalibrationSource(
    runId,
    revalidate,
    country,
  );
  return source?.calibration ?? null;
}

export async function resolveStagingCalibrationSource(
  runId: string,
  revalidate: number,
  country: MicrocosmCountry = "us",
  revisionOverride?: string,
): Promise<ResolvedStagingCalibrationSource | null> {
  if (stagingUnavailableReason(country)) return null;
  assertSafeReleaseId(runId, "run");
  const staging = stagingSource(country, revisionOverride);
  const [progressArtifact, runManifestArtifact] = await Promise.all([
    stagingHashedJsonOrNull(
      `runs/${runId}/progress.json`,
      revalidate,
      country,
      staging.revision,
    ),
    stagingHashedJsonOrNull(
      `runs/${runId}/run_manifest.json`,
      revalidate,
      country,
      staging.revision,
    ),
  ]);
  const progress = progressArtifact == null
    ? null
    : parseStagingProgress(progressArtifact.payload);
  const runManifest =
    runManifestArtifact == null
      ? null
      : parseStagingManifest(runManifestArtifact.payload);
  validateStagingRunConsistency(runId, { progress, runManifest });
  const candidateReleaseId =
    stringValue(progress?.candidate_release_id) ??
    stringValue(runManifest?.candidate_release_id) ??
    runId;
  const artifacts = objectOrNull(runManifest?.artifacts);
  const diagnosticsArtifact = objectOrNull(artifacts?.calibration_diagnostics);
  const structured = isStructuredStagingDocument(runManifest);
  const diagnosticsPath = structured
    ? stringValue(diagnosticsArtifact?.staging_path)
    : `runs/${runId}/calibration_diagnostics.json`;
  if (!diagnosticsPath) {
    // No telemetry diagnostics artifact: a run that staged its dataset bundle
    // carries the same diagnostics there.
    return loadStagedBundleCalibrationSource(
      runId,
      runManifest,
      progress,
      candidateReleaseId,
      revalidate,
      country,
      staging.revision,
      runManifestArtifact?.source ?? null,
      progressArtifact?.source ?? null,
    );
  }
  const diagnostics = await stagingHashedJsonOrNull(
    diagnosticsPath,
    revalidate,
    country,
    staging.revision,
  );
  if (!diagnostics) {
    if (structured && diagnosticsArtifact) {
      throw new IncompatibleStagingDataError(
        "artifact calibration_diagnostics is declared but absent.",
      );
    }
    return null;
  }
  const expectedDigest = stringValue(diagnosticsArtifact?.sha256);
  if (structured && diagnostics.sha256 !== expectedDigest) {
    throw new IncompatibleStagingDataError(
      `artifact calibration_diagnostics digest does not match its run manifest declaration.`,
    );
  }
  const [buildManifest, releaseManifest] = await Promise.all([
    stagingHashedJsonOrNull(
      `runs/${runId}/build_manifest.json`,
      revalidate,
      country,
      staging.revision,
    ),
    stagingHashedJsonOrNull(
      `runs/${runId}/release_manifest.json`,
      revalidate,
      country,
      staging.revision,
    ),
  ]);
  const updatedAt = stringValue(progress?.updated_at) ??
    stringValue(runManifest?.updated_at);
  const calibration = buildCalibration(
    diagnostics.payload,
    candidateReleaseId,
    updatedAt,
    buildManifest?.payload ?? {},
    releaseManifest?.payload ?? {},
    {},
    country,
    "huggingface_live",
    diagnostics.sha256,
    staging.revision,
  );
  return {
    calibration,
    candidateReleaseId,
    updatedAt,
    stagingRepo: staging.repo,
    stagingRevision: staging.revision,
    sourceArtifacts: {
      calibrationDiagnostics: diagnostics.source,
      buildManifest: buildManifest?.source ?? null,
      releaseManifest: releaseManifest?.source ?? null,
      stagingRunManifest: runManifestArtifact?.source ?? null,
      stagingProgress: progressArtifact?.source ?? null,
      stagedDatasetReceipt: null,
    },
  };
}

export async function loadStagingTargetChangeDataset(
  runId: string,
  releaseId: string,
  country: MicrocosmCountry = "us",
): Promise<TargetChangeDataset | null> {
  if (stagingUnavailableReason(country)) return null;
  assertSafeReleaseId(runId, "run");
  assertSafeReleaseOrBuildFolderId(releaseId, "release");
  const progress = await stagingJsonOrNull(
    `runs/${runId}/progress.json`,
    TARGET_CHANGE_MUTABLE_CACHE_SECONDS,
    country,
  );
  const parsedProgress = progress == null ? null : parseStagingProgress(progress);
  validateStagingRunConsistency(runId, { progress: parsedProgress });
  const ttlSeconds = stagingTargetChangeCacheTtlSeconds(parsedProgress?.status);
  const cacheKey = `${country}:${runId}:${releaseId}`;
  const now = Date.now();
  for (const [key, entry] of targetChangeCache) {
    if (entry.expiresAt <= now) targetChangeCache.delete(key);
  }
  const cached = targetChangeCache.get(cacheKey);
  if (cached && cached.expiresAt > now) return cached.promise;

  const promise = Promise.all([
    loadRelease(releaseId, TARGET_CHANGE_FINAL_CACHE_SECONDS, country),
    loadStagingCalibration(runId, ttlSeconds, country),
  ]).then(([release, candidate]) =>
    candidate ? buildTargetChangeDataset(release, candidate) : null,
  );
  targetChangeCache.set(cacheKey, {
    expiresAt: now + ttlSeconds * 1000,
    promise,
  });
  while (targetChangeCache.size > TARGET_CHANGE_CACHE_LIMIT) {
    const oldest = targetChangeCache.keys().next().value;
    if (typeof oldest !== "string") break;
    targetChangeCache.delete(oldest);
  }
  try {
    const result = await promise;
    if (!result) targetChangeCache.delete(cacheKey);
    return result;
  } catch (error) {
    targetChangeCache.delete(cacheKey);
    throw error;
  }
}

export async function loadStagingTargetChangeDatasetFromBuild(
  runId: string,
  currentBuildArtifactId: string,
  country: MicrocosmCountry = "us",
): Promise<TargetChangeDataset | null> {
  if (stagingUnavailableReason(country)) return null;
  assertSafeReleaseId(runId, "run");
  const progress = await stagingJsonOrNull(
    `runs/${runId}/progress.json`,
    TARGET_CHANGE_MUTABLE_CACHE_SECONDS,
    country,
  );
  const parsedProgress = progress == null ? null : parseStagingProgress(progress);
  validateStagingRunConsistency(runId, { progress: parsedProgress });
  const ttlSeconds = stagingTargetChangeCacheTtlSeconds(parsedProgress?.status);
  const cacheKey = `${country}:${runId}:build:${currentBuildArtifactId}`;
  const now = Date.now();
  for (const [key, entry] of targetChangeCache) {
    if (entry.expiresAt <= now) targetChangeCache.delete(key);
  }
  const cached = targetChangeCache.get(cacheKey);
  if (cached && cached.expiresAt > now) return cached.promise;

  const promise = Promise.all([
    loadCalibrationComparisonSource(country, currentBuildArtifactId),
    loadStagingCalibration(runId, ttlSeconds, country),
  ]).then(([current, candidate]) =>
    candidate
      ? buildTargetChangeDatasetFromSummaryAndCalibration(
          {
            country,
            comparison: current.index.targetComparison,
            targets: current.targetSummaries,
          },
          candidate,
        )
      : null,
  );
  targetChangeCache.set(cacheKey, {
    promise,
    expiresAt: now + ttlSeconds * 1000,
  });
  while (targetChangeCache.size > TARGET_CHANGE_CACHE_LIMIT) {
    const oldest = targetChangeCache.keys().next().value;
    if (typeof oldest !== "string") break;
    targetChangeCache.delete(oldest);
  }
  try {
    const result = await promise;
    if (!result) targetChangeCache.delete(cacheKey);
    return result;
  } catch (error) {
    targetChangeCache.delete(cacheKey);
    throw error;
  }
}

export async function loadStagingRun(
  runId: string,
  revalidate: number,
  country: MicrocosmCountry = "us",
): Promise<StagingRunDetail> {
  const unavailable = unavailableStaging(country);
  if (unavailable) {
    return {
      ...unavailable,
      run_id: runId,
      candidate_release_id: null,
      release_id: null,
      country_code: null,
      run_kind: null,
      non_release: null,
      schema_version: null,
      delivery: null,
      progress: null,
      run_manifest: null,
      calibration_progress: null,
      events: [],
      has_calibration: false,
      calibration: null,
      reform_validation: null,
      build_manifest: null,
      release_manifest: null,
    };
  }
  assertSafeReleaseId(runId, "run");
  const repository = stagingSource(country);
  const [
    progressRaw,
    runManifestRaw,
    calibrationProgressRaw,
    eventsText,
    cal,
    reformValidationRaw,
  ] = await Promise.all([
    stagingJsonOrNull(`runs/${runId}/progress.json`, revalidate, country),
    stagingJsonOrNull(`runs/${runId}/run_manifest.json`, revalidate, country),
    stagingJsonOrNull(
      `runs/${runId}/calibration_progress.json`,
      revalidate,
      country,
    ),
    stagingTextOrNull(`runs/${runId}/events.ndjson`, revalidate, country),
    loadStagingCalibration(runId, revalidate, country),
    stagingJsonOrNull(`runs/${runId}/reform_validation.json`, revalidate, country),
  ]);
  const progress = progressRaw == null ? null : parseStagingProgress(progressRaw);
  const runManifest =
    runManifestRaw == null ? null : parseStagingManifest(runManifestRaw);
  const calibrationProgress =
    calibrationProgressRaw == null
      ? null
      : parseStagingCalibrationProgress(calibrationProgressRaw);
  const events = parseStagingEvents(eventsText);
  validateStagingRunConsistency(runId, {
    progress,
    runManifest,
    calibrationProgress,
    events,
  });
  const candidateReleaseId =
    stringValue(progress?.candidate_release_id) ??
    stringValue(runManifest?.candidate_release_id) ??
    runId;
  const identity = progress ?? runManifest;
  return {
    available: true,
    source_repo: repository.repo,
    revision: repository.revision,
    run_id: runId,
    candidate_release_id: candidateReleaseId,
    release_id: stringValue(identity?.release_id),
    country_code: stringValue(identity?.country_code),
    run_kind: stringValue(identity?.run_kind),
    non_release: booleanValue(identity?.non_release),
    schema_version: schemaVersionValue(identity?.schema_version),
    delivery: objectOrNull(identity?.delivery),
    progress,
    run_manifest: runManifest,
    calibration_progress: calibrationProgress,
    events,
    has_calibration: cal != null,
    calibration: cal ? latestMicrocosmCalibrationSummary(cal) : null,
    reform_validation: reformValidationRaw
      ? buildReformValidation(
          reformValidationRaw,
          candidateReleaseId,
          stringValue(progress?.updated_at),
        )
      : null,
    build_manifest: cal?.build_manifest ?? null,
    release_manifest: cal?.release_manifest ?? null,
  };
}

// Telemetry only (progress, manifest, calibration progress, events), without
// the candidate diagnostics loadStagingRun also resolves. The build monitor
// reads every run this way to time stages across runs.
export async function loadStagingRunTelemetry(
  runId: string,
  revalidate: number,
  country: MicrocosmCountry = "us",
) {
  assertSafeReleaseId(runId, "run");
  const [progressRaw, runManifestRaw, calibrationProgressRaw, eventsText] =
    await Promise.all([
      stagingJsonOrNull(`runs/${runId}/progress.json`, revalidate, country),
      stagingJsonOrNull(`runs/${runId}/run_manifest.json`, revalidate, country),
      stagingJsonOrNull(`runs/${runId}/calibration_progress.json`, revalidate, country),
      stagingTextOrNull(`runs/${runId}/events.ndjson`, revalidate, country),
    ]);
  const progress = progressRaw == null ? null : parseStagingProgress(progressRaw);
  const runManifest =
    runManifestRaw == null ? null : parseStagingManifest(runManifestRaw);
  const calibrationProgress =
    calibrationProgressRaw == null
      ? null
      : parseStagingCalibrationProgress(calibrationProgressRaw);
  const events = parseStagingEvents(eventsText);
  validateStagingRunConsistency(runId, {
    progress,
    runManifest,
    calibrationProgress,
    events,
  });
  return {
    run_id: runId,
    progress,
    run_manifest: runManifest,
    calibration_progress: calibrationProgress,
    events,
  };
}

export async function loadStagingReformValidationRaw(
  runId: string,
  revalidate: number,
  country: MicrocosmCountry = "us",
): Promise<JsonObject | null> {
  if (stagingUnavailableReason(country)) return null;
  assertSafeReleaseId(runId, "run");
  return stagingJsonOrNull(
    `runs/${runId}/reform_validation.json`,
    revalidate,
    country,
  );
}

export async function loadStagingTargetDiagnostics(
  requestUrl: string,
  runId: string,
  revalidate: number,
  country: MicrocosmCountry = "us",
) {
  const unavailable = unavailableStaging(country);
  if (unavailable) return { ...unavailable, run_id: runId };
  const cal = await loadStagingCalibration(runId, revalidate, country);
  if (!cal) {
    return {
      available: false,
      run_id: runId,
      detail: "This staging run has not uploaded calibration_diagnostics.json yet.",
    };
  }
  return latestMicrocosmTargetDiagnosticsPage(requestUrl, cal);
}

// --- which release a staging candidate is compared with -----------------
// A national candidate is compared with the country's current release, the
// build the dashboard manifest marks latest (for the UK, the one
// latest-national.json names). A local-area candidate calibrates local
// geographies the national release does not, so it is compared with the
// local-area release instead. That release has no pointer file and is never
// promoted, so its most recently published build, as the dashboard manifest
// records it, is used. A run is a local-area candidate when its run
// manifest's pipeline id is one the country registers as local-area: no
// staged receipt or manifest records a dataset role, and the dataset file
// names change between builds.

export type StagingComparisonBasis = "current_release" | "newest_local_area_release";

export interface StagingComparisonRelease {
  release_id: string;
  basis: StagingComparisonBasis;
  /** The run's pipeline builds local-area candidates. */
  local_area_candidate: boolean;
}

export interface StagingComparisonReleaseDependencies {
  runPipelineId(runId: string, country: MicrocosmCountry): Promise<string | null>;
  currentReleaseId(country: MicrocosmCountry): Promise<string>;
  newestLocalAreaReleaseId(country: MicrocosmCountry): Promise<string | null>;
}

/** The `pipeline.id` a staging run's manifest records, or null without one. */
export async function stagingRunPipelineId(
  runId: string,
  revalidate: number,
  country: MicrocosmCountry,
): Promise<string | null> {
  assertSafeReleaseId(runId, "run");
  const manifest = await stagingJsonOrNull(`runs/${runId}/run_manifest.json`, revalidate, country);
  const pipelineId = asObject(manifest?.pipeline).id;
  return typeof pipelineId === "string" && pipelineId.trim() ? pipelineId.trim() : null;
}

/**
 * The most recently published release build whose release is local-area, by
 * the dataset roles `releases` carries (read from releases.json, or from each
 * release manifest, as the release picker reads them) and the publication
 * time the dashboard manifest records. A build releases.json marks withdrawn
 * is never chosen.
 */
export function newestPublishedLocalAreaRelease(
  manifest: CalibrationTreeManifest,
  country: MicrocosmCountry,
  releases: readonly ReleaseEntry[],
): CalibrationTreeManifestEntry | null {
  const localAreaReleases = new Set(
    releases
      .filter((release) => release.is_local_area && release.withdrawn !== true)
      .map((release) => release.release_id),
  );
  const builds = (manifest.countries[country]?.builds ?? []).filter(
    (build) =>
      build.kind === "release" &&
      build.releaseId != null &&
      localAreaReleases.has(build.releaseId),
  );
  return calibrationBuildsNewestFirst(builds)[0] ?? null;
}

/**
 * The releases a local-area Staging candidate may be compared with, with
 * their roles. A repository with releases.json lists the builds it names and
 * nothing else; one without lists the folders on main and its published
 * tagged builds. A releases.json that cannot be read or is refused throws,
 * so the candidate gets no baseline.
 */
export async function stagingBaselineReleases(
  revalidate: number,
  country: MicrocosmCountry,
  manifest: CalibrationTreeManifest,
): Promise<ReleaseEntry[]> {
  const recorded = await loadRecordedReleases(revalidate, country, manifest);
  if (recorded) return recorded;
  // The published tagged builds are the ones the folder listing cannot name.
  const folders = await loadReleases(revalidate, country);
  const tagged = await loadPublishedCutReleases(revalidate, country, manifest, folders);
  return [...folders, ...tagged];
}

function defaultStagingComparisonReleaseDependencies(
  revalidate: number,
): StagingComparisonReleaseDependencies {
  return {
    runPipelineId: (runId, country) => stagingRunPipelineId(runId, revalidate, country),
    currentReleaseId: async (country) =>
      (await loadPointerReleaseId(revalidate, country)).release_id,
    newestLocalAreaReleaseId: async (country) => {
      const { manifest } = await readCalibrationTreeManifest();
      const releases = await stagingBaselineReleases(revalidate, country, manifest);
      return newestPublishedLocalAreaRelease(manifest, country, releases)?.releaseId ?? null;
    },
  };
}

export async function resolveStagingComparisonRelease(
  runId: string,
  country: MicrocosmCountry,
  dependencies: StagingComparisonReleaseDependencies =
    defaultStagingComparisonReleaseDependencies(300),
): Promise<StagingComparisonRelease> {
  const localAreaPipelines = stagingLocalAreaPipelines(country);
  const pipelineId = localAreaPipelines.length
    ? await dependencies.runPipelineId(runId, country)
    : null;
  const localAreaCandidate = pipelineId != null && localAreaPipelines.includes(pipelineId);
  if (localAreaCandidate) {
    const localAreaRelease = await dependencies.newestLocalAreaReleaseId(country);
    if (localAreaRelease) {
      return {
        release_id: localAreaRelease,
        basis: "newest_local_area_release",
        local_area_candidate: true,
      };
    }
  }
  return {
    release_id: await dependencies.currentReleaseId(country),
    basis: "current_release",
    local_area_candidate: localAreaCandidate,
  };
}

/** A staging run compared with a release, as `/microcosm/staging/compare` returns it. */
export type StagingComparison = Awaited<ReturnType<typeof loadStagingComparison>>;

export async function loadStagingComparison(
  runId: string,
  releaseId: string,
  revalidate: number,
  country: MicrocosmCountry = "us",
) {
  const unavailable = unavailableStaging(country);
  if (unavailable) return { ...unavailable, run_id: runId };
  const [release, staging] = await Promise.all([
    loadRelease(releaseId || "latest", revalidate, country),
    loadStagingCalibration(runId, revalidate, country),
  ]);
  if (!staging) {
    return {
      available: false,
      run_id: runId,
      detail: "This staging run has not uploaded calibration_diagnostics.json yet.",
    };
  }
  return {
    available: true,
    run_id: runId,
    ...buildComparison(release, staging),
  };
}
